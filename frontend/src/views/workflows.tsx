"use client";

import { CalendarClock, Play } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import { PageHeader } from "@/components/app-shell";
import { DagView } from "@/components/lazy";
import { EmptyState, ErrorState, LoadingBlock, Skeleton } from "@/components/states";
import { StatusBadge } from "@/components/status";
import { useToast } from "@/components/toast";
import { RelativeTime, Section } from "@/components/widgets";
import { formatDuration, formatUntil, runDuration, shortId } from "@/lib/format";
import { useMeta, useRuns, useTriggerRun, useWorkflow, useWorkflows } from "@/lib/queries";
import type { Workflow } from "@/lib/types";
import { useNow } from "@/lib/use-now";

function RunButton({
  workflow,
  variant = "primary",
}: {
  workflow: Workflow;
  variant?: "primary" | "secondary";
}) {
  const trigger = useTriggerRun();
  const router = useRouter();
  const toast = useToast();
  const meta = useMeta();
  // The public demo only runs the sample pipeline (in your sandbox).
  if (meta.data?.demo_mode && workflow.name !== "order-pipeline") return null;
  return (
    <button
      type="button"
      className={variant === "primary" ? "btn-primary" : "btn-secondary"}
      disabled={trigger.isPending}
      onClick={() =>
        trigger.mutate(workflow.id, {
          onSuccess: (run) => router.push(`/runs/${run.id}`),
          onError: (error) => toast("error", `Couldn't start ${workflow.name}: ${error.message}`),
        })
      }
    >
      <Play aria-hidden="true" className="size-4" />
      {trigger.isPending ? "Starting…" : "Run now"}
      <span className="sr-only"> {workflow.name}</span>
    </button>
  );
}

function Schedule({ workflow }: { workflow: Workflow }) {
  if (!workflow.cron) return <span className="text-muted">Manual only</span>;
  return (
    <span className="inline-flex items-center gap-1.5">
      <CalendarClock aria-hidden="true" className="size-3.5 text-muted" />
      <code className="font-mono">{workflow.cron}</code>
      <span className="text-muted">({workflow.timezone})</span>
      {workflow.next_fire_at ? (
        <span className="text-muted">· next {formatUntil(workflow.next_fire_at)}</span>
      ) : null}
    </span>
  );
}

export function WorkflowsView() {
  const workflows = useWorkflows();
  return (
    <>
      <PageHeader
        title="Workflows"
        description="Each workflow is a validated DAG of built-in tasks. Runs are versioned snapshots of the workflow."
      />
      {workflows.isPending ? (
        <LoadingBlock label="workflows">
          <div className="grid gap-3 md:grid-cols-2">
            {Array.from({ length: 2 }, (_, i) => (
              <Skeleton key={i} className="h-36 w-full" />
            ))}
          </div>
        </LoadingBlock>
      ) : workflows.isError ? (
        <ErrorState error={workflows.error} onRetry={() => workflows.refetch()} />
      ) : workflows.data.items.length === 0 ? (
        <EmptyState
          title="No workflows yet"
          description={
            <>
              Create one with <code className="font-mono">POST /api/v1/workflows</code>, or run{" "}
              <code className="font-mono">conductor seed</code> to load the sample pipeline.
            </>
          }
        />
      ) : (
        <ul className="grid gap-3 md:grid-cols-2">
          {workflows.data.items.map((w) => (
            <li key={w.id} className="card flex flex-col gap-3 p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="truncate text-base font-semibold">
                    <Link href={`/workflows/${w.id}`} className="hover:underline">
                      {w.name}
                    </Link>
                  </h2>
                  <p className="text-xs text-muted">
                    v{w.version} · {w.definition.nodes.length} steps · {w.enabled ? "enabled" : "disabled"}
                  </p>
                </div>
                <RunButton workflow={w} variant="secondary" />
              </div>
              <p className="text-sm">
                <Schedule workflow={w} />
              </p>
              <p className="flex flex-wrap gap-1.5">
                {w.definition.nodes.map((n) => (
                  <span key={n.key} className="rounded-md bg-surface-2 px-1.5 py-0.5 font-mono text-xs">
                    {n.key}
                  </span>
                ))}
              </p>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

export function WorkflowDetailView({ id }: { id: string }) {
  const workflow = useWorkflow(id);
  const runs = useRuns({ workflow_id: id, limit: 10 });
  const now = useNow();

  if (workflow.isPending) {
    return (
      <LoadingBlock label="workflow">
        <Skeleton className="mb-6 h-10 w-72" />
        <Skeleton className="h-80 w-full" />
      </LoadingBlock>
    );
  }
  if (workflow.isError) return <ErrorState error={workflow.error} onRetry={() => workflow.refetch()} />;
  const w = workflow.data;

  return (
    <>
      <PageHeader
        title={w.name}
        description={<Schedule workflow={w} />}
        actions={<RunButton workflow={w} />}
      />
      <DagView
        label={`Workflow ${w.name}, ${w.definition.nodes.length} steps`}
        nodes={w.definition.nodes.map((n) => ({
          key: n.key,
          task_type: n.task_type,
          depends_on: n.depends_on,
        }))}
      />
      <div className="mt-4">
        <Section title="Recent runs">
          {runs.data?.items.length ? (
            <ul className="divide-y divide-border">
              {runs.data.items.map((r) => (
                <li key={r.id}>
                  <Link
                    href={`/runs/${r.id}`}
                    className="-mx-2 flex items-center gap-3 rounded-lg px-2 py-2 text-sm hover:bg-surface-2"
                  >
                    <StatusBadge status={r.status} size="sm" />
                    <span className="font-mono text-xs">{shortId(r.id)}</span>
                    <span className="text-xs text-muted">
                      {r.trigger} · v{r.version}
                    </span>
                    <span className="ml-auto text-xs text-muted">
                      <RelativeTime iso={r.created_at} now={now} /> ·{" "}
                      {formatDuration(runDuration(r.started_at, r.finished_at, now) ?? NaN)}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted">No runs yet. Press “Run now” to start one.</p>
          )}
        </Section>
      </div>
    </>
  );
}

/** Route entry: reads the `[id]` segment (runtime data, so it renders under Suspense). */
export function WorkflowDetailRoute() {
  const { id } = useParams<{ id: string }>();
  return <WorkflowDetailView id={id} />;
}
