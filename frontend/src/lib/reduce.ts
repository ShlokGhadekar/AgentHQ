// Everything the UI shows is derived from the run's events by this one pure function, the same
// way the backend's TaskBoard.apply works. Live view and replay are the same code: replay just
// passes a shorter prefix of the event list.

import type { AgentEvent, AgentState, RunState, TaskStatus } from "./types";

export interface TaskView {
  id: string;
  title: string;
  description: string;
  assignee: string;
  dependsOn: string[];
  status: TaskStatus;
  result: string | null;
  attempts: number;
}

export interface AgentView {
  name: string;
  state: AgentState;
  tokensIn: number;
  tokensOut: number;
  calls: number;
  model: string | null;
  // Estimated at list price; calls without a known price (the scripted demo) add nothing.
  costUsd: number;
  pricedCalls: number;
}

export interface Approval {
  summary: string;
  diff: string;
  tests: { command: string; passed: boolean; exit_code: number; output: string } | null;
  decision: { approved: boolean; comment: string } | null;
}

export interface Indexing {
  active: boolean;
  stage: string | null;
  done: number;
  total: number;
  source: string | null;
}

export interface RunView {
  runState: RunState;
  tasks: TaskView[];
  agents: Record<string, AgentView>;
  approval: Approval | null;
  prUrl: string | null;
  indexing: Indexing | null;
  report: { text: string; failed: boolean; stopped: boolean } | null;
  startedAt: string | null;
  lastAt: string | null;
}

export const emptyRun = (): RunView => ({
  runState: "preparing",
  tasks: [],
  agents: {},
  approval: null,
  prUrl: null,
  indexing: null,
  report: null,
  startedAt: null,
  lastAt: null,
});

export function reduceEvents(events: AgentEvent[]): RunView {
  const view = emptyRun();
  const tasks = new Map<string, TaskView>();
  for (const event of events) {
    apply(view, tasks, event);
  }
  view.tasks = [...tasks.values()];
  return view;
}

function agent(view: RunView, name: string): AgentView {
  view.agents[name] ??= { name, state: "idle", tokensIn: 0, tokensOut: 0, calls: 0, model: null, costUsd: 0, pricedCalls: 0 };
  return view.agents[name];
}

function apply(view: RunView, tasks: Map<string, TaskView>, event: AgentEvent): void {
  const p = event.payload;
  view.startedAt ??= event.timestamp;
  view.lastAt = event.timestamp;
  switch (event.type) {
    case "task_created":
      tasks.set(event.task_id!, {
        id: event.task_id!,
        title: p.title,
        description: p.description ?? "",
        assignee: p.assignee,
        dependsOn: p.depends_on ?? [],
        status: "todo",
        result: null,
        attempts: 0,
      });
      break;
    case "task_assigned": {
      const task = tasks.get(event.task_id!);
      if (task) {
        task.status = "in_progress";
        task.assignee = event.recipient ?? task.assignee;
        task.attempts += 1;
      }
      break;
    }
    case "status_change":
      if (p.agent_state && event.sender) {
        agent(view, event.sender).state = p.agent_state;
      } else if (p.run_state) {
        view.runState = p.run_state;
      } else if (p.task_status) {
        const task = tasks.get(event.task_id!);
        if (task) {
          task.status = p.task_status;
          task.result = p.summary ?? p.reason ?? task.result;
          task.assignee = p.assignee ?? task.assignee;
        }
      }
      break;
    case "llm_usage": {
      const a = agent(view, event.sender!);
      a.tokensIn += event.tokens_in;
      a.tokensOut += event.tokens_out;
      a.calls += 1;
      a.model = p.model ?? a.model;
      if (typeof p.cost_usd === "number") {
        a.costUsd += p.cost_usd;
        a.pricedCalls += 1;
      }
      break;
    }
    case "indexing_progress":
      view.indexing = { active: p.active, stage: p.stage, done: p.done ?? 0, total: p.total ?? 0, source: p.source };
      break;
    case "approval_requested":
      view.approval = { summary: p.summary, diff: p.diff ?? "", tests: p.tests ?? null, decision: null };
      view.runState = "awaiting_approval";
      break;
    case "approval_decision":
      if (view.approval) view.approval.decision = { approved: p.approved, comment: p.comment ?? "" };
      if (view.runState === "awaiting_approval") view.runState = "running";
      break;
    case "message":
      if (p.pr_url) view.prUrl = p.pr_url;
      break;
    case "run_complete":
      view.runState = "complete";
      view.report = { text: p.report ?? "", failed: Boolean(p.failed), stopped: Boolean(p.stopped) };
      break;
  }
}

// The backend publishes "waiting" when an agent's turn ends with an open task, but doesn't revisit
// it when someone else closes that task (e.g. the reviewer approves). Read it against the board.
export function displayState(view: RunView, name: string): AgentState {
  const state = view.agents[name]?.state ?? "idle";
  if (view.runState === "complete") return state === "error" ? "error" : "idle";
  if (state !== "waiting") return state;
  const hasOpenTask = view.tasks.some((t) => t.assignee === name && (t.status === "in_progress" || t.status === "review"));
  return hasOpenTask ? "waiting" : "idle";
}

export function currentTask(view: RunView, name: string): TaskView | undefined {
  return view.tasks.find((t) => t.assignee === name && (t.status === "in_progress" || t.status === "review"));
}

// Which link in the org graph an event travels along, so the graph can animate it.
const FILE_AND_SANDBOX_TOOLS = new Set(["read_file", "list_files", "search_code", "write_file", "edit_file", "run_tests", "run_linter", "view_diff"]);

export function linkOf(event: AgentEvent): [string, string] | null {
  const { type, sender, recipient, payload } = event;
  if (type === "message" || type === "task_assigned") {
    return sender && recipient && sender !== "system" ? [sender, recipient] : null;
  }
  if (type === "approval_requested") return ["manager", "human"];
  if (type === "approval_decision") return ["human", "manager"];
  if (type === "tool_call" && sender) {
    if (payload.tool === "ask_codebase") return [sender, "repolens"];
    if (FILE_AND_SANDBOX_TOOLS.has(payload.tool)) return [sender, "sandbox"];
  }
  return null;
}
