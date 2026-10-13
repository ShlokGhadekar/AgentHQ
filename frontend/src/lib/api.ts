// Thin wrappers around the backend's HTTP API. Errors carry the backend's own message.

import type { AgentCard, RunSummary } from "./types";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Request failed with status ${response.status}`);
  }
  return response.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) => call<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });

export const api = {
  agents: () => call<AgentCard[]>("/agents"),
  runs: () => call<RunSummary[]>("/runs"),
  startRun: (goal: string, repo: string | null, demo: boolean) => post<{ run_id: string }>("/runs", { goal, repo, demo }),
  pause: (runId: string) => post(`/runs/${runId}/pause`),
  resume: (runId: string) => post(`/runs/${runId}/resume`),
  stop: (runId: string) => post(`/runs/${runId}/stop`),
  decide: (runId: string, approved: boolean, comment: string) =>
    post<{ pr_url: string | null }>(`/runs/${runId}/approval`, { approved, comment }),
  message: (runId: string, to: string, content: string) => post(`/runs/${runId}/messages`, { to, content }),
};
