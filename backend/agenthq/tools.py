"""Tools agents use: messaging, planning, moving tasks through the board, and working on the code.

Each tool is a Pydantic model for its arguments plus an async handler. The model gives us the
JSON Schema the LLM sees and validates what the LLM sends back, from one definition.
Which agent may use which tool is configured in agents.yaml.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from agenthq.events import Event, EventType
from agenthq.llm import ToolSpec
from agenthq.repolens import RepoLensRequestError, RepoLensUnavailable
from agenthq.tasks import Task, TaskStatus
from agenthq.workspace import Workspace

if TYPE_CHECKING:
    from agenthq.runtime import Run


class ApprovalError(Exception):
    """An approval request or decision came at the wrong time."""

MAX_TASKS_PER_RUN = 6
MAX_DIFF_CHARS = 15_000


class ToolArgs(BaseModel):
    """Base for tool arguments. Unknown fields are rejected, not ignored: a model that writes
    `line_start` instead of `start_line` must hear about it, or it silently gets the wrong result."""

    model_config = ConfigDict(extra="forbid")


class ToolError(Exception):
    """The tool was used wrongly. The message goes back to the model so it can correct itself."""


@dataclass
class ToolContext:
    """Who is calling the tool, and on which task's conversation."""

    run: "Run"
    agent: str
    task_id: str | None


@dataclass(frozen=True)
class ToolOutput:
    """A tool result with structured `details` for the event log (shown in the UI, not to the LLM)."""

    text: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Tool:
    """A tool an agent can call.

    `ends_turn` tools are the agent's last action for now (submitting a task, finishing the run).
    The runtime stops the turn right after them instead of asking the LLM what to do next,
    which saves one LLM call per task.
    """

    name: str
    description: str
    args_model: type[ToolArgs]
    handler: Callable[[ToolContext, Any], Awaitable[str | ToolOutput]]
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

class SendMessageArgs(ToolArgs):
    to: str = Field(description="Name of the teammate, or 'human' to answer the human user")
    content: str


async def send_message(ctx: ToolContext, args: SendMessageArgs) -> str:
    if args.to != "human" and (args.to == ctx.agent or args.to not in ctx.run.agents):
        teammates = ", ".join(n for n in ctx.run.agents if n != ctx.agent)
        raise ToolError(f"Unknown recipient {args.to!r}. Teammates: {teammates}, or 'human'")
    await ctx.run.publish(Event(
        run_id=ctx.run.run_id, type=EventType.MESSAGE, sender=ctx.agent, recipient=args.to,
        task_id=ctx.task_id, payload={"content": args.content},
    ))
    if args.to == "human":
        return "Message shown to the human."
    return f"Message sent to {args.to}. Their reply will arrive as a new message; you can end your turn now."


# --- Manager: planning --------------------------------------------------------

class NewTask(ToolArgs):
    id: str = Field(description="Short unique id such as T1")
    title: str
    description: str = Field(description="What to do and what 'done' means")
    assignee: str = Field(description="Name of the teammate who does the task")
    depends_on: list[str] = Field(default_factory=list, description="Ids of tasks that must be done first")


class CreateTasksArgs(ToolArgs):
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
        if task.assignee not in ctx.run.assignable:
            raise ToolError(f"{task.id}: {task.assignee!r} can't take tasks. Choose from {', '.join(ctx.run.assignable)}")
        unknown = [dep for dep in task.depends_on if dep not in known]
        if unknown:
            raise ToolError(f"{task.id} depends on {', '.join(unknown)}, which must be listed before it")
        known.add(task.id)


class RetryTaskArgs(ToolArgs):
    task_id: str
    note: str = Field(description="What to do differently this time")
    assignee: str | None = Field(default=None, description="Give it to another teammate; omit to keep the same one")


async def retry_task(ctx: ToolContext, args: RetryTaskArgs) -> str:
    task = _task(ctx, args.task_id)
    if task.status != TaskStatus.FAILED:
        raise ToolError(f"Only failed tasks can be retried; {task.id} is {task.status}")
    if task.attempts >= ctx.run.settings.max_task_attempts:
        raise ToolError(f"{task.id} already failed {task.attempts} times, the limit. Call finish_run with an honest report.")
    if args.assignee and args.assignee not in ctx.run.assignable:
        raise ToolError(f"{args.assignee!r} can't take tasks. Choose from {', '.join(ctx.run.assignable)}")
    await _change_status(ctx, task, TaskStatus.TODO, note=args.note, assignee=args.assignee or task.assignee)
    return f"{task.id} is queued again for {args.assignee or task.assignee}."


class FinishRunArgs(ToolArgs):
    report: str = Field(description="Final report: what changed, which files, how it was verified, open issues")


async def finish_run(ctx: ToolContext, args: FinishRunArgs) -> str:
    if ctx.run.approval_pending:
        raise ToolError("An approval request is waiting for the human. Wait for their decision before finishing.")
    await ctx.run.publish(Event(
        run_id=ctx.run.run_id, type=EventType.RUN_COMPLETE, sender=ctx.agent, payload={"report": args.report},
    ))
    return "Run finished."


class RequestApprovalArgs(ToolArgs):
    summary: str = Field(description="For the human: what changed, in which files, and how it was tested")


async def request_approval(ctx: ToolContext, args: RequestApprovalArgs) -> str:
    try:
        await ctx.run.request_approval(args.summary)
    except ApprovalError as error:
        raise ToolError(str(error)) from error
    return "Approval requested. The human's decision will arrive as a message; end your turn now."


# --- Workers: finishing tasks -------------------------------------------------

class SubmitTaskArgs(ToolArgs):
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


class FailTaskArgs(ToolArgs):
    reason: str = Field(description="Why the task can't be completed")


async def fail_task(ctx: ToolContext, args: FailTaskArgs) -> str:
    task = _own_task(ctx)
    await _change_status(ctx, task, TaskStatus.FAILED, reason=args.reason)
    return f"{task.id} is marked as failed. The manager will decide what happens next."


# --- Reviewer -----------------------------------------------------------------

class ApproveTaskArgs(ToolArgs):
    comment: str = Field(default="", description="Optional note for the record")


async def approve_task(ctx: ToolContext, args: ApproveTaskArgs) -> str:
    task = _task_under_review(ctx)
    await _change_status(ctx, task, TaskStatus.DONE, comment=args.comment)
    return f"{task.id} is approved and done."


class RequestChangesArgs(ToolArgs):
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


# --- Codebase: RepoLens, files, sandbox ----------------------------------------

class AskCodebaseArgs(ToolArgs):
    question: str = Field(description="A specific question, e.g. 'Where is the /login endpoint defined?'")


async def ask_codebase(ctx: ToolContext, args: AskCodebaseArgs) -> ToolOutput:
    if ctx.run.repolens is None or not ctx.run.repo:
        raise ToolError("RepoLens isn't available in this run. Use search_code and read_file.")
    misses = ctx.run.repolens_misses
    key = (ctx.agent, ctx.task_id)
    if misses[key] >= 2:
        raise ToolError("RepoLens already failed to answer twice here. Use search_code and read_file instead.")
    try:
        result = await ctx.run.repolens.ask(ctx.run.repo, args.question, on_progress=ctx.run.report_indexing)
    except RepoLensUnavailable as error:
        raise ToolError(f"RepoLens is unavailable ({error}). Use search_code and read_file instead.") from error
    except RepoLensRequestError as error:
        raise ToolError("Internal error while calling RepoLens (logged). Use search_code and read_file instead.") from error
    # RepoLensConfigError is not caught: the task fails and the Manager learns why.
    details = {
        "question": args.question, "found": result.found, "best_similarity": result.best_similarity,
        "citations": [{"path": c.path, "start_line": c.start_line, "end_line": c.end_line} for c in result.citations],
    }
    if not result.found:
        misses[key] += 1
        advice = ("Rephrase the question once, using names of functions, files or concepts that might appear in the code."
                  if misses[key] == 1 else "Use search_code and read_file instead.")
        return ToolOutput(f"RepoLens could not find this in the repository. {advice}", details)
    cited = "\n\n".join(f"{c.path}:{c.start_line}-{c.end_line}\n```\n{c.code}\n```" for c in result.citations)
    return ToolOutput(f"{result.answer}\n\nCitations:\n{cited}\n\nUse read_file to see the code around these lines.", details)


class ReadFileArgs(ToolArgs):
    path: str = Field(description="Path relative to the repository root")
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1, description="Inclusive; omit to read to the end")


async def read_file(ctx: ToolContext, args: ReadFileArgs) -> str:
    return _workspace(ctx).read(args.path, args.start_line, args.end_line)


class ListFilesArgs(ToolArgs):
    path: str = Field(default=".", description="Directory relative to the repository root")


async def list_files(ctx: ToolContext, args: ListFilesArgs) -> str:
    return "\n".join(_workspace(ctx).list_files(args.path)) or "(no files)"


class SearchCodeArgs(ToolArgs):
    pattern: str = Field(description="Python regular expression, matched against each line")
    path: str = Field(default=".", description="Directory to search, relative to the repository root")


async def search_code(ctx: ToolContext, args: SearchCodeArgs) -> str:
    return "\n".join(_workspace(ctx).search(args.pattern, args.path)) or "No matches."


class WriteFileArgs(ToolArgs):
    path: str = Field(description="Path relative to the repository root")
    content: str = Field(description="The complete new file content")


async def write_file(ctx: ToolContext, args: WriteFileArgs) -> str:
    return _workspace(ctx).write(args.path, args.content)


class EditFileArgs(ToolArgs):
    path: str = Field(description="Path relative to the repository root")
    old_text: str = Field(min_length=1, description="Exact text to replace, including indentation; must appear exactly once")
    new_text: str


async def edit_file(ctx: ToolContext, args: EditFileArgs) -> str:
    return _workspace(ctx).edit(args.path, args.old_text, args.new_text)


class NoArgs(ToolArgs):
    pass


async def run_tests(ctx: ToolContext, _args: NoArgs) -> ToolOutput:
    return await _run_in_sandbox(ctx, ctx.run.settings.test_command)


async def run_linter(ctx: ToolContext, _args: NoArgs) -> ToolOutput:
    return await _run_in_sandbox(ctx, ctx.run.settings.lint_command)


async def view_diff(ctx: ToolContext, _args: NoArgs) -> str:
    diff = await _workspace(ctx).diff()
    if not diff:
        return "No changes yet."
    return diff if len(diff) <= MAX_DIFF_CHARS else diff[:MAX_DIFF_CHARS] + "\n[... diff cut ...]"



async def _run_in_sandbox(ctx: ToolContext, command: str) -> ToolOutput:
    if ctx.run.sandbox is None:
        raise ToolError("No sandbox in this run")
    result = await ctx.run.sandbox.run(command, ctx.run.settings.command_timeout_s)
    return ToolOutput(result.summary(), {"command": command, "exit_code": result.exit_code, "passed": result.passed})


def _workspace(ctx: ToolContext) -> Workspace:
    if ctx.run.workspace is None:
        raise ToolError("This run has no repository checkout")
    return ctx.run.workspace


TOOLS: dict[str, Tool] = {tool.name: tool for tool in [
    Tool("send_message", "Send a message to one teammate. Plain text replies are not delivered to anyone; use this to talk.",
         SendMessageArgs, send_message),
    Tool("create_tasks", "Create the plan: tasks with assignees and dependencies.", CreateTasksArgs, create_tasks, ends_turn=True),
    Tool("retry_task", "Put a failed task back in To Do, optionally with another assignee.", RetryTaskArgs, retry_task, ends_turn=True),
    Tool("request_approval", "Ask the human to approve the finished changes; a pull request opens only if they do.",
         RequestApprovalArgs, request_approval, ends_turn=True),
    Tool("finish_run", "End the run with a final report.", FinishRunArgs, finish_run, ends_turn=True),
    Tool("submit_task", "Finish your current task and hand in the result.", SubmitTaskArgs, submit_task, ends_turn=True),
    Tool("fail_task", "Give up on your current task and explain why.", FailTaskArgs, fail_task, ends_turn=True),
    Tool("approve_task", "Approve the task under review.", ApproveTaskArgs, approve_task, ends_turn=True),
    Tool("request_changes", "Send the task under review back with comments.", RequestChangesArgs, request_changes, ends_turn=True),
    Tool("ask_codebase", "Ask RepoLens (RAG over the repository) a question; answers cite files and line ranges.",
         AskCodebaseArgs, ask_codebase),
    Tool("read_file", "Read a file with line numbers, optionally only a line range.", ReadFileArgs, read_file),
    Tool("list_files", "List the files in the repository or a folder of it.", ListFilesArgs, list_files),
    Tool("search_code", "Search all files for a regular expression (like grep).", SearchCodeArgs, search_code),
    Tool("write_file", "Create a file or replace its whole content.", WriteFileArgs, write_file),
    Tool("edit_file", "Replace one exact snippet in a file. Prefer this over write_file for existing files.",
         EditFileArgs, edit_file),
    Tool("run_tests", "Run the repository's test suite in the sandbox.", NoArgs, run_tests),
    Tool("run_linter", "Run the linter (ruff) in the sandbox.", NoArgs, run_linter),
    Tool("view_diff", "Show all changes made to the repository in this run.", NoArgs, view_diff),
]}
