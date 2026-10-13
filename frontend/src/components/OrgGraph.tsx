import {
  BaseEdge,
  type Edge,
  type EdgeProps,
  getStraightPath,
  Handle,
  type Node,
  type NodeChange,
  type NodeProps,
  Position,
  ReactFlow,
  useReactFlow,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef } from "react";
import { useTicker } from "../lib/hooks";
import { currentTask, displayState, linkOf, type RunView, type TaskView } from "../lib/reduce";
import type { AgentCard, AgentEvent, AgentState } from "../lib/types";

// The team drawn as a fixed technical drawing: the human above the Manager, the workers in a row,
// and the two services they use (dashed: outside the team) underneath.
const LAYOUT: Record<string, { x: number; y: number }> = {
  human: { x: 330, y: 0 },
  manager: { x: 330, y: 120 },
  researcher: { x: 30, y: 270 },
  engineer: { x: 330, y: 270 },
  reviewer: { x: 630, y: 270 },
  repolens: { x: 30, y: 430 },
  sandbox: { x: 330, y: 430 },
};

// [source, sourceSide, target, targetSide]. Traffic in the other direction plays the dot backwards.
const LINKS: [string, string, string, string][] = [
  ["human", "bottom", "manager", "top"],
  ["manager", "bottom", "researcher", "top"],
  ["manager", "bottom", "engineer", "top"],
  ["manager", "bottom", "reviewer", "top"],
  ["researcher", "right", "engineer", "left"],
  ["engineer", "right", "reviewer", "left"],
  ["researcher", "bottom", "repolens", "top"],
  ["researcher", "bottom", "sandbox", "left"],
  ["engineer", "bottom", "sandbox", "top"],
  ["reviewer", "bottom", "sandbox", "right"],
];

const SERVICES: Record<string, { label: string; note: string }> = {
  human: { label: "You", note: "Goals, messages and sign-off" },
  repolens: { label: "RepoLens", note: "Answers questions about the code" },
  sandbox: { label: "Sandbox", note: "The repo clone where code is edited and tested" },
};

const ACTIVE_FOR_MS = 2400;

const STATE_COLOR: Record<AgentState, string> = {
  idle: "var(--color-line)",
  thinking: "var(--color-signal)",
  waiting: "var(--color-wait)",
  error: "var(--color-pencil)",
};

const STATE_WORDS: Record<AgentState, string> = { idle: "Idle", thinking: "Thinking", waiting: "Waiting", error: "Error" };

type AgentNodeData = {
  label: string;
  model: string | null;
  state: AgentState;
  task?: TaskView;
  tokens: number;
  selected: boolean;
};
type ServiceNodeData = { label: string; note: string; busy: boolean; selected: boolean };

type AgentNodeType = Node<AgentNodeData, "agent">;
type ServiceNodeType = Node<ServiceNodeData, "service">;
type SignalEdgeType = Edge<{ active: boolean; reverse: boolean }, "signal">;

function Handles() {
  // One invisible source and target handle per side; edges pick sides from LINKS.
  return (
    <>
      {(["top", "bottom", "left", "right"] as const).map((side) => (
        <span key={side}>
          <Handle id={`${side}-out`} type="source" position={side as Position} />
          <Handle id={`${side}-in`} type="target" position={side as Position} />
        </span>
      ))}
    </>
  );
}

function AgentNode({ data }: NodeProps<AgentNodeType>) {
  const color = STATE_COLOR[data.state];
  return (
    <div
      className={`relative w-[220px] border bg-cyanotype-deep/80 px-3 py-2.5 pl-4 text-paper transition-colors ${
        data.selected ? "border-signal" : "border-paper/60"
      }`}
    >
      <Handles />
      <span
        className={`absolute inset-y-0 left-0 w-1 ${data.state === "thinking" ? "breathe" : ""}`}
        style={{ background: color }}
      />
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-lg font-semibold capitalize leading-tight">{data.label}</span>
        <span className="text-sm" style={{ color }}>
          {STATE_WORDS[data.state]}
        </span>
      </div>
      <div className="text-xs text-line">{data.model ?? "default model"}</div>
      <div className="mt-1.5 truncate text-sm text-paper/90" title={data.task?.title}>
        {data.task ? `${data.task.id} ${data.task.title}` : "No task"}
      </div>
      <div className="text-xs text-line">{data.tokens > 0 ? `${formatTokens(data.tokens)} tokens` : " "}</div>
    </div>
  );
}

function ServiceNode({ data }: NodeProps<ServiceNodeType>) {
  return (
    <div
      className={`w-[220px] border border-dashed px-3 py-2.5 text-paper ${
        data.selected ? "border-signal" : data.busy ? "border-signal/80" : "border-paper/45"
      }`}
    >
      <Handles />
      <div className="text-lg font-semibold leading-tight">{data.label}</div>
      <div className="text-xs text-line">{data.note}</div>
    </div>
  );
}

function SignalEdge({ sourceX, sourceY, targetX, targetY, data }: EdgeProps<SignalEdgeType>) {
  const [path] = getStraightPath({ sourceX, sourceY, targetX, targetY });
  const active = Boolean(data?.active);
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  return (
    <>
      <BaseEdge
        path={path}
        style={{ stroke: active ? "var(--color-signal)" : "rgb(242 245 247 / 0.32)", strokeWidth: active ? 2 : 1.25 }}
      />
      {active && !reducedMotion && (
        <circle r={4} fill="var(--color-signal)">
          <animateMotion
            dur="1.1s"
            repeatCount="indefinite"
            path={path}
            keyPoints={data?.reverse ? "1;0" : "0;1"}
            keyTimes="0;1"
            calcMode="linear"
          />
        </circle>
      )}
    </>
  );
}

const nodeTypes = { agent: AgentNode, service: ServiceNode };
const edgeTypes = { signal: SignalEdge };

export function formatTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k` : String(n);
}

interface Props {
  team: AgentCard[];
  view: RunView;
  events: AgentEvent[];
  arrivals: Map<string, number>;
  selected: string | null;
  onSelect: (name: string | null) => void;
}

export function OrgGraph({ team, view, events, arrivals, selected, onSelect }: Props) {
  // Recent traffic per link: which links carried an event in the last few seconds, and which way.
  const latestArrival = events.length ? (arrivals.get(events[events.length - 1].id) ?? 0) : 0;
  const now = useTicker(300, Date.now() - latestArrival < ACTIVE_FOR_MS + 300);
  const traffic = useMemo(() => {
    const result = new Map<string, { reverse: boolean }>();
    for (let i = events.length - 1; i >= 0 && i >= events.length - 40; i--) {
      const arrived = arrivals.get(events[i].id) ?? 0;
      if (now - arrived > ACTIVE_FOR_MS) continue;
      const link = linkOf(events[i]);
      if (!link) continue;
      const forward = LINKS.find(([s, , t]) => s === link[0] && t === link[1]);
      const backward = LINKS.find(([s, , t]) => s === link[1] && t === link[0]);
      const match = forward ?? backward;
      if (match && !result.has(`${match[0]}-${match[2]}`)) result.set(`${match[0]}-${match[2]}`, { reverse: !forward });
    }
    return result;
  }, [events, arrivals, now]);

  // The nodes are rebuilt from the run's state on every update. React Flow only shows a node once it
  // knows its measured size, and only re-measures when the size changes, so a rebuilt node would stay
  // hidden. Remember each measurement and hand it back with the rebuilt node.
  const measured = useRef(new Map<string, { width: number; height: number }>());
  const onNodesChange = useCallback((changes: NodeChange[]) => {
    for (const change of changes) {
      if (change.type === "dimensions" && change.dimensions) measured.current.set(change.id, change.dimensions);
    }
  }, []);

  const nodes = useMemo<(AgentNodeType | ServiceNodeType)[]>(() => {
    const agentNodes: AgentNodeType[] = team.map((card) => {
      const usage = view.agents[card.name];
      return {
        id: card.name,
        type: "agent",
        position: LAYOUT[card.name] ?? { x: 0, y: 0 },
        data: {
          label: card.name,
          model: usage?.model ?? card.model,
          state: displayState(view, card.name),
          task: currentTask(view, card.name),
          tokens: usage ? usage.tokensIn + usage.tokensOut : 0,
          selected: selected === card.name,
        },
      };
    });
    const busy = new Set([...traffic.keys()].flatMap((key) => key.split("-")));
    const serviceNodes: ServiceNodeType[] = Object.entries(SERVICES).map(([id, service]) => ({
      id,
      type: "service",
      position: LAYOUT[id],
      data: { ...service, busy: busy.has(id) || (id === "repolens" && Boolean(view.indexing?.active)), selected: selected === id },
    }));
    return [...serviceNodes, ...agentNodes].map((node) => ({ ...node, measured: measured.current.get(node.id) }));
  }, [team, view, selected, traffic]);

  const edges = useMemo<SignalEdgeType[]>(
    () =>
      LINKS.map(([source, sourceSide, target, targetSide]) => {
        const flow = traffic.get(`${source}-${target}`);
        return {
          id: `${source}-${target}`,
          source,
          target,
          sourceHandle: `${sourceSide}-out`,
          targetHandle: `${targetSide}-in`,
          type: "signal",
          data: { active: Boolean(flow), reverse: Boolean(flow?.reverse) },
        };
      }),
    [traffic],
  );

  // Keep the whole drawing in view: refit once the team has loaded and whenever the canvas resizes.
  const { fitView } = useReactFlow();
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const refit = () => fitView({ padding: 0.12 });
    const timer = setTimeout(refit, 0);
    const observer = new ResizeObserver(refit);
    if (container.current) observer.observe(container.current);
    return () => {
      clearTimeout(timer);
      observer.disconnect();
    };
  }, [fitView, team.length]);

  return (
    <div ref={container} className="h-full w-full">
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      fitView
      fitViewOptions={{ padding: 0.12 }}
      minZoom={0.2}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
      zoomOnScroll={false}
      panOnDrag={false}
      onNodeClick={(_, node) => onSelect(node.id === selected ? null : node.id)}
      onPaneClick={() => onSelect(null)}
      proOptions={{ hideAttribution: true }}
    />
    </div>
  );
}
