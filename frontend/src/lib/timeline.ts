import type { RunEvent } from "./types";

/** How an attempt ended. `running` means it has not ended yet. */
export type AttemptOutcome =
  "running" | "succeeded" | "retried" | "dead" | "failed" | "lease_expired" | "cancelled" | "released";

export interface Attempt {
  nodeKey: string;
  attempt: number;
  workerId: string | null;
  start: number; // epoch ms
  end: number; // epoch ms (now, if still running)
  outcome: AttemptOutcome;
}

const ENDINGS: Record<string, AttemptOutcome> = {
  task_succeeded: "succeeded",
  task_retry_scheduled: "retried",
  task_dead_lettered: "dead",
  task_failed: "failed",
  lease_expired: "lease_expired",
  task_cancelled: "cancelled",
  task_released: "released",
};

/**
 * Turn a run's event log into attempt bars for a Gantt chart: each
 * ``task_claimed`` starts a bar, the task's next ending event closes it.
 * A lease expiry is followed by a retry/dead-letter event for the same
 * attempt; the expiry is what ended the bar, so the follow-up is ignored.
 */
export function attemptsFromEvents(events: RunEvent[], now: number = Date.now()): Attempt[] {
  const open = new Map<string, Attempt>();
  const done: Attempt[] = [];
  const sorted = [...events].sort((a, b) => a.id - b.id);
  for (const e of sorted) {
    if (!e.task_id || !e.node_key) continue;
    const at = new Date(e.at).getTime();
    if (e.type === "task_claimed") {
      open.set(e.task_id, {
        nodeKey: e.node_key,
        attempt: Number(e.payload.attempt ?? 1),
        workerId: typeof e.payload.worker_id === "string" ? e.payload.worker_id : null,
        start: at,
        end: at,
        outcome: "running",
      });
      continue;
    }
    const outcome = ENDINGS[e.type];
    const current = open.get(e.task_id);
    if (outcome && current) {
      done.push({ ...current, end: Math.max(at, current.start), outcome });
      open.delete(e.task_id);
    }
  }
  for (const attempt of open.values()) done.push({ ...attempt, end: Math.max(now, attempt.start) });
  return done.sort((a, b) => a.start - b.start);
}

/** The time range a timeline should cover. */
export function timelineBounds(attempts: Attempt[], fallbackStart: number): [number, number] {
  if (!attempts.length) return [fallbackStart, fallbackStart + 1000];
  const start = Math.min(...attempts.map((a) => a.start));
  const end = Math.max(...attempts.map((a) => a.end));
  return [start, Math.max(end, start + 1000)];
}
