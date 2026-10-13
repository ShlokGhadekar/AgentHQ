import { useState } from "react";
import { api } from "../lib/api";
import type { RunState, RunSummary } from "../lib/types";

const STATE_WORDS: Record<RunState, string> = {
  preparing: "Preparing",
  running: "Running",
  paused: "Paused",
  awaiting_approval: "Waiting for your sign-off",
  complete: "Finished",
};

interface Props {
  runs: RunSummary[];
  runId: string | null;
  runState: RunState | null;
  onPick: (runId: string | null) => void;
  onNewRun: () => void;
}

export function Header({ runs, runId, runState, onPick, onNewRun }: Props) {
  const [error, setError] = useState<string | null>(null);
  const act = (action: (id: string) => Promise<unknown>) => async () => {
    if (!runId) return;
    try {
      await action(runId);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const live = runState !== null && runState !== "complete";

  return (
    <header className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-ink/15 bg-paper px-4 py-2.5">
      <h1 className="text-2xl font-bold tracking-tight">AgentHQ</h1>
      <label className="order-last flex w-full min-w-0 items-center gap-2 text-sm sm:order-none sm:w-auto sm:flex-1">
        <span className="text-ink/60">Run</span>
        <select
          value={runId ?? ""}
          onChange={(e) => onPick(e.target.value || null)}
          className="min-w-0 flex-1 truncate border border-ink/25 bg-white px-2 py-1"
        >
          <option value="">{runs.length ? "Choose a run" : "No runs yet"}</option>
          {[...runs].reverse().map((run) => (
            <option key={run.run_id} value={run.run_id}>
              {run.goal} ({run.run_id})
            </option>
          ))}
        </select>
      </label>
      {runState && <span className="text-sm font-semibold">{STATE_WORDS[runState]}</span>}
      {live && (
        <div className="flex gap-2 text-sm">
          {runState === "paused" ? (
            <button type="button" onClick={act(api.resume)} className="border border-ink px-2.5 py-1 font-semibold hover:bg-paper-shade">
              Resume
            </button>
          ) : (
            <button type="button" onClick={act(api.pause)} className="border border-ink px-2.5 py-1 font-semibold hover:bg-paper-shade">
              Pause
            </button>
          )}
          <button type="button" onClick={act(api.stop)} className="border border-pencil px-2.5 py-1 font-semibold text-pencil hover:bg-pencil/10">
            Stop
          </button>
        </div>
      )}
      <button type="button" onClick={onNewRun} className="ml-auto bg-ink px-3 py-1.5 font-semibold text-paper hover:bg-cyanotype sm:ml-0">
        New run
      </button>
      {error && <p className="w-full text-sm text-pencil">{error}</p>}
    </header>
  );
}
