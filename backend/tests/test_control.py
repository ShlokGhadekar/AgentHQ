"""Run control: the approval gate and pull requests, pause/resume/stop, and guardrails inside real runs."""

import asyncio
from pathlib import Path

import pytest
from fakeredis.aioredis import FakeRedis

from agenthq.events import EventType
from agenthq.llm import LLMResponse, ToolCall
from agenthq.runtime import Run
from agenthq.tasks import TaskStatus
from agenthq.tools import ApprovalError
from agenthq.workspace import Workspace
from tests.conftest import RunFactory, run_to_end
from tests.mocks import FakeSandbox


def call(name: str, **arguments: object) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=f"{name}-call", name=name, arguments=arguments)])


ONE_TASK = call("create_tasks", tasks=[{"id": "T1", "title": "Version", "description": "Set version 1.1", "assignee": "engineer"}])
ASK_APPROVAL = call("request_approval", summary="Set version 1.1 in app/main.py")
FINISH = call("finish_run", report="done")


class FakePublisher:
    """Records pull requests instead of talking to GitHub."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    async def open(self, workspace: Workspace, repo_url: str, branch: str, title: str, body: str) -> str:
        self.opened.append((branch, body))
        return "https://github.com/x/y/pull/1"


def engineer_edits() -> list[LLMResponse]:
    return [call("edit_file", path="app/main.py", old_text='FastAPI(title="Notes API")',
                 new_text='FastAPI(title="Notes API", version="1.1")'),
            call("submit_task", summary="Set version 1.1")]


@pytest.fixture
def coding_run(make_run: RunFactory, sample_git_repo: Path, tmp_path: Path):
    """A one-task run that really edits a clone, gets reviewed, and asks for approval."""

    def build(manager_after_decision: list[LLMResponse], publisher: FakePublisher | None = None) -> Run:
        return make_run({
            "manager": [ONE_TASK, ASK_APPROVAL, *manager_after_decision],
            "engineer": engineer_edits(),
            "reviewer": [call("approve_task")],
        }, repo=str(sample_git_repo), workspace=Workspace(tmp_path / "ws" / "run-test"),
            sandbox=FakeSandbox(), publisher=publisher)

    return build


# --- Approval gate ------------------------------------------------------------

async def test_approval_request_carries_diff_and_fresh_test_results(coding_run, redis: FakeRedis) -> None:
    run = coding_run([FINISH], FakePublisher())
    events = await run_to_end(run, redis, approve=True)
    request = next(e for e in events if e.type == EventType.APPROVAL_REQUESTED)
    assert 'version="1.1"' in request.payload["diff"]
    assert request.payload["tests"]["passed"]  # the system ran the tests itself
    assert request.recipient == "human"


async def test_approval_opens_the_pull_request_after_the_decision_event(coding_run, redis: FakeRedis) -> None:
    publisher = FakePublisher()
    events = await run_to_end(coding_run([FINISH], publisher), redis, approve=True)
    types = [e.type for e in events]
    assert publisher.opened and publisher.opened[0][0] == "agenthq/run-test"
    pr_message = next(e for e in events if e.payload.get("pr_url"))
    # The decision is in the log before anything is published.
    assert types.index(EventType.APPROVAL_DECISION) < events.index(pr_message)
    assert events[-1].type == EventType.RUN_COMPLETE


async def test_rejection_never_opens_a_pull_request(coding_run, redis: FakeRedis) -> None:
    publisher = FakePublisher()
    events = await run_to_end(coding_run([FINISH], publisher), redis, approve=False, comment="Use 2.0")
    assert publisher.opened == []
    manager_saw = next(e for e in events if e.recipient == "manager" and "rejected" in e.payload.get("content", ""))
    assert "Use 2.0" in manager_saw.payload["content"]


async def test_pull_request_code_refuses_without_an_approval(coding_run) -> None:
    run = coding_run([], FakePublisher())
    with pytest.raises(Exception, match="without a human approval"):
        await run._open_pull_request()


async def test_decision_without_a_request_is_rejected(make_run: RunFactory) -> None:
    with pytest.raises(ApprovalError):
        await make_run({}).decide_approval(True)


async def test_approval_needs_all_tasks_done(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({"manager": [ONE_TASK, ASK_APPROVAL, FINISH], "engineer": [LLMResponse(text="thinking")] * 2})
    events = await run_to_end(run, redis, approve=None)
    early = next(e for e in events if e.type == EventType.TOOL_RESULT and e.payload["tool"] == "request_approval")
    assert not early.payload["ok"] and "Every task must be done" in early.payload["result"]


async def test_finish_run_waits_for_a_pending_decision(coding_run, redis: FakeRedis) -> None:
    run = coding_run([FINISH, FINISH])
    await run.start()
    async with asyncio.timeout(5):
        while not run.approval_pending:
            await asyncio.sleep(0.005)
    assert not run.finished.is_set()  # no decision yet, so the run can't end
    await run.decide_approval(True)
    async with asyncio.timeout(5):
        await run.wait()


# --- Pause, resume, stop -------------------------------------------------------

async def test_pause_holds_llm_calls_until_resume(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({"manager": [call("finish_run", report="done")]})
    await run.pause()
    await run.start()
    await asyncio.sleep(0.1)
    assert run.agents["manager"]._llm.calls == []  # the goal arrived, but nobody may think while paused
    await run.resume()
    async with asyncio.timeout(5):
        await run.wait()
    states = [e.payload["run_state"] for _, e in await _events(redis) if "run_state" in e.payload]
    assert "paused" in states and states[-1] == "running"


async def test_stop_ends_a_paused_run_with_a_reason(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({"manager": [ONE_TASK]})
    await run.pause()
    await run.start()
    async with asyncio.timeout(5):
        await run.stop("Stopped by the user")  # must wake the paused agents, or this hangs
    last = (await _events(redis))[-1][1]
    assert last.type == EventType.RUN_COMPLETE and last.payload["stopped"]


# --- Guardrails inside runs ----------------------------------------------------

async def test_step_limit_fails_the_task_and_the_manager_decides(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({
        "manager": [ONE_TASK, FINISH],
        # The engineer keeps sending messages that never finish the task.
        "engineer": [call("send_message", to="researcher", content="hi")] * 20,
        "researcher": [LLMResponse(text="ok")] * 20,
    })
    run.settings.max_steps_per_task = 3
    run.settings.max_messages_without_progress = 100
    await run_to_end(run, redis)
    task = run.board.tasks["T1"]
    assert task.status == TaskStatus.FAILED and "Guardrail" in (task.result or "")


async def test_token_cap_stops_the_run(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({"manager": [ONE_TASK], "engineer": [LLMResponse(text="x", tokens_in=500)] * 10})
    run.settings.run_token_cap = 1000
    events = await run_to_end(run, redis)
    assert events[-1].type == EventType.RUN_COMPLETE and "Token cap" in events[-1].payload["report"]


async def test_retries_are_capped(make_run: RunFactory, redis: FakeRedis) -> None:
    retry = call("retry_task", task_id="T1", note="again")
    run = make_run({
        "manager": [ONE_TASK, retry, retry, FINISH],
        "engineer": [call("fail_task", reason="can't")] * 3,
    })
    run.settings.max_task_attempts = 2
    events = await run_to_end(run, redis)
    retries = [e.payload for e in events if e.type == EventType.TOOL_RESULT and e.payload["tool"] == "retry_task"]
    assert [r["ok"] for r in retries] == [True, False]


async def _events(redis: FakeRedis) -> list:
    from agenthq.bus import read_events
    return await read_events(redis, "0-0", block_ms=10)
