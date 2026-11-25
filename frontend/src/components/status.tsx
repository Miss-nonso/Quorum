import {
  Ban,
  CheckCircle2,
  HeartCrack,
  CircleDashed,
  CircleDot,
  Clock3,
  HeartPulse,
  Loader2,
  PowerOff,
  Skull,
  TriangleAlert,
  XCircle,
  type LucideIcon,
} from "lucide-react";

import type { RunStatus, TaskStatus, WorkerStatus } from "@/lib/types";

export type AnyStatus = TaskStatus | RunStatus | WorkerStatus;

interface StatusMeta {
  label: string;
  icon: LucideIcon;
  /** CSS variable suffix, e.g. "succeeded" -> var(--st-succeeded). */
  tone: "pending" | "ready" | "running" | "succeeded" | "failed" | "dead" | "cancelled";
  spin?: boolean;
  description: string;
}

export const STATUS: Record<AnyStatus, StatusMeta> = {
  pending: {
    label: "Waiting",
    icon: CircleDashed,
    tone: "pending",
    description: "Waiting for the steps it depends on.",
  },
  ready: {
    label: "Queued",
    icon: Clock3,
    tone: "ready",
    description: "Ready to run; waiting for a free worker.",
  },
  running: {
    label: "Running",
    icon: Loader2,
    tone: "running",
    spin: true,
    description: "A worker is executing it now.",
  },
  succeeded: {
    label: "Succeeded",
    icon: CheckCircle2,
    tone: "succeeded",
    description: "Finished successfully.",
  },
  failed: { label: "Failed", icon: XCircle, tone: "failed", description: "Stopped with an error." },
  dead: {
    label: "Dead-lettered",
    icon: Skull,
    tone: "dead",
    description: "Out of retries; parked in the dead-letter queue.",
  },
  cancelled: {
    label: "Cancelled",
    icon: Ban,
    tone: "cancelled",
    description: "Cancelled before it finished.",
  },
  active: { label: "Healthy", icon: HeartPulse, tone: "succeeded", description: "Heartbeating normally." },
  unhealthy: {
    label: "Unhealthy",
    icon: TriangleAlert,
    tone: "ready",
    description: "Missed recent heartbeats.",
  },
  stopped: { label: "Stopped", icon: PowerOff, tone: "cancelled", description: "Shut down cleanly." },
};

/** Workers share the word "dead" with tasks but mean something else by it. */
const WORKER_STATUS: Partial<Record<string, StatusMeta>> = {
  dead: {
    label: "Dead",
    icon: HeartCrack,
    tone: "failed",
    description: "Silent for too long; its tasks were handed to other workers.",
  },
};

export type StatusKind = "task" | "worker";

export function statusMeta(status: string, kind: StatusKind = "task"): StatusMeta {
  const special = kind === "worker" ? WORKER_STATUS[status] : undefined;
  return (
    special ??
    STATUS[status as AnyStatus] ?? {
      label: status,
      icon: CircleDot,
      tone: "pending",
      description: status,
    }
  );
}

export function toneVars(tone: StatusMeta["tone"]): React.CSSProperties {
  return { color: `var(--st-${tone})`, backgroundColor: `var(--st-${tone}-bg)` };
}

export function StatusIcon({
  status,
  kind = "task",
  className = "size-4",
}: {
  status: string;
  kind?: StatusKind;
  className?: string;
}) {
  const meta = statusMeta(status, kind);
  const Icon = meta.icon;
  return (
    <Icon
      aria-hidden="true"
      className={`${className} shrink-0 ${meta.spin ? "animate-spin motion-reduce:animate-none" : ""}`}
      style={{ color: `var(--st-${meta.tone})` }}
    />
  );
}

/** A status pill: icon + text + colour, never colour alone. */
export function StatusBadge({
  status,
  kind = "task",
  size = "md",
}: {
  status: string;
  kind?: StatusKind;
  size?: "sm" | "md";
}) {
  const meta = statusMeta(status, kind);
  const Icon = meta.icon;
  const sizing = size === "sm" ? "gap-1 px-1.5 py-0.5 text-xs" : "gap-1.5 px-2 py-1 text-xs";
  return (
    <span
      className={`inline-flex items-center rounded-full font-medium whitespace-nowrap ${sizing}`}
      style={toneVars(meta.tone)}
      title={meta.description}
    >
      <Icon
        aria-hidden="true"
        className={`size-3.5 ${meta.spin ? "animate-spin motion-reduce:animate-none" : ""}`}
      />
      {meta.label}
    </span>
  );
}
