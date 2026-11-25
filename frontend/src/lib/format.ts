/** Formatting helpers. Pure, so they are unit tested. */

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : "—";
}

/** "1.2 s", "340 ms", "2 m 05 s", "1 h 03 m". */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} m ${String(Math.floor(s % 60)).padStart(2, "0")} s`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} m`;
}

/** "just now", "12 s ago", "3 min ago", "2 h ago", or a date for anything older than a day. */
export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  const diff = Math.round((now - then) / 1000);
  if (diff < 0) return formatUntil(iso, now);
  if (diff < 5) return "just now";
  if (diff < 60) return `${diff} s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86_400) return `${Math.floor(diff / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function formatUntil(iso: string, now: number = Date.now()): string {
  const diff = Math.round((new Date(iso).getTime() - now) / 1000);
  if (diff <= 0) return "now";
  if (diff < 60) return `in ${diff} s`;
  if (diff < 3600) return `in ${Math.floor(diff / 60)} min`;
  if (diff < 86_400) return `in ${Math.floor(diff / 3600)} h`;
  return `in ${Math.floor(diff / 86_400)} d`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatNumber(n: number): string {
  return new Intl.NumberFormat(undefined).format(n);
}

/** Run duration: finished - started, or so far if still running. */
export function runDuration(
  startedAt: string | null,
  finishedAt: string | null,
  now: number = Date.now(),
): number | null {
  if (!startedAt) return null;
  const end = finishedAt ? new Date(finishedAt).getTime() : now;
  return end - new Date(startedAt).getTime();
}

export function prettyJson(value: unknown, maxLength = 20_000): string {
  const text = JSON.stringify(value, null, 2) ?? "null";
  return text.length > maxLength ? `${text.slice(0, maxLength)}\n… (truncated)` : text;
}
