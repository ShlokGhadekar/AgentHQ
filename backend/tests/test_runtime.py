"""Runs of the real team with scripted LLMs: the full demo, plus the failure paths."""

import asyncio

from fakeredis.aioredis import FakeRedis

from agenthq.bus import read_events
from agenthq.cli import DEMO_SCRIPT
from agenthq.events import Event, EventType
from agenthq.llm import LLMError, LLMResponse, ToolCall
from agenthq.llm.fake import load_scripts
from agenthq.runtime import Run
from agenthq.tasks import TaskBoard, TaskStatus
from tests.conftest import RunFactory


def call(name: str, **arguments: object) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=f"{name}-call", name=name, arguments=arguments)])


def plan(*tasks: dict) -> LLMResponse:
    return call("create_tasks", tasks=list(tasks))


async def run_to_end(run: Run, redis: FakeRedis) -> list[Event]:
    """Run until run_complete and return the global event stream."""
    await run.start()
    await asyncio.wait_for(run.wait(), timeout=5)
    return [event for _, event in await read_events(redis, "0-0", block_ms=10)]


async def test_full_demo_run(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run(load_scripts(DEMO_SCRIPT))
    events = await run_to_end(run, redis)

    assert {t.id: t.status for t in run.board.tasks.values()} == {"T1": "done", "T2": "done", "T3": "done"}
    assert events[-1].type == EventType.RUN_COMPLETE
    # The Engineer asked the Researcher directly, without going through the Manager.
    assert any(e.type == EventType.MESSAGE and (e.sender, e.recipient) == ("engineer", "researcher") for e in events)
    # T2 went to review, came back with changes requested, and was approved the second time.
    t2_statuses = [e.payload["task_status"] for e in events if e.task_id == "T2" and "task_status" in e.payload]
    assert t2_statuses == ["review", "in_progress", "review", "done"]
    # Every agent's tokens were counted.
    assert set(run.usage) == {"manager", "researcher", "engineer", "reviewer"}


async def test_board_can_be_rebuilt_from_the_event_stream(make_run: RunFactory, redis: FakeRedis) -> None:
    # Event sourcing: replaying the global stream must give exactly the board the run ended with.
    run = make_run(load_scripts(DEMO_SCRIPT))
    events = await run_to_end(run, redis)
    rebuilt = TaskBoard()
    for event in events:
        rebuilt.apply(event)
    assert rebuilt.tasks == run.board.tasks


async def test_tasks_wait_for_dependencies(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run(load_scripts(DEMO_SCRIPT))
    events = await run_to_end(run, redis)
    order = [e.task_id for e in events if e.type == EventType.TASK_ASSIGNED]
    assert order == ["T1", "T2", "T3"]
    t1_done = next(i for i, e in enumerate(events) if e.task_id == "T1" and e.payload.get("task_status") == "done")
    t2_assigned = next(i for i, e in enumerate(events) if e.type == EventType.TASK_ASSIGNED and e.task_id == "T2")
    assert t1_done < t2_assigned


async def test_failed_task_is_retried_with_another_assignee(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({
        "manager": [
            plan({"id": "T1", "title": "Find login", "description": "d", "assignee": "engineer"}),
            call("retry_task", task_id="T1", assignee="researcher", note="This is research work."),
            call("finish_run", report="done"),
        ],
        "engineer": [call("fail_task", reason="This is a research question, not a code change.")],
        "researcher": [call("submit_task", summary="app/auth.py:12-34")],
    })
    await run_to_end(run, redis)
    task = run.board.tasks["T1"]
    assert (task.status, task.assignee, task.result) == (TaskStatus.DONE, "researcher", "app/auth.py:12-34")
    # The retry started a fresh conversation containing the Manager's note.
    first_prompt = run.agents["researcher"]._llm.calls[0][1]
    assert len(first_prompt) == 1 and "This is research work." in first_prompt[0].content
    # The Manager was told why it failed.
    manager_inputs = [m.content for _, messages, _ in run.agents["manager"]._llm.calls for m in messages]
    assert any("research question" in text for text in manager_inputs)


async def test_llm_error_fails_the_task_and_tells_the_manager(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({
        "manager": [
            plan({"id": "T1", "title": "Research", "description": "d", "assignee": "researcher"}),
            call("finish_run", report="Gave up: the researcher's LLM is down."),
        ],
        "researcher": [LLMError("Groq is down", retryable=False)],
    })
    events = await run_to_end(run, redis)
    assert run.board.tasks["T1"].status == TaskStatus.FAILED
    assert any(e.type == EventType.ERROR and e.sender == "researcher" for e in events)


async def test_rate_limited_call_is_retried_and_logged(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({
        "manager": [LLMError("429 rate limit", retryable=True, retry_after_s=0.01), call("finish_run", report="ok")],
    })
    events = await run_to_end(run, redis)
    retries = [e for e in events if e.type == EventType.ERROR]
    assert [(e.payload["attempt"], e.payload["retrying_in_s"]) for e in retries] == [(1, 0.0)]
    assert events[-1].type == EventType.RUN_COMPLETE


async def test_no_retry_when_the_provider_asks_for_a_long_wait(make_run: RunFactory, redis: FakeRedis) -> None:
    quota = LLMError("429 tokens per day", retryable=True, retry_after_s=1200)
    run = make_run({"manager": [quota]})
    events = await run_to_end(run, redis)
    assert not [e for e in events if "retrying_in_s" in e.payload]  # gave up at once
    assert events[-1].payload["failed"]


async def test_retries_give_up_after_the_limit(make_run: RunFactory, redis: FakeRedis) -> None:
    down = LLMError("503", retryable=True)
    run = make_run({
        "manager": [plan({"id": "T1", "title": "Research", "description": "d", "assignee": "researcher"}),
                    call("finish_run", report="researcher's LLM is down")],
        "researcher": [down, down, down],  # first try + 2 retries (llm_max_retries=2 in tests)
    })
    await run_to_end(run, redis)
    assert run.board.tasks["T1"].status == TaskStatus.FAILED


async def test_manager_failure_ends_the_run_instead_of_hanging(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({"manager": [LLMError("invalid API key", retryable=False)]})
    events = await run_to_end(run, redis)
    assert events[-1].type == EventType.RUN_COMPLETE
    assert events[-1].payload["failed"] and "invalid API key" in events[-1].payload["report"]


async def test_stalled_agent_is_nudged_once(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({
        "manager": [plan({"id": "T1", "title": "Research", "description": "d", "assignee": "researcher"}),
                    call("finish_run", report="ok")],
        "researcher": [LLMResponse(text="I would look at auth.py."), call("submit_task", summary="auth.py")],
    })
    await run_to_end(run, redis)
    assert run.board.tasks["T1"].status == TaskStatus.DONE
    last_prompt = run.agents["researcher"]._llm.calls[-1][1][-1].content
    assert "still in progress" in last_prompt


async def test_agent_that_stalls_twice_fails_its_task(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({
        "manager": [plan({"id": "T1", "title": "Research", "description": "d", "assignee": "researcher"}),
                    call("finish_run", report="stuck")],
        "researcher": [LLMResponse(text="Thinking."), LLMResponse(text="Still thinking.")],
    })
    await run_to_end(run, redis)
    assert run.board.tasks["T1"].status == TaskStatus.FAILED


async def test_invalid_plan_is_rejected_and_the_manager_can_fix_it(make_run: RunFactory, redis: FakeRedis) -> None:
    bad = plan({"id": "T1", "title": "x", "description": "d", "assignee": "intern"})
    good = plan({"id": "T1", "title": "x", "description": "d", "assignee": "researcher"})
    run = make_run({
        "manager": [bad, good, call("finish_run", report="ok")],
        "researcher": [call("submit_task", summary="done")],
    })
    events = await run_to_end(run, redis)
    failed = next(e for e in events if e.type == EventType.TOOL_RESULT and not e.payload["ok"])
    assert "intern" in failed.payload["result"]
    assert run.board.tasks["T1"].status == TaskStatus.DONE


async def test_only_the_assignee_can_submit(make_run: RunFactory, redis: FakeRedis) -> None:
    run = make_run({
        "manager": [plan({"id": "T1", "title": "Code", "description": "d", "assignee": "engineer"}),
                    call("finish_run", report="ok")],
        "engineer": [call("send_message", to="researcher", content="Where is login?"), LLMResponse(text="Waiting."),
                     call("submit_task", summary="done")],
        # The Researcher is in T1's conversation (it got the question) but doesn't own T1.
        "researcher": [call("submit_task", summary="hijack"), call("send_message", to="engineer", content="app/auth.py"),
                       LLMResponse(text="Answered.")],
        "reviewer": [call("approve_task")],
    })
    events = await run_to_end(run, redis)
    rejected = next(e for e in events if e.type == EventType.TOOL_RESULT and e.sender == "researcher")
    assert not rejected.payload["ok"]
    assert run.board.tasks["T1"].result == "done"
