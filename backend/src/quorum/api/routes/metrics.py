"""``GET /metrics`` in the Prometheus text exposition format (hand-written; no client lib).

Every number is derived from the database, so any API instance reports the
same cluster-wide values. Counters are totals of ``run_events`` by type,
which only ever grows (until a demo reset, which Prometheus treats as a
counter reset). Latencies are quantiles over the last five minutes.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from quorum.api.deps import Hub, ReadConn
from quorum.store import queries

router = APIRouter(tags=["overview"])

TASK_STATUSES = ("pending", "ready", "running", "succeeded", "failed", "dead", "cancelled")
WORKER_STATUSES = ("active", "unhealthy", "dead", "stopped")
COUNTED_EVENTS = {
    "task_enqueued": ("quorum_tasks_enqueued_total", "Tasks enqueued (jobs and run roots)."),
    "task_claimed": ("quorum_task_claims_total", "Task attempts started by a worker."),
    "task_succeeded": ("quorum_tasks_succeeded_total", "Tasks that succeeded."),
    "task_retry_scheduled": ("quorum_task_retries_total", "Failed attempts that were retried."),
    "task_dead_lettered": ("quorum_tasks_dead_lettered_total", "Tasks moved to the DLQ."),
    "lease_expired": ("quorum_lease_expirations_total", "Leases recovered by the reaper."),
    "stale_write_rejected": (
        "quorum_stale_writes_rejected_total",
        "Writes refused because their fencing token was stale.",
    ),
    "run_succeeded": ("quorum_runs_succeeded_total", "Workflow runs that succeeded."),
    "run_failed": ("quorum_runs_failed_total", "Workflow runs that failed."),
}


def _family(name: str, kind: str, help_text: str, samples: Iterable[str]) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}", *samples]


def _summary(name: str, help_text: str, stats: dict[str, Any]) -> list[str]:
    lines = []
    for q, key in (("0.5", "p50"), ("0.95", "p95"), ("0.99", "p99")):
        value = stats.get(key)
        lines.append(f'{name}{{quantile="{q}"}} {float(value) if value is not None else "NaN"}')
    lines.append(f"{name}_sum {float(stats.get('total') or 0)}")
    lines.append(f"{name}_count {int(stats.get('n') or 0)}")
    return _family(name, "summary", help_text, lines)


def render(samples: dict[str, Any], *, sse_clients: int, listener_up: bool) -> str:
    out: list[str] = []
    tasks = samples["tasks_by_status"]
    out += _family(
        "quorum_tasks",
        "gauge",
        "Task instances by status (ready = queue depth).",
        (f'quorum_tasks{{status="{s}"}} {tasks.get(s, 0)}' for s in TASK_STATUSES),
    )
    workers = samples["workers_by_status"]
    out += _family(
        "quorum_workers",
        "gauge",
        "Registered workers by status.",
        (f'quorum_workers{{status="{s}"}} {workers.get(s, 0)}' for s in WORKER_STATUSES),
    )
    out += _family(
        "quorum_oldest_ready_task_age_seconds",
        "gauge",
        "How long the oldest claimable task has waited.",
        [f"quorum_oldest_ready_task_age_seconds {samples['oldest_ready_age_s']:.3f}"],
    )
    totals = samples["event_totals"]
    for event, (name, help_text) in COUNTED_EVENTS.items():
        out += _family(name, "counter", help_text, [f"{name} {totals.get(event, 0)}"])
    out += _summary(
        "quorum_task_duration_seconds",
        "Claim-to-success time of tasks finished in the last 5 minutes.",
        samples["duration"],
    )
    out += _summary(
        "quorum_queue_wait_seconds",
        "Ready-to-claimed time of claims in the last 5 minutes.",
        samples["queue_wait"],
    )
    out += _family(
        "quorum_sse_clients",
        "gauge",
        "Connected SSE clients on this API instance.",
        [f"quorum_sse_clients {sse_clients}"],
    )
    out += _family(
        "quorum_event_listener_up",
        "gauge",
        "1 if this instance's LISTEN connection is up.",
        [f"quorum_event_listener_up {1 if listener_up else 0}"],
    )
    return "\n".join(out) + "\n"


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Prometheus metrics",
    responses={200: {"content": {"text/plain; version=0.0.4": {}}}},
)
async def metrics(conn: ReadConn, hub: Hub) -> PlainTextResponse:
    text = render(
        await queries.metric_samples(conn),
        sse_clients=hub.subscriber_count,
        listener_up=hub.connected,
    )
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")
