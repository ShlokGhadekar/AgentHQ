from agenthq.console import format_event
from agenthq.events import Event, EventType


def test_message_line() -> None:
    event = Event(run_id="r", type=EventType.MESSAGE, sender="engineer", recipient="researcher",
                  task_id="T2", payload={"content": "Where is the config?\nThanks"})
    line = format_event(event, color=False)
    assert "engineer -> researcher" in line
    assert "T2" in line
    assert "Where is the config? Thanks" in line  # one line per event


def test_long_lines_are_cut() -> None:
    event = Event(run_id="r", type=EventType.MESSAGE, sender="manager", payload={"content": "x" * 500})
    assert len(format_event(event, color=False)) <= 140


def test_task_status_line() -> None:
    event = Event(run_id="r", type=EventType.STATUS_CHANGE, sender="reviewer", task_id="T2",
                  payload={"task_status": "in_progress", "comments": "add Retry-After"})
    assert "[in_progress] add Retry-After" in format_event(event, color=False)
