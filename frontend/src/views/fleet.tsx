"use client";

import { RotateCcw, Server } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { PageHeader } from "@/components/app-shell";
import { EmptyState, ErrorState, LoadingBlock, Skeleton } from "@/components/states";
import { StatusBadge } from "@/components/status";
import { useToast } from "@/components/toast";
import { JsonBlock, Pagination, RelativeTime } from "@/components/widgets";
import { shortId } from "@/lib/format";
import { useDeadLetters, useReplay, useWorkers } from "@/lib/queries";
import type { Worker } from "@/lib/types";
import { useNow } from "@/lib/use-now";

// ------------------------------------------------------------------ workers

function HeartbeatAge({ seconds }: { seconds: number }) {
  const tone = seconds < 10 ? "succeeded" : seconds < 30 ? "ready" : "failed";
  return (
    <span className="tabular-nums" style={{ color: `var(--st-${tone})` }}>
      {seconds < 1 ? "<1 s" : `${Math.round(seconds)} s`} ago
    </span>
  );
}

function WorkerCard({ worker, now, snapshotAt }: { worker: Worker; now: number; snapshotAt: number }) {
  // Age = server-computed age at snapshot time + time since the snapshot arrived.
  const age = worker.heartbeat_age_s + Math.max(0, (now - snapshotAt) / 1000);
  const load = worker.concurrency ? worker.current_load / worker.concurrency : 0;
  return (
    <li className="card flex flex-col gap-3 p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 truncate font-mono text-sm font-semibold">
            <Server aria-hidden="true" className="size-4 text-muted" />
            {worker.hostname}:{worker.pid}
          </h2>
          <p className="font-mono text-xs text-muted">id {shortId(worker.id)}</p>
        </div>
        <StatusBadge status={worker.status} kind="worker" size="sm" />
      </div>
      <dl className="grid grid-cols-2 gap-y-1 text-sm">
        <dt className="text-muted">Last heartbeat</dt>
        <dd className="text-right">
          <HeartbeatAge seconds={age} />
        </dd>
        <dt className="text-muted">Up since</dt>
        <dd className="text-right">
          <RelativeTime iso={worker.started_at} now={now} />
        </dd>
      </dl>
      <div>
        <div className="mb-1 flex justify-between text-xs text-muted">
          <span id={`load-${worker.id}`}>Load</span>
          <span className="tabular-nums">
            {worker.current_load} of {worker.concurrency} slots
          </span>
        </div>
        <div
          role="progressbar"
          aria-labelledby={`load-${worker.id}`}
          aria-valuemin={0}
          aria-valuemax={worker.concurrency}
          aria-valuenow={worker.current_load}
          className="h-2 overflow-hidden rounded-full bg-surface-2"
        >
          <div
            className="h-full rounded-full bg-accent transition-[width]"
            style={{ width: `${Math.min(100, load * 100)}%` }}
          />
        </div>
      </div>
      <div>
        <h3 className="label mb-1">Working on</h3>
        {worker.current_tasks.length ? (
          <ul className="flex flex-col gap-1 text-sm">
            {worker.current_tasks.map((t) => (
              <li key={t.task_id}>
                <Link href={`/runs/${t.run_id}`} className="link font-mono text-xs">
                  {t.node_key}
                </Link>{" "}
                <span className="text-xs text-muted">
                  {t.task_type}
                  {t.attempt > 1 ? ` · attempt ${t.attempt}` : ""}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted">Idle</p>
        )}
      </div>
    </li>
  );
}

export function WorkersView() {
  const workers = useWorkers();
  const now = useNow();
  // Ages are computed by the server, so browser clock skew doesn't affect them.
  const snapshotAt = workers.dataUpdatedAt;

  return (
    <>
      <PageHeader
        title="Workers"
        description="Each worker heartbeats every few seconds and renews the leases on its tasks. Miss enough heartbeats and its tasks are recovered by another worker."
      />
      {workers.isPending ? (
        <LoadingBlock label="workers">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-56 w-full" />
            ))}
          </div>
        </LoadingBlock>
      ) : workers.isError ? (
        <ErrorState error={workers.error} onRetry={() => workers.refetch()} />
      ) : workers.data.length === 0 ? (
        <EmptyState
          title="No workers running"
          description={
            <>
              Start one with <code className="font-mono">quorum worker</code> or{" "}
              <code className="font-mono">docker compose up --scale worker=3</code>.
            </>
          }
        />
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {workers.data.map((w) => (
            <WorkerCard key={w.id} worker={w} now={now} snapshotAt={snapshotAt} />
          ))}
        </ul>
      )}
    </>
  );
}

// ------------------------------------------------------------------ dead letters

export function DeadLettersView() {
  const [offset, setOffset] = useState(0);
  const letters = useDeadLetters(offset);
  const replay = useReplay();
  const toast = useToast();
  const now = useNow(5000);

  return (
    <>
      <PageHeader
        title="Dead-letter queue"
        description="Tasks that ran out of retries. Inspect what went wrong, fix the cause, then replay. Replays keep the idempotency key, so side effects that already happened aren't repeated."
      />
      {letters.isPending ? (
        <LoadingBlock label="dead letters">
          <div className="flex flex-col gap-2">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-20 w-full" />
            ))}
          </div>
        </LoadingBlock>
      ) : letters.isError ? (
        <ErrorState error={letters.error} onRetry={() => letters.refetch()} />
      ) : letters.data.items.length === 0 ? (
        <EmptyState
          title="The dead-letter queue is empty"
          description="Nothing has exhausted its retries. When something does, it lands here for you to inspect and replay."
        />
      ) : (
        <>
          <ul className="flex flex-col gap-3">
            {letters.data.items.map((l) => (
              <li key={l.id} className="card p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h2 className="font-mono text-sm font-semibold">
                      {l.node_key}
                      <span className="ml-2 font-sans text-xs font-normal text-muted">
                        {l.task_type} · {l.workflow_name ?? "standalone job"} ·{" "}
                        <Link href={`/runs/${l.run_id}`} className="link">
                          run {shortId(l.run_id)}
                        </Link>
                      </span>
                    </h2>
                    <p className="mt-1 text-sm">{l.reason}</p>
                    <p className="text-xs text-muted">
                      Dead-lettered <RelativeTime iso={l.created_at} now={now} /> after {l.attempt} attempt
                      {l.attempt === 1 ? "" : "s"}
                    </p>
                  </div>
                  <button
                    type="button"
                    className="btn-primary"
                    disabled={replay.isPending && replay.variables?.id === l.id}
                    onClick={() =>
                      replay.mutate(l, {
                        onSuccess: () => toast("success", `Replaying ${l.node_key}. It's back in the queue.`),
                        onError: (e) => toast("error", `Replay failed: ${e.message}`),
                      })
                    }
                  >
                    <RotateCcw aria-hidden="true" className="size-4" /> Replay
                    <span className="sr-only"> {l.node_key}</span>
                  </button>
                </div>
                <details className="mt-3">
                  <summary className="cursor-pointer text-sm text-muted">Error and payload</summary>
                  <div className="mt-2 grid gap-3 md:grid-cols-2">
                    <div>
                      <h3 className="label mb-1">Error</h3>
                      <JsonBlock value={l.payload.error} label={`Error for ${l.node_key}`} />
                    </div>
                    <div>
                      <h3 className="label mb-1">Params</h3>
                      <JsonBlock value={l.payload.params} label={`Params for ${l.node_key}`} />
                    </div>
                  </div>
                </details>
              </li>
            ))}
          </ul>
          <Pagination offset={offset} limit={20} total={letters.data.total} onChange={setOffset} />
        </>
      )}
    </>
  );
}
