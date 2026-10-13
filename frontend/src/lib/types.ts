// Shapes of what the backend sends. They mirror agenthq/events.py and the run API.

export type EventType =
  | "task_created"
  | "task_assigned"
  | "message"
  | "tool_call"
  | "tool_result"
  | "status_change"
  | "approval_requested"
  | "approval_decision"
  | "llm_usage"
  | "indexing_progress"
  | "error"
  | "run_complete";

export interface AgentEvent {
  id: string;
  run_id: string;
  timestamp: string;
  type: EventType;
  sender: string | null;
  recipient: string | null;
  task_id: string | null;
  // Free-form per event type, exactly like the backend's Event.payload.
  payload: Record<string, any>;
  tokens_in: number;
  tokens_out: number;
}

export interface AgentCard {
  name: string;
  description: string;
  tools: string[];
  model: string | null;
}

export type RunState = "preparing" | "running" | "paused" | "awaiting_approval" | "complete";

export interface RunSummary {
  run_id: string;
  goal: string;
  repo: string | null;
  state: RunState;
}

export type TaskStatus = "todo" | "in_progress" | "review" | "done" | "failed";

export type AgentState = "idle" | "thinking" | "waiting" | "error";
