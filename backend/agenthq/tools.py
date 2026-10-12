"""Tools agents use to coordinate: messaging, planning and moving tasks through the board.

Each tool is a Pydantic model for its arguments plus an async handler. The model gives us the
JSON Schema the LLM sees and validates what the LLM sends back, from one definition.
Which agent may use which tool is configured in agents.yaml.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from agenthq.events import Event, EventType
from agenthq.llm import ToolSpec
from agenthq.tasks import Task, TaskStatus

if TYPE_CHECKING:
    from agenthq.runtime import Run

MAX_TASKS_PER_RUN = 6


class ToolError(Exception):
    """The tool was used wrongly. The message goes back to the model so it can correct itself."""


@dataclass
class ToolContext:
    """Who is calling the tool, and on which task's conversation."""

    run: "Run"
    agent: str
    task_id: str | None


@dataclass(frozen=True)
class Tool:
    """A tool an agent can call.

    `ends_turn` tools are the agent's last action for now (submitting a task, finishing the run).
    The runtime stops the turn right after them instead of asking the LLM what to do next,
    which saves one LLM call per task.
    """

    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], Awaitable[str]]
    ends_turn: bool = False

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=_inline_refs(self.args_model.model_json_schema()))


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace Pydantic's "$ref"s to "$defs" with the definitions themselves.

    Nested models produce refs, and not every provider's function calling resolves them.
    """
    definitions = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(definitions[node["$ref"].split("/")[-1]])
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


# --- Messaging ----------------------------------------------------------------

class SendMessageArgs(BaseModel):
    to: str = Field(description="Name of the teammate")
    content: str


async def send_message(ctx: ToolContext, args: SendMessageArgs) -> str:
    if args.to == ctx.agent or args.to not in ctx.run.agents:
        raise ToolError(f"Unknown teammate {args.to!r}. Teammates: {', '.join(n for n in ctx.run.agents if n != ctx.agent)}")
    await ctx.run.publish(Event(
        run_id=ctx.run.run_id, type=EventType.MESSAGE, sender=ctx.agent, recipient=args.to,
        task_id=ctx.task_id, payload={"content": args.content},
    ))
    return f"Message sent to {args.to}. Their reply will arrive as a new message; you can end your turn now."


# --- Manager: planning --------------------------------------------------------

class NewTask(BaseModel):
    id: str = Field(description="Short unique id such as T1")
    title: str
    description: str = Field(description="What to do and what 'done' means")
    assignee: str = Field(description="Name of the teammate who does the task")
    depends_on: list[str] = Field(default_factory=list, description="Ids of tasks that must be done first")


class CreateTasksArgs(BaseModel):
    tasks: list[NewTask]


async def create_tasks(ctx: ToolContext, args: CreateTasksArgs) -> str:
    _check_plan(ctx, args.tasks)
    for task in args.tasks:
        await ctx.run.publish(Event(
            run_id=ctx.run.run_id, type=EventType.TASK_CREATED, sender=ctx.agent, task_id=task.id,
            payload=task.model_dump(exclude={"id"}),
        ))
    return (
        f"Created {', '.join(t.id for t in args.tasks)}. Tasks are handed out automatically when their "
        "dependencies are done. You will get a message when all tasks are done or one fails."
    )


def _check_plan(ctx: ToolContext, tasks: list[NewTask]) -> None:
    """Reject the whole plan before creating anything, so a bad plan never half-exists."""
    board = ctx.run.board
    if not tasks:
        raise ToolError("Provide at least one task")
    if len(board.tasks) + len(tasks) > MAX_TASKS_PER_RUN:
        raise ToolError(f"A run can have at most {MAX_TASKS_PER_RUN} tasks")
    known = set(board.tasks)
    for task in tasks:
        if task.id in known:
            raise ToolError(f"Task id {task.id} is used twice")
        if task.assignee not in ctx.run.workers:
            raise ToolError(f"{task.id}: unknown assignee {task.assignee!r}. Choose from {', '.join(ctx.run.workers)}")
        unknown = [dep for dep in task.depends_on if dep not in known]
        if unknown:
            raise ToolError(f"{task.id} depends on {', '.join(unknown)}, which must be listed before it")
        known.add(task.id)


class RetryTaskArgs(BaseModel):
    task_id: str
    note: str = Field(description="What to do differently this time")
    assignee: str | None = Field(default=None, description="Give it to another teammate; omit to keep the same one")


async def retry_task(ctx: ToolContext, args: RetryTaskArgs) -> str:
    task = _task(ctx, args.task_id)
    if task.status != TaskStatus.FAILED:
        raise ToolError(f"Only failed tasks can be retried; {task.id} is {task.status}")
    if args.assignee and args.assignee not in ctx.run.workers:
        raise ToolError(f"Unknown assignee {args.assignee!r}. Choose from {', '.join(ctx.run.workers)}")
    await _change_status(ctx, task, TaskStatus.TODO, note=args.note, assignee=args.assignee or task.assignee)
    return f"{task.id} is queued again for {args.assignee or task.assignee}."


class FinishRunArgs(BaseModel):
    report: str = Field(description="Final report: what changed, which files, how it was verified, open issues")


async def finish_run(ctx: ToolContext, args: FinishRunArgs) -> str:
    await ctx.run.publish(Event(
        run_id=ctx.run.run_id, type=EventType.RUN_COMPLETE, sender=ctx.agent, payload={"report": args.report},
    ))
    return "Run finished."


# --- Workers: finishing tasks -------------------------------------------------

class SubmitTaskArgs(BaseModel):
    summary: str = Field(description="What you found or changed, with file paths")


async def submit_task(ctx: ToolContext, args: SubmitTaskArgs) -> str:
    task = _own_task(ctx)
    reviewer = ctx.run.agents[ctx.agent].config.review_by
    if not reviewer:
        await _change_status(ctx, task, TaskStatus.DONE, summary=args.summary)
        return f"{task.id} is done."
    await _change_status(ctx, task, TaskStatus.REVIEW, summary=args.summary)
    await ctx.run.publish(Event(
        run_id=ctx.run.run_id, type=EventType.MESSAGE, sender="system", recipient=reviewer, task_id=task.id,
        payload={"content": (
            f"Please review task {task.id} ({task.title}), done by {ctx.agent}.\n"
            f"Task description: {task.description}\n\nTheir summary:\n{args.summary}\n\n"
            "Approve with approve_task, or send it back with request_changes."
        )},
    ))
    return f"{task.id} was sent to {reviewer} for review. Their feedback will arrive as a message."


class FailTaskArgs(BaseModel):
    reason: str = Field(description="Why the task can't be completed")


async def fail_task(ctx: ToolContext, args: FailTaskArgs) -> str:
    task = _own_task(ctx)
    await _change_status(ctx, task, TaskStatus.FAILED, reason=args.reason)
    return f"{task.id} is marked as failed. The manager will decide what happens next."


# --- Reviewer -----------------------------------------------------------------

class ApproveTaskArgs(BaseModel):
    comment: str = Field(default="", description="Optional note for the record")


async def approve_task(ctx: ToolContext, args: ApproveTaskArgs) -> str:
    task = _task_under_review(ctx)
    await _change_status(ctx, task, TaskStatus.DONE, comment=args.comment)
    return f"{task.id} is approved and done."


class RequestChangesArgs(BaseModel):
    comments: str = Field(description="Specific changes needed, one per line")


async def request_changes(ctx: ToolContext, args: RequestChangesArgs) -> str:
    task = _task_under_review(ctx)
    await _change_status(ctx, task, TaskStatus.IN_PROGRESS, comments=args.comments)
    await ctx.run.publish(Event(
        run_id=ctx.run.run_id, type=EventType.MESSAGE, sender=ctx.agent, recipient=task.assignee, task_id=task.id,
        payload={"content": f"Changes requested on {task.id}:\n{args.comments}"},
    ))
    return f"{task.id} was sent back to {task.assignee}."


# --- Helpers ------------------------------------------------------------------

def _task(ctx: ToolContext, task_id: str | None) -> Task:
    if task_id not in ctx.run.board.tasks:
        raise ToolError(f"Unknown task {task_id}")
    return ctx.run.board.tasks[task_id]


def _own_task(ctx: ToolContext) -> Task:
    """The task this conversation is about, which must be assigned to the caller and in progress."""
    if ctx.task_id is None:
        raise ToolError("This conversation isn't about a task")
    task = _task(ctx, ctx.task_id)
    if task.assignee != ctx.agent or task.status != TaskStatus.IN_PROGRESS:
        raise ToolError(f"{task.id} is not in progress with you (assignee {task.assignee}, status {task.status})")
    return task


def _task_under_review(ctx: ToolContext) -> Task:
    if ctx.task_id is None:
        raise ToolError("This conversation isn't about a task")
    task = _task(ctx, ctx.task_id)
    if task.status != TaskStatus.REVIEW or ctx.run.agents[task.assignee].config.review_by != ctx.agent:
        raise ToolError(f"{task.id} is not waiting for your review (status {task.status})")
    return task


async def _change_status(ctx: ToolContext, task: Task, status: TaskStatus, **details: str) -> None:
    await ctx.run.publish(Event(
        run_id=ctx.run.run_id, type=EventType.STATUS_CHANGE, sender=ctx.agent, task_id=task.id,
        payload={"task_status": status, **details},
    ))


TOOLS: dict[str, Tool] = {tool.name: tool for tool in [
    Tool("send_message", "Send a message to one teammate. Plain text replies are not delivered to anyone; use this to talk.",
         SendMessageArgs, send_message),
    Tool("create_tasks", "Create the plan: tasks with assignees and dependencies.", CreateTasksArgs, create_tasks, ends_turn=True),
    Tool("retry_task", "Put a failed task back in To Do, optionally with another assignee.", RetryTaskArgs, retry_task, ends_turn=True),
    Tool("finish_run", "End the run with a final report.", FinishRunArgs, finish_run, ends_turn=True),
    Tool("submit_task", "Finish your current task and hand in the result.", SubmitTaskArgs, submit_task, ends_turn=True),
    Tool("fail_task", "Give up on your current task and explain why.", FailTaskArgs, fail_task, ends_turn=True),
    Tool("approve_task", "Approve the task under review.", ApproveTaskArgs, approve_task, ends_turn=True),
    Tool("request_changes", "Send the task under review back with comments.", RequestChangesArgs, request_changes, ends_turn=True),
]}
