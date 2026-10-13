import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { describe, type Tone } from "../lib/describe";
import { currentTask, displayState, type RunView } from "../lib/reduce";
import type { AgentCard, AgentEvent } from "../lib/types";
import { formatTokens } from "./OrgGraph";

const TONE_CLASS: Record<Tone, string> = {
  normal: "text-ink",
  good: "text-approved",
  bad: "text-pencil",
  quiet: "text-ink/55",
};

interface Props {
  runId: string | null;
  team: AgentCard[];
  view: RunView;
  events: AgentEvent[];
  selected: string | null;
  onSelect: (name: string | null) => void;
  onStart: () => void;
}

export function SidePanel({ runId, team, view, events, selected, onSelect, onStart }: Props) {
  const choices = [
    { id: null, label: "All activity" },
    { id: "human", label: "You" },
    ...team.map((t) => ({ id: t.name, label: capitalize(t.name) })),
    { id: "repolens", label: "RepoLens" },
    { id: "sandbox", label: "Sandbox" },
  ];
  const agent = team.find((t) => t.name === selected);
  return (
    <aside className="flex h-full min-h-0 flex-col border-l border-ink/15 bg-paper">
      <nav className="flex flex-wrap gap-x-3 gap-y-1 border-b border-ink/15 px-4 py-2.5 text-sm" aria-label="Show details for">
        {choices.map((choice) => (
          <button
            key={choice.label}
            type="button"
            onClick={() => onSelect(choice.id)}
            className={`border-b-2 pb-0.5 ${selected === choice.id ? "border-ink font-semibold" : "border-transparent text-ink/60 hover:text-ink"}`}
          >
            {choice.label}
          </button>
        ))}
      </nav>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {!runId && (
          <div className="max-w-sm">
            <p className="text-lg leading-snug">
              Describe a change. The team plans it, builds it in a sandbox, and asks you to sign off before anything leaves.
            </p>
            <button type="button" onClick={onStart} className="mt-3 bg-ink px-3 py-1.5 font-semibold text-paper hover:bg-cyanotype">
              Start a run
            </button>
          </div>
        )}
        {runId && selected === null && <Feed events={events} />}
        {runId && agent && <AgentDetail card={agent} view={view} events={events} />}
        {runId && selected === "repolens" && <RepoLensDetail view={view} events={events} />}
        {runId && selected === "sandbox" && <SandboxDetail events={events} />}
        {runId && selected === "human" && <Feed events={events.filter((e) => e.sender === "human" || e.recipient === "human" || e.type === "approval_requested")} />}
      </div>
      {runId && agent && view.runState !== "complete" && <ChatBox runId={runId} to={agent.name} />}
    </aside>
  );
}

function Feed({ events }: { events: AgentEvent[] }) {
  const end = useRef<HTMLDivElement>(null);
  const lines = events.map((e) => ({ event: e, line: describe(e) })).filter((x) => x.line);
  useEffect(() => {
    // Braces matter: newer browsers return a Promise from scrollIntoView, and React treats any
    // returned value as a cleanup function.
    end.current?.scrollIntoView({ block: "end" });
  }, [lines.length]);
  if (lines.length === 0) return <p className="text-ink/60">Nothing yet.</p>;
  return (
    <ol className="space-y-2">
      {lines.map(({ event, line }) => (
        <li key={event.id} className="grid grid-cols-[3.25rem_1fr] gap-2 text-sm">
          <time className="pt-px text-xs tabular-nums text-ink/45">{clock(event.timestamp)}</time>
          <div className={TONE_CLASS[line!.tone]}>
            <span className="font-semibold capitalize">{line!.who}</span> {line!.text}
          </div>
        </li>
      ))}
      <div ref={end} />
    </ol>
  );
}

function AgentDetail({ card, view, events }: { card: AgentCard; view: RunView; events: AgentEvent[] }) {
  const usage = view.agents[card.name];
  const task = currentTask(view, card.name);
  const mine = events.filter((e) => e.sender === card.name || e.recipient === card.name);
  return (
    <div className="space-y-4">
      <header>
        <h2 className="text-2xl font-semibold capitalize leading-tight">{card.name}</h2>
        <p className="text-sm text-ink/70">{card.description}</p>
        <dl className="mt-2 grid grid-cols-3 gap-2 text-sm">
          <Stat label="State" value={capitalize(displayState(view, card.name))} />
          <Stat label="Model" value={usage?.model ?? card.model ?? "default"} />
          <Stat label="Tokens" value={usage ? `${formatTokens(usage.tokensIn)} in, ${formatTokens(usage.tokensOut)} out` : "none"} />
        </dl>
      </header>
      <Section title="Current task">
        {task ? (
          <div>
            <div className="font-semibold">
              {task.id} {task.title}
            </div>
            <p className="text-sm text-ink/70">{task.description}</p>
          </div>
        ) : (
          <p className="text-sm text-ink/60">No task right now.</p>
        )}
      </Section>
      <Section title="Messages and tool calls">
        <ToolFeed events={mine} />
      </Section>
    </div>
  );
}

/** Like Feed, but tool results can be opened to see exactly what the agent got back. */
function ToolFeed({ events }: { events: AgentEvent[] }) {
  const items = events.filter((e) => e.type !== "status_change" || e.payload.task_status);
  if (items.length === 0) return <p className="text-sm text-ink/60">Nothing yet.</p>;
  return (
    <ol className="space-y-2 text-sm">
      {items.map((event) => {
        if (event.type === "tool_result") {
          const line = describe(event);
          return (
            <li key={event.id}>
              <details>
                <summary className={`cursor-pointer ${line ? TONE_CLASS[line.tone] : "text-ink/55"}`}>
                  {line ? line.text : `${event.payload.tool} returned`}
                </summary>
                <pre className="mt-1 max-h-72 overflow-auto bg-paper-shade p-2 font-mono text-xs whitespace-pre-wrap">{event.payload.result}</pre>
              </details>
            </li>
          );
        }
        const line = describe(event);
        return line ? (
          <li key={event.id} className={TONE_CLASS[line.tone]}>
            <span className="font-semibold capitalize">{line.who}</span> {line.text}
          </li>
        ) : null;
      })}
    </ol>
  );
}

function RepoLensDetail({ view, events }: { view: RunView; events: AgentEvent[] }) {
  const answers = events.filter((e) => e.type === "tool_result" && e.payload.tool === "ask_codebase");
  return (
    <div className="space-y-4">
      <header>
        <h2 className="text-2xl font-semibold leading-tight">RepoLens</h2>
        <p className="text-sm text-ink/70">Hybrid search over the repository; every answer cites files and line ranges.</p>
        <p className="mt-1 text-sm">
          {view.indexing?.active ? `Indexing: ${view.indexing.stage} ${view.indexing.done}/${view.indexing.total}` : view.indexing ? "Repository indexed" : "Index already loaded"}
        </p>
      </header>
      <Section title={`Questions (${answers.length})`}>
        {answers.length === 0 && <p className="text-sm text-ink/60">No questions yet.</p>}
        <ol className="space-y-3">
          {answers.map((e) => (
            <li key={e.id} className="text-sm">
              <div className="font-semibold">{e.payload.details?.question}</div>
              {e.payload.details?.found ? (
                <ul className="mt-0.5">
                  {(e.payload.details.citations ?? []).map((c: { path: string; start_line: number; end_line: number }) => (
                    <li key={`${c.path}:${c.start_line}`} className="font-mono text-xs">
                      {c.path}:{c.start_line}-{c.end_line}
                    </li>
                  ))}
                  <li className="text-xs text-ink/55">best similarity {e.payload.details.best_similarity.toFixed(2)}</li>
                </ul>
              ) : (
                <p className="text-pencil">{e.payload.ok ? "Not found in the repository" : e.payload.result}</p>
              )}
            </li>
          ))}
        </ol>
      </Section>
    </div>
  );
}

function SandboxDetail({ events }: { events: AgentEvent[] }) {
  const runs = events.filter((e) => e.type === "tool_result" && e.payload.details?.command);
  const edits = events.filter((e) => e.type === "tool_call" && (e.payload.tool === "edit_file" || e.payload.tool === "write_file"));
  const files = [...new Set(edits.map((e) => e.payload.arguments.path as string))];
  return (
    <div className="space-y-4">
      <header>
        <h2 className="text-2xl font-semibold leading-tight">Sandbox</h2>
        <p className="text-sm text-ink/70">A private clone of the repository. Tests and the linter run in a container with no network.</p>
      </header>
      <Section title={`Files changed (${files.length})`}>
        {files.length === 0 ? <p className="text-sm text-ink/60">No changes yet.</p> : (
          <ul className="font-mono text-xs">{files.map((f) => <li key={f}>{f}</li>)}</ul>
        )}
      </Section>
      <Section title={`Test and lint runs (${runs.length})`}>
        <ol className="space-y-1.5 text-sm">
          {runs.map((e) => (
            <li key={e.id}>
              <details>
                <summary className={`cursor-pointer ${e.payload.details.passed ? "text-approved" : "text-pencil"}`}>
                  {clock(e.timestamp)} {e.sender}: {e.payload.details.command} {e.payload.details.passed ? "passed" : "failed"}
                </summary>
                <pre className="mt-1 max-h-72 overflow-auto bg-paper-shade p-2 font-mono text-xs whitespace-pre-wrap">{e.payload.result}</pre>
              </details>
            </li>
          ))}
        </ol>
      </Section>
    </div>
  );
}

function ChatBox({ runId, to }: { runId: string; to: string }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (!text.trim()) return;
    try {
      await api.message(runId, to, text.trim());
      setText("");
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <form onSubmit={send} className="border-t border-ink/15 p-3">
      <label className="sr-only" htmlFor="chat">
        Message the {to}
      </label>
      <div className="flex gap-2">
        <input
          id="chat"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={`Message the ${to}`}
          className="min-w-0 flex-1 border border-ink/25 bg-white px-2.5 py-1.5"
        />
        <button type="submit" className="bg-ink px-3 py-1.5 font-semibold text-paper hover:bg-cyanotype">
          Send
        </button>
      </div>
      {error && <p className="mt-1 text-sm text-pencil">{error}</p>}
    </form>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-1.5 border-b border-ink/15 pb-1 text-base font-semibold">{title}</h3>
      {children}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-ink/55">{label}</dt>
      <dd className="truncate" title={value}>
        {value}
      </dd>
    </div>
  );
}

export const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
