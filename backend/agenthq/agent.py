"""An agent: an async worker loop around an LLM with a role prompt and a set of tools.

Loop: read inbox -> add the event to the conversation it belongs to -> let the LLM act with
tools until its turn ends -> wait for the next event. Everything it does is published as events.
"""

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, Field, ValidationError

from agenthq.events import Event, EventType
from agenthq.llm import LLMClient, LLMError, LLMResponse, Message, ToolCall
from agenthq.pricing import estimate_cost
from agenthq.sandbox import SandboxError
from agenthq.tasks import BoardError, TaskStatus
from agenthq.tools import TOOLS, ToolContext, ToolError, ToolOutput
from agenthq.workspace import WorkspaceError

if TYPE_CHECKING:
    from agenthq.runtime import Run

AGENTS_FILE = Path(__file__).with_name("agents.yaml")

ELIDED = "[Output removed to keep the conversation short. Call the tool again if you need it.]"
# Tool results always kept in full: the agent is usually still working with its latest outputs.
KEEP_RECENT_TOOL_RESULTS = 2

NUDGE = (
    "You ended your turn, but task {task_id} is still in progress with you. Finish it with submit_task, "
    "explain a blocker with fail_task, or ask a teammate with send_message."
)
LEAD_NUDGE = (
    "You ended your turn without acting on the goal. Use your tools to make progress, "
    "or call finish_run with a report if it is done or can't be done."
)


class AgentConfig(BaseModel):
    """One entry of agents.yaml. name, description and tools together form the capability card."""

    name: str
    description: str
    prompt: str
    tools: list[str]
    model: str | None = None  # None: the provider's default model
    review_by: str | None = Field(default=None, description="Agent that must approve this agent's tasks")

    def card(self) -> dict[str, object]:
        """The capability card the Manager reads when assigning work."""
        return {"name": self.name, "description": self.description, "tools": self.tools}


def load_agent_configs(path: Path = AGENTS_FILE) -> list[AgentConfig]:
    """Read agents.yaml and check that it only names tools and reviewers that exist."""
    configs = [AgentConfig(name=name, **fields) for name, fields in yaml.safe_load(path.read_text()).items()]
    names = {config.name for config in configs}
    for config in configs:
        unknown = set(config.tools) - TOOLS.keys()
        if unknown:
            raise ValueError(f"{config.name} uses unknown tools: {', '.join(sorted(unknown))}")
        if config.review_by and config.review_by not in names:
            raise ValueError(f"{config.name} is reviewed by unknown agent {config.review_by}")
    return configs


class Agent:
    """One team member. Conversations are kept per task, so each task has its own focused context."""

    def __init__(self, config: AgentConfig, llm: LLMClient, run: "Run") -> None:
        self.config = config
        self.name = config.name
        self._llm = llm
        self._run = run
        self._tools = {name: TOOLS[name] for name in config.tools}
        # task id -> conversation. None is the conversation for messages that aren't about a task.
        self._threads: dict[str | None, list[Message]] = {}

    async def run_loop(self) -> None:
        """Process inbox events one at a time until the run ends."""
        await self._set_state("idle", card=self.config.card())
        while not self._run.finished.is_set():
            for event in await self._run.bus.read_inbox(self.name, block_ms=self._run.settings.inbox_block_ms):
                await self._run.running.wait()  # paused: hold new work until resumed
                if self._run.finished.is_set():
                    return
                await self.handle(event)

    async def handle(self, event: Event) -> None:
        """React to one inbox event with one LLM turn."""
        if event.type == EventType.TASK_ASSIGNED:
            # A (re)assignment starts a clean conversation. A retried task must not inherit the
            # context that made it fail; the Manager's note and dependency results are in the assignment.
            self._threads[event.task_id] = []
        thread = self._threads.setdefault(event.task_id, [])
        thread.append(Message(role="user", content=self._describe(event)))
        await self._set_state("thinking")
        try:
            await self._take_turn(event.task_id, thread)
        except Exception as error:  # an agent must never crash the run; report and hand the task back
            await self._report_failure(event.task_id, error)
            return
        await self._set_state("waiting" if self._has_open_task() else "idle")

    async def _take_turn(self, task_id: str | None, thread: list[Message]) -> None:
        """Call the LLM and run its tool calls until it ends the turn.

        There is no per-turn cap here: the guardrails count LLM calls per task and per run, and
        fail the task (or stop the run) when a limit is crossed, which ends this loop.
        """
        nudged = False
        waiting_for_reply = False
        while True:
            await self._run.running.wait()
            if self._turn_cancelled(task_id):
                return
            response = await self._complete(thread, task_id)
            thread.append(response.as_message())
            if not response.tool_calls:
                if waiting_for_reply or not self._is_stalled(task_id):
                    return
                if nudged:
                    raise RuntimeError("stopped twice without finishing the task")
                # LLMs sometimes just describe what they'd do; one reminder usually gets them to act.
                reminder = NUDGE.format(task_id=task_id) if task_id else LEAD_NUDGE
                thread.append(Message(role="user", content=reminder))
                nudged = True
                continue
            turn_over = False
            for call in response.tool_calls:
                # Every call gets a result, even after a turn-ending one: providers reject a
                # conversation where a tool call has no matching result.
                result, ok = await self._call_tool(call, task_id)
                thread.append(Message(role="tool", tool_call_id=call.id, content=result))
                turn_over |= ok and self._tools[call.name].ends_turn
                waiting_for_reply |= ok and call.name == "send_message"
            if turn_over:
                return

    def _turn_cancelled(self, task_id: str | None) -> bool:
        """True once the run is over, or our task was failed under us (e.g. by a guardrail)."""
        if self._run.finished.is_set():
            return True
        task = self._run.board.tasks.get(task_id) if task_id else None
        return task is not None and task.assignee == self.name and task.status == TaskStatus.FAILED

    async def _complete(self, thread: list[Message], task_id: str | None) -> LLMResponse:
        """One LLM call, retrying temporary failures with exponential backoff."""
        settings = self._run.settings
        system, tools = self._system_prompt(), [tool.spec() for tool in self._tools.values()]
        fixed_chars = len(system) + len(json.dumps([t.model_dump() for t in tools]))
        budget = settings.llm_context_budget_tokens
        self._fit_context(thread, fixed_chars, budget)
        for attempt in range(settings.llm_max_retries + 1):
            try:
                response = await self._llm.complete(system, thread, tools, model=self.config.model)
                break
            except LLMError as error:
                if not error.retryable or attempt == settings.llm_max_retries:
                    raise
                if error.too_large:
                    # Providers count tokens in ways we can only estimate, so when one says the request
                    # is too big, shrink harder and try again at once. Give up if nothing is left to cut.
                    budget = int(budget * 0.6)
                    if not self._fit_context(thread, fixed_chars, budget, keep_recent=1):
                        raise
                    await self._publish(EventType.ERROR, task_id=task_id, payload={
                        "message": f"{error} Shrinking the conversation to ~{budget} tokens.", "retrying_in_s": 0,
                        "attempt": attempt + 1,
                    })
                    continue
                # A provider asking for a longer wait than we accept means a quota is used up (e.g. a
                # daily token limit), not a momentary spike, so retrying only burns time.
                if (error.retry_after_s or 0) > settings.llm_retry_max_wait_s:
                    raise
                # Exponential backoff (2s, 4s, 8s, ...) unless the provider told us how long to wait.
                delay = min(error.retry_after_s or settings.llm_retry_base_s * 2 ** attempt, settings.llm_retry_max_wait_s)
                await self._publish(EventType.ERROR, task_id=task_id, payload={
                    "message": str(error), "retrying_in_s": round(delay, 1), "attempt": attempt + 1,
                })
                await asyncio.sleep(delay)
        # task_id lets the guardrails count steps per task, and the UI show what each call was for.
        # The cost is worked out once, here, so stats and evals just add up what the log says.
        cost = estimate_cost(response.model, response.tokens_in, response.tokens_out)
        await self._publish(EventType.LLM_USAGE, task_id=task_id,
                            payload={"model": response.model, "text": response.text, "cost_usd": cost},
                            tokens_in=response.tokens_in, tokens_out=response.tokens_out)
        return response

    @staticmethod
    def _fit_context(thread: list[Message], fixed_chars: int, budget_tokens: int,
                     keep_recent: int = KEEP_RECENT_TOOL_RESULTS) -> bool:
        """Shrink the conversation to the token budget by removing old tool outputs, oldest first.

        Tool outputs (file contents, test logs) are most of the context and the easiest to get
        back, so they go first; the agent's own reasoning and every message it received stay.
        The full outputs are still in the event log. Tokens are estimated at 3 characters each,
        a cautious figure for code. Returns whether anything was removed.
        """
        budget_chars = budget_tokens * 3
        size = fixed_chars + sum(len(m.content) + len(json.dumps([c.arguments for c in m.tool_calls])) for m in thread)
        tool_results = [m for m in thread if m.role == "tool" and m.content != ELIDED]
        removed = False
        for message in tool_results[:len(tool_results) - keep_recent]:
            if size <= budget_chars:
                break
            size -= len(message.content) - len(ELIDED)
            message.content = ELIDED
            removed = True
        return removed

    async def _call_tool(self, call: ToolCall, task_id: str | None) -> tuple[str, bool]:
        """Run one tool call. Returns (result text for the LLM, whether it succeeded)."""
        await self._publish(EventType.TOOL_CALL, task_id=task_id,
                            payload={"tool": call.name, "arguments": call.arguments, "call_id": call.id})
        tool = self._tools.get(call.name)
        output = ToolOutput(f"Error: you have no tool named {call.name!r}")
        ok = False
        if tool is not None:
            try:
                result = await tool.handler(ToolContext(self._run, self.name, task_id), tool.args_model.model_validate(call.arguments))
                output = result if isinstance(result, ToolOutput) else ToolOutput(result)
                ok = True
            except ValidationError as error:
                output = ToolOutput(f"Error: invalid arguments: {error}")
            # Mistakes the model can fix go back to it as the tool result. Anything else (e.g. a
            # RepoLens configuration error) propagates and fails the task, so the Manager hears about it.
            except (ToolError, BoardError, WorkspaceError, SandboxError) as error:
                output = ToolOutput(f"Error: {error}")
        await self._publish(EventType.TOOL_RESULT, task_id=task_id, payload={
            "tool": call.name, "call_id": call.id, "ok": ok, "result": output.text, "details": output.details,
        })
        return output.text, ok

    def _is_stalled(self, task_id: str | None) -> bool:
        """True if the turn ended too early: our task is still in progress, or we lead the run and
        nothing is in flight (no open task, no pending sign-off), so nobody else will move it forward."""
        if task_id is None:
            in_flight = any(t.status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW)
                            for t in self._run.board.tasks.values())
            return self.name == self._run.lead and not in_flight and not self._run.approval_pending
        task = self._run.board.tasks.get(task_id)
        return task is not None and task.assignee == self.name and task.status == TaskStatus.IN_PROGRESS

    def _has_open_task(self) -> bool:
        return any(t.assignee == self.name and t.status in (TaskStatus.IN_PROGRESS, TaskStatus.REVIEW)
                   for t in self._run.board.tasks.values())

    async def _report_failure(self, task_id: str | None, error: Exception) -> None:
        """Publish the error, then let the run decide what it means (see Run.handle_agent_error)."""
        await self._publish(EventType.ERROR, task_id=task_id, payload={"message": f"{type(error).__name__}: {error}"})
        await self._set_state("error")
        await self._run.handle_agent_error(self.name, task_id, error)

    def _describe(self, event: Event) -> str:
        """Turn an inbox event into the user message the LLM sees."""
        if event.type == EventType.TASK_ASSIGNED:
            return self._run.describe_task(event.task_id)
        sender = "the human user" if event.sender == "human" else event.sender
        return f"Message from {sender}:\n{event.payload.get('content', '')}"

    def _system_prompt(self) -> str:
        # Teammates are described in words only. Listing their tool names here made models try to
        # call those tools themselves, which providers reject.
        team = "\n".join(f"- {agent.name}: {agent.config.description}" for agent in self._run.agents.values() if agent is not self)
        return (
            f"{self.config.prompt.strip()}\n\n"
            f"## Your teammates\n{team}\n\n"
            f"## Your tools\nYou can only call: {', '.join(self.config.tools)}.\n\n"
            f"## This run\nGoal: {self._run.goal}\nRepository: {self._run.repo or 'not set'}\n\n"
            f"You are {self.name}. Act only through your tools. Plain text you write is logged but not "
            "delivered to anyone, so use send_message to talk to a teammate, or to 'human' to answer the "
            "human user. Keep messages short and specific."
        )

    async def _set_state(self, state: str, **extra: object) -> None:
        """Publish this agent's state (idle, thinking, waiting, error) for the UI."""
        await self._publish(EventType.STATUS_CHANGE, payload={"agent_state": state, **extra})

    async def _publish(self, type_: EventType, task_id: str | None = None, payload: dict | None = None,
                       tokens_in: int = 0, tokens_out: int = 0) -> None:
        await self._run.publish(Event(
            run_id=self._run.run_id, type=type_, sender=self.name, task_id=task_id,
            payload=payload or {}, tokens_in=tokens_in, tokens_out=tokens_out,
        ))
