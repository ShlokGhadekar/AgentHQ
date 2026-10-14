"""Evaluation harness: run each task in team mode and in single-agent mode, check the result, report.

    python -m agenthq.evals run [--tasks health_endpoint,logout] [--modes team,solo] [--rerun]
    python -m agenthq.evals report

A task passes when, in a fresh sandbox, the repo's whole test suite passes and then the task's
hidden acceptance tests pass. Runs are scored from their event log, like everything else.
Results are saved per task and mode as they finish, so a long evaluation can be spread over
several sessions (Groq's free tier allows about one full run per model per day): finished pairs are
skipped next time, and a run cut short by a daily quota is recorded as "quota", left out of the
numbers, and retried later.
"""

import argparse
import asyncio
import json
import logging
import shutil
import statistics
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from redis.asyncio import Redis

from agenthq.agent import AGENTS_FILE, AgentConfig, load_agent_configs
from agenthq.bus import RedisBus
from agenthq.config import Settings, get_settings
from agenthq.events import Event, EventType
from agenthq.llm import LLMClient, make_llm_client
from agenthq.repolens import CodebaseQA, RepoLensTool
from agenthq.runtime import Run, new_run_id
from agenthq.sandbox import DockerSandbox, Sandbox
from agenthq.workspace import Workspace

log = logging.getLogger(__name__)

EVALS_DIR = Path(__file__).resolve().parents[2] / "evals"
SOLO_FILE = Path(__file__).with_name("agents_solo.yaml")
SAMPLE_REPO_URL = "https://github.com/ShlokGhadekar/agenthq-sample-notes"

# Which team file and lead each mode uses. Same runtime, tools, guardrails and token cap for both.
MODES: dict[str, tuple[Path, str]] = {"team": (AGENTS_FILE, "manager"), "solo": (SOLO_FILE, "solo")}


@dataclass(frozen=True)
class EvalTask:
    id: str
    goal: str
    difficulty: str


@dataclass
class EvalResult:
    task: str
    mode: str
    status: str  # "passed", "failed", "quota" (not scored, retried later) or "error" (not scored)
    reason: str
    # How the agents' side ended: "done" (they declared it finished), or why it stopped.
    outcome: str
    tokens: int
    cost_usd: float | None
    seconds: float
    llm_calls: int
    run_id: str
    finished_at: str


def load_tasks(evals_dir: Path = EVALS_DIR) -> list[EvalTask]:
    return [EvalTask(t["id"], " ".join(t["goal"].split()), t["difficulty"]) for t in yaml.safe_load((evals_dir / "tasks.yaml").read_text())]


class Evaluator:
    """Runs tasks against the sample repo and scores them. Every outside dependency is passed in."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        repolens: CodebaseQA | None,
        llm_for: Callable[[AgentConfig], LLMClient],
        make_sandbox: Callable[[], Sandbox],
        repo_url: str = SAMPLE_REPO_URL,
        evals_dir: Path = EVALS_DIR,
        run_timeout_s: float = 2700,
        poll_s: float = 0.5,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.repolens = repolens
        self.llm_for = llm_for
        self.make_sandbox = make_sandbox
        self.repo_url = repo_url
        self.evals_dir = evals_dir
        self.run_timeout_s = run_timeout_s
        self.poll_s = poll_s

    async def evaluate(self, task: EvalTask, mode: str) -> EvalResult:
        team_file, lead = MODES[mode]
        configs = load_agent_configs(team_file)
        run_id = new_run_id()
        workspace = Workspace(self.settings.workspaces_dir / run_id)
        run = Run(run_id, task.goal, self.repo_url, RedisBus(self.redis, run_id, [c.name for c in configs]), configs,
                  self.llm_for, self.settings, workspace=workspace, sandbox=self.make_sandbox(), repolens=self.repolens,
                  publisher=None, lead=lead)
        outcome = await self._play(run)
        if quota_exhausted(run.history):
            return self._result(task, mode, run, "quota", "A model's daily token quota ran out", outcome)
        if not (workspace.root / ".git").exists():
            return self._result(task, mode, run, "error", "The repository was never cloned", outcome)
        passed, reason = await self.check(task, workspace)
        return self._result(task, mode, run, "passed" if passed else "failed", reason, outcome)

    async def _play(self, run: Run) -> str:
        """Run until the agents are done, and say how it ended.

        The team asks for sign-off when it thinks it is finished; that is where a human would review,
        so the eval stops there and nothing is approved. The solo agent ends with finish_run.
        """
        await run.start()
        try:
            async with asyncio.timeout(self.run_timeout_s):
                while not run.finished.is_set() and not run.approval_pending:
                    await asyncio.sleep(self.poll_s)
        except TimeoutError:
            await run.stop(f"Eval timeout after {self.run_timeout_s:.0f}s")
            return "timeout"
        if run.approval_pending:
            await run.stop("Eval: the team asked for sign-off, so the work is complete")
            return "done"
        await run.wait()
        report = run.history[-1].payload if run.history else {}
        return "stopped: " + report.get("report", "")[:200] if report.get("failed") else "done"

    async def check(self, task: EvalTask, workspace: Workspace) -> tuple[bool, str]:
        """Run the repo's tests, then the hidden acceptance tests, in a fresh sandbox."""
        sandbox = self.make_sandbox()
        await sandbox.start(workspace)
        try:
            suite = await sandbox.run(self.settings.test_command, self.settings.command_timeout_s)
            if not suite.passed:
                return False, f"The repo's own tests fail: {last_line(suite.output)}"
            hidden = f"tests/test_acceptance_{task.id}.py"
            shutil.copy(self.evals_dir / "acceptance" / f"test_{task.id}.py", workspace.root / hidden)
            acceptance = await sandbox.run(f"pytest -q -p no:cacheprovider {hidden}", self.settings.command_timeout_s)
            if not acceptance.passed:
                return False, f"Acceptance tests fail: {last_line(acceptance.output)}"
            return True, f"Suite and acceptance tests pass ({last_line(acceptance.output)})"
        finally:
            await sandbox.stop()

    def _result(self, task: EvalTask, mode: str, run: Run, status: str, reason: str, outcome: str) -> EvalResult:
        tokens, cost, calls, seconds = usage_of(run.history)
        return EvalResult(task.id, mode, status, reason, outcome, tokens, cost, seconds, calls, run.run_id,
                          datetime.now(UTC).isoformat(timespec="seconds"))


# --- Scoring from events ------------------------------------------------------------

def usage_of(events: list[Event]) -> tuple[int, float | None, int, float]:
    """(tokens, estimated cost or None if nothing was priced, LLM calls, seconds), all from the event log."""
    calls = [e for e in events if e.type == EventType.LLM_USAGE]
    priced = [e.payload["cost_usd"] for e in calls if isinstance(e.payload.get("cost_usd"), int | float)]
    seconds = (events[-1].timestamp - events[0].timestamp).total_seconds() if events else 0.0
    return sum(e.tokens_in + e.tokens_out for e in calls), (sum(priced) if priced else None), len(calls), seconds


def quota_exhausted(events: list[Event]) -> bool:
    """A daily token quota ran out somewhere in the run, so its result says nothing about the agents."""
    return any(e.type == EventType.ERROR and "tokens per day" in e.payload.get("message", "") for e in events)


def last_line(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


# --- Results file and report ----------------------------------------------------------

class ResultsFile:
    """One JSON file of results keyed by "task/mode", saved after every run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.results: dict[str, EvalResult] = {}
        if path.exists():
            self.results = {key: EvalResult(**value) for key, value in json.loads(path.read_text()).items()}

    def done(self, task: str, mode: str) -> bool:
        result = self.results.get(f"{task}/{mode}")
        return result is not None and result.status in ("passed", "failed")

    def save(self, result: EvalResult) -> None:
        self.results[f"{result.task}/{result.mode}"] = result
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({k: asdict(v) for k, v in sorted(self.results.items())}, indent=2) + "\n")


def render_report(tasks: list[EvalTask], results: dict[str, EvalResult], models: dict[str, str]) -> str:
    """The markdown report: a summary per mode, then every task side by side."""
    scored = {mode: [r for r in results.values() if r.mode == mode and r.status in ("passed", "failed")] for mode in MODES}
    lines = [
        "# AgentHQ evaluation",
        "",
        f"{len(tasks)} tasks on the sample repo, each run by the four-agent team and by a single agent with all "
        "the same tools. A task passes when the repo's own tests and the task's hidden acceptance tests pass "
        "in a fresh sandbox. Costs are estimates at list prices; the runs themselves used Groq's free tier.",
        "",
        "| Mode | Tasks scored | Success rate | Avg tokens | Avg cost | Avg time | Avg LLM calls |",
        "|---|---|---|---|---|---|---|",
    ]
    for mode, items in scored.items():
        if not items:
            lines.append(f"| {mode} | 0 | – | – | – | – | – |")
            continue
        passed = sum(r.status == "passed" for r in items)
        costs = [r.cost_usd for r in items if r.cost_usd is not None]
        lines.append(
            f"| {mode} | {len(items)} | {passed}/{len(items)} ({100 * passed / len(items):.0f}%) "
            f"| {statistics.mean(r.tokens for r in items):,.0f} "
            f"| {f'${statistics.mean(costs):.4f}' if costs else '–'} "
            f"| {statistics.mean(r.seconds for r in items) / 60:.1f} min "
            f"| {statistics.mean(r.llm_calls for r in items):.0f} |"
        )
    lines += ["", "Models: " + "; ".join(f"{mode}: {models[mode]}" for mode in MODES), "",
              "## Per task", "", "| Task | Difficulty | Team | Solo |", "|---|---|---|---|"]
    for task in tasks:
        cells = [_cell(results.get(f"{task.id}/{mode}")) for mode in MODES]
        lines.append(f"| {task.id} | {task.difficulty} | {cells[0]} | {cells[1]} |")
    lines += ["", "Pass/fail reasons, outcomes and run ids are in `results.json`; every run can be replayed in the UI.", ""]
    return "\n".join(lines)


def _cell(result: EvalResult | None) -> str:
    if result is None:
        return "not run"
    if result.status not in ("passed", "failed"):
        return f"{result.status} (not scored)"
    mark = "pass" if result.status == "passed" else "fail"
    return f"{mark}, {result.tokens / 1000:.0f}k tokens, {result.seconds / 60:.1f} min"


def models_of_modes() -> dict[str, str]:
    """Which model(s) each mode used, for the report."""
    described = {}
    for mode, (team_file, _lead) in MODES.items():
        models = sorted({c.model or "default" for c in load_agent_configs(team_file)})
        described[mode] = ", ".join(models)
    return described


# --- Command line --------------------------------------------------------------------

async def run_command(args: argparse.Namespace) -> None:
    settings = get_settings()
    tasks = [t for t in load_tasks() if not args.tasks or t.id in args.tasks.split(",")]
    modes = args.modes.split(",")
    unknown = set(modes) - MODES.keys()
    if unknown:
        raise SystemExit(f"Unknown mode(s): {', '.join(sorted(unknown))}. Choose from {', '.join(MODES)}")
    results = ResultsFile(EVALS_DIR / "results.json")
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    repolens = RepoLensTool(settings.repolens_url, settings.repolens_index_timeout_s, settings.repolens_ask_timeout_s)
    llm = make_llm_client(settings)
    evaluator = Evaluator(settings, redis, repolens, lambda _config: llm,
                          lambda: DockerSandbox(settings.sandbox_image, settings.workspaces_volume))
    try:
        for task in tasks:
            for mode in modes:
                if results.done(task.id, mode) and not args.rerun:
                    print(f"skip  {task.id}/{mode} (already scored)")
                    continue
                print(f"run   {task.id}/{mode} ...", flush=True)
                result = await evaluator.evaluate(task, mode)
                results.save(result)
                print(f"      {result.status}: {result.reason} [{result.tokens:,} tokens, {result.seconds / 60:.1f} min, {result.run_id}]")
                if result.status == "quota":
                    print("A daily quota is used up; stopping. Run again later to continue where this left off.")
                    return
    finally:
        write_report(results)
        await repolens.aclose()
        await redis.aclose()


def write_report(results: ResultsFile) -> None:
    (EVALS_DIR / "REPORT.md").write_text(render_report(load_tasks(), results.results, models_of_modes()))
    print(f"Report: {EVALS_DIR / 'REPORT.md'}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="agenthq.evals")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run tasks (skipping ones already scored) and update the report")
    run.add_argument("--tasks", help="Comma-separated task ids (default: all)")
    run.add_argument("--modes", default="team,solo", help="Comma-separated modes: team, solo")
    run.add_argument("--rerun", action="store_true", help="Run again even if already scored")
    commands.add_parser("report", help="Rewrite REPORT.md from results.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "report":
        write_report(ResultsFile(EVALS_DIR / "results.json"))
    else:
        asyncio.run(run_command(args))


if __name__ == "__main__":
    main()
