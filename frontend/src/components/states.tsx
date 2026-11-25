import { AlertCircle, Inbox, RotateCw } from "lucide-react";
import type { ReactNode } from "react";

import { ApiError } from "@/lib/api";

export function Skeleton({ className = "h-4 w-full" }: { className?: string }) {
  return <div aria-hidden="true" className={`skeleton ${className}`} />;
}

/** A labelled loading region: screen readers hear "Loading …", sighted users see shapes. */
export function LoadingBlock({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div role="status" aria-live="polite">
      <span className="sr-only">Loading {label}…</span>
      {children}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
  icon = <Inbox aria-hidden="true" className="size-6 text-muted" />,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border px-6 py-10 text-center">
      {icon}
      <p className="font-medium">{title}</p>
      {description ? <p className="max-w-md text-sm text-muted">{description}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const isApi = error instanceof ApiError;
  const unreachable = isApi && error.code === "network_error";
  return (
    <div
      role="alert"
      className="flex flex-col items-center gap-2 rounded-xl border px-6 py-8 text-center"
      style={{ borderColor: "var(--st-failed)", backgroundColor: "var(--st-failed-bg)" }}
    >
      <AlertCircle aria-hidden="true" className="size-6" style={{ color: "var(--st-failed)" }} />
      <p className="font-medium">{unreachable ? "Can't reach the API" : "Something went wrong"}</p>
      <p className="max-w-md text-sm text-muted">
        {unreachable
          ? "The Conductor API isn't responding. It may be restarting; this page retries automatically."
          : error instanceof Error
            ? error.message
            : "An unexpected error occurred."}
      </p>
      {isApi && error.requestId ? (
        <p className="font-mono text-xs text-muted">request id {error.requestId}</p>
      ) : null}
      {onRetry ? (
        <button type="button" onClick={onRetry} className="btn-secondary mt-2">
          <RotateCw aria-hidden="true" className="size-4" /> Try again
        </button>
      ) : null}
    </div>
  );
}
