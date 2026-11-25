// Mirrors backend/src/quorum/api/schemas.py. Keep in sync.

export type TaskStatus = "pending" | "ready" | "running" | "succeeded" | "failed" | "dead" | "cancelled";

export type RunStatus = "pending" | "running" | "succeeded" | "failed" | "cancelled";

export type WorkerStatus = "active" | "unhealthy" | "dead" | "stopped";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface NodeSpec {
  key: string;
  task_type: string;
  params: Record<string, unknown>;
  depends_on: string[];
  failure_policy: "retry_then_dead" | "fail_run" | "continue";
  priority: number;
  max_attempts: number;
  timeout_s: number;
}

export interface Workflow {
  id: string;
  name: string;
  version: number;
  definition: { nodes: NodeSpec[] };
  cron: string | null;
  timezone: string;
  enabled: boolean;
  created_at: string;
  next_fire_at: string | null;
}

export interface Run {
  id: string;
  workflow_id: string | null;
  workflow_name: string | null;
  version: number | null;
  status: RunStatus;
  trigger: string;
  scheduled_for: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  task_count: number;
}

export interface Task {
  id: string;
  node_key: string;
  task_type: string;
  params: Record<string, unknown>;
  status: TaskStatus;
  priority: number;
  effective_priority: number;
  attempt: number;
  max_attempts: number;
  failure_policy: string;
  timeout_s: number;
  not_before: string;
  lease_owner: string | null;
  lease_expires_at: string | null;
  fencing_token: number;
  idempotency_key: string;
  result: unknown;
  error: { kind?: string; message?: string; type?: string; attempt?: number } | null;
  created_at: string;
  updated_at: string;
  depends_on: string[];
}

export interface RunEvent {
  id: number;
  run_id: string;
  task_id: string | null;
  node_key: string | null;
  type: string;
  payload: Record<string, unknown>;
  at: string;
}

export interface RunDetail extends Run {
  tasks: Task[];
  events: RunEvent[];
}

export interface WorkerTask {
  task_id: string;
  run_id: string;
  node_key: string;
  task_type: string;
  attempt: number;
}

export interface Worker {
  id: string;
  hostname: string;
  pid: number;
  status: WorkerStatus;
  concurrency: number;
  current_load: number;
  capabilities: string[];
  started_at: string;
  last_heartbeat_at: string;
  heartbeat_age_s: number;
  current_tasks: WorkerTask[];
}

export interface DeadLetter {
  id: string;
  task_id: string;
  run_id: string;
  node_key: string;
  task_type: string;
  workflow_name: string | null;
  attempt: number;
  task_status: string;
  reason: string;
  payload: { params?: unknown; error?: Record<string, unknown> };
  created_at: string;
  replayed_at: string | null;
}

export interface Stats {
  tasks: {
    pending: number;
    queued: number;
    running: number;
    succeeded: number;
    failed: number;
    dead: number;
    cancelled: number;
  };
  workers: Partial<Record<WorkerStatus, number>>;
  open_dead_letters: number;
  throughput: { start: string; succeeded: number; failed: number }[];
  bucket_s: number;
  scheduler: {
    leader_id: string;
    hostname: string;
    pid: number;
    elected_at: string;
    heartbeat_at: string;
    healthy: boolean;
  } | null;
}

export interface SandboxState {
  sandbox_id: string;
  failures_enabled: boolean;
  runs: { id: string; status: RunStatus; created_at: string }[];
  flood: {
    low: Partial<Record<TaskStatus, number>>;
    high: Partial<Record<TaskStatus, number>>;
    max_aging_bonus: number;
  };
}
