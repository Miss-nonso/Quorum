import { sandboxId } from "./sandbox";
import type { DeadLetter, Page, Run, RunDetail, SandboxState, Stats, Worker, Workflow } from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

/** An error response from the API, in its shared `{"error": {...}}` shape. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string | null,
    readonly details: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}/api/v1${path}`, {
      ...init,
      // The sandbox header is harmless outside the demo and scopes actions within it.
      headers: { "Content-Type": "application/json", "X-Sandbox-Id": sandboxId(), ...init?.headers },
    });
  } catch {
    throw new ApiError(0, "network_error", "Can't reach the Conductor API.", null);
  }
  if (!response.ok) {
    let body: { error?: { code?: string; message?: string; request_id?: string; details?: unknown } } = {};
    try {
      body = await response.json();
    } catch {
      // not JSON: fall through to a generic message
    }
    throw new ApiError(
      response.status,
      body.error?.code ?? "http_error",
      body.error?.message ?? `Request failed (${response.status}).`,
      body.error?.request_id ?? response.headers.get("x-request-id"),
      body.error?.details ?? null,
    );
  }
  return (await response.json()) as T;
}

function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  meta: () => request<{ version: string; demo_mode: boolean }>("/meta"),
  stats: () => request<Stats>("/stats"),
  workers: () => request<Worker[]>("/workers"),
  workflows: (limit = 50) => request<Page<Workflow>>(`/workflows${query({ limit })}`),
  workflow: (id: string) => request<Workflow>(`/workflows/${id}`),
  triggerRun: (workflowId: string) =>
    request<Run>(`/workflows/${workflowId}/runs`, { method: "POST", body: "{}" }),
  runs: (filters: {
    status?: string;
    trigger?: string;
    workflow_id?: string;
    limit?: number;
    offset?: number;
  }) => request<Page<Run>>(`/runs${query(filters)}`),
  run: (id: string) => request<RunDetail>(`/runs/${id}`),
  cancelRun: (id: string) => request<Run>(`/runs/${id}/cancel`, { method: "POST", body: "{}" }),
  deadLetters: (offset = 0, limit = 20) =>
    request<Page<DeadLetter>>(`/dead-letters${query({ offset, limit })}`),
  replay: (id: string) =>
    request<{ task_id: string; run_id: string }>(`/dead-letters/${id}/replay`, {
      method: "POST",
      body: "{}",
    }),
  workflowByName: async (name: string) =>
    (await request<Page<Workflow>>(`/workflows${query({ limit: 100 })}`)).items.find(
      (w) => w.name === name,
    ) ?? null,
  deadLettersForRun: async (runId: string) =>
    (await request<Page<DeadLetter>>(`/dead-letters${query({ limit: 100 })}`)).items.filter(
      (l) => l.run_id === runId,
    ),
  demo: {
    sandbox: () => request<SandboxState>("/demo/sandbox"),
    run: () => request<Run>("/demo/runs", { method: "POST", body: "{}" }),
    kill: () =>
      request<{ worker_id: string; hostname: string; tasks: string[] }>("/demo/chaos/kill-worker", {
        method: "POST",
        body: "{}",
      }),
    flood: () => request<{ low: number; high: number }>("/demo/chaos/flood", { method: "POST", body: "{}" }),
    failures: (enabled: boolean) =>
      request<{ enabled: boolean }>("/demo/chaos/toggle-failures", {
        method: "POST",
        body: JSON.stringify({ enabled }),
      }),
  },
};
