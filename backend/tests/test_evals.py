"""The evaluation harness: scripted runs in both modes, scoring, results and the report."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fakeredis.aioredis import FakeRedis

from agenthq.config import Settings
from agenthq.evals import EVALS_DIR, MODES, EvalResult, EvalTask, Evaluator, ResultsFile, load_tasks, render_report
from agenthq.llm import FakeLLMClient, LLMError, LLMResponse, ToolCall
from agenthq.sandbox import CommandResult
from tests.mocks import FakeSandbox

HEALTH = EvalTask("health_endpoint", "Add GET /health returning {\"status\": \"ok\"}.", "easy")
ADD_HEALTH = {"path": "app/main.py", "old_text": "    return app", "new_text": '''
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app'''}


def call(name: str, **arguments: object) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=f"{name}-1", name=name, arguments=arguments)])


def evaluator(scripts: dict[str, list], sandboxes: list[FakeSandbox], redis: FakeRedis, repo: Path, tmp_path: Path) -> Evaluator:
    settings = Settings(_env_file=None, inbox_block_ms=20, llm_retry_base_s=0, llm_max_retries=0, workspaces_dir=tmp_path / "ws")
    queue = list(sandboxes)
    return Evaluator(settings, redis, None, lambda config: FakeLLMClient(scripts.get(config.name, []), model="openai/gpt-oss-120b"),
                     lambda: queue.pop(0), repo_url=str(repo), poll_s=0.02)


def check_sandbox(suite_ok: bool = True, acceptance_ok: bool = True) -> FakeSandbox:
    return FakeSandbox([CommandResult("pytest -q", 0 if suite_ok else 1, "14 passed" if suite_ok else "1 failed, 13 passed"),
                        CommandResult("pytest acceptance", 0 if acceptance_ok else 1, "1 passed" if acceptance_ok else "1 failed")])


async def test_team_run_stops_at_sign_off_and_passes(redis: FakeRedis, sample_git_repo: Path, tmp_path: Path) -> None:
    plan = call("create_tasks", tasks=[{"id": "T1", "title": "Health", "description": "Add /health", "assignee": "engineer"}])
    scripts = {
        "manager": [plan, call("request_approval", summary="Added /health")],
        "engineer": [call("edit_file", **ADD_HEALTH), call("submit_task", summary="Added /health")],
        "reviewer": [call("approve_task")],
    }
    checker = check_sandbox()
    # One sandbox for the run, then a fresh one for the check.
    result = await evaluator(scripts, [FakeSandbox(), checker], redis, sample_git_repo, tmp_path).evaluate(HEALTH, "team")

    assert (result.status, result.outcome) == ("passed", "done")
    assert result.llm_calls == 5 and result.tokens > 0 and result.cost_usd is not None
    # The hidden test went into the clone only for the check, and ran on its own after the suite.
    workspace = tmp_path / "ws" / result.run_id
    assert (workspace / "tests" / "test_acceptance_health_endpoint.py").exists()
    assert checker.commands == ["pytest -q", "pytest -q -p no:cacheprovider tests/test_acceptance_health_endpoint.py"]
    assert '@app.get("/health")' in (workspace / "app" / "main.py").read_text()


async def test_solo_run_fails_when_the_suite_fails(redis: FakeRedis, sample_git_repo: Path, tmp_path: Path) -> None:
    scripts = {"solo": [call("edit_file", **ADD_HEALTH), call("finish_run", report="Added /health")]}
    sandboxes = [FakeSandbox(), check_sandbox(suite_ok=False)]
    result = await evaluator(scripts, sandboxes, redis, sample_git_repo, tmp_path).evaluate(HEALTH, "solo")
    assert result.status == "failed"
    assert result.reason == "The repo's own tests fail: 1 failed, 13 passed"


async def test_a_used_up_daily_quota_is_not_scored(redis: FakeRedis, sample_git_repo: Path, tmp_path: Path) -> None:
    quota = LLMError("429 Rate limit reached on tokens per day (TPD): Limit 200000", retryable=True, retry_after_s=900)
    result = await evaluator({"solo": [quota]}, [FakeSandbox()], redis, sample_git_repo, tmp_path).evaluate(HEALTH, "solo")
    assert result.status == "quota"


def result(task: str, mode: str, status: str, tokens: int = 1000, cost: float | None = 0.01, seconds: float = 60) -> EvalResult:
    return EvalResult(task, mode, status, "", "done", tokens, cost, seconds, 10, f"run-{task}-{mode}", "2026-10-13T12:00:00+00:00")


def test_results_file_saves_and_resumes(tmp_path: Path) -> None:
    path = tmp_path / "results.json"
    first = ResultsFile(path)
    first.save(result("logout", "team", "passed"))
    first.save(result("logout", "solo", "quota"))
    again = ResultsFile(path)
    assert again.done("logout", "team")
    assert not again.done("logout", "solo")  # a quota result is retried next time
    assert again.results["logout/team"] == first.results["logout/team"]


def test_report_summarises_each_mode() -> None:
    tasks = [EvalTask("a", "g", "easy"), EvalTask("b", "g", "hard")]
    results = {
        "a/team": result("a", "team", "passed", tokens=1000, cost=0.01, seconds=60),
        "b/team": result("b", "team", "failed", tokens=3000, cost=0.03, seconds=180),
        "a/solo": result("a", "solo", "passed", tokens=500, cost=None, seconds=30),
        "b/solo": result("b", "solo", "quota"),
    }
    report = render_report(tasks, results, {mode: "m" for mode in MODES})
    assert "| team | 2 | 1/2 (50%) | 2,000 | $0.0200 | 2.0 min | 10 |" in report
    assert "| solo | 1 | 1/1 (100%) | 500 | – | 0.5 min | 10 |" in report
    assert "| b | hard | fail, 3k tokens, 3.0 min | quota (not scored) |" in report


def test_every_task_has_acceptance_tests_and_a_reference_solution() -> None:
    for task in load_tasks():
        assert (EVALS_DIR / "acceptance" / f"test_{task.id}.py").exists(), task.id
        assert (EVALS_DIR / "solutions" / f"{task.id}.patch").exists(), task.id
    assert 8 <= len(load_tasks()) <= 10


@pytest.mark.evals
@pytest.mark.parametrize("task", [t.id for t in load_tasks()])
def test_acceptance_tests_fail_before_and_pass_after_the_reference_solution(task: str, sample_git_repo: Path, tmp_path: Path) -> None:
    """Guards the guard: a broken acceptance test would silently skew every result."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(sample_git_repo), str(repo)], check=True)
    shutil.copy(EVALS_DIR / "acceptance" / f"test_{task}.py", repo / "tests" / f"test_acceptance_{task}.py")

    def acceptance_passes() -> bool:
        return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"tests/test_acceptance_{task}.py"],
                              cwd=repo, capture_output=True).returncode == 0

    assert not acceptance_passes()
    subprocess.run(["git", "apply", str(EVALS_DIR / "solutions" / f"{task}.patch")], cwd=repo, check=True)
    assert acceptance_passes()
    suite = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--ignore", f"tests/test_acceptance_{task}.py"],
                           cwd=repo, capture_output=True)
    assert suite.returncode == 0, suite.stdout.decode()[-500:]
