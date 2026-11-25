"use client";

import { formatTime } from "@/lib/format";
import type { Stats } from "@/lib/types";

const W = 600;
const H = 150;
const PAD = { top: 8, right: 8, bottom: 22, left: 28 };

function path(values: number[], max: number, closed: boolean): string {
  const iw = W - PAD.left - PAD.right;
  const ih = H - PAD.top - PAD.bottom;
  const step = values.length > 1 ? iw / (values.length - 1) : 0;
  const pts = values.map((v, i) => [PAD.left + i * step, PAD.top + ih - (v / max) * ih] as const);
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  if (!closed || !pts.length) return line;
  const base = PAD.top + ih;
  return `${line} L${pts[pts.length - 1][0].toFixed(1)},${base} L${pts[0][0].toFixed(1)},${base} Z`;
}

/**
 * Completions per bucket over the last few minutes, as a small SVG area
 * chart: no charting library, so it costs almost nothing to load. Failures
 * are dashed as well as red, and the data is available as a table.
 */
export function ThroughputChart({ stats }: { stats: Stats }) {
  const points = stats.throughput;
  const ok = points.map((p) => p.succeeded);
  const bad = points.map((p) => p.failed);
  const max = Math.max(1, ...ok, ...bad);
  const ticks = [0, Math.ceil(max / 2), max];
  const total = ok.reduce((a, b) => a + b, 0);
  const failed = bad.reduce((a, b) => a + b, 0);
  const minutes = Math.round((points.length * stats.bucket_s) / 60);
  const ih = H - PAD.top - PAD.bottom;
  const labels = points.length
    ? [points[0], points[Math.floor(points.length / 2)], points[points.length - 1]]
    : [];

  return (
    <figure>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="h-44 w-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={`Throughput: ${total} tasks succeeded and ${failed} failed in the last ${minutes} minutes.`}
      >
        {ticks.map((t) => {
          const y = PAD.top + ih - (t / max) * ih;
          return (
            <g key={t}>
              <line
                x1={PAD.left}
                x2={W - PAD.right}
                y1={y}
                y2={y}
                stroke="var(--border)"
                strokeDasharray="3 3"
              />
              <text x={PAD.left - 6} y={y + 4} fontSize="11" textAnchor="end" fill="var(--text-muted)">
                {t}
              </text>
            </g>
          );
        })}
        <path d={path(ok, max, true)} fill="var(--chart-ok)" fillOpacity={0.15} />
        <path d={path(ok, max, false)} fill="none" stroke="var(--chart-ok)" strokeWidth={2} />
        <path d={path(bad, max, true)} fill="var(--chart-bad)" fillOpacity={0.12} />
        <path
          d={path(bad, max, false)}
          fill="none"
          stroke="var(--chart-bad)"
          strokeWidth={2}
          strokeDasharray="5 3"
        />
        {labels.map((p, i) => (
          <text
            key={p.start}
            x={i === 0 ? PAD.left : i === 1 ? W / 2 : W - PAD.right}
            y={H - 6}
            fontSize="11"
            textAnchor={i === 0 ? "start" : i === 1 ? "middle" : "end"}
            fill="var(--text-muted)"
          >
            {formatTime(p.start)}
          </text>
        ))}
      </svg>
      <figcaption className="mt-1 flex gap-4 text-xs text-muted">
        <span className="flex items-center gap-1.5">
          <svg width="16" height="4" aria-hidden="true">
            <line x1="0" x2="16" y1="2" y2="2" stroke="var(--chart-ok)" strokeWidth="2" />
          </svg>
          Succeeded
        </span>
        <span className="flex items-center gap-1.5">
          <svg width="16" height="4" aria-hidden="true">
            <line
              x1="0"
              x2="16"
              y1="2"
              y2="2"
              stroke="var(--chart-bad)"
              strokeWidth="2"
              strokeDasharray="5 3"
            />
          </svg>
          Failed
        </span>
      </figcaption>
    </figure>
  );
}
