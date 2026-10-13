"""A run: one goal, one team of agents, one task board, one stream of events.

`Run.publish` is the only way anything happens. It applies the event to the board, writes it to
the bus and then works out the consequences (hand out tasks whose dependencies are done, tell
the Manager when it must decide something, enforce the guardrails), all under one lock so the
board and the event log always agree.
"""

import asyncio
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from agenthq.agent import Agent, AgentConfig
from agenthq.bus import RedisBus
from agenthq.config import Settings
from agenthq.events import Event, EventType
from agenthq.guards import Guardrails, Violation
from agenthq.llm import LLMClient
from agenthq.pull_requests import PublishError, PullRequestPublisher
from agenthq.repolens import CodebaseQA
from agenthq.sandbox import Sandbox
from agenthq.tasks import TaskBoard, TaskStatus
from agenthq.tools import ApprovalError
from agenthq.workspace import Workspace

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
        workspace: Workspace | None = None,
        sandbox: Sandbox | None = None,
        repolens: CodebaseQA | None = None,
        publisher: PullRequestPublisher | None = None,
    ) -> None:
        self.run_id = run_id
        self.created_at = datetime.now(UTC)
        self.goal = goal
        self.repo = repo
        self.bus = bus
        self.settings = settings
        # The code the agents work on. All optional, so coordination-only runs (the scripted demo) need none.
        self.workspace = workspace
        self.sandbox = sandbox
        self.repolens = repolens
        self.publisher = publisher
        # (agent, task) -> how many RepoLens questions came back "not found"; see the ask_codebase tool.
        self.repolens_misses: Counter[tuple[str, str | None]] = Counter()
        self.board = TaskBoard()
        self.agents = {config.name: Agent(config, llm_for(config), self) for config in configs}
        if MANAGER not in self.agents:
            raise ValueError(f"agents.yaml must define a {MANAGER!r} agent")
        # Read from the capability cards: only an agent that can submit a task can be given one.
        self.assignable = [name for name, agent in self.agents.items() if "submit_task" in agent.config.tools]
        self.guards = Guardrails(settings, set(self.agents))
        self.usage: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # agent -> [tokens_in, tokens_out]
        self.finished = asyncio.Event()
        # Cleared while paused. Agents wait on it before every LLM call and every new inbox event.
        self.running = asyncio.Event()
        self.running.set()
        # Both derived from events in _append: the PR code checks `approved`, not a flag a tool could set.
        self.approval_pending = False
        self.approved = False
        # This run's events in order, kept in memory: a UI that connects mid-run gets these first,
        # and the PR body quotes the approval request from here.
        self.history: list[Event] = []
        self._lock = asyncio.Lock()
        self._loops: list[asyncio.Task[None]] = []
        self._told_manager_all_done = False

    # --- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Prepare the repo, start every agent's loop, then hand the goal to the Manager."""
        await self._set_run_state("preparing", goal=self.goal, repo=self.repo)
        try:
            await self._prepare()
        except Exception as error:  # any setup failure ends the run cleanly with the reason
            await self.publish(self._event(EventType.ERROR, payload={"message": f"Setup failed: {error}"}))
            await self._end(f"The run could not start: {error}", failed=True)
            return
        await self._set_run_state("running" if self.running.is_set() else "paused")
        self._loops = [asyncio.create_task(agent.run_loop(), name=agent.name) for agent in self.agents.values()]
        await self.publish(Event(
            run_id=self.run_id, type=EventType.MESSAGE, sender="human", recipient=MANAGER,
            payload={"content": f"Goal: {self.goal}"},
        ))

    async def wait(self) -> None:
        """Wait until the run is finished and every agent loop has exited."""
        await self.finished.wait()
        await asyncio.gather(*self._loops)
        await self._teardown()

    async def pause(self) -> None:
        """Agents finish the LLM call they are in, then wait. Nothing is lost; resume continues."""
        if not self.finished.is_set() and self.running.is_set():
            self.running.clear()
            await self._set_run_state("paused")

    async def resume(self) -> None:
        if not self.finished.is_set() and not self.running.is_set():
            self.running.set()
            await self._set_run_state("running")

    async def stop(self, reason: str = "Stopped by the user") -> None:
        """End the run now, with a run_complete event saying why. Agents exit after their current call."""
        await self._end(reason, failed=True, stopped=True)
        self.running.set()  # wake paused agents so they can see the run is over
        await asyncio.gather(*self._loops, return_exceptions=True)
        await self._teardown()

    async def handle_agent_error(self, agent: str, task_id: str | None, error: Exception) -> None:
        """An agent's turn failed for good (retries exhausted, or a non-retryable error).

        A worker's task fails, which wakes the Manager to retry or reassign it. If the Manager itself
        fails, nobody is left to make decisions, so the run ends with the reason instead of hanging.
        """
        if agent == MANAGER:
            await self._end(f"Run stopped: the manager hit an error it could not recover from: {error}", failed=True)
            return
        task = self.board.tasks.get(task_id) if task_id else None
        if task and task.assignee == agent and task.status == TaskStatus.IN_PROGRESS:
            await self.publish(Event(run_id=self.run_id, type=EventType.STATUS_CHANGE, sender=agent, task_id=task.id,
                                     payload={"task_status": TaskStatus.FAILED, "reason": f"{agent} hit an error: {error}"}))

    async def report_indexing(self, progress: dict[str, Any]) -> None:
        """Publish RepoLens indexing progress, for the UI's progress bar."""
        await self.publish(self._event(EventType.INDEXING_PROGRESS, sender="repolens", payload=progress))

    # --- Approval gate ----------------------------------------------------------

    async def request_approval(self, summary: str) -> None:
        """Ask the human to approve the changes, with a diff and test results the system produced itself.

        The tests are run again here rather than trusting what the Engineer or Reviewer reported,
        so the approval card shows the real state of the code.
        """
        if self.approval_pending:
            raise ApprovalError("An approval request is already waiting for the human")
        if not self.board.all_done():
            raise ApprovalError("Every task must be done before asking for approval")
        diff = await self.workspace.diff() if self.workspace else ""
        tests = None
        if self.sandbox:
            result = await self.sandbox.run(self.settings.test_command, self.settings.command_timeout_s)
            tests = {"command": result.command, "passed": result.passed, "exit_code": result.exit_code,
                     "output": result.summary()}
        await self.publish(Event(run_id=self.run_id, type=EventType.APPROVAL_REQUESTED, sender=MANAGER, recipient="human",
                                 payload={"summary": summary, "diff": diff, "tests": tests}))

    async def decide_approval(self, approved: bool, comment: str = "") -> str | None:
        """Record the human's decision and act on it. Returns the pull request URL, if one was opened."""
        if not self.approval_pending:
            raise ApprovalError("No approval request is waiting")
        await self.publish(Event(run_id=self.run_id, type=EventType.APPROVAL_DECISION, sender="human",
                                 payload={"approved": approved, "comment": comment}))
        if not approved:
            await self.publish(self._tell_manager(
                f"The human rejected the changes. Their comment: {comment or '(none)'}\n"
                "Plan follow-up tasks with create_tasks to address it, or call finish_run if it can't be done."
            ))
            return None
        url, outcome = None, ""
        try:
            url = await self._open_pull_request()
            outcome = f"The pull request is open: {url}"
        except PublishError as error:
            outcome = f"Opening the pull request failed: {error}. The changes stay in the run's workspace."
        await self.publish(Event(run_id=self.run_id, type=EventType.MESSAGE, sender="system", recipient=MANAGER,
                                 payload={"content": f"The human approved the changes. {outcome}\n"
                                                     "Call finish_run with the final report.", "pr_url": url}))
        return url

    async def _open_pull_request(self) -> str:
        # The gate itself: this is the only code that opens pull requests, and it refuses unless the
        # event log holds an approving decision from the human.
        if not self.approved:
            raise PublishError("Refusing to open a pull request without a human approval")
        if not (self.workspace and self.repo):
            raise PublishError("This run has no repository, so there is no pull request to open")
        if not self.publisher:
            raise PublishError("No GitHub token is configured (set GITHUB_TOKEN)")
        request = next(e for e in reversed(self.history) if e.type == EventType.APPROVAL_REQUESTED)
        tests = request.payload.get("tests") or {}
        body = (
            f"{request.payload['summary']}\n\n"
            f"Tests: {'passing' if tests.get('passed') else 'not run or failing'} (`{tests.get('command', 'n/a')}`)\n\n"
            f"---\nOpened by AgentHQ run `{self.run_id}` after human approval. Goal: {self.goal}"
        )
        return await self.publisher.open(self.workspace, self.repo, f"agenthq/{self.run_id}", self.goal[:72], body)

    # --- Event pipeline -------------------------------------------------------

    async def publish(self, event: Event) -> None:
        """Apply an event and everything that follows from it, atomically with respect to other agents.

        Without the lock, two agents finishing at the same moment could both see a task as
        ready and hand it out twice, or write events in a different order than the board saw them.
        """
        async with self._lock:
            queue = [event]
            while queue:
                # A finished run's log is closed, so run_complete is always its last event. Agents
                # still finishing a call at that moment can't add to it.
                if self.finished.is_set():
                    return
                current = queue.pop(0)
                violations = await self._append(current)
                queue.extend(self._enforce(violations))
                queue.extend(self._consequences(current))

    async def _append(self, event: Event) -> list[Violation]:
        # Apply first: if the event breaks a board rule it raises here and is never written.
        self.board.apply(event)
        await self.bus.publish(event)
        self.history.append(event)
        if event.type == EventType.LLM_USAGE and event.sender:
            self.usage[event.sender][0] += event.tokens_in
            self.usage[event.sender][1] += event.tokens_out
        elif event.type == EventType.APPROVAL_REQUESTED:
            self.approval_pending = True
        elif event.type == EventType.APPROVAL_DECISION:
            self.approval_pending = False
            self.approved = bool(event.payload.get("approved"))
        elif event.type == EventType.RUN_COMPLETE:
            self.finished.set()
        return self.guards.observe(event)

    def _enforce(self, violations: list[Violation]) -> list[Event]:
        """Turn crossed limits into events: a failed task (the Manager decides) or the end of the run."""
        events = []
        for violation in violations:
            if violation.stops_run:
                events.append(self._event(EventType.RUN_COMPLETE, payload={
                    "report": f"Run stopped by a guardrail: {violation.reason}", "failed": True}))
                break
            task = self.board.tasks.get(violation.task_id or "")
            if task and task.status in (TaskStatus.IN_PROGRESS, TaskStatus.REVIEW):
                events.append(self._event(EventType.STATUS_CHANGE, task_id=task.id, payload={
                    "task_status": TaskStatus.FAILED, "reason": f"Guardrail: {violation.reason}"}))
        return events

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
            retries_left = self.settings.max_task_attempts - task.attempts
            follow_ups.append(self._tell_manager(
                f"Task {task.id} ({task.title}) failed. Reason: {task.result}\n"
                + (f"You can retry it with retry_task ({retries_left} attempt(s) left; you can pick another assignee "
                   "and add a note), or call finish_run if the goal can't be reached."
                   if retries_left > 0 else "It has no attempts left. Call finish_run with an honest report.")
            ))
        elif self.board.all_done() and not self._told_manager_all_done:
            self._told_manager_all_done = True
            results = "\n".join(f"- {t.id} {t.title} ({t.assignee}): {t.result}" for t in self.board.tasks.values())
            follow_ups.append(self._tell_manager(
                f"All tasks are done:\n{results}\n\nAsk the human to approve the changes with request_approval."
            ))
        return follow_ups

    # --- Helpers --------------------------------------------------------------

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

    def _tell_manager(self, content: str) -> Event:
        # No task_id: the Manager handles these in its main conversation, next to its plan.
        return Event(run_id=self.run_id, type=EventType.MESSAGE, sender="system", recipient=MANAGER,
                     payload={"content": content})

    def _event(self, type_: EventType, sender: str = "system", **fields: Any) -> Event:
        return Event(run_id=self.run_id, type=type_, sender=sender, **fields)

    async def _end(self, report: str, failed: bool = False, stopped: bool = False) -> None:
        await self.publish(self._event(EventType.RUN_COMPLETE, payload={"report": report, "failed": failed, "stopped": stopped}))

    async def _prepare(self) -> None:
        """Clone the repo, then start the sandbox and make sure RepoLens has the repo indexed.

        The last two are independent, so they run in parallel: indexing can take minutes.
        """
        if not self.repo:
            return
        if self.workspace:
            await self.workspace.clone(self.repo)
        steps = []
        if self.sandbox and self.workspace:
            steps.append(self.sandbox.start(self.workspace))
        if self.repolens:
            steps.append(self.repolens.ensure_indexed(self.repo, self.report_indexing))
        await asyncio.gather(*steps)

    async def _teardown(self) -> None:
        # The workspace is kept: it holds the changes (and the branch, if a pull request was opened).
        if self.sandbox:
            await self.sandbox.stop()

    async def _set_run_state(self, state: str, **details: Any) -> None:
        await self.publish(self._event(EventType.STATUS_CHANGE, payload={"run_state": state, **details}))
