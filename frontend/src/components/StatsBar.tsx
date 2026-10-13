import type { RunView } from "../lib/reduce";
import { formatTokens } from "./OrgGraph";

interface Props {
  view: RunView;
  // When the run is still going, elapsed time counts up to now; otherwise to the last event.
  now: number | null;
  onReplay: (() => void) | null;
}

// The run's numbers along the bottom edge of the drawing, like a drawing's scale bar.
export function StatsBar({ view, now, onReplay }: Props) {
  const agents = Object.values(view.agents);
  const tokens = agents.reduce((sum, a) => sum + a.tokensIn + a.tokensOut, 0);
  const priced = agents.some((a) => a.pricedCalls > 0);
  const cost = agents.reduce((sum, a) => sum + a.costUsd, 0);
  const start = view.startedAt ? Date.parse(view.startedAt) : null;
  const end = now ?? (view.lastAt ? Date.parse(view.lastAt) : null);

  return (
    <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1 border-t border-paper/25 bg-cyanotype-deep/90 px-4 py-2 text-sm text-paper">
      <Figure label="Elapsed" value={start && end ? duration(end - start) : "0s"} />
      <Figure label="Tokens" value={tokens.toLocaleString()} />
      <Figure
        label="Estimated cost"
        value={priced ? `$${cost.toFixed(cost < 0.1 ? 4 : 2)}` : "n/a"}
        title={
          priced
            ? "At the providers' list prices. On a free tier the real bill is $0."
            : "No estimate: scripted demo runs have no cost, and runs recorded before cost tracking have none stored."
        }
      />
      <div className="flex flex-wrap gap-x-4 text-paper/80">
        {agents.map((a) => (
          <span key={a.name} title={`${a.calls} calls`}>
            <span className="capitalize">{a.name}</span> {formatTokens(a.tokensIn + a.tokensOut)}
          </span>
        ))}
      </div>
      {onReplay && (
        <button type="button" onClick={onReplay} className="ml-auto border border-paper/60 px-2.5 py-0.5 font-semibold hover:bg-paper/10">
          Replay
        </button>
      )}
    </div>
  );
}

function Figure({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <span title={title}>
      <span className="text-paper/60">{label}</span> <span className="font-semibold tabular-nums">{value}</span>
    </span>
  );
}

export function duration(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}
