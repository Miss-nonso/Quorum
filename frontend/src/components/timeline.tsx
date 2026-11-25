"use client";

import { useMemo } from "react";

import { formatDuration, shortId } from "@/lib/format";
import { attemptsFromEvents, timelineBounds, type Attempt, type AttemptOutcome } from "@/lib/timeline";
import type { RunEvent } from "@/lib/types";

const OUTCOME: Record<AttemptOutcome, { label: string; tone: string; pattern?: boolean }> = {
  running: { label: "Running", tone: "running" },
  succeeded: { label: "Succeeded", tone: "succeeded" },
  retried: { label: "Failed, retried", tone: "ready", pattern: true },
  dead: { label: "Dead-lettered", tone: "dead", pattern: true },
  failed: { label: "Failed", tone: "failed", pattern: true },
  lease_expired: { label: "Worker lost", tone: "failed", pattern: true },
  cancelled: { label: "Cancelled", tone: "cancelled", pattern: true },
  released: { label: "Released", tone: "cancelled" },
};

const ROW = 30;
const LABEL_W = 120;

/**
 * Gantt view of every attempt in a run. Parallel branches overlap in time;
 * retries show as extra bars on the same row. Failed attempts are hatched,
 * so the chart does not rely on colour alone, and a data table carries the
 * same information for screen readers.
 */
export function Timeline({
  events,
  order,
  runStart,
  now,
}: {
  events: RunEvent[];
  order: string[];
  runStart: number;
  now: number;
}) {
  const attempts = useMemo(() => attemptsFromEvents(events, now), [events, now]);
  const [start, end] = timelineBounds(attempts, runStart);
  const span = end - start;
  const rows = order.length ? order : [...new Set(attempts.map((a) => a.nodeKey))];
  const width = 1000;
  const height = rows.length * ROW + 28;
  const x = (t: number) => LABEL_W + ((t - start) / span) * (width - LABEL_W - 12);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => start + f * span);

  if (!attempts.length) {
    return <p className="text-sm text-muted">No attempts yet. Bars appear as workers pick up tasks.</p>;
  }

  return (
    <div>
      <div className="overflow-x-auto">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          className="min-w-[640px]"
          role="img"
          aria-label={`Execution timeline: ${attempts.length} attempts over ${formatDuration(span)}. Details in the table below.`}
        >
          <defs>
            <pattern
              id="hatch"
              width="6"
              height="6"
              patternUnits="userSpaceOnUse"
              patternTransform="rotate(45)"
            >
              <line
                x1="0"
                y1="0"
                x2="0"
                y2="6"
                stroke="var(--surface)"
                strokeWidth="2"
                strokeOpacity="0.55"
              />
            </pattern>
          </defs>
          {ticks.map((t, i) => (
            <g key={i}>
              <line
                x1={x(t)}
                x2={x(t)}
                y1={0}
                y2={height - 20}
                stroke="var(--border)"
                strokeDasharray="3 3"
              />
              <text x={x(t)} y={height - 6} fontSize="11" textAnchor="middle" fill="var(--text-muted)">
                +{formatDuration(t - start)}
              </text>
            </g>
          ))}
          {rows.map((key, r) => (
            <g key={key} transform={`translate(0 ${r * ROW})`}>
              <text x={0} y={ROW / 2 + 4} fontSize="12" fontFamily="var(--font-mono)" fill="var(--text)">
                {key.length > 15 ? `${key.slice(0, 14)}…` : key}
              </text>
              {attempts
                .filter((a) => a.nodeKey === key)
                .map((a) => {
                  const meta = OUTCOME[a.outcome];
                  const barX = x(a.start);
                  const barW = Math.max(3, x(a.end) - barX);
                  return (
                    <g key={`${a.attempt}-${a.start}`}>
                      <title>{`${key} attempt ${a.attempt}: ${meta.label}, ${formatDuration(a.end - a.start)}${a.workerId ? ` on worker ${shortId(a.workerId)}` : ""}`}</title>
                      <rect
                        x={barX}
                        y={6}
                        width={barW}
                        height={ROW - 12}
                        rx={4}
                        fill={`var(--st-${meta.tone})`}
                      />
                      {meta.pattern ? (
                        <rect x={barX} y={6} width={barW} height={ROW - 12} rx={4} fill="url(#hatch)" />
                      ) : null}
                    </g>
                  );
                })}
            </g>
          ))}
        </svg>
      </div>
      <Legend />
      <details className="mt-3 text-sm">
        <summary className="cursor-pointer text-muted">Timeline as a table</summary>
        <AttemptTable attempts={attempts} origin={start} />
      </details>
    </div>
  );
}

function Legend() {
  const shown: AttemptOutcome[] = ["running", "succeeded", "retried", "lease_expired", "dead"];
  return (
    <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted" aria-label="Legend">
      {shown.map((o) => (
        <li key={o} className="flex items-center gap-1.5">
          <svg width="18" height="10" aria-hidden="true">
            <rect width="18" height="10" rx="2" fill={`var(--st-${OUTCOME[o].tone})`} />
            {OUTCOME[o].pattern ? <rect width="18" height="10" rx="2" fill="url(#hatch)" /> : null}
          </svg>
          {OUTCOME[o].label}
        </li>
      ))}
    </ul>
  );
}

function AttemptTable({ attempts, origin }: { attempts: Attempt[]; origin: number }) {
  return (
    <table className="mt-2 w-full text-left text-xs">
      <thead className="text-muted">
        <tr>
          <th scope="col" className="py-1 pr-3 font-medium">
            Step
          </th>
          <th scope="col" className="py-1 pr-3 font-medium">
            Attempt
          </th>
          <th scope="col" className="py-1 pr-3 font-medium">
            Started
          </th>
          <th scope="col" className="py-1 pr-3 font-medium">
            Duration
          </th>
          <th scope="col" className="py-1 pr-3 font-medium">
            Outcome
          </th>
          <th scope="col" className="py-1 font-medium">
            Worker
          </th>
        </tr>
      </thead>
      <tbody>
        {attempts.map((a) => (
          <tr key={`${a.nodeKey}-${a.attempt}-${a.start}`} className="border-t border-border">
            <td className="py-1 pr-3 font-mono">{a.nodeKey}</td>
            <td className="py-1 pr-3">{a.attempt}</td>
            <td className="py-1 pr-3">+{formatDuration(a.start - origin)}</td>
            <td className="py-1 pr-3">{formatDuration(a.end - a.start)}</td>
            <td className="py-1 pr-3">{OUTCOME[a.outcome].label}</td>
            <td className="py-1 font-mono">{shortId(a.workerId)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
