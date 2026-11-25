"use client";

import { Ban, X } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useMemo, useState } from "react";

import { PageHeader } from "@/components/app-shell";
import { DagView } from "@/components/lazy";
import { topologicalOrder } from "@/lib/dag-layout";
import { ErrorState, LoadingBlock, Skeleton } from "@/components/states";
import { StatusBadge, statusMeta } from "@/components/status";
import { Timeline } from "@/components/timeline";
import { useToast } from "@/components/toast";
import { EventFeed, JsonBlock, RelativeTime, Section } from "@/components/widgets";
import { formatDuration, runDuration, shortId } from "@/lib/format";
import { useCancelRun, useRun } from "@/lib/queries";
import type { RunDetail, Task } from "@/lib/types";
import { useNow } from "@/lib/use-now";

// ------------------------------------------------------------------ run detail

function TaskPanel({ task, run, onClose }: { task: Task; run: RunDetail; onClose: () => void }) {
  const events = useMemo(
    () =>
      run.events
        .filter((e) => e.task_id === task.id)
        .slice()
        .reverse(),
    [run.events, task.id],
  );
  return (
    <aside aria-labelledby="task-panel-title" className="card flex flex-col gap-4 p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 id="task-panel-title" className="font-mono text-base font-semibold">
            {task.node_key}
          </h2>
          <p className="text-xs text-muted">{task.task_type}</p>
        </div>
        <button
          type="button"
          className="rounded p-1 text-muted hover:text-text"
          onClick={onClose}
          aria-label="Close step details"
        >
          <X aria-hidden="true" className="size-4" />
        </button>
      </div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-sm">
        <dt className="text-muted">Status</dt>
        <dd>
          <StatusBadge status={task.status} size="sm" />
        </dd>
        <dt className="text-muted">Attempts</dt>
        <dd className="tabular-nums">
          {task.attempt} of {task.max_attempts}
        </dd>
        <dt className="text-muted">On failure</dt>
        <dd>{task.failure_policy.replaceAll("_", " ")}</dd>
        <dt className="text-muted">Priority</dt>
        <dd className="tabular-nums">
          {task.priority}
          {task.effective_priority !== task.priority ? ` (aged to ${task.effective_priority})` : ""}
        </dd>
        <dt className="text-muted">Fencing token</dt>
        <dd className="font-mono tabular-nums">{task.fencing_token}</dd>
        {task.lease_owner ? (
          <>
            <dt className="text-muted">Leased to</dt>
            <dd className="font-mono">{shortId(task.lease_owner)}</dd>
          </>
        ) : null}
      </dl>
      {task.error ? (
        <div>
          <h3 className="label mb-1">Last error</h3>
          <p
            className="rounded-lg p-2 text-sm"
            style={{ color: "var(--st-failed)", backgroundColor: "var(--st-failed-bg)" }}
          >
            {task.error.message ?? task.error.kind}
          </p>
        </div>
      ) : null}
      {task.result !== null ? (
        <div>
          <h3 className="label mb-1">Result</h3>
          <JsonBlock value={task.result} label={`Result of ${task.node_key}`} />
        </div>
      ) : null}
      <div>
        <h3 className="label mb-1">Params</h3>
        <JsonBlock value={task.params} label={`Params of ${task.node_key}`} />
      </div>
      <div>
        <h3 className="label mb-1">What happened</h3>
        <EventFeed events={events} showRunLinks={false} emptyText="Nothing yet." />
      </div>
    </aside>
  );
}

export function RunDetailView({ id }: { id: string }) {
  const run = useRun(id);
  const cancel = useCancelRun(id);
  const toast = useToast();
  const now = useNow();
  const [selected, setSelected] = useState<string | null>(null);
  const select = useCallback((key: string) => setSelected((s) => (s === key ? null : key)), []);

  if (run.isPending) {
    return (
      <LoadingBlock label="run">
        <Skeleton className="mb-6 h-10 w-80" />
        <Skeleton className="h-80 w-full" />
      </LoadingBlock>
    );
  }
  if (run.isError) return <ErrorState error={run.error} onRetry={() => run.refetch()} />;

  const r = run.data;
  const order = topologicalOrder(r.tasks);
  const task = r.tasks.find((t) => t.node_key === selected) ?? null;
  const cancellable = r.status === "running" || r.status === "failed";
  const counts = r.tasks.reduce<Record<string, number>>(
    (acc, t) => ({ ...acc, [t.status]: (acc[t.status] ?? 0) + 1 }),
    {},
  );

  return (
    <>
      <PageHeader
        title={
          <span className="flex flex-wrap items-center gap-3">
            {r.workflow_name ?? "Standalone job"}
            <StatusBadge status={r.status} />
          </span>
        }
        description={
          <>
            Run <span className="font-mono">{shortId(r.id)}</span> · {r.trigger}
            {r.version ? ` · v${r.version}` : ""} · started <RelativeTime iso={r.started_at} now={now} /> ·{" "}
            {formatDuration(runDuration(r.started_at, r.finished_at, now) ?? NaN)}
            {r.workflow_id ? (
              <>
                {" "}
                ·{" "}
                <Link href={`/workflows/${r.workflow_id}`} className="link">
                  workflow
                </Link>
              </>
            ) : null}
          </>
        }
        actions={
          cancellable ? (
            <button
              type="button"
              className="btn-secondary"
              disabled={cancel.isPending}
              onClick={() =>
                cancel.mutate(undefined, {
                  onSuccess: () => toast("success", "Run cancelled."),
                  onError: (e) => toast("error", e.message),
                })
              }
            >
              <Ban aria-hidden="true" className="size-4" /> {cancel.isPending ? "Cancelling…" : "Cancel run"}
            </button>
          ) : null
        }
      />

      <p className="sr-only" aria-live="polite">
        Run is {statusMeta(r.status).label}.{" "}
        {Object.entries(counts)
          .map(([s, n]) => `${n} ${statusMeta(s).label}`)
          .join(", ")}
        .
      </p>

      <div className={`grid gap-4 ${task ? "lg:grid-cols-[1fr_22rem]" : ""}`}>
        <div className="flex min-w-0 flex-col gap-4">
          <DagView
            label={`Run of ${r.workflow_name ?? "job"}: live step states. Select a step for details.`}
            nodes={r.tasks.map((t) => ({
              key: t.node_key,
              task_type: t.task_type,
              depends_on: t.depends_on,
              status: t.status,
              attempt: t.attempt,
              max_attempts: t.max_attempts,
            }))}
            selectedKey={selected}
            onSelect={select}
            height={360}
          />
          <Section title="Timeline">
            <Timeline
              events={r.events}
              order={order}
              runStart={new Date(r.started_at ?? r.created_at).getTime()}
              now={r.finished_at ? new Date(r.finished_at).getTime() : now}
            />
          </Section>
          <Section title="Event log">
            <div
              className="max-h-96 overflow-y-auto"
              tabIndex={0}
              role="region"
              aria-label="Event log, scrollable"
            >
              <EventFeed events={r.events.slice().reverse()} showRunLinks={false} limit={500} />
            </div>
          </Section>
        </div>
        {task ? <TaskPanel task={task} run={r} onClose={() => setSelected(null)} /> : null}
      </div>
    </>
  );
}

/** Route entry: reads the `[id]` segment (runtime data, so it renders under Suspense). */
export function RunDetailRoute() {
  const { id } = useParams<{ id: string }>();
  return <RunDetailView id={id} />;
}
