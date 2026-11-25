"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import {
  formatDuration,
  formatNumber,
  formatRelative,
  formatTime,
  prettyJson,
  runDuration,
  shortId,
} from "@/lib/format";
import { useNow } from "@/lib/use-now";
import { narrate, type Tone } from "@/lib/narrate";
import type { RunEvent, Stats } from "@/lib/types";
import { StatusIcon } from "./status";

export function StatCard({
  label,
  value,
  status,
  hint,
}: {
  label: string;
  value: number | undefined;
  status: string;
  hint: string;
}) {
  return (
    <div className="card flex flex-col gap-1 p-4">
      <div className="flex items-center gap-2 text-sm text-muted">
        <StatusIcon status={status} />
        {label}
      </div>
      <div className="text-3xl font-semibold tabular-nums">
        {value === undefined ? (
          <span className="skeleton inline-block h-8 w-16 align-middle" />
        ) : (
          formatNumber(value)
        )}
      </div>
      <p className="text-xs text-muted">{hint}</p>
    </div>
  );
}

export function ThroughputChart({ stats }: { stats: Stats }) {
  const data = stats.throughput.map((p) => ({ ...p, t: formatTime(p.start) }));
  const total = data.reduce((sum, p) => sum + p.succeeded, 0);
  return (
    <div
      className="h-44"
      role="img"
      aria-label={`Throughput: ${total} tasks succeeded in the last ${Math.round((data.length * stats.bucket_s) / 60)} minutes.`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="t"
            tick={{ fontSize: 11, fill: "var(--text-muted)" }}
            interval="preserveStartEnd"
            minTickGap={40}
          />
          <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "var(--text-muted)" }} />
          <Tooltip
            contentStyle={{
              background: "var(--surface)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              fontSize: 12,
            }}
            labelStyle={{ color: "var(--text)" }}
          />
          <Area
            type="monotone"
            dataKey="succeeded"
            name="Succeeded"
            stroke="var(--chart-ok)"
            fill="var(--chart-ok)"
            fillOpacity={0.15}
            isAnimationActive={false}
          />
          <Area
            type="monotone"
            dataKey="failed"
            name="Failed"
            stroke="var(--chart-bad)"
            fill="var(--chart-bad)"
            fillOpacity={0.15}
            strokeDasharray="4 2"
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

const TONE_STATUS: Record<Tone, string> = {
  info: "pending",
  good: "succeeded",
  warn: "ready",
  bad: "failed",
};

/** A plain-language narration of engine events, newest first. */
export function EventFeed({
  events,
  showRunLinks = true,
  emptyText = "Waiting for activity…",
  limit = 30,
}: {
  events: RunEvent[];
  showRunLinks?: boolean;
  emptyText?: string;
  limit?: number;
}) {
  if (!events.length) return <p className="text-sm text-muted">{emptyText}</p>;
  return (
    <ol className="flex flex-col divide-y divide-border" aria-label="Event feed">
      {events.slice(0, limit).map((e) => {
        const { text, tone } = narrate(e);
        return (
          <li key={e.id} className="flex items-start gap-2.5 py-2 text-sm">
            <span className="mt-0.5">
              <StatusIcon status={TONE_STATUS[tone]} />
            </span>
            <span className="min-w-0 flex-1">
              {text}
              {showRunLinks ? (
                <>
                  {" "}
                  <Link href={`/runs/${e.run_id}`} className="link text-xs">
                    run {shortId(e.run_id)}
                  </Link>
                </>
              ) : null}
            </span>
            <time dateTime={e.at} className="shrink-0 text-xs text-muted tabular-nums" title={e.at}>
              {formatTime(e.at)}
            </time>
          </li>
        );
      })}
    </ol>
  );
}

export function Section({
  title,
  actions,
  children,
  id,
}: {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
  id?: string;
}) {
  const headingId = id ?? `section-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  return (
    <section aria-labelledby={headingId} className="card p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 id={headingId} className="text-base font-semibold">
          {title}
        </h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

export function JsonBlock({ value, label }: { value: unknown; label: string }) {
  return (
    <pre
      tabIndex={0}
      aria-label={label}
      className="max-h-72 overflow-auto rounded-lg bg-surface-2 p-3 font-mono text-xs leading-relaxed"
    >
      {prettyJson(value)}
    </pre>
  );
}

export function Pagination({
  offset,
  limit,
  total,
  onChange,
}: {
  offset: number;
  limit: number;
  total: number;
  onChange: (offset: number) => void;
}) {
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <nav aria-label="Pagination" className="mt-4 flex items-center justify-between gap-2 text-sm">
      <p className="text-muted">
        {total === 0
          ? "No results"
          : `${offset + 1}–${Math.min(offset + limit, total)} of ${formatNumber(total)}`}
      </p>
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="btn-secondary px-2"
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
          aria-label="Previous page"
        >
          <ChevronLeft aria-hidden="true" className="size-4" />
        </button>
        <span className="tabular-nums" aria-current="page">
          Page {page} of {pages}
        </span>
        <button
          type="button"
          className="btn-secondary px-2"
          disabled={offset + limit >= total}
          onClick={() => onChange(offset + limit)}
          aria-label="Next page"
        >
          <ChevronRight aria-hidden="true" className="size-4" />
        </button>
      </div>
    </nav>
  );
}

/**
 * "3 min ago". Ticks on its own, so only this element re-renders as time
 * passes (not the whole page). Pass `now` to render from a shared clock.
 */
export function RelativeTime({ iso, now }: { iso: string | null; now?: number }) {
  const ticking = useNow(now === undefined ? 5000 : 0);
  if (!iso) return <span>—</span>;
  return (
    <time dateTime={iso} title={new Date(iso).toLocaleString()}>
      {formatRelative(iso, now ?? ticking)}
    </time>
  );
}

/** A run's duration; ticks once a second only while the run is unfinished. */
export function LiveDuration({ start, end }: { start: string | null; end: string | null }) {
  const now = useNow(end ? 0 : 1000);
  return <>{formatDuration(runDuration(start, end, now) ?? NaN)}</>;
}
