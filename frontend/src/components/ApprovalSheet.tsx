import { type ReactNode, useState } from "react";
import { api } from "../lib/api";
import type { Approval } from "../lib/reduce";

interface Props {
  runId: string;
  approval: Approval;
  hasRepo: boolean;
}

// The sign-off sheet, laid out like the title block of a technical drawing: what changed, how it
// was checked, and a place for the person who approves it. Nothing ships without this signature.
export function ApprovalSheet({ runId, approval, hasRepo }: Props) {
  const [comment, setComment] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const files = changedFiles(approval.diff);

  const decide = async (approved: boolean) => {
    setSending(true);
    setError(null);
    try {
      await api.decide(runId, approved, comment);
    } catch (e) {
      setError((e as Error).message);
      setSending(false);
    }
  };

  return (
    <section
      aria-label="Sign-off"
      className="absolute right-4 bottom-4 flex max-h-[calc(100%-2rem)] w-[min(600px,calc(100%-2rem))] flex-col border-2 border-ink bg-paper text-ink shadow-[6px_6px_0_rgb(14_34_54/0.35)]"
    >
      <h2 className="border-b-2 border-ink px-4 py-2 text-xl font-semibold">Sign-off needed</h2>
      <dl className="grid grid-cols-[7rem_1fr] border-b border-ink text-sm">
        <Cell label="Change">{approval.summary}</Cell>
        <Cell label="Files">
          {files.length ? files.map((f) => `${f.path} (+${f.added} −${f.removed})`).join(", ") : "No file changes"}
        </Cell>
        <Cell label="Tests">
          {approval.tests ? (
            <span className={approval.tests.passed ? "text-approved" : "text-pencil"}>
              {approval.tests.passed ? "Passing" : "Failing"} when AgentHQ re-ran <code className="font-mono text-xs">{approval.tests.command}</code>
            </span>
          ) : (
            "Not run (no sandbox in this run)"
          )}
        </Cell>
      </dl>
      {approval.diff && (
        <details className="min-h-0 overflow-auto border-b border-ink">
          <summary className="cursor-pointer px-4 py-1.5 text-sm font-semibold">Show the diff</summary>
          <pre className="px-4 pb-3 font-mono text-xs leading-relaxed">
            {approval.diff.split("\n").map((line, i) => (
              <span key={i} className={`block ${diffClass(line)}`}>
                {line || " "}
              </span>
            ))}
          </pre>
        </details>
      )}
      <div className="space-y-2 px-4 py-3">
        <label className="block text-sm font-semibold" htmlFor="signoff-comment">
          Comment for the team
        </label>
        <textarea
          id="signoff-comment"
          rows={2}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="Required if you send it back"
          className="w-full border border-ink/40 bg-white px-2 py-1.5 text-sm"
        />
        {error && <p className="text-sm text-pencil">{error}</p>}
        <div className="flex flex-wrap justify-end gap-2">
          <button
            type="button"
            disabled={sending || !comment.trim()}
            onClick={() => decide(false)}
            className="border border-ink px-3 py-1.5 font-semibold hover:bg-paper-shade disabled:opacity-40"
          >
            Send back
          </button>
          <button
            type="button"
            disabled={sending}
            onClick={() => decide(true)}
            className="bg-approved px-3 py-1.5 font-semibold text-white hover:brightness-95 disabled:opacity-40"
          >
            {hasRepo ? "Approve and open pull request" : "Approve"}
          </button>
        </div>
      </div>
    </section>
  );
}

function Cell({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="border-t border-r border-ink/40 px-4 py-1.5 font-semibold first-of-type:border-t-0">{label}</dt>
      <dd className="border-t border-ink/40 px-3 py-1.5 whitespace-pre-line first-of-type:border-t-0">{children}</dd>
    </>
  );
}

function diffClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ")) return "font-medium text-ink";
  if (line.startsWith("+")) return "bg-approved/15 text-ink";
  if (line.startsWith("-")) return "bg-pencil/15 text-ink";
  if (line.startsWith("@@")) return "text-cyanotype";
  return "text-ink/70";
}

export function changedFiles(diff: string): { path: string; added: number; removed: number }[] {
  const files: { path: string; added: number; removed: number }[] = [];
  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git")) files.push({ path: line.split(" b/").pop() ?? "", added: 0, removed: 0 });
    else if (files.length && line.startsWith("+") && !line.startsWith("+++")) files[files.length - 1].added++;
    else if (files.length && line.startsWith("-") && !line.startsWith("---")) files[files.length - 1].removed++;
  }
  return files;
}
