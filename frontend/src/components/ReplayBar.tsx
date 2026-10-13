import { useEffect, useState } from "react";
import type { AgentEvent } from "../lib/types";

interface Props {
  events: AgentEvent[];
  step: number; // how many events are applied: 0 shows the empty drawing, events.length the end
  onStep: (step: number) => void;
  onLeave: () => void;
}

const SPEEDS = [1, 4, 16];

// Step through a run's events. Everything above (graph, board, feed, stats) is rebuilt from the
// first `step` events by the same reducer the live view uses, so replay shows exactly what was seen.
export function ReplayBar({ events, step, onStep, onLeave }: Props) {
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(4);
  const last = events.length;

  useEffect(() => {
    if (!playing) return;
    if (step >= last) {
      setPlaying(false);
      return;
    }
    const timer = setTimeout(() => onStep(step + 1), 900 / speed);
    return () => clearTimeout(timer);
  }, [playing, step, last, speed, onStep]);

  const current = step > 0 ? events[step - 1] : null;
  return (
    <div className="border-t-2 border-signal bg-cyanotype-deep px-4 py-2.5 text-paper">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
        <span className="font-semibold">Replay</span>
        <button type="button" onClick={() => onStep(Math.max(0, step - 1))} disabled={step === 0} className={buttonClass}>
          Back
        </button>
        <button type="button" onClick={() => setPlaying((p) => !p)} disabled={step >= last} className={buttonClass}>
          {playing ? "Pause" : "Play"}
        </button>
        <button type="button" onClick={() => onStep(Math.min(last, step + 1))} disabled={step >= last} className={buttonClass}>
          Forward
        </button>
        <label className="flex items-center gap-1.5">
          <span className="text-paper/70">Speed</span>
          <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))} className="border border-paper/40 bg-cyanotype-deep px-1 py-0.5">
            {SPEEDS.map((s) => (
              <option key={s} value={s}>
                {s}×
              </option>
            ))}
          </select>
        </label>
        <span className="tabular-nums text-paper/80">
          Event {step} of {last}
          {current && `, ${new Date(current.timestamp).toLocaleTimeString([], { hour12: false })}`}
        </span>
        <button type="button" onClick={onLeave} className="ml-auto underline hover:text-signal">
          Leave replay
        </button>
      </div>
      <label className="sr-only" htmlFor="replay-position">
        Replay position
      </label>
      <input
        id="replay-position"
        type="range"
        min={0}
        max={last}
        value={step}
        onChange={(e) => {
          setPlaying(false);
          onStep(Number(e.target.value));
        }}
        className="mt-2 w-full accent-signal"
      />
    </div>
  );
}

const buttonClass = "border border-paper/60 px-2.5 py-0.5 font-semibold hover:bg-paper/10 disabled:opacity-35";
