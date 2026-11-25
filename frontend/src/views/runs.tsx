"use client";

import { X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";

import { EmptyState, ErrorState, LoadingBlock, Skeleton } from "@/components/states";
import { StatusBadge, statusMeta } from "@/components/status";
import { Pagination, RelativeTime } from "@/components/widgets";
import { formatDuration, runDuration, shortId } from "@/lib/format";
import { useRuns } from "@/lib/queries";
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
      {/* The page header is rendered statically by app/runs/page.tsx. */}
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
