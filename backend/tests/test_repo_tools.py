"""The codebase tools inside real (scripted) runs: RepoLens handling, file edits and the sandbox."""

from pathlib import Path

from fakeredis.aioredis import FakeRedis

from agenthq.events import Event, EventType
from agenthq.llm import LLMError, LLMResponse, ToolCall
from agenthq.repolens import RepoLensConfigError
from agenthq.sandbox import CommandResult
from agenthq.tasks import TaskStatus
from agenthq.workspace import Workspace
from tests.conftest import RunFactory, run_to_end
from tests.mocks import FOUND, NOT_FOUND, UNAVAILABLE, FakeSandbox, MockRepoLensTool

RESEARCH_PLAN = LLMResponse(tool_calls=[ToolCall(id="p", name="create_tasks", arguments={"tasks": [
    {"id": "T1", "title": "Find login", "description": "Where is /login?", "assignee": "researcher"}]})])
FINISH = LLMResponse(tool_calls=[ToolCall(id="f", name="finish_run", arguments={"report": "done"})])


def call(name: str, **arguments: object) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=f"{name}-{len(arguments)}", name=name, arguments=arguments)])


def ask(question: str) -> LLMResponse:
    return call("ask_codebase", question=question)




def tool_results(events: list[Event], tool: str) -> list[dict]:
    return [e.payload for e in events if e.type == EventType.TOOL_RESULT and e.payload["tool"] == tool]


async def test_repolens_answer_is_recorded_with_citations(make_run: RunFactory, redis: FakeRedis) -> None:
    repolens = MockRepoLensTool([FOUND])
    run = make_run({"manager": [RESEARCH_PLAN, FINISH],
                    "researcher": [ask("Where is POST /login defined?"), call("submit_task", summary="app/auth.py:29-37")]},
                   repo="https://github.com/x/y", repolens=repolens)
    events = await run_to_end(run, redis)

    assert repolens.indexed == ["https://github.com/x/y"]  # indexed at run start
    assert any(e.type == EventType.INDEXING_PROGRESS for e in events)
    details = tool_results(events, "ask_codebase")[0]["details"]
    assert details["found"] and details["best_similarity"] == 0.71
    assert details["citations"] == [{"path": "app/auth.py", "start_line": 29, "end_line": 37}]


async def test_not_found_allows_one_rephrase_then_points_to_search(make_run: RunFactory, redis: FakeRedis) -> None:
    repolens = MockRepoLensTool([NOT_FOUND, NOT_FOUND])
    run = make_run({"manager": [RESEARCH_PLAN, FINISH],
                    "researcher": [ask("rate limiter?"), ask("throttling middleware?"), ask("third try?"),
                                   call("submit_task", summary="No rate limiting exists yet.")]},
                   repo="https://github.com/x/y", repolens=repolens)
    events = await run_to_end(run, redis)

    results = tool_results(events, "ask_codebase")
    assert "Rephrase the question once" in results[0]["result"]
    assert "Use search_code" in results[1]["result"]
    assert not results[2]["ok"] and len(repolens.questions) == 2  # the third question never reached RepoLens
    assert run.board.tasks["T1"].status == TaskStatus.DONE


async def test_repolens_outage_degrades_to_a_tool_error(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({"manager": [RESEARCH_PLAN, FINISH],
                    "researcher": [ask("Where is login?"), call("submit_task", summary="found it with search_code")]},
                   repo="https://github.com/x/y", repolens=MockRepoLensTool([UNAVAILABLE]))
    events = await run_to_end(run, redis)
    result = tool_results(events, "ask_codebase")[0]
    assert not result["ok"] and "search_code" in result["result"]
    assert run.board.tasks["T1"].status == TaskStatus.DONE


async def test_repolens_config_error_fails_the_task_for_the_manager(make_run: RunFactory, redis: FakeRedis) -> None:
    error = RepoLensConfigError("Repository not found or private")
    run = make_run({"manager": [RESEARCH_PLAN, FINISH], "researcher": [ask("Where is login?")]},
                   repo="https://github.com/x/y", repolens=MockRepoLensTool([error]))
    await run_to_end(run, redis)
    task = run.board.tasks["T1"]
    assert task.status == TaskStatus.FAILED and "not found or private" in (task.result or "")
    manager_saw = [m.content for _, messages, _ in run.agents["manager"]._llm.calls for m in messages]
    assert any("not found or private" in text for text in manager_saw)


async def test_setup_failure_ends_the_run_with_a_reason(make_run: RunFactory, redis: FakeRedis, tmp_path: Path) -> None:
    run = make_run({}, repo=str(tmp_path / "no-such-repo"), workspace=Workspace(tmp_path / "ws"))
    events = await run_to_end(run, redis)
    assert events[-1].type == EventType.RUN_COMPLETE
    assert "could not start" in events[-1].payload["report"]


async def test_engineer_edits_the_clone_and_tests_run_in_the_sandbox(
    make_run: RunFactory, redis: FakeRedis, sample_git_repo: Path, tmp_path: Path,
) -> None:
    """A full scripted run with real file tools on a clone of the sample repo."""
    sandbox = FakeSandbox([CommandResult("pytest -q", 1, "1 failed"), CommandResult("pytest -q", 0, "14 passed")])
    workspace = Workspace(tmp_path / "ws" / "run-test")
    run = make_run({
        "manager": [call("create_tasks", tasks=[
            {"id": "T1", "title": "Version the API", "description": "Set version 1.1", "assignee": "engineer"}]), FINISH],
        "engineer": [
            call("read_file", path="app/main.py"),
            call("edit_file", path="app/main.py", old_text='FastAPI(title="Notes API")',
                 new_text='FastAPI(title="Notes API", version="1.1")'),
            call("run_tests"),
            call("run_tests"),
            call("submit_task", summary="Set version 1.1 in app/main.py; tests pass."),
        ],
        "reviewer": [call("view_diff"), call("run_linter"), call("approve_task", comment="ok")],
    }, repo=str(sample_git_repo), workspace=workspace, sandbox=sandbox)
    events = await run_to_end(run, redis)

    assert run.board.tasks["T1"].status == TaskStatus.DONE
    assert 'version="1.1"' in (workspace.root / "app/main.py").read_text()
    # The original repo is untouched; only the run's clone changed.
    assert 'version="1.1"' not in (sample_git_repo / "app/main.py").read_text()
    assert [r["details"]["passed"] for r in tool_results(events, "run_tests")] == [False, True]
    assert '+    app = FastAPI(title="Notes API", version="1.1")' in tool_results(events, "view_diff")[0]["result"]
    assert sandbox.commands == ["pytest -q", "pytest -q", "ruff check ."]
    assert sandbox.started and sandbox.stopped


async def test_unknown_arguments_are_rejected_not_ignored(
    make_run: RunFactory, redis: FakeRedis, sample_git_repo: Path, tmp_path: Path,
) -> None:
    run = make_run({"manager": [RESEARCH_PLAN, FINISH],
                    "researcher": [call("read_file", path="app/auth.py", line_start=20),
                                   call("submit_task", summary="ok")]},
                   repo=str(sample_git_repo), workspace=Workspace(tmp_path / "ws" / "run-test"))
    events = await run_to_end(run, redis)
    result = tool_results(events, "read_file")[0]
    assert not result["ok"] and "line_start" in result["result"]


async def test_old_tool_outputs_are_removed_to_fit_the_budget(
    make_run: RunFactory, redis: FakeRedis, sample_git_repo: Path, tmp_path: Path,
) -> None:
    reads = [call("read_file", path=f) for f in ("app/auth.py", "app/notes.py", "app/storage.py", "app/models.py")]
    run = make_run({"manager": [RESEARCH_PLAN, FINISH], "researcher": [*reads, call("submit_task", summary="ok")]},
                   repo=str(sample_git_repo), workspace=Workspace(tmp_path / "ws" / "run-test"))
    run.settings.llm_context_budget_tokens = 1500  # ~4,500 characters: too small for four whole files
    await run_to_end(run, redis)

    last_call = run.agents["researcher"]._llm.calls[-1][1]
    outputs = [m.content for m in last_call if m.role == "tool"]
    assert outputs[0].startswith("[Output removed")  # oldest output went first
    assert not outputs[-1].startswith("[Output removed")  # the latest is always kept
    assert last_call[0].role == "user"  # the assignment itself is never removed


async def test_too_large_request_shrinks_the_conversation_and_retries(
    make_run: RunFactory, redis: FakeRedis, sample_git_repo: Path, tmp_path: Path,
) -> None:
    too_large = LLMError("413 Request too large", retryable=True, too_large=True)
    reads = [call("read_file", path=f) for f in ("app/auth.py", "app/notes.py", "app/storage.py")]
    run = make_run({"manager": [RESEARCH_PLAN, FINISH],
                    "researcher": [*reads, too_large, call("submit_task", summary="ok")]},
                   repo=str(sample_git_repo), workspace=Workspace(tmp_path / "ws" / "run-test"))
    events = await run_to_end(run, redis)

    assert run.board.tasks["T1"].status == TaskStatus.DONE
    retry = next(e for e in events if e.type == EventType.ERROR)
    assert "Shrinking the conversation" in retry.payload["message"]
    outputs = [m.content for m in run.agents["researcher"]._llm.calls[-1][1] if m.role == "tool"]
    assert sum(not o.startswith("[Output removed") for o in outputs) == 1  # only the newest survived
