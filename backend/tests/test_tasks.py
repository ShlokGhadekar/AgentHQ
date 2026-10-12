import pytest

from agenthq.events import Event, EventType
from agenthq.tasks import BoardError, TaskBoard, TaskStatus


def created(task_id: str, depends_on: list[str] | None = None) -> Event:
    return Event(run_id="r", type=EventType.TASK_CREATED, task_id=task_id,
                 payload={"title": f"task {task_id}", "assignee": "engineer", "depends_on": depends_on or []})


def assigned(task_id: str, to: str = "engineer") -> Event:
    return Event(run_id="r", type=EventType.TASK_ASSIGNED, recipient=to, task_id=task_id)


def status(task_id: str, value: TaskStatus, **details: str) -> Event:
    return Event(run_id="r", type=EventType.STATUS_CHANGE, task_id=task_id, payload={"task_status": value, **details})


def board_with(*events: Event) -> TaskBoard:
    board = TaskBoard()
    for event in events:
        board.apply(event)
    return board


def test_happy_path_through_review() -> None:
    board = board_with(
        created("T1"), assigned("T1"),
        status("T1", TaskStatus.REVIEW, summary="v1"),
        status("T1", TaskStatus.IN_PROGRESS, comments="fix it"),
        status("T1", TaskStatus.REVIEW, summary="v2"),
        status("T1", TaskStatus.DONE),
    )
    task = board.tasks["T1"]
    assert task.status == TaskStatus.DONE
    assert task.result == "v2"
    assert board.all_done()


@pytest.mark.parametrize(("history", "illegal"), [
    ([], TaskStatus.DONE),                                             # todo -> done skips the work
    ([assigned("T1"), status("T1", TaskStatus.DONE)], TaskStatus.IN_PROGRESS),  # done is final
    ([assigned("T1"), status("T1", TaskStatus.FAILED)], TaskStatus.DONE),       # failed must be retried first
])
def test_illegal_transitions_are_rejected(history: list[Event], illegal: TaskStatus) -> None:
    board = board_with(created("T1"), *history)
    with pytest.raises(BoardError):
        board.apply(status("T1", illegal))


def test_rejected_event_leaves_board_unchanged() -> None:
    board = board_with(created("T1"))
    with pytest.raises(BoardError):
        board.apply(status("T1", TaskStatus.DONE))
    assert board.tasks["T1"].status == TaskStatus.TODO


def test_ready_respects_dependencies() -> None:
    board = board_with(created("T1"), created("T2", ["T1"]), created("T3", ["T1", "T2"]))
    assert [t.id for t in board.ready()] == ["T1"]
    board.apply(assigned("T1"))
    board.apply(status("T1", TaskStatus.DONE))
    assert [t.id for t in board.ready()] == ["T2"]


def test_cannot_assign_before_dependencies_are_done() -> None:
    board = board_with(created("T1"), created("T2", ["T1"]))
    with pytest.raises(BoardError, match="T1"):
        board.apply(assigned("T2"))


def test_dependencies_must_exist() -> None:
    with pytest.raises(BoardError, match="unknown"):
        board_with(created("T2", ["T1"]))


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(BoardError):
        board_with(created("T1"), created("T1"))


def test_retry_can_reassign_with_note() -> None:
    board = board_with(created("T1"), assigned("T1"), status("T1", TaskStatus.FAILED, reason="stuck"))
    assert board.tasks["T1"].result == "stuck"
    board.apply(status("T1", TaskStatus.TODO, assignee="researcher", note="look in app/auth.py"))
    task = board.tasks["T1"]
    assert (task.status, task.assignee, task.note) == (TaskStatus.TODO, "researcher", "look in app/auth.py")
    assert board.ready() == [task]


def test_unrelated_events_are_ignored() -> None:
    board = board_with(created("T1"), Event(run_id="r", type=EventType.STATUS_CHANGE, payload={"agent_state": "idle"}))
    assert board.tasks["T1"].status == TaskStatus.TODO
