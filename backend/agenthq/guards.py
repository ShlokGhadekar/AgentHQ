"""Guardrails: the limits that keep a run from looping or spending without bound.

Everything is counted from events, like the task board, so the counts are exactly what the
event log shows and replay can explain why a limit fired. `observe` is called once per event
and returns the violations that event caused:

- task-level ones fail that task, which wakes the Manager to retry, reassign or give up;
- run-level ones (total steps, the token cap) stop the run, because nothing inside it can fix them.
"""

from collections import Counter
from dataclasses import dataclass

from agenthq.config import Settings
from agenthq.events import Event, EventType
from agenthq.tasks import TaskStatus


@dataclass(frozen=True)
class Violation:
    reason: str
    task_id: str | None = None  # None: the whole run must stop

    @property
    def stops_run(self) -> bool:
        return self.task_id is None


class Guardrails:
    """Counts steps, tokens, messages and failures for one run and reports crossed limits."""

    def __init__(self, settings: Settings, agents: set[str]) -> None:
        self._settings = settings
        self._agents = agents
        self.total_steps = 0
        self.total_tokens = 0
        # One "step" is one LLM call. Counted per agent per task attempt.
        self._task_steps: Counter[tuple[str, str]] = Counter()
        # Messages between a pair of agents about one task since that task last changed status.
        self._chatter: Counter[tuple[str, frozenset[str]]] = Counter()
        self._review_rounds: Counter[str] = Counter()
        self._failed_calls_in_a_row: Counter[tuple[str, str | None]] = Counter()
        self._run_stopped = False

    def observe(self, event: Event) -> list[Violation]:
        handlers = {
            EventType.LLM_USAGE: self._on_llm_call,
            EventType.MESSAGE: self._on_message,
            EventType.STATUS_CHANGE: self._on_status_change,
            EventType.TASK_ASSIGNED: self._on_assigned,
            EventType.TOOL_RESULT: self._on_tool_result,
        }
        handler = handlers.get(event.type)
        violations = handler(event) if handler else []
        # Report a run-level stop once, even if later events cross the same line again.
        if any(v.stops_run for v in violations):
            if self._run_stopped:
                violations = [v for v in violations if not v.stops_run]
            self._run_stopped = True
        return violations

    def _on_llm_call(self, event: Event) -> list[Violation]:
        s = self._settings
        self.total_steps += 1
        self.total_tokens += event.tokens_in + event.tokens_out
        violations = []
        if self.total_tokens > s.run_token_cap:
            violations.append(Violation(f"Token cap reached: {self.total_tokens:,} tokens used (cap {s.run_token_cap:,})"))
        elif self.total_steps > s.max_steps_per_run:
            violations.append(Violation(f"Step limit reached: {self.total_steps} LLM calls (limit {s.max_steps_per_run})"))
        if event.task_id and event.sender:
            key = (event.sender, event.task_id)
            self._task_steps[key] += 1
            if self._task_steps[key] == s.max_steps_per_task + 1:
                violations.append(Violation(
                    f"{event.sender} used more than {s.max_steps_per_task} LLM calls on {event.task_id} without finishing",
                    event.task_id,
                ))
        return violations

    def _on_message(self, event: Event) -> list[Violation]:
        # Only agent-to-agent talk about a task can loop; system notices and human chat can't.
        if not event.task_id or event.sender not in self._agents or event.recipient not in self._agents:
            return []
        key = (event.task_id, frozenset((event.sender, event.recipient)))
        self._chatter[key] += 1
        if self._chatter[key] == self._settings.max_messages_without_progress + 1:
            first, second = sorted(key[1])
            return [Violation(
                f"Possible loop: {first} and {second} exchanged {self._chatter[key]} messages about "
                f"{event.task_id} without the task moving forward",
                event.task_id,
            )]
        return []

    def _on_status_change(self, event: Event) -> list[Violation]:
        status = event.payload.get("task_status")
        if not status or not event.task_id:
            return []
        # Any status change is progress, so conversations about the task start counting from zero.
        for key in [k for k in self._chatter if k[0] == event.task_id]:
            del self._chatter[key]
        if status == TaskStatus.IN_PROGRESS:  # only reached from review: the reviewer sent it back
            self._review_rounds[event.task_id] += 1
            if self._review_rounds[event.task_id] == self._settings.max_review_rounds:
                return [Violation(
                    f"The reviewer sent {event.task_id} back {self._review_rounds[event.task_id]} times",
                    event.task_id,
                )]
        return []

    def _on_assigned(self, event: Event) -> list[Violation]:
        # A retry is a fresh attempt with a fresh conversation, so its step count starts over.
        if event.task_id and event.recipient:
            self._task_steps[(event.recipient, event.task_id)] = 0
        return []

    def _on_tool_result(self, event: Event) -> list[Violation]:
        key = (event.sender or "", event.task_id)
        if event.payload.get("ok"):
            self._failed_calls_in_a_row[key] = 0
            return []
        self._failed_calls_in_a_row[key] += 1
        if event.task_id and self._failed_calls_in_a_row[key] == self._settings.max_failed_tool_calls_in_a_row:
            return [Violation(
                f"{event.sender}'s last {self._failed_calls_in_a_row[key]} tool calls on {event.task_id} all failed",
                event.task_id,
            )]
        return []
