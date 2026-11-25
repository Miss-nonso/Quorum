"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BadgeHelp, Play, RotateCcw, Skull, Waves, Zap } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";

import { DagView } from "@/components/lazy";
import { GuideProvider, GuideStep, useGuide } from "@/components/guide";
import { ErrorState, LoadingBlock, Skeleton } from "@/components/states";
import { StatusBadge } from "@/components/status";
import { Timeline } from "@/components/timeline";
import { useToast } from "@/components/toast";
import { EventFeed, LiveDuration, Section } from "@/components/widgets";
import { api, ApiError } from "@/lib/api";
import { topologicalOrder } from "@/lib/dag-layout";
import { shortId } from "@/lib/format";
import { useLive } from "@/lib/live";
import { useReplay, useRun, useWorkers } from "@/lib/queries";
import type { SandboxState, TaskStatus } from "@/lib/types";
import { useNow } from "@/lib/use-now";

const SAMPLE = "order-pipeline";

function useSandbox() {
  return useQuery({
    queryKey: ["sandbox"],
    queryFn: api.demo.sandbox,
    refetchInterval: 2000,
    retry: (count, error) => count < 3 && !(error instanceof ApiError && error.status === 403),
  });
}

function explain(error: unknown): string {
  return error instanceof ApiError ? error.message : "Something went wrong. Please try again.";
}

// ------------------------------------------------------------------ the run panel

function CurrentRun({ runId }: { runId: string }) {
  const run = useRun(runId);
  const replay = useReplay();
  const toast = useToast();
  const now = useNow(500);
  const letters = useQuery({
    queryKey: ["dead-letters", "run", runId],
    queryFn: () => api.deadLettersForRun(runId),
    enabled: run.data?.tasks.some((t) => t.status === "dead") ?? false,
  });

  if (run.isPending) {
    return (
      <LoadingBlock label="your run">
        <Skeleton className="h-80 w-full" />
      </LoadingBlock>
    );
  }
  if (run.isError) return <ErrorState error={run.error} onRetry={() => run.refetch()} />;
  const r = run.data;
  const order = topologicalOrder(r.tasks);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <StatusBadge status={r.status} />
        <span className="text-muted">
          Run <span className="font-mono">{shortId(r.id)}</span> ·{" "}
          <LiveDuration start={r.started_at} end={r.finished_at} />
        </span>
        <Link href={`/runs/${r.id}`} className="link ml-auto">
          Full run view
        </Link>
      </div>
      {letters.data?.map((l) => (
        <div
          key={l.id}
          role="alert"
          className="flex flex-wrap items-center gap-3 rounded-lg border p-3 text-sm"
          style={{ borderColor: "var(--st-dead)", backgroundColor: "var(--st-dead-bg)" }}
        >
          <Skull aria-hidden="true" className="size-4" style={{ color: "var(--st-dead)" }} />
          <p className="flex-1">
            <strong className="font-mono">{l.node_key}</strong> ran out of retries and is in the dead-letter
            queue. Replaying it resumes the run, and the injected fault is cleared, as if you&apos;d fixed the
            cause.
          </p>
          <button
            type="button"
            className="btn-primary"
            disabled={replay.isPending}
            onClick={() =>
              replay.mutate(l, {
                onSuccess: () => toast("success", `${l.node_key} replayed. Watch the run resume.`),
                onError: (e) => toast("error", explain(e)),
              })
            }
          >
            <RotateCcw aria-hidden="true" className="size-4" /> Replay {l.node_key}
          </button>
        </div>
      ))}
      <DagView
        label="Your run, live"
        nodes={r.tasks.map((t) => ({
          key: t.node_key,
          task_type: t.task_type,
          depends_on: t.depends_on,
          status: t.status,
          attempt: t.attempt,
          max_attempts: t.max_attempts,
        }))}
        height={300}
      />
      <Timeline
        events={r.events}
        order={order}
        runStart={new Date(r.started_at ?? r.created_at).getTime()}
        now={r.finished_at ? new Date(r.finished_at).getTime() : now}
      />
    </div>
  );
}

function PreviewRun() {
  const workflow = useQuery({
    queryKey: ["workflow-by-name", SAMPLE],
    queryFn: () => api.workflowByName(SAMPLE),
  });
  if (workflow.isPending) return <Skeleton className="h-80 w-full" />;
  if (!workflow.data) {
    return (
      <p className="text-sm text-muted">The sample workflow isn&apos;t installed. Run `conductor seed`.</p>
    );
  }
  return (
    <DagView
      label="The sample workflow, before it runs"
      nodes={workflow.data.definition.nodes.map((n) => ({
        key: n.key,
        task_type: n.task_type,
        depends_on: n.depends_on,
      }))}
      height={300}
    />
  );
}

// ------------------------------------------------------------------ chaos panel

function Bar({ label, done, total, tone }: { label: string; done: number; total: number; tone: string }) {
  const id = `bar-${label.replace(/\W/g, "")}`;
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs">
        <span id={id}>{label}</span>
        <span className="text-muted tabular-nums">
          {done} / {total} done
        </span>
      </div>
      <div
        role="progressbar"
        aria-labelledby={id}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={done}
        className="h-2 overflow-hidden rounded-full bg-surface-2"
      >
        <div
          className="h-full rounded-full transition-[width]"
          style={{ width: `${total ? (done / total) * 100 : 0}%`, backgroundColor: `var(--st-${tone})` }}
        />
      </div>
    </div>
  );
}

function bandTotals(band: Partial<Record<TaskStatus, number>>) {
  const total = Object.values(band).reduce((a, b) => a + (b ?? 0), 0);
  return { total, done: band.succeeded ?? 0 };
}

function ChaosPanel({ sandbox }: { sandbox: SandboxState }) {
  const qc = useQueryClient();
  const toast = useToast();
  const refresh = () => qc.invalidateQueries({ queryKey: ["sandbox"] });

  const kill = useMutation({
    mutationFn: api.demo.kill,
    onSuccess: (r) =>
      toast(
        "success",
        r.tasks.length
          ? `Crashing worker ${shortId(r.worker_id)} while it runs ${r.tasks.join(", ")}. Watch the recovery.`
          : `Crashing idle worker ${shortId(r.worker_id)}. A fresh worker will take its place.`,
      ),
    onError: (e) => toast("error", explain(e)),
  });
  const flood = useMutation({
    mutationFn: api.demo.flood,
    onSuccess: (r) => {
      refresh();
      toast("success", `Queued ${r.low} low- and ${r.high} high-priority jobs.`);
    },
    onError: (e) => toast("error", explain(e)),
  });
  const failures = useMutation({
    mutationFn: api.demo.failures,
    onSuccess: refresh,
    onError: (e) => toast("error", explain(e)),
  });

  const low = bandTotals(sandbox.flood.low);
  const high = bandTotals(sandbox.flood.high);
  const failuresOn = failures.isPending
    ? (failures.variables ?? sandbox.failures_enabled)
    : sandbox.failures_enabled;

  return (
    <div className="flex flex-col divide-y divide-border">
      <div className="flex flex-col gap-2 pb-4">
        <h3 className="flex items-center gap-2 font-semibold">
          <Skull aria-hidden="true" className="size-4" style={{ color: "var(--st-failed)" }} /> Kill a worker
        </h3>
        <p className="text-sm text-muted">
          Crashes a worker mid-task, with no cleanup. Its claim expires after a few seconds, and another
          worker finishes the task. Side effects still happen exactly once.
        </p>
        <button
          type="button"
          className="btn-secondary self-start"
          style={{ borderColor: "var(--st-failed)", color: "var(--st-failed)" }}
          disabled={kill.isPending}
          onClick={() => kill.mutate()}
        >
          <Zap aria-hidden="true" className="size-4" /> {kill.isPending ? "Killing…" : "Kill a worker"}
        </button>
      </div>

      <div className="flex flex-col gap-2 py-4">
        <h3 className="flex items-center gap-2 font-semibold">
          <Waves aria-hidden="true" className="size-4 text-accent" /> Flood the queue
        </h3>
        <p className="text-sm text-muted">
          Adds a burst of low-priority jobs and a few urgent ones. Urgent jobs jump the queue, and the longer
          a job waits, the higher its priority climbs, so the low ones never starve.
        </p>
        <button
          type="button"
          className="btn-secondary self-start"
          disabled={flood.isPending}
          onClick={() => flood.mutate()}
        >
          <Waves aria-hidden="true" className="size-4" /> {flood.isPending ? "Queuing…" : "Flood the queue"}
        </button>
        {low.total || high.total ? (
          <div className="mt-1 flex flex-col gap-2" aria-live="polite">
            <Bar label="High priority" {...high} tone="running" />
            <Bar label="Low priority" {...low} tone="ready" />
            {sandbox.flood.max_aging_bonus > 0 ? (
              <p className="text-xs text-muted">
                Waiting jobs have aged up to +{sandbox.flood.max_aging_bonus} priority.
              </p>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="flex items-start gap-3 pt-4">
        <div className="flex-1">
          <h3 id="failures-label" className="flex items-center gap-2 font-semibold">
            <BadgeHelp aria-hidden="true" className="size-4" style={{ color: "var(--st-dead)" }} /> Inject
            failures
          </h3>
          <p id="failures-desc" className="mt-1 text-sm text-muted">
            Your next runs: <span className="font-mono">transform</span> fails twice and recovers by retrying
            with backoff; <span className="font-mono">notify</span> keeps failing until it&apos;s
            dead-lettered. Then you replay it with one click.
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={failuresOn}
          aria-labelledby="failures-label"
          aria-describedby="failures-desc"
          onClick={() => failures.mutate(!failuresOn)}
          className="relative mt-1 inline-flex h-6 w-11 shrink-0 items-center rounded-full border border-border transition-colors"
          style={{ backgroundColor: failuresOn ? "var(--st-dead)" : "var(--surface-2)" }}
        >
          <span
            aria-hidden="true"
            className="inline-block size-5 rounded-full bg-surface shadow-card transition-transform"
            style={{ transform: failuresOn ? "translateX(20px)" : "translateX(1px)" }}
          />
          <span className="sr-only">{failuresOn ? "On" : "Off"}</span>
        </button>
      </div>
    </div>
  );
}

function WorkersMini() {
  const workers = useWorkers();
  const busy = workers.data?.filter((w) => w.status !== "stopped") ?? [];
  if (!workers.data) return <Skeleton className="h-16 w-full" />;
  if (!busy.length) return <p className="text-sm text-muted">No workers online.</p>;
  return (
    <ul className="flex flex-col gap-2" aria-live="polite">
      {busy.map((w) => (
        <li key={w.id} className="flex items-center gap-2 text-sm">
          <StatusBadge status={w.status} kind="worker" size="sm" />
          <span className="font-mono text-xs">{shortId(w.id)}</span>
          <span className="min-w-0 flex-1 truncate text-xs text-muted">
            {w.current_tasks.length ? `running ${w.current_tasks.map((t) => t.node_key).join(", ")}` : "idle"}
          </span>
        </li>
      ))}
    </ul>
  );
}

// ------------------------------------------------------------------ page

function DemoBody() {
  const sandbox = useSandbox();
  const qc = useQueryClient();
  const toast = useToast();
  const { events } = useLive();
  const { restart, current } = useGuide();
  const start = useMutation({
    mutationFn: api.demo.run,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sandbox"] }),
    onError: (e) => toast("error", explain(e)),
  });

  const myRunIds = useMemo(() => new Set(sandbox.data?.runs.map((r) => r.id)), [sandbox.data]);
  const myEvents = useMemo(() => events.filter((e) => myRunIds.has(e.run_id)), [events, myRunIds]);
  const latest = sandbox.data?.runs[0];
  const demoOff = sandbox.error instanceof ApiError && sandbox.error.status === 403;

  return (
    <>
      <section aria-labelledby="hero" className="mb-6 flex flex-col gap-3">
        <h1 id="hero" className="max-w-3xl text-3xl font-semibold tracking-tight sm:text-4xl">
          Kill a worker. Watch nothing get lost.
        </h1>
        <p className="max-w-2xl text-muted">
          Conductor is a distributed workflow engine built on nothing but PostgreSQL. Run the sample pipeline,
          then crash a worker, flood the queue or inject failures, and watch it recover live.
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            className="btn-primary px-4 py-2.5 text-base"
            disabled={start.isPending || demoOff}
            onClick={() => start.mutate()}
          >
            <Play aria-hidden="true" className="size-4" />{" "}
            {start.isPending ? "Starting…" : "Run the pipeline"}
          </button>
          {/* Always rendered, so the row never re-wraps (no layout shift). */}
          <button type="button" className="btn-secondary" onClick={restart}>
            {current ? "Restart the guide" : "Show the guide"}
          </button>
          <Link href="/overview" className="link text-sm">
            Or see the whole system
          </Link>
        </div>
        <GuideStep id="run" />
        {demoOff ? (
          <p className="text-sm text-muted">
            Demo controls are turned off on this deployment (set{" "}
            <code className="font-mono">CONDUCTOR_DEMO_MODE=true</code>).
          </p>
        ) : null}
      </section>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="flex min-w-0 flex-col gap-4 lg:col-span-2">
          <Section title={latest ? "Your run" : "The pipeline"}>
            <GuideStep id="workflow" />
            {latest ? <CurrentRun runId={latest.id} /> : <PreviewRun />}
          </Section>
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          <Section title="Chaos controls">
            <GuideStep id="chaos" />
            {sandbox.data ? (
              <ChaosPanel sandbox={sandbox.data} />
            ) : sandbox.isError && !demoOff ? (
              <ErrorState error={sandbox.error} onRetry={() => sandbox.refetch()} />
            ) : demoOff ? (
              <p className="text-sm text-muted">Unavailable on this deployment.</p>
            ) : (
              <Skeleton className="h-64 w-full" />
            )}
          </Section>
          <Section title="Workers">
            <WorkersMini />
          </Section>
          <Section title="What's happening">
            <GuideStep id="story" />
            <div
              tabIndex={0}
              role="region"
              aria-label="Story of your runs"
              aria-live="polite"
              className="max-h-96 overflow-y-auto"
            >
              <EventFeed
                events={myEvents}
                showRunLinks={false}
                emptyText="Run the pipeline and the story of your run appears here."
              />
            </div>
          </Section>
        </div>
      </div>
    </>
  );
}

export function DemoView() {
  return (
    <GuideProvider>
      <DemoBody />
    </GuideProvider>
  );
}
