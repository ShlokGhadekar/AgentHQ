import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { AgentCard, AgentEvent, RunSummary } from "./types";

/** A run's events over the WebSocket: everything so far, then live. Reconnects until the run completes. */
export function useRunEvents(runId: string | null): {
  events: AgentEvent[];
  arrivals: Map<string, number>;
  connected: boolean;
  missing: boolean;
  archived: boolean;
} {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [missing, setMissing] = useState(false);
  // The server sent a past run's recorded events and closed: it is history, not live.
  const [archived, setArchived] = useState(false);
  // When each event arrived in this tab, so the graph animates live traffic but not history.
  const arrivals = useRef(new Map<string, number>());

  useEffect(() => {
    setEvents([]);
    setMissing(false);
    setArchived(false);
    arrivals.current = new Map();
    if (!runId) return;
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let finished = false;
    let closedByUs = false;
    const seen = new Set<string>();
    let firstBatch = true;

    const connect = () => {
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${protocol}://${location.host}/ws/runs/${runId}`);
      socket.onopen = () => {
        setConnected(true);
        // History arrives in a burst right after connecting; don't animate it.
        setTimeout(() => (firstBatch = false), 300);
      };
      socket.onmessage = (message) => {
        const event = JSON.parse(message.data) as AgentEvent;
        if (seen.has(event.id)) return; // a reconnect resends history
        seen.add(event.id);
        arrivals.current.set(event.id, firstBatch ? 0 : Date.now());
        if (event.type === "run_complete") finished = true;
        setEvents((current) => [...current, event]);
      };
      socket.onclose = (close) => {
        setConnected(false);
        // 4404: unknown run; 4001: a past run, all of it already sent. Neither is worth retrying.
        if (close.code === 4404) setMissing(true);
        else if (close.code === 4001) setArchived(true);
        else if (!closedByUs && !finished) retry = setTimeout(connect, 2000);
      };
    };
    connect();
    return () => {
      closedByUs = true;
      clearTimeout(retry);
      socket?.close();
    };
  }, [runId]);

  return { events, arrivals: arrivals.current, connected, missing, archived };
}

/** The list of runs, refreshed every few seconds so states (running, complete) stay current. */
export function useRuns(): { runs: RunSummary[]; refresh: () => void } {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    const load = () => api.runs().then((r) => alive && setRuns(r)).catch(() => {});
    load();
    const timer = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [tick]);
  return { runs, refresh: () => setTick((t) => t + 1) };
}

export function useAgents(): AgentCard[] {
  const [agents, setAgents] = useState<AgentCard[]>([]);
  useEffect(() => {
    api.agents().then(setAgents).catch(() => {});
  }, []);
  return agents;
}

/** Re-render every `ms` while `active`, for things that fade with time (graph link highlights). */
export function useTicker(ms: number, active: boolean): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(timer);
  }, [ms, active]);
  return now;
}
