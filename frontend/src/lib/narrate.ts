import { shortId } from "./format";
import type { RunEvent } from "./types";

export type Tone = "info" | "good" | "warn" | "bad";

export interface Narration {
  text: string;
  tone: Tone;
}

const num = (v: unknown) => (typeof v === "number" ? v : Number(v ?? 0));

/**
 * Explain an engine event in plain language, for people who have never
 * heard of leases or fencing tokens. Used by the live feed and run view.
 */
export function narrate(e: RunEvent): Narration {
  const task = e.node_key ? `“${e.node_key}”` : "the task";
  const worker =
    typeof e.payload.worker_id === "string" ? `worker ${shortId(e.payload.worker_id)}` : "a worker";
  const p = e.payload;
  switch (e.type) {
    case "run_created":
      return { text: `A new run started with ${num(p.nodes)} steps.`, tone: "info" };
    case "run_scheduled":
      return { text: `The schedule (${String(p.cron)}) started this run on time.`, tone: "info" };
    case "run_succeeded":
      return { text: "Every step finished. The run succeeded.", tone: "good" };
    case "run_failed":
      return { text: "The run stopped: a step could not complete.", tone: "bad" };
    case "run_resumed":
      return { text: "The run picked up where it stopped.", tone: "info" };
    case "run_cancelled":
      return { text: "The run was cancelled.", tone: "warn" };
    case "task_enqueued":
      return { text: `${task} joined the queue.`, tone: "info" };
    case "task_ready":
      return {
        text: p.unblocked_by
          ? `${task} is ready: everything it depends on has finished.`
          : `${task} is ready to run.`,
        tone: "info",
      };
    case "task_claimed":
      return {
        text:
          num(p.attempt) > 1
            ? `${worker} picked up ${task} (attempt ${num(p.attempt)}).`
            : `${worker} picked up ${task}.`,
        tone: "info",
      };
    case "task_succeeded":
      return { text: `${task} finished successfully.`, tone: "good" };
    case "task_retry_scheduled":
      return {
        text: `${task} failed (attempt ${num(p.attempt)} of ${num(p.max_attempts)}); retrying in ${num(p.delay_s).toFixed(1)} s.`,
        tone: "warn",
      };
    case "task_dead_lettered":
      return {
        text: `${task} ran out of retries and moved to the dead-letter queue for a human to look at.`,
        tone: "bad",
      };
    case "task_failed":
      return { text: `${task} failed permanently.`, tone: "bad" };
    case "task_cancelled":
      return { text: `${task} was cancelled.`, tone: "warn" };
    case "task_released":
      return { text: `${worker} shut down cleanly and handed ${task} back.`, tone: "info" };
    case "task_replayed":
      return { text: `${task} was replayed from the dead-letter queue.`, tone: "info" };
    case "lease_expired":
      return {
        text: `${worker} went silent, so its claim on ${task} expired. The task goes back in the queue for another worker.`,
        tone: "warn",
      };
    case "lease_lost":
      return {
        text: `${worker} noticed it no longer owns ${task} and stopped working on it.`,
        tone: "warn",
      };
    case "chaos_worker_killed":
      return {
        text: `The demo crashed ${worker} while it was running ${task}. Its claim will expire, and another worker will pick the task up.`,
        tone: "bad",
      };
    case "stale_write_rejected":
      return {
        text: `A late result for ${task} from ${worker} was rejected: another worker had already taken over.`,
        tone: "warn",
      };
    default:
      return { text: `${e.type.replaceAll("_", " ")}${e.node_key ? ` (${e.node_key})` : ""}`, tone: "info" };
  }
}
