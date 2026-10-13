import { ReactFlowProvider } from "@xyflow/react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { ApprovalSheet } from "./components/ApprovalSheet";
import { Header } from "./components/Header";
import { NewRunForm } from "./components/NewRunForm";
import { OrgGraph } from "./components/OrgGraph";
import { ReplayBar } from "./components/ReplayBar";
import { SidePanel } from "./components/SidePanel";
import { StatsBar } from "./components/StatsBar";
import { TaskBoard } from "./components/TaskBoard";
import { useAgents, useRunEvents, useRuns, useTicker } from "./lib/hooks";
import { type Indexing, reduceEvents, type RunView } from "./lib/reduce";

// The selected run lives in the URL hash, so a reload (or a shared link) shows the same run.
const runFromHash = () => (location.hash.startsWith("#run-") ? location.hash.slice(1) : null);

export default function App() {
  const [runId, setRunId] = useState<string | null>(runFromHash);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  // null: live view. A number: replaying, with that many of the run's events applied.
  const [replayStep, setReplayStep] = useState<number | null>(null);
  const team = useAgents();
  const { runs, refresh } = useRuns();
  const { events, arrivals, connected, missing, archived } = useRunEvents(runId);
  const run = runs.find((r) => r.run_id === runId);

  const replaying = replayStep !== null;
  const shown = useMemo(() => (replayStep === null ? events : events.slice(0, replayStep)), [events, replayStep]);
  const view = useMemo(() => reduceEvents(shown), [shown]);
  // In replay, the newest event shown "arrives" now, so its link in the graph lights up as you step.
  const shownArrivals = useMemo(() => {
    if (!replaying) return arrivals;
    const newest = shown[shown.length - 1];
    return new Map(newest ? [[newest.id, Date.now()]] : []);
  }, [replaying, shown, arrivals]);

  // Recorded, sent in full, yet no run_complete: the server stopped while this run was going.
  const interrupted = archived && !replaying && view.runState !== "complete";
  // Nothing received and no connection: the server is unreachable, so show that instead of a run.
  const unreachable = Boolean(runId) && !connected && !missing && !archived && events.length === 0;
  const live = Boolean(runId) && !missing && !archived && !unreachable && !replaying && view.runState !== "complete";
  const now = useTicker(1000, live);

  useEffect(() => {
    if (runFromHash() !== runId) history.replaceState(null, "", runId ? `#${runId}` : location.pathname);
    setReplayStep(null);
  }, [runId]);
  useEffect(() => {
    // Follow links and back/forward that change only the hash (no page load).
    const follow = () => {
      setRunId(runFromHash());
      setSelected(null);
    };
    window.addEventListener("hashchange", follow);
    return () => window.removeEventListener("hashchange", follow);
  }, []);

  const pick = (id: string | null) => {
    setRunId(id);
    setSelected(null);
  };

  return (
    <div className="grid h-full grid-cols-[minmax(0,1fr)] grid-rows-[auto_1fr_minmax(9rem,30vh)] lg:grid-cols-[minmax(0,1fr)_minmax(20rem,26rem)]">
      <div className="lg:col-span-2">
        <Header
          runs={runs}
          runId={runId}
          runState={!runId || missing || unreachable ? null : interrupted ? "interrupted" : view.runState}
          canControl={live}
          onPick={pick}
          onNewRun={() => setCreating(true)}
        />
      </div>
      <main className="blueprint flex min-h-[26rem] flex-col overflow-hidden">
        <div className="relative min-h-0 flex-1">
          {view.indexing?.active && <IndexingBar indexing={view.indexing} />}
          <ReactFlowProvider>
            <OrgGraph team={team} view={view} events={shown} arrivals={shownArrivals} selected={selected} onSelect={setSelected} />
          </ReactFlowProvider>
          {runId && missing && <Notice title={`The server doesn't have ${runId}`}>Pick another run or start a new one.</Notice>}
          {unreachable && <Notice title="Can't reach the AgentHQ server">Retrying every few seconds. Check that the backend is running.</Notice>}
          {interrupted && (
            <Notice title="This run was interrupted">
              The server stopped before the run finished. Everything it recorded until then is shown, and you can replay it.
            </Notice>
          )}
          {runId && view.report && <Report view={view} />}
          {live && runId && view.approval && !view.approval.decision && (
            <ApprovalSheet runId={runId} approval={view.approval} hasRepo={Boolean(run?.repo)} />
          )}
        </div>
        {runId && !missing && !unreachable && (
          <StatsBar view={view} now={live ? now : null} onReplay={!replaying && events.length > 0 ? () => setReplayStep(0) : null} />
        )}
        {replaying && <ReplayBar events={events} step={replayStep} onStep={setReplayStep} onLeave={() => setReplayStep(null)} />}
      </main>
      <div className="row-span-2 min-h-0 max-lg:row-span-1 max-lg:h-[28rem]">
        <SidePanel
          runId={runId}
          team={team}
          view={view}
          events={shown}
          canChat={live}
          selected={selected}
          onSelect={setSelected}
          onStart={() => setCreating(true)}
        />
      </div>
      <TaskBoard tasks={view.tasks} />
      {creating && (
        <NewRunForm
          onClose={() => setCreating(false)}
          onStarted={(id) => {
            setCreating(false);
            refresh();
            pick(id);
          }}
        />
      )}
    </div>
  );
}

function IndexingBar({ indexing }: { indexing: Indexing }) {
  const percent = indexing.total ? Math.round((indexing.done / indexing.total) * 100) : 0;
  const repo = indexing.source?.split("/").slice(-1)[0] ?? "the repository";
  return (
    <div className="absolute inset-x-0 top-0 z-10 bg-cyanotype-deep/90 px-4 py-2 text-sm text-paper" role="status">
      <div className="flex justify-between">
        <span>
          RepoLens is indexing {repo}: {indexing.stage ?? "starting"}
        </span>
        <span className="tabular-nums">{indexing.total ? `${indexing.done} of ${indexing.total}` : ""}</span>
      </div>
      <div className="mt-1 h-1 bg-paper/20">
        {/* Embedding reports progress in large steps, so an empty bar at "embed" is normal. */}
        <div className="h-full bg-signal transition-[width]" style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

function Notice({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="absolute top-3 left-3 z-10 max-w-md border-l-4 border-pencil bg-paper px-4 py-3 text-ink">
      <h2 className="font-semibold">{title}</h2>
      <p className="mt-1 text-sm">{children}</p>
    </div>
  );
}

function Report({ view }: { view: RunView }) {
  const [hidden, setHidden] = useState(false);
  const report = view.report!;
  if (hidden) {
    return (
      <button type="button" onClick={() => setHidden(false)} className="absolute top-3 left-3 z-10 bg-paper px-3 py-1 text-sm font-semibold text-ink">
        Show the report
      </button>
    );
  }
  return (
    <div className={`absolute top-3 left-3 z-10 max-w-xl border-l-4 bg-paper px-4 py-3 text-ink ${report.failed ? "border-pencil" : "border-approved"}`}>
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="font-semibold">{report.stopped ? "Run stopped" : report.failed ? "Run ended without reaching the goal" : "Run finished"}</h2>
        <button type="button" onClick={() => setHidden(true)} className="text-sm text-ink/60 underline hover:text-ink">
          Hide
        </button>
      </div>
      <p className="mt-1 max-h-40 overflow-y-auto text-sm whitespace-pre-line">{report.text}</p>
      {view.prUrl && (
        <a href={view.prUrl} target="_blank" rel="noreferrer" className="mt-2 inline-block font-semibold text-cyanotype underline">
          Open the pull request
        </a>
      )}
    </div>
  );
}
