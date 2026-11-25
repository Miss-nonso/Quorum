"use client";

import { Ban, X } from "lucide-react";
import Link from "next/link";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo, useState } from "react";

import { PageHeader } from "@/components/app-shell";
import { DagView } from "@/components/dag-view";
import { EmptyState, ErrorState, LoadingBlock, Skeleton } from "@/components/states";
import { StatusBadge, statusMeta } from "@/components/status";
import { Timeline } from "@/components/timeline";
import { useToast } from "@/components/toast";
import { EventFeed, JsonBlock, Pagination, RelativeTime, Section } from "@/components/widgets";
import { formatDuration, runDuration, shortId } from "@/lib/format";
import { useCancelRun, useRun, useRuns } from "@/lib/queries";
import type { RunDetail, Task } from "@/lib/types";
import { useNow } from "@/lib/use-now";

// ------------------------------------------------------------------ history

const STATUSES = ["running", "succeeded", "failed", "cancelled"] as const;
const TRIGGERS = ["manual", "schedule", "api", "demo"] as const;
const PAGE_SIZE = 20;

export function RunsView() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const status = params.get("status") ?? "";
  const trigger = params.get("trigger") ?? "";
  const offset = Math.max(0, Number(params.get("offset") ?? 0) || 0);
  const runs = useRuns({
    status: status || undefined,
    trigger: trigger || undefined,
    limit: PAGE_SIZE,
    offset,
  });
  const now = useNow();

  // Filters live in the URL: shareable, and Back works.
  const update = useCallback(
    (changes: Record<string, string | number | null>) => {
      const next = new URLSearchParams(params.toString());
      for (const [k, v] of Object.entries(changes)) {
        if (v === null || v === "" || v === 0) next.delete(k);
        else next.set(k, String(v));
      }
      router.replace(`${pathname}${next.size ? `?${next}` : ""}`, { scroll: false });
    },
    [params, pathname, router],
  );

  return (
    <>
      <PageHeader
        title="History"
        description="Every run, newest first. Filter by outcome or by what started it."
      />
      <div className="card p-4">
        <form
          className="mb-4 flex flex-wrap items-end gap-3"
          onSubmit={(e) => e.preventDefault()}
          aria-label="Filter runs"
        >
          <label className="flex flex-col gap-1 text-sm">
            <span className="label">Status</span>
            <select
              className="field"
              value={status}
              onChange={(e) => update({ status: e.target.value, offset: null })}
            >
              <option value="">Any status</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {statusMeta(s).label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="label">Trigger</span>
            <select
              className="field"
              value={trigger}
              onChange={(e) => update({ trigger: e.target.value, offset: null })}
            >
              <option value="">Any trigger</option>
              {TRIGGERS.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          {status || trigger ? (
            <button
              type="button"
              className="btn-secondary"
              onClick={() => update({ status: null, trigger: null, offset: null })}
            >
              <X aria-hidden="true" className="size-4" /> Clear filters
            </button>
          ) : null}
        </form>

        {runs.isPending ? (
          <LoadingBlock label="runs">
            <div className="flex flex-col gap-2">
              {Array.from({ length: 8 }, (_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          </LoadingBlock>
        ) : runs.isError ? (
          <ErrorState error={runs.error} onRetry={() => runs.refetch()} />
        ) : runs.data.items.length === 0 ? (
          <EmptyState
            title={status || trigger ? "No runs match these filters" : "No runs yet"}
            description={
              status || trigger ? "Try clearing the filters." : "Runs appear here as soon as they start."
            }
          />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">Runs, newest first</caption>
                <thead className="label">
                  <tr className="border-b border-border">
                    <th scope="col" className="py-2 pr-4 font-medium">
                      Run
                    </th>
                    <th scope="col" className="py-2 pr-4 font-medium">
                      Status
                    </th>
                    <th scope="col" className="py-2 pr-4 font-medium">
                      Workflow
                    </th>
                    <th scope="col" className="py-2 pr-4 font-medium">
                      Trigger
                    </th>
                    <th scope="col" className="py-2 pr-4 font-medium">
                      Started
                    </th>
                    <th scope="col" className="py-2 text-right font-medium">
                      Duration
                    </th>
                  </tr>
                </thead>
                <tbody aria-busy={runs.isFetching}>
                  {runs.data.items.map((r) => (
                    <tr key={r.id} className="border-b border-border last:border-0 hover:bg-surface-2">
                      <td className="py-2 pr-4">
                        <Link href={`/runs/${r.id}`} className="link font-mono text-xs">
                          {shortId(r.id)}
                        </Link>
                      </td>
                      <td className="py-2 pr-4">
                        <StatusBadge status={r.status} size="sm" />
                      </td>
                      <td className="py-2 pr-4">
                        {r.workflow_name ?? <span className="text-muted">Standalone job</span>}
                      </td>
                      <td className="py-2 pr-4 text-muted">{r.trigger}</td>
                      <td className="py-2 pr-4 text-muted">
                        <RelativeTime iso={r.started_at ?? r.created_at} now={now} />
                      </td>
                      <td className="py-2 text-right tabular-nums">
                        {formatDuration(runDuration(r.started_at, r.finished_at, now) ?? NaN)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              offset={offset}
              limit={PAGE_SIZE}
              total={runs.data.total}
              onChange={(o) => update({ offset: o })}
            />
          </>
        )}
      </div>
    </>
  );
}

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

function topoOrder(tasks: Task[]): string[] {
  const byKey = new Map(tasks.map((t) => [t.node_key, t]));
  const out: string[] = [];
  const seen = new Set<string>();
  const visit = (key: string) => {
    if (seen.has(key)) return;
    seen.add(key);
    byKey.get(key)?.depends_on.forEach(visit);
    out.push(key);
  };
  tasks.forEach((t) => visit(t.node_key));
  return out;
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
  const order = topoOrder(r.tasks);
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
            <div className="max-h-96 overflow-y-auto">
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
