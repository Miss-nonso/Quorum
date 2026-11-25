"use client";

import { Crown } from "lucide-react";
import Link from "next/link";

import { PageHeader } from "@/components/app-shell";
import { ErrorState, LoadingBlock, Skeleton } from "@/components/states";
import { StatusBadge } from "@/components/status";
import { EventFeed, RelativeTime, Section, StatCard, ThroughputChart } from "@/components/widgets";
import { formatDuration, runDuration, shortId } from "@/lib/format";
import { useLive } from "@/lib/live";
import { useRuns, useStats, useWorkers } from "@/lib/queries";
import { useNow } from "@/lib/use-now";

export function OverviewView() {
  const stats = useStats();
  const workers = useWorkers();
  const runs = useRuns({ limit: 6 });
  const { events } = useLive();
  const now = useNow();
  const t = stats.data?.tasks;

  return (
    <>
      <PageHeader
        title="Overview"
        description="Live state of the queue, the workers and recent runs. Everything updates in real time."
      />
      {stats.isError ? <ErrorState error={stats.error} onRetry={() => stats.refetch()} /> : null}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <StatCard label="Queued" value={t?.queued} status="ready" hint="Ready, waiting for a worker" />
        <StatCard label="Running" value={t?.running} status="running" hint="Leased to a worker now" />
        <StatCard label="Succeeded" value={t?.succeeded} status="succeeded" hint="Finished successfully" />
        <StatCard label="Failed" value={t?.failed} status="failed" hint="Failed permanently" />
        <StatCard
          label="Dead-lettered"
          value={t?.dead}
          status="dead"
          hint="Out of retries, awaiting replay"
        />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <div className="flex flex-col gap-4 lg:col-span-2">
          <Section title="Throughput (last 5 minutes)">
            {stats.data ? (
              <ThroughputChart stats={stats.data} />
            ) : (
              <LoadingBlock label="throughput">
                <Skeleton className="h-44 w-full" />
              </LoadingBlock>
            )}
          </Section>

          <Section
            title="Recent runs"
            actions={
              <Link href="/runs" className="link text-sm">
                View history
              </Link>
            }
          >
            {runs.isPending ? (
              <LoadingBlock label="recent runs">
                <div className="flex flex-col gap-2">
                  {Array.from({ length: 4 }, (_, i) => (
                    <Skeleton key={i} className="h-9 w-full" />
                  ))}
                </div>
              </LoadingBlock>
            ) : runs.isError ? (
              <ErrorState error={runs.error} onRetry={() => runs.refetch()} />
            ) : runs.data.items.length === 0 ? (
              <p className="text-sm text-muted">
                No runs yet.{" "}
                <Link href="/workflows" className="link">
                  Start one from a workflow
                </Link>
                .
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {runs.data.items.map((r) => (
                  <li key={r.id}>
                    <Link
                      href={`/runs/${r.id}`}
                      className="-mx-2 flex items-center gap-3 rounded-lg px-2 py-2 text-sm hover:bg-surface-2"
                    >
                      <StatusBadge status={r.status} size="sm" />
                      <span className="min-w-0 flex-1 truncate font-medium">
                        {r.workflow_name ?? "Standalone job"}
                        <span className="ml-2 font-mono text-xs font-normal text-muted">{shortId(r.id)}</span>
                      </span>
                      <span className="hidden text-xs text-muted sm:inline">{r.trigger}</span>
                      <span className="w-20 text-right text-xs text-muted tabular-nums">
                        {formatDuration(runDuration(r.started_at, r.finished_at, now) ?? NaN)}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </div>

        <div className="flex flex-col gap-4">
          <Section
            title="Workers"
            actions={
              <Link href="/workers" className="link text-sm">
                Details
              </Link>
            }
          >
            {workers.data ? (
              workers.data.length === 0 ? (
                <p className="text-sm text-muted">
                  No workers are running. Start one with <code className="font-mono">conductor worker</code>.
                </p>
              ) : (
                <ul className="flex flex-col gap-2">
                  {workers.data.slice(0, 6).map((w) => (
                    <li key={w.id} className="flex items-center gap-2 text-sm">
                      <StatusBadge status={w.status} kind="worker" size="sm" />
                      <span className="min-w-0 flex-1 truncate font-mono text-xs">
                        {w.hostname}:{w.pid}
                      </span>
                      <span className="text-xs text-muted tabular-nums">
                        {w.current_load}/{w.concurrency} busy
                      </span>
                    </li>
                  ))}
                </ul>
              )
            ) : (
              <LoadingBlock label="workers">
                <Skeleton className="h-16 w-full" />
              </LoadingBlock>
            )}
            {stats.data?.scheduler ? (
              <p className="mt-3 flex items-center gap-1.5 border-t border-border pt-3 text-xs text-muted">
                <Crown aria-hidden="true" className="size-3.5" />
                Scheduler leader {stats.data.scheduler.hostname}:{stats.data.scheduler.pid}, elected{" "}
                <RelativeTime iso={stats.data.scheduler.elected_at} now={now} />
                {stats.data.scheduler.healthy ? "" : " (heartbeat overdue)"}
              </p>
            ) : null}
          </Section>

          <Section title="Live events">
            <div aria-live="polite" aria-relevant="additions" className="max-h-[26rem] overflow-y-auto">
              <EventFeed events={events} limit={25} />
            </div>
          </Section>
        </div>
      </div>
    </>
  );
}
