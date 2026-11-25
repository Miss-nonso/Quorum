"use client";

/**
 * One EventSource for the whole app. It feeds TanStack Query's cache:
 *
 * - `stats` / `workers` snapshots are written straight into the cache;
 * - `run_event`s are kept in a short in-memory feed and invalidate the
 *   queries they affect (batched, so a burst of events causes one refetch).
 *
 * EventSource reconnects on its own and re-sends `Last-Event-ID`, and the
 * server replays anything missed, so the feed has no gaps after a drop.
 */

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { API_URL } from "./api";
import type { RunEvent, Stats, Worker } from "./types";

export type ConnectionState = "connecting" | "live" | "reconnecting" | "offline";

interface LiveContextValue {
  state: ConnectionState;
  events: RunEvent[];
  /** Server clock minus browser clock, in ms (for heartbeat ages). */
  clockSkewMs: number;
}

const LiveContext = createContext<LiveContextValue>({ state: "connecting", events: [], clockSkewMs: 0 });

const FEED_SIZE = 60;
const OFFLINE_AFTER_MS = 15_000;

export function LiveProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<ConnectionState>("connecting");
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [clockSkewMs, setClockSkewMs] = useState(0);
  const pending = useRef<{ runs: Set<string>; lists: boolean; dlq: boolean }>({
    runs: new Set(),
    lists: false,
    dlq: false,
  });

  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    // `recent` only applies to a fresh connect; reconnects resume via Last-Event-ID.
    const source = new EventSource(`${API_URL}/api/v1/stream?recent=25`);
    let offlineTimer: ReturnType<typeof setTimeout> | undefined;
    let flushTimer: ReturnType<typeof setTimeout> | undefined;

    const flush = () => {
      flushTimer = undefined;
      const { runs, lists, dlq } = pending.current;
      runs.forEach((id) => queryClient.invalidateQueries({ queryKey: ["run", id] }));
      if (lists) queryClient.invalidateQueries({ queryKey: ["runs"] });
      if (dlq) queryClient.invalidateQueries({ queryKey: ["dead-letters"] });
      pending.current = { runs: new Set(), lists: false, dlq: false };
    };
    const schedule = () => {
      flushTimer ??= setTimeout(flush, 250);
    };

    source.onopen = () => {
      clearTimeout(offlineTimer);
      setState("live");
    };
    source.onerror = () => {
      // EventSource retries by itself; show "offline" only if it keeps failing.
      setState((s) => (s === "offline" ? s : "reconnecting"));
      clearTimeout(offlineTimer);
      offlineTimer = setTimeout(() => setState("offline"), OFFLINE_AFTER_MS);
    };
    source.addEventListener("hello", (e) => {
      const { server_time } = JSON.parse((e as MessageEvent).data) as { server_time: string };
      setClockSkewMs(new Date(server_time).getTime() - Date.now());
    });
    source.addEventListener("stats", (e) => {
      queryClient.setQueryData<Stats>(["stats"], JSON.parse((e as MessageEvent).data));
    });
    source.addEventListener("workers", (e) => {
      queryClient.setQueryData<Worker[]>(["workers"], JSON.parse((e as MessageEvent).data));
    });
    source.addEventListener("run_event", (e) => {
      const event = JSON.parse((e as MessageEvent).data) as RunEvent;
      setEvents((prev) => {
        if (prev.length && prev[0].id >= event.id) return prev; // replayed duplicate
        return [event, ...prev].slice(0, FEED_SIZE);
      });
      pending.current.runs.add(event.run_id);
      if (event.type.startsWith("run_") || event.type === "task_enqueued") pending.current.lists = true;
      if (event.type === "task_dead_lettered" || event.type === "task_replayed") pending.current.dlq = true;
      schedule();
    });

    return () => {
      clearTimeout(offlineTimer);
      clearTimeout(flushTimer);
      source.close();
    };
  }, [queryClient]);

  const value = useMemo(() => ({ state, events, clockSkewMs }), [state, events, clockSkewMs]);
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveContextValue {
  return useContext(LiveContext);
}
