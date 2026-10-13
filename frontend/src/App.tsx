import { ReactFlowProvider } from "@xyflow/react";
import { useEffect, useMemo, useState } from "react";
import { ApprovalSheet } from "./components/ApprovalSheet";
import { Header } from "./components/Header";
import { NewRunForm } from "./components/NewRunForm";
import { OrgGraph } from "./components/OrgGraph";
import { SidePanel } from "./components/SidePanel";
import { TaskBoard } from "./components/TaskBoard";
import { useAgents, useRunEvents, useRuns } from "./lib/hooks";
import { type Indexing, reduceEvents, type RunView } from "./lib/reduce";

// The selected run lives in the URL hash, so a reload (or a shared link) shows the same run.
const runFromHash = () => (location.hash.startsWith("#run-") ? location.hash.slice(1) : null);

export default function App() {
  const [runId, setRunId] = useState<string | null>(runFromHash);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const team = useAgents();
  const { runs, refresh } = useRuns();
  const { events, arrivals } = useRunEvents(runId);
  const view = useMemo(() => reduceEvents(events), [events]);
  const run = runs.find((r) => r.run_id === runId);

  useEffect(() => {
    history.replaceState(null, "", runId ? `#${runId}` : location.pathname);
  }, [runId]);

  const pick = (id: string | null) => {
    setRunId(id);
    setSelected(null);
  };

  return (
    <div className="grid h-full grid-rows-[auto_1fr_minmax(9rem,30vh)] lg:grid-cols-[1fr_minmax(20rem,26rem)]">
      <div className="lg:col-span-2">
        <Header runs={runs} runId={runId} runState={runId ? view.runState : null} onPick={pick} onNewRun={() => setCreating(true)} />
      </div>
      <main className="blueprint relative min-h-[26rem] overflow-hidden">
        {view.indexing?.active && <IndexingBar indexing={view.indexing} />}
        <ReactFlowProvider>
          <OrgGraph team={team} view={view} events={events} arrivals={arrivals} selected={selected} onSelect={setSelected} />
        </ReactFlowProvider>
        {runId && view.report && <Report view={view} />}
        {runId && view.approval && !view.approval.decision && view.runState !== "complete" && (
          <ApprovalSheet runId={runId} approval={view.approval} hasRepo={Boolean(run?.repo)} />
        )}
      </main>
      <div className="row-span-2 min-h-0 max-lg:row-span-1 max-lg:h-[28rem]">
        <SidePanel
          runId={runId}
          team={team}
          view={view}
          events={events}
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

function Report({ view }: { view: RunView }) {
  const report = view.report!;
  return (
    <div className={`absolute top-3 left-3 z-10 max-w-xl border-l-4 bg-paper px-4 py-3 text-ink ${report.failed ? "border-pencil" : "border-approved"}`}>
      <h2 className="font-semibold">{report.stopped ? "Run stopped" : report.failed ? "Run ended without reaching the goal" : "Run finished"}</h2>
      <p className="mt-1 max-h-40 overflow-y-auto text-sm whitespace-pre-line">{report.text}</p>
      {view.prUrl && (
        <a href={view.prUrl} target="_blank" rel="noreferrer" className="mt-2 inline-block font-semibold text-cyanotype underline">
          Open the pull request
        </a>
      )}
    </div>
  );
}
