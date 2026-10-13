"""Tasks and the task board.

The board is never edited directly: it is rebuilt by applying events in order (`TaskBoard.apply`).
The runtime, the live UI and replay all use this same function, so they can't disagree about
what state a task is in.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from agenthq.events import Event, EventType


class TaskStatus(StrEnum):
    """The Kanban columns, plus `failed` for tasks waiting on a Manager decision."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"


# Every legal move. Anything else is a bug (or a confused agent) and is rejected.
ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.TODO: {TaskStatus.IN_PROGRESS},
    TaskStatus.IN_PROGRESS: {TaskStatus.REVIEW, TaskStatus.DONE, TaskStatus.FAILED},
    TaskStatus.REVIEW: {TaskStatus.DONE, TaskStatus.IN_PROGRESS, TaskStatus.FAILED},
    TaskStatus.FAILED: {TaskStatus.TODO},  # the Manager retries it
    TaskStatus.DONE: set(),
}


class BoardError(Exception):
    """An event would break a board rule (unknown task, illegal transition, ...)."""


@dataclass
class Task:
    """One unit of work, owned by one agent at a time."""

    id: str
    title: str
    description: str
    assignee: str
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.TODO
    # The latest summary (on submit) or failure reason, shown to the Manager and dependent tasks.
    result: str | None = None
    # Extra instructions from the Manager when it retries the task.
    note: str | None = None
    # How many times the task has been handed out (1 + retries).
    attempts: int = 0


class TaskBoard:
    """All tasks of one run, derived from the run's events."""

    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}

    def apply(self, event: Event) -> None:
        """Update the board for one event. Raises BoardError if the event breaks a rule.

        Events that don't concern tasks are ignored, so callers can pass every event through.
        """
        if event.type == EventType.TASK_CREATED:
            self._create(event)
        elif event.type == EventType.TASK_ASSIGNED:
            task = self.get(event.task_id)
            unfinished = [dep for dep in task.depends_on if self.tasks[dep].status != TaskStatus.DONE]
            if unfinished:
                raise BoardError(f"Task {task.id} can't start before {', '.join(unfinished)} is done")
            self._move(task, TaskStatus.IN_PROGRESS)
            task.assignee = event.recipient or task.assignee
            task.attempts += 1
        elif event.type == EventType.STATUS_CHANGE and "task_status" in event.payload:
            task = self.get(event.task_id)
            self._move(task, TaskStatus(event.payload["task_status"]))
            # Only some changes carry these fields: submit has a summary, fail a reason, retry an assignee and note.
            task.result = event.payload.get("summary") or event.payload.get("reason") or task.result
            task.assignee = event.payload.get("assignee") or task.assignee
            task.note = event.payload.get("note") or task.note

    def get(self, task_id: str | None) -> Task:
        """Look up a task, raising BoardError if it doesn't exist."""
        if task_id not in self.tasks:
            raise BoardError(f"Unknown task {task_id}")
        return self.tasks[task_id]

    def ready(self) -> list[Task]:
        """Tasks waiting in To Do whose dependencies are all done: these can be handed out now."""
        return [
            task for task in self.tasks.values()
            if task.status == TaskStatus.TODO and all(self.tasks[dep].status == TaskStatus.DONE for dep in task.depends_on)
        ]

    def all_done(self) -> bool:
        return bool(self.tasks) and all(task.status == TaskStatus.DONE for task in self.tasks.values())

    def _create(self, event: Event) -> None:
        task_id = event.task_id
        if not task_id or task_id in self.tasks:
            raise BoardError(f"Task id {task_id!r} is missing or already used")
        depends_on = list(event.payload.get("depends_on", []))
        missing = [dep for dep in depends_on if dep not in self.tasks]
        if missing:
            # Dependencies must already exist, which also makes cycles impossible.
            raise BoardError(f"Task {task_id} depends on unknown task(s) {', '.join(missing)}")
        self.tasks[task_id] = Task(
            id=task_id,
            title=event.payload["title"],
            description=event.payload.get("description", ""),
            assignee=event.payload["assignee"],
            depends_on=depends_on,
        )

    @staticmethod
    def _move(task: Task, new_status: TaskStatus) -> None:
        if new_status not in ALLOWED_TRANSITIONS[task.status]:
            raise BoardError(f"Task {task.id} can't move from {task.status} to {new_status}")
        task.status = new_status
