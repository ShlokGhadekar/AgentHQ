"""An agent: an async worker loop around an LLM with a role prompt and a set of tools.

Loop: read inbox -> add the event to the conversation it belongs to -> let the LLM act with
tools until its turn ends -> wait for the next event. Everything it does is published as events.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, Field, ValidationError

from agenthq.events import Event, EventType
from agenthq.llm import LLMClient, LLMResponse, Message, ToolCall
from agenthq.tasks import BoardError, TaskStatus
from agenthq.tools import TOOLS, ToolContext, ToolError

if TYPE_CHECKING:
    from agenthq.runtime import Run

AGENTS_FILE = Path(__file__).with_name("agents.yaml")

NUDGE = (
    "You ended your turn, but task {task_id} is still in progress with you. Finish it with submit_task, "
    "explain a blocker with fail_task, or ask a teammate with send_message."
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
                if self._run.finished.is_set():
                    return
                await self.handle(event)

    async def handle(self, event: Event) -> None:
        """React to one inbox event with one LLM turn."""
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
        """Call the LLM and run its tool calls until it ends the turn."""
        nudged = False
        waiting_for_reply = False
        for _ in range(self._run.settings.max_llm_calls_per_turn):
            response = await self._complete(thread)
            thread.append(response.as_message())
            if not response.tool_calls:
                if waiting_for_reply or not self._is_stalled(task_id):
                    return
                if nudged:
                    raise RuntimeError("stopped twice without finishing the task")
                # LLMs sometimes just describe what they'd do; one reminder usually gets them to act.
                thread.append(Message(role="user", content=NUDGE.format(task_id=task_id)))
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
        raise RuntimeError(f"used {self._run.settings.max_llm_calls_per_turn} LLM calls in one turn")

    async def _complete(self, thread: list[Message]) -> LLMResponse:
        response = await self._llm.complete(
            self._system_prompt(), thread, [tool.spec() for tool in self._tools.values()], model=self.config.model
        )
        await self._publish(EventType.LLM_USAGE, payload={"model": response.model, "text": response.text},
                            tokens_in=response.tokens_in, tokens_out=response.tokens_out)
        return response

    async def _call_tool(self, call: ToolCall, task_id: str | None) -> tuple[str, bool]:
        """Run one tool call. Returns (result text for the LLM, whether it succeeded)."""
        await self._publish(EventType.TOOL_CALL, task_id=task_id,
                            payload={"tool": call.name, "arguments": call.arguments, "call_id": call.id})
        tool = self._tools.get(call.name)
        ok = False
        if tool is None:
            result = f"Error: you have no tool named {call.name!r}"
        else:
            try:
                result = await tool.handler(ToolContext(self._run, self.name, task_id), tool.args_model.model_validate(call.arguments))
                ok = True
            except ValidationError as error:
                result = f"Error: invalid arguments: {error}"
            except (ToolError, BoardError) as error:
                result = f"Error: {error}"
        await self._publish(EventType.TOOL_RESULT, task_id=task_id,
                            payload={"tool": call.name, "call_id": call.id, "ok": ok, "result": result})
        return result, ok

    def _is_stalled(self, task_id: str | None) -> bool:
        """True if this conversation's task is ours and still in progress, so the turn ended too early."""
        task = self._run.board.tasks.get(task_id) if task_id else None
        return task is not None and task.assignee == self.name and task.status == TaskStatus.IN_PROGRESS

    def _has_open_task(self) -> bool:
        return any(t.assignee == self.name and t.status in (TaskStatus.IN_PROGRESS, TaskStatus.REVIEW)
                   for t in self._run.board.tasks.values())

    async def _report_failure(self, task_id: str | None, error: Exception) -> None:
        """Publish the error and, if our own task was in progress, fail it so the Manager hears about it."""
        await self._publish(EventType.ERROR, task_id=task_id, payload={"message": f"{type(error).__name__}: {error}"})
        await self._set_state("error")
        if self._is_stalled(task_id):
            await self._publish(EventType.STATUS_CHANGE, task_id=task_id,
                                payload={"task_status": TaskStatus.FAILED, "reason": f"{self.name} hit an error: {error}"})

    def _describe(self, event: Event) -> str:
        """Turn an inbox event into the user message the LLM sees."""
        if event.type == EventType.TASK_ASSIGNED:
            return self._run.describe_task(event.task_id)
        sender = "the human user" if event.sender == "human" else event.sender
        return f"Message from {sender}:\n{event.payload.get('content', '')}"

    def _system_prompt(self) -> str:
        team = "\n".join(
            f"- {agent.name}: {agent.config.description} (tools: {', '.join(agent.config.tools)})"
            for agent in self._run.agents.values()
        )
        return (
            f"{self.config.prompt.strip()}\n\n"
            f"## Your team\n{team}\n\n"
            f"## This run\nGoal: {self._run.goal}\nRepository: {self._run.repo or 'not set'}\n\n"
            f"You are {self.name}. Act only through your tools. Plain text you write is logged but not "
            "delivered to anyone, so use send_message to talk to a teammate. Keep messages short and specific."
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
