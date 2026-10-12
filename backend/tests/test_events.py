from datetime import UTC

import pytest
from pydantic import ValidationError

from agenthq.events import Event, EventType


def test_defaults_are_filled_in() -> None:
    event = Event(run_id="run1", type=EventType.MESSAGE)
    assert len(event.id) == 32
    assert event.timestamp.tzinfo == UTC
    assert event.payload == {}
    assert (event.tokens_in, event.tokens_out) == (0, 0)


def test_ids_are_unique() -> None:
    assert Event(run_id="r", type="message").id != Event(run_id="r", type="message").id


def test_json_round_trip_keeps_every_field() -> None:
    # Events travel through Redis and Postgres as JSON, so a round trip must be lossless.
    event = Event(
        run_id="run1",
        type=EventType.TOOL_RESULT,
        sender="researcher",
        recipient="engineer",
        task_id="T2",
        payload={"found": True, "citations": [{"path": "app/auth.py", "start_line": 3, "end_line": 9}]},
        tokens_in=120,
        tokens_out=40,
    )
    assert Event.model_validate_json(event.model_dump_json()) == event


def test_type_is_serialized_as_plain_string() -> None:
    assert Event(run_id="r", type=EventType.LLM_USAGE).model_dump(mode="json")["type"] == "llm_usage"


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Event(run_id="r", type="teleport")


def test_all_spec_types_exist() -> None:
    expected = {
        "task_created", "task_assigned", "message", "tool_call", "tool_result", "status_change",
        "approval_requested", "approval_decision", "llm_usage", "indexing_progress", "error", "run_complete",
    }
    assert {t.value for t in EventType} == expected
