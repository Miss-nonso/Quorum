"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "./api";
import type { DeadLetter, Page, RunDetail } from "./types";

// Live data arrives over SSE; polling is only the fallback if the stream drops.
const FALLBACK_POLL_MS = 10_000;

export const useMeta = () => useQuery({ queryKey: ["meta"], queryFn: api.meta, staleTime: Infinity });

export const useStats = () =>
  useQuery({ queryKey: ["stats"], queryFn: api.stats, refetchInterval: FALLBACK_POLL_MS });

export const useWorkers = () =>
  useQuery({ queryKey: ["workers"], queryFn: api.workers, refetchInterval: FALLBACK_POLL_MS });

export const useWorkflows = () => useQuery({ queryKey: ["workflows"], queryFn: () => api.workflows() });

export const useWorkflow = (id: string) =>
  useQuery({ queryKey: ["workflow", id], queryFn: () => api.workflow(id) });

export interface RunFilters {
  status?: string;
  trigger?: string;
  workflow_id?: string;
  limit?: number;
  offset?: number;
}

export const useRuns = (filters: RunFilters) =>
  useQuery({
    queryKey: ["runs", filters],
    queryFn: () => api.runs(filters),
    placeholderData: keepPreviousData,
    refetchInterval: FALLBACK_POLL_MS,
  });

export const useRun = (id: string) =>
  useQuery({
    queryKey: ["run", id],
    queryFn: () => api.run(id),
    refetchInterval: (q) => {
      const data = q.state.data as RunDetail | undefined;
      return data && data.status === "running" ? FALLBACK_POLL_MS : false;
    },
  });

export const useDeadLetters = (offset: number) =>
  useQuery({
    queryKey: ["dead-letters", offset],
    queryFn: () => api.deadLetters(offset),
    placeholderData: keepPreviousData,
  });

/** Start a run. On the public demo this becomes a sandboxed demo run of the sample. */
export function useTriggerRun() {
  const qc = useQueryClient();
  const meta = useMeta();
  return useMutation({
    mutationFn: (workflowId: string) => (meta.data?.demo_mode ? api.demo.run() : api.triggerRun(workflowId)),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["runs"] }),
  });
}

export function useCancelRun(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.cancelRun(runId),
    onSettled: () => qc.invalidateQueries({ queryKey: ["run", runId] }),
  });
}

/** Optimistic: the letter disappears at once; it comes back if the replay fails. */
export function useReplay() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (letter: DeadLetter) => api.replay(letter.id),
    onMutate: async (letter) => {
      await qc.cancelQueries({ queryKey: ["dead-letters"] });
      // Cached shapes differ: paginated lists hold a Page, per-run lookups a plain array.
      const snapshots = qc.getQueriesData<Page<DeadLetter> | DeadLetter[]>({ queryKey: ["dead-letters"] });
      for (const [key, data] of snapshots) {
        if (Array.isArray(data)) {
          qc.setQueryData<DeadLetter[]>(
            key,
            data.filter((l) => l.id !== letter.id),
          );
        } else if (data) {
          qc.setQueryData<Page<DeadLetter>>(key, {
            ...data,
            items: data.items.filter((l) => l.id !== letter.id),
            total: Math.max(0, data.total - 1),
          });
        }
      }
      return { snapshots };
    },
    onError: (_error, _letter, context) => {
      context?.snapshots.forEach(([key, page]) => qc.setQueryData(key, page));
    },
    onSettled: (_data, _error, letter) => {
      qc.invalidateQueries({ queryKey: ["dead-letters"] });
      qc.invalidateQueries({ queryKey: ["run", letter.run_id] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });
}
