import { type FormEvent, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";

const SAMPLE_REPO = "https://github.com/ShlokGhadekar/agenthq-sample-notes";

interface Props {
  onStarted: (runId: string) => void;
  onClose: () => void;
}

export function NewRunForm({ onStarted, onClose }: Props) {
  const [goal, setGoal] = useState("");
  const [repo, setRepo] = useState(SAMPLE_REPO);
  const [demo, setDemo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  const start = async (event: FormEvent) => {
    event.preventDefault();
    setStarting(true);
    try {
      const { run_id } = await api.startRun(goal.trim(), demo ? null : repo.trim() || null, demo);
      onStarted(run_id);
    } catch (e) {
      setError((e as Error).message);
      setStarting(false);
    }
  };

  return (
    <dialog
      ref={dialog}
      onClose={onClose}
      className="m-auto w-[min(560px,calc(100%-2rem))] border-2 border-ink bg-paper p-0 text-ink backdrop:bg-cyanotype-deep/60"
    >
      <form onSubmit={start} className="space-y-4 p-5">
        <h2 className="text-2xl font-semibold">New run</h2>
        <div>
          <label htmlFor="goal" className="block font-semibold">
            What should the team do?
          </label>
          <textarea
            id="goal"
            required
            rows={3}
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder="Add rate limiting to POST /login: at most 5 attempts per minute per IP, then 429 with Retry-After. Add tests."
            className="mt-1 w-full border border-ink/30 bg-white px-2.5 py-2"
          />
        </div>
        <div>
          <label htmlFor="repo" className={`block font-semibold ${demo ? "text-ink/40" : ""}`}>
            Repository
          </label>
          <input
            id="repo"
            type="url"
            disabled={demo}
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            className="mt-1 w-full border border-ink/30 bg-white px-2.5 py-1.5 disabled:opacity-40"
          />
          <p className="mt-1 text-sm text-ink/60">A public GitHub repository. Nothing changes there until you sign off.</p>
        </div>
        <label className="flex items-start gap-2">
          <input type="checkbox" checked={demo} onChange={(e) => setDemo(e.target.checked)} className="mt-1" />
          <span>
            <span className="font-semibold">Scripted demo</span>
            <span className="block text-sm text-ink/60">Replays a recorded team run. No API key or repository needed.</span>
          </span>
        </label>
        {error && <p className="text-sm text-pencil">{error}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className="border border-ink px-3 py-1.5 font-semibold hover:bg-paper-shade">
            Cancel
          </button>
          <button type="submit" disabled={starting || !goal.trim()} className="bg-ink px-3 py-1.5 font-semibold text-paper hover:bg-cyanotype disabled:opacity-40">
            {starting ? "Starting…" : "Start run"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
