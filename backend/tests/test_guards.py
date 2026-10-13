"""Guardrails in isolation: feed events, check which limits fire."""

from agenthq.config import Settings
from agenthq.events import Event, EventType
from agenthq.guards import Guardrails

AGENTS = {"manager", "researcher", "engineer", "reviewer"}


def guards(**limits: int) -> Guardrails:
    return Guardrails(Settings(_env_file=None, **limits), AGENTS)


def llm_call(agent: str = "engineer", task: str | None = "T1", tokens: int = 100) -> Event:
    return Event(run_id="r", type=EventType.LLM_USAGE, sender=agent, task_id=task, tokens_in=tokens)


def message(sender: str, recipient: str, task: str = "T1") -> Event:
    return Event(run_id="r", type=EventType.MESSAGE, sender=sender, recipient=recipient, task_id=task, payload={"content": "?"})


def status(task: str, value: str) -> Event:
    return Event(run_id="r", type=EventType.STATUS_CHANGE, task_id=task, payload={"task_status": value})


def tool_result(ok: bool, agent: str = "engineer", task: str = "T1") -> Event:
    return Event(run_id="r", type=EventType.TOOL_RESULT, sender=agent, task_id=task, payload={"ok": ok})


def fired(g: Guardrails, events: list[Event]) -> list:
    return [v for e in events for v in g.observe(e)]


def test_step_limit_per_task_fails_that_task() -> None:
    g = guards(max_steps_per_task=3)
    violations = fired(g, [llm_call() for _ in range(4)])
    assert len(violations) == 1
    assert violations[0].task_id == "T1" and not violations[0].stops_run


def test_a_retry_resets_the_step_count() -> None:
    g = guards(max_steps_per_task=3)
    fired(g, [llm_call() for _ in range(3)])
    g.observe(Event(run_id="r", type=EventType.TASK_ASSIGNED, recipient="engineer", task_id="T1"))
    assert fired(g, [llm_call() for _ in range(3)]) == []


def test_token_cap_stops_the_run_once() -> None:
    g = guards(run_token_cap=250)
    violations = fired(g, [llm_call(tokens=100) for _ in range(5)])
    assert len(violations) == 1 and violations[0].stops_run
    assert "Token cap" in violations[0].reason


def test_total_step_limit_stops_the_run() -> None:
    g = guards(max_steps_per_run=2)
    violations = fired(g, [llm_call("manager", None) for _ in range(3)])
    assert [v.stops_run for v in violations] == [True]


def test_chatter_without_progress_is_a_loop() -> None:
    g = guards(max_messages_without_progress=4)
    back_and_forth = [message("engineer", "researcher"), message("researcher", "engineer")] * 3
    violations = fired(g, back_and_forth)
    assert len(violations) == 1 and "engineer and researcher" in violations[0].reason


def test_progress_resets_the_chatter_count() -> None:
    g = guards(max_messages_without_progress=4)
    events = [message("engineer", "researcher")] * 4 + [status("T1", "review")] + [message("engineer", "researcher")] * 4
    assert fired(g, events) == []


def test_messages_from_system_or_human_never_count() -> None:
    g = guards(max_messages_without_progress=1)
    assert fired(g, [message("system", "engineer"), message("human", "engineer"), message("system", "engineer")]) == []


def test_review_ping_pong_is_capped() -> None:
    g = guards(max_review_rounds=2)
    rounds = [status("T1", "review"), status("T1", "in_progress")] * 2
    violations = fired(g, rounds)
    assert len(violations) == 1 and "sent T1 back 2 times" in violations[0].reason


def test_repeated_tool_failures_fail_the_task() -> None:
    g = guards(max_failed_tool_calls_in_a_row=3)
    assert fired(g, [tool_result(False), tool_result(False), tool_result(True), tool_result(False)]) == []
    violations = fired(g, [tool_result(False), tool_result(False)])
    assert len(violations) == 1 and violations[0].task_id == "T1"
