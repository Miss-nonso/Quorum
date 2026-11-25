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
 *
 * Presence: when the tab is hidden (30 s) or nobody has touched it for
 * 10 minutes, the stream closes and background polling pauses. On free
 * hosting that lets the server and database scale to zero instead of being
 * kept awake by a forgotten tab. Any interaction resumes it, replaying what
 * was missed (`after=<last event id>`).
 */

import { focusManager, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { API_URL } from "./api";
import type { RunEvent, Stats, Worker } from "./types";

export type ConnectionState = "connecting" | "live" | "reconnecting" | "offline" | "paused";

interface LiveContextValue {
  state: ConnectionState;
  events: RunEvent[];
  /** Server clock minus browser clock, in ms (for heartbeat ages). */
  clockSkewMs: number;
  /** Never connected yet and still trying: a sleeping free-tier server waking up. */
  wakingUp: boolean;
}

const LiveContext = createContext<LiveContextValue>({
  state: "connecting",
  events: [],
  clockSkewMs: 0,
  wakingUp: false,
});

const FEED_SIZE = 60;
const OFFLINE_AFTER_MS = 15_000;
const WAKING_AFTER_MS = 4_000;
const IDLE_AFTER_MS = 10 * 60_000;
const HIDDEN_AFTER_MS = 30_000;

/** False once the tab is hidden for a while or idle for `idleMs`; true again on any interaction. */
export function usePresence(idleMs = IDLE_AFTER_MS, hiddenMs = HIDDEN_AFTER_MS): boolean {
  const [present, setPresent] = useState(true);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const away = (after: number) => {
      clearTimeout(timer);
      timer = setTimeout(() => setPresent(false), after);
    };
    const active = () => {
      if (document.visibilityState === "hidden") return;
      setPresent(true);
      away(idleMs);
    };
    const visibility = () => (document.visibilityState === "hidden" ? away(hiddenMs) : active());
    const events = ["pointerdown", "pointermove", "keydown", "scroll", "touchstart"] as const;
    events.forEach((e) => window.addEventListener(e, active, { passive: true }));
    document.addEventListener("visibilitychange", visibility);
    visibility();
    return () => {
      clearTimeout(timer);
      events.forEach((e) => window.removeEventListener(e, active));
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [idleMs, hiddenMs]);
  return present;
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<ConnectionState>("connecting");
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [clockSkewMs, setClockSkewMs] = useState(0);
  const [everConnected, setEverConnected] = useState(false);
  const [slowStart, setSlowStart] = useState(false);
  const present = usePresence();
  const lastEventId = useRef<number | null>(null);
  const pending = useRef<{ runs: Set<string>; lists: boolean; dlq: boolean }>({
    runs: new Set(),
    lists: false,
    dlq: false,
  });

  // Pause background polling while the visitor is away (TanStack treats the
  // window as unfocused); undefined restores its normal focus tracking.
  useEffect(() => {
    focusManager.setFocused(present ? undefined : false);
  }, [present]);

  useEffect(() => {
    const id = setTimeout(() => setSlowStart(true), WAKING_AFTER_MS);
    return () => clearTimeout(id);
  }, []);

  useEffect(() => {
    if (typeof EventSource === "undefined" || !present) return;
    // Fresh page: the last few events. Resuming after a pause: everything missed.
    // (Automatic reconnects inside one EventSource resume via Last-Event-ID.)
    const query = lastEventId.current === null ? "recent=25" : `after=${lastEventId.current}`;
    const source = new EventSource(`${API_URL}/api/v1/stream?${query}`);
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
      setEverConnected(true);
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
      lastEventId.current = Math.max(lastEventId.current ?? 0, event.id);
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
  }, [queryClient, present]);

  const shown: ConnectionState = present ? state : "paused";
  const wakingUp = present && slowStart && !everConnected;
  const value = useMemo(
    () => ({ state: shown, events, clockSkewMs, wakingUp }),
    [shown, events, clockSkewMs, wakingUp],
  );
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveContextValue {
  return useContext(LiveContext);
}
