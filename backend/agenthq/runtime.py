"""A run: one goal, one team of agents, one task board, one stream of events.

`Run.publish` is the only way anything happens. It applies the event to the board, writes it to
the bus and then works out the consequences (hand out tasks whose dependencies are done, tell
the Manager when it must decide something), all under one lock so the board and the event log
always agree.
"""

import asyncio
from collections import defaultdict
from collections.abc import Callable
from uuid import uuid4

from agenthq.agent import Agent, AgentConfig
from agenthq.bus import RedisBus
from agenthq.config import Settings
from agenthq.events import Event, EventType
from agenthq.llm import LLMClient
from agenthq.tasks import TaskBoard, TaskStatus

MANAGER = "manager"


def new_run_id() -> str:
    return f"run-{uuid4().hex[:8]}"


class Run:
    """Owns the agents of one run and the single path every event goes through."""

    def __init__(
        self,
        run_id: str,
        goal: str,
        repo: str | None,
        bus: RedisBus,
        configs: list[AgentConfig],
        llm_for: Callable[[AgentConfig], LLMClient],
        settings: Settings,
    ) -> None:
        self.run_id = run_id
        self.goal = goal
        self.repo = repo
        self.bus = bus
        self.settings = settings
        self.board = TaskBoard()
        self.agents = {config.name: Agent(config, llm_for(config), self) for config in configs}
        if MANAGER not in self.agents:
            raise ValueError(f"agents.yaml must define a {MANAGER!r} agent")
        self.workers = [name for name in self.agents if name != MANAGER]
        self.finished = asyncio.Event()
        # agent -> [tokens_in, tokens_out]; the token cap in Phase 4 reads this.
        self.usage: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        self._lock = asyncio.Lock()
        self._loops: list[asyncio.Task[None]] = []
        self._told_manager_all_done = False

    async def start(self) -> None:
        """Start every agent's loop, then hand the goal to the Manager."""
        self._loops = [asyncio.create_task(agent.run_loop(), name=agent.name) for agent in self.agents.values()]
        await self.publish(Event(
            run_id=self.run_id, type=EventType.MESSAGE, sender="human", recipient=MANAGER,
            payload={"content": f"Goal: {self.goal}"},
        ))

    async def wait(self) -> None:
        """Wait until the run is finished and every agent loop has exited."""
        await self.finished.wait()
        await asyncio.gather(*self._loops)

    async def stop(self) -> None:
        """Stop without a final report (timeouts, Ctrl+C). Agents exit after their current turn."""
        self.finished.set()
        await asyncio.gather(*self._loops, return_exceptions=True)

    async def publish(self, event: Event) -> None:
        """Apply an event and its consequences, atomically with respect to other agents.

        Without the lock, two agents finishing at the same moment could both see a task as
        ready and hand it out twice, or write events in a different order than the board saw them.
        """
        async with self._lock:
            # A finished run's log is closed, so run_complete is always its last event. Agents still
            # finishing a turn at that moment can't add to it.
            if self.finished.is_set():
                return
            await self._append(event)
            for follow_up in self._consequences(event):
                await self._append(follow_up)

    async def _append(self, event: Event) -> None:
        # Apply first: if the event breaks a board rule it raises here and is never written.
        self.board.apply(event)
        await self.bus.publish(event)
        if event.type == EventType.LLM_USAGE and event.sender:
            self.usage[event.sender][0] += event.tokens_in
            self.usage[event.sender][1] += event.tokens_out
        if event.type == EventType.RUN_COMPLETE:
            self.finished.set()

    def _consequences(self, event: Event) -> list[Event]:
        """Events that follow automatically from a task being created or changing status."""
        changes_tasks = event.type == EventType.TASK_CREATED or (
            event.type == EventType.STATUS_CHANGE and "task_status" in event.payload
        )
        if not changes_tasks:
            return []
        status = event.payload.get("task_status")
        if event.type == EventType.TASK_CREATED or status == TaskStatus.TODO:
            # New work exists (a new task or a retry), so the "all done" message must be sent again later.
            self._told_manager_all_done = False
        follow_ups = [
            # The scheduler is plain code, not an LLM: the Manager chose assignees and dependencies,
            # so handing out a task the moment it is unblocked needs no judgement (and no tokens).
            Event(run_id=self.run_id, type=EventType.TASK_ASSIGNED, sender=MANAGER, recipient=task.assignee,
                  task_id=task.id, payload={"title": task.title})
            for task in self.board.ready()
        ]
        if status == TaskStatus.FAILED:
            task = self.board.get(event.task_id)
            follow_ups.append(self._tell_manager(
                f"Task {task.id} ({task.title}) failed. {task.assignee} says: {task.result}\n"
                "Retry it with retry_task (you can pick another assignee and add a note), or finish_run if the goal can't be reached."
            ))
        elif self.board.all_done() and not self._told_manager_all_done:
            self._told_manager_all_done = True
            results = "\n".join(f"- {t.id} {t.title} ({t.assignee}): {t.result}" for t in self.board.tasks.values())
            follow_ups.append(self._tell_manager(f"All tasks are done:\n{results}\n\nWrite the final report with finish_run."))
        return follow_ups

    def _tell_manager(self, content: str) -> Event:
        # No task_id: the Manager handles these in its main conversation, next to its plan.
        return Event(run_id=self.run_id, type=EventType.MESSAGE, sender="system", recipient=MANAGER,
                     payload={"content": content})

    def describe_task(self, task_id: str | None) -> str:
        """The assignment message an agent sees: the task plus the results of the tasks it depends on."""
        task = self.board.get(task_id)
        parts = [f"You are assigned task {task.id}: {task.title}\n\n{task.description}"]
        if task.depends_on:
            results = "\n\n".join(f"{dep} ({self.board.tasks[dep].title}):\n{self.board.tasks[dep].result}" for dep in task.depends_on)
            parts.append(f"Results of the tasks this depends on:\n\n{results}")
        if task.note:
            parts.append(f"This is a retry. Note from the manager: {task.note}")
        parts.append("When you are done, call submit_task. If you can't do it, call fail_task.")
        return "\n\n".join(parts)
