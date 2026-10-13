import { describe, expect, it } from "vitest";
import { changedFiles } from "../components/ApprovalSheet";
import { currentTask, displayState, linkOf, reduceEvents } from "./reduce";
import type { AgentEvent, EventType } from "./types";

let counter = 0;
function event(type: EventType, fields: Partial<AgentEvent> = {}): AgentEvent {
  counter += 1;
  return {
    id: `e${counter}`,
    run_id: "run-1",
    timestamp: new Date(2026, 9, 13, 12, 0, counter).toISOString(),
    type,
    sender: null,
    recipient: null,
    task_id: null,
    payload: {},
    tokens_in: 0,
    tokens_out: 0,
    ...fields,
  };
}

const created = (id: string, assignee = "engineer", depends_on: string[] = []) =>
  event("task_created", { sender: "manager", task_id: id, payload: { title: `Task ${id}`, assignee, depends_on } });
const assigned = (id: string, to = "engineer") => event("task_assigned", { sender: "manager", recipient: to, task_id: id });
const status = (id: string, task_status: string, extra: Record<string, unknown> = {}) =>
  event("status_change", { task_id: id, payload: { task_status, ...extra } });
const agentState = (name: string, agent_state: string) => event("status_change", { sender: name, payload: { agent_state } });

describe("reduceEvents", () => {
  it("moves a task across the board like the backend's TaskBoard", () => {
    const view = reduceEvents([
      created("T1"),
      assigned("T1"),
      status("T1", "review", { summary: "v1" }),
      status("T1", "in_progress", { comments: "fix" }),
      status("T1", "review", { summary: "v2" }),
      status("T1", "done"),
    ]);
    expect(view.tasks).toHaveLength(1);
    expect(view.tasks[0]).toMatchObject({ status: "done", result: "v2", attempts: 1 });
  });

  it("counts attempts and follows a retry to a new assignee", () => {
    const view = reduceEvents([
      created("T1"),
      assigned("T1"),
      status("T1", "failed", { reason: "stuck" }),
      status("T1", "todo", { assignee: "researcher", note: "try again" }),
      assigned("T1", "researcher"),
    ]);
    expect(view.tasks[0]).toMatchObject({ status: "in_progress", assignee: "researcher", attempts: 2 });
  });

  it("sums tokens and calls per agent", () => {
    const view = reduceEvents([
      event("llm_usage", { sender: "engineer", tokens_in: 100, tokens_out: 20, payload: { model: "m" } }),
      event("llm_usage", { sender: "engineer", tokens_in: 50, tokens_out: 5, payload: { model: "m" } }),
    ]);
    expect(view.agents.engineer).toMatchObject({ tokensIn: 150, tokensOut: 25, calls: 2, model: "m" });
  });

  it("adds up estimated cost, ignoring calls without a price", () => {
    const view = reduceEvents([
      event("llm_usage", { sender: "engineer", tokens_in: 1000, payload: { model: "openai/gpt-oss-120b", cost_usd: 0.0015 } }),
      event("llm_usage", { sender: "engineer", tokens_in: 1000, payload: { model: "openai/gpt-oss-120b", cost_usd: 0.0025 } }),
      event("llm_usage", { sender: "manager", tokens_in: 10, payload: { model: "scripted demo", cost_usd: null } }),
    ]);
    expect(view.agents.engineer.costUsd).toBeCloseTo(0.004);
    expect(view.agents.engineer.pricedCalls).toBe(2);
    expect(view.agents.manager).toMatchObject({ costUsd: 0, pricedCalls: 0, calls: 1 });
  });

  it("tracks the approval from request to decision and the pull request link", () => {
    const requested = reduceEvents([event("approval_requested", { payload: { summary: "s", diff: "", tests: null } })]);
    expect(requested.runState).toBe("awaiting_approval");
    const decided = reduceEvents([
      event("approval_requested", { payload: { summary: "s", diff: "", tests: null } }),
      event("approval_decision", { sender: "human", payload: { approved: true, comment: "" } }),
      event("message", { sender: "system", recipient: "manager", payload: { content: "ok", pr_url: "https://github.com/x/y/pull/1" } }),
      event("run_complete", { payload: { report: "done", failed: false } }),
    ]);
    expect(decided.approval?.decision).toEqual({ approved: true, comment: "" });
    expect(decided.prUrl).toBe("https://github.com/x/y/pull/1");
    expect(decided.runState).toBe("complete");
    expect(decided.report).toEqual({ text: "done", failed: false, stopped: false });
  });

  it("replays a prefix to show the run as it was at that moment", () => {
    const events = [created("T1"), assigned("T1"), status("T1", "done")];
    expect(reduceEvents(events.slice(0, 2)).tasks[0].status).toBe("in_progress");
    expect(reduceEvents(events).tasks[0].status).toBe("done");
  });
});

describe("displayState", () => {
  it("shows a stale 'waiting' as idle once the agent has no open task", () => {
    const events = [created("T1"), assigned("T1"), agentState("engineer", "waiting")];
    expect(displayState(reduceEvents(events), "engineer")).toBe("waiting");
    const closed = reduceEvents([...events, status("T1", "review"), status("T1", "done")]);
    expect(displayState(closed, "engineer")).toBe("idle");
    expect(currentTask(closed, "engineer")).toBeUndefined();
  });
});

describe("linkOf", () => {
  it("maps events to the graph link they travel along", () => {
    expect(linkOf(event("message", { sender: "engineer", recipient: "researcher" }))).toEqual(["engineer", "researcher"]);
    expect(linkOf(event("tool_call", { sender: "researcher", payload: { tool: "ask_codebase" } }))).toEqual(["researcher", "repolens"]);
    expect(linkOf(event("tool_call", { sender: "reviewer", payload: { tool: "run_tests" } }))).toEqual(["reviewer", "sandbox"]);
    expect(linkOf(event("message", { sender: "system", recipient: "manager" }))).toBeNull();
  });
});

describe("changedFiles", () => {
  it("counts added and removed lines per file", () => {
    const diff = [
      "diff --git a/app/auth.py b/app/auth.py",
      "--- a/app/auth.py",
      "+++ b/app/auth.py",
      "@@ -1,2 +1,3 @@",
      "-old",
      "+new",
      "+more",
    ].join("\n");
    expect(changedFiles(diff)).toEqual([{ path: "app/auth.py", added: 2, removed: 1 }]);
  });
});
