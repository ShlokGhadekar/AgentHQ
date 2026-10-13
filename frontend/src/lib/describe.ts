// Plain-language lines for the activity feed. Bookkeeping events (agent state flips, token counts)
// return null: they drive the graph and stats, but aren't news.

import type { AgentEvent } from "./types";

export type Tone = "normal" | "good" | "bad" | "quiet";

export interface Line {
  who: string;
  text: string;
  tone: Tone;
}

const who = (name: string | null) => (name === "human" ? "You" : name === "system" ? "AgentHQ" : (name ?? ""));

export function describe(event: AgentEvent): Line | null {
  const p = event.payload;
  const from = who(event.sender);
  switch (event.type) {
    case "message":
      return { who: from, text: `to ${who(event.recipient)}: ${p.content}`, tone: "normal" };
    case "task_created":
      return { who: from, text: `planned ${event.task_id} "${p.title}" for ${p.assignee}`, tone: "normal" };
    case "task_assigned":
      return { who: from, text: `handed ${event.task_id} to ${event.recipient}`, tone: "quiet" };
    case "status_change":
      if (p.task_status) {
        const detail = p.summary ?? p.reason ?? p.comments ?? p.comment ?? p.note ?? "";
        const tone: Tone = p.task_status === "done" ? "good" : p.task_status === "failed" ? "bad" : "normal";
        return { who: from, text: `${event.task_id} is now ${p.task_status.replace("_", " ")}${detail ? `: ${detail}` : ""}`, tone };
      }
      if (p.run_state) return { who: "AgentHQ", text: `Run is ${p.run_state.replace("_", " ")}`, tone: "quiet" };
      return null;
    case "tool_call":
      return { who: from, text: toolCallText(p.tool, p.arguments ?? {}), tone: "quiet" };
    case "tool_result":
      if (p.details?.found !== undefined) {
        const cited = (p.details.citations ?? []).map((c: Citation) => `${c.path}:${c.start_line}-${c.end_line}`).join(", ");
        return p.details.found
          ? { who: "RepoLens", text: `found ${cited || "an answer"} (similarity ${p.details.best_similarity.toFixed(2)})`, tone: "good" }
          : { who: "RepoLens", text: "could not find this in the repository", tone: "bad" };
      }
      if (p.details?.passed !== undefined) {
        return { who: "Sandbox", text: `${p.details.command} ${p.details.passed ? "passed" : "failed"}`, tone: p.details.passed ? "good" : "bad" };
      }
      return p.ok ? null : { who: from, text: `${p.tool} failed: ${p.result}`, tone: "bad" };
    case "approval_requested":
      return { who: from, text: "asked you to sign off on the changes", tone: "normal" };
    case "approval_decision":
      return { who: "You", text: p.approved ? "approved the changes" : `sent the changes back${p.comment ? `: ${p.comment}` : ""}`, tone: p.approved ? "good" : "bad" };
    case "error":
      return p.retrying_in_s !== undefined
        ? { who: from, text: `retrying in ${p.retrying_in_s}s: ${shorten(p.message)}`, tone: "quiet" }
        : { who: from, text: shorten(p.message), tone: "bad" };
    case "run_complete":
      return { who: "AgentHQ", text: p.report, tone: p.failed ? "bad" : "good" };
    default:
      return null;
  }
}

export interface Citation {
  path: string;
  start_line: number;
  end_line: number;
}

function toolCallText(tool: string, args: Record<string, unknown>): string {
  switch (tool) {
    case "ask_codebase":
      return `asked RepoLens: ${args.question}`;
    case "read_file":
      return `read ${args.path}${args.start_line ? ` from line ${args.start_line}` : ""}`;
    case "edit_file":
      return `edited ${args.path}`;
    case "write_file":
      return `wrote ${args.path}`;
    case "search_code":
      return `searched for /${args.pattern}/`;
    case "run_tests":
      return "ran the tests";
    case "run_linter":
      return "ran the linter";
    case "view_diff":
      return "looked at the diff";
    default:
      return `used ${tool}`;
  }
}

// Provider errors embed whole JSON bodies; the first sentence is what a person needs.
function shorten(message: string): string {
  const clean = message.replace(/\s+/g, " ");
  return clean.length > 220 ? `${clean.slice(0, 219)}…` : clean;
}
