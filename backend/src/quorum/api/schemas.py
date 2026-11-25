"""Request and response models: the public API contract (and the OpenAPI docs)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from quorum.domain.backoff import BackoffPolicy
from quorum.domain.dag import WorkflowDefinition
from quorum.domain.priority import PRIORITY_MAX, PRIORITY_MIN


class Page[T](BaseModel):
    """A page of results. ``total`` counts every match, not just this page."""

    items: list[T]
    total: int
    limit: int
    offset: int


# ------------------------------------------------------------------ workflows


class WorkflowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    definition: WorkflowDefinition
    cron: str | None = Field(default=None, max_length=100, examples=["*/15 9-17 * * MON-FRI"])
    timezone: str = Field(default="UTC", max_length=64, examples=["Europe/London"])
    enabled: bool = True


class WorkflowUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: WorkflowDefinition
    cron: str | None = Field(default=None, max_length=100)
    timezone: str = Field(default="UTC", max_length=64)
    enabled: bool = True


class WorkflowOut(BaseModel):
    id: UUID
    name: str
    version: int
    definition: WorkflowDefinition
    cron: str | None
    timezone: str
    enabled: bool
    created_at: datetime
    next_fire_at: datetime | None = None


# ------------------------------------------------------------------ runs & tasks


class RunOut(BaseModel):
    id: UUID
    workflow_id: UUID | None
    workflow_name: str | None
    version: int | None
    status: str
    trigger: str
    scheduled_for: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    task_count: int


class TaskOut(BaseModel):
    id: UUID
    node_key: str
    task_type: str
    params: dict[str, Any]
    status: str
    priority: int
    effective_priority: int
    attempt: int
    max_attempts: int
    failure_policy: str
    timeout_s: float
    not_before: datetime
    lease_owner: UUID | None
    lease_expires_at: datetime | None
    fencing_token: int
    idempotency_key: str
    result: Any
    error: Any
    created_at: datetime
    updated_at: datetime
    depends_on: list[str]


class EventOut(BaseModel):
    id: int
    run_id: UUID
    task_id: UUID | None
    node_key: str | None
    type: str
    payload: dict[str, Any]
    at: datetime


class RunDetail(RunOut):
    tasks: list[TaskOut]
    events: list[EventOut]


class TriggerRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ jobs


class JobIn(BaseModel):
    """A standalone task: a one-node run with no workflow."""

    model_config = ConfigDict(extra="forbid")

    task_type: str = Field(examples=["sleep"])
    params: dict[str, Any] = Field(default_factory=dict, examples=[{"seconds": 1}])
    priority: int = Field(default=0, ge=PRIORITY_MIN, le=PRIORITY_MAX)
    max_attempts: int = Field(default=3, ge=1, le=20)
    backoff: BackoffPolicy = Field(default_factory=BackoffPolicy)
    timeout_s: float = Field(default=60, gt=0, le=3600)
    idempotency_key: str | None = Field(default=None, max_length=200)
    delay_s: float = Field(default=0, ge=0, le=86_400)


class JobOut(BaseModel):
    run_id: UUID
    task_id: UUID
    idempotency_key: str


# ------------------------------------------------------------------ workers & DLQ


class WorkerTask(BaseModel):
    task_id: UUID
    run_id: UUID
    node_key: str
    task_type: str
    attempt: int


class WorkerOut(BaseModel):
    id: UUID
    hostname: str
    pid: int
    status: Literal["active", "unhealthy", "dead", "stopped"]
    concurrency: int
    current_load: int
    capabilities: list[str]
    started_at: datetime
    last_heartbeat_at: datetime
    heartbeat_age_s: float
    current_tasks: list[WorkerTask]


class DeadLetterOut(BaseModel):
    id: UUID
    task_id: UUID
    run_id: UUID
    node_key: str
    task_type: str
    workflow_name: str | None
    attempt: int
    task_status: str
    reason: str
    payload: dict[str, Any]
    created_at: datetime
    replayed_at: datetime | None


class ReplayOut(BaseModel):
    task_id: UUID
    run_id: UUID


# ------------------------------------------------------------------ overview


class ThroughputPoint(BaseModel):
    start: datetime
    succeeded: int
    failed: int


class SchedulerLeader(BaseModel):
    leader_id: UUID
    hostname: str
    pid: int
    elected_at: datetime
    heartbeat_at: datetime
    healthy: bool


class StatsOut(BaseModel):
    tasks: dict[str, int]
    workers: dict[str, int]
    open_dead_letters: int
    throughput: list[ThroughputPoint]
    bucket_s: int
    scheduler: SchedulerLeader | None


class TaskTypeOut(BaseModel):
    name: str
    description: str
    has_side_effects: bool
    params_schema: dict[str, Any]
