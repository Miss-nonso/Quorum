"""The public demo: sandboxes, chaos controls and housekeeping.

Every visitor gets a random sandbox id (generated in their browser, sent as
``X-Sandbox-Id``). Demo runs and flood jobs are tagged with it, so each
visitor sees and controls their own runs, while the worker pool, the point
of the demo, is shared.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from quorum.core.config import Settings
from quorum.domain.backoff import BackoffPolicy
from quorum.samples import SAMPLE_WORKFLOW_NAME
from quorum.store import runs as run_store
from quorum.store import tasks as task_store
from quorum.store import workflows as wf_store
from quorum.store.events import EventType, record_event

SANDBOX_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class DemoLimitError(Exception):
    """A demo cap was hit (maps to 429)."""


class DemoUnavailableError(Exception):
    """The demo can't serve this right now (maps to 409/503)."""


async def touch_sandbox(conn: AsyncConnection, sandbox_id: str) -> bool:
    """Create or refresh the sandbox; lock it for the transaction. Returns failures flag."""
    row = (
        await conn.execute(
            text(
                """
                INSERT INTO demo_sandboxes (id) VALUES (:id)
                ON CONFLICT (id) DO UPDATE SET last_seen_at = now()
                RETURNING failures_enabled
                """
            ),
            {"id": sandbox_id},
        )
    ).one()
    # The upsert locks the row until commit, serialising this visitor's actions.
    return bool(row.failures_enabled)


async def set_failures(conn: AsyncConnection, sandbox_id: str, enabled: bool) -> None:
    await touch_sandbox(conn, sandbox_id)
    await conn.execute(
        text("UPDATE demo_sandboxes SET failures_enabled = :on WHERE id = :id"),
        {"id": sandbox_id, "on": enabled},
    )


def demo_chaos(
    settings: Settings, *, failures: bool, nodes: list[str]
) -> dict[str, dict[str, Any]]:
    """Per-node fault injection for a demo run.

    Every step is paced so people can watch it. With failures on, `transform`
    fails twice then succeeds (retries with backoff), and `notify` fails every
    attempt, so it is dead-lettered and the run stops until it is replayed.
    """
    pace = {"delay_s": settings.demo_step_delay_s}
    chaos: dict[str, dict[str, Any]] = {key: dict(pace) for key in nodes}
    if failures:
        chaos["transform"] = {**pace, "fail_attempts": 2}
        chaos["notify"] = {**pace, "fail_attempts": 99}
    return chaos


async def start_demo_run(
    conn: AsyncConnection, sandbox_id: str, settings: Settings
) -> run_store.CreatedRun:
    failures = await touch_sandbox(conn, sandbox_id)
    active = (
        await conn.execute(
            text(
                """
                SELECT count(*) FILTER (WHERE sandbox_id = :id) AS mine, count(*) AS total
                FROM workflow_runs WHERE trigger = 'demo' AND status = 'running'
                  AND workflow_id IS NOT NULL
                """
            ),
            {"id": sandbox_id},
        )
    ).one()
    if active.mine >= settings.demo_max_runs_per_sandbox:
        raise DemoLimitError(f"you already have {active.mine} runs going; let one finish first")
    if active.total >= settings.demo_max_active_runs:
        raise DemoLimitError("the demo is busy right now; try again in a moment")
    workflow = await wf_store.get_workflow_by_name(conn, SAMPLE_WORKFLOW_NAME)
    if workflow is None:
        raise DemoUnavailableError("the sample workflow is not installed (run `quorum seed`)")
    chaos = demo_chaos(
        settings, failures=failures, nodes=[n.key for n in workflow.definition.nodes]
    )
    return await run_store.create_workflow_run(
        conn, workflow.id, trigger="demo", sandbox_id=sandbox_id, chaos=chaos
    )


@dataclass(frozen=True, slots=True)
class KillResult:
    worker_id: UUID
    hostname: str
    tasks: list[str]  # node keys it was running


async def kill_worker(conn: AsyncConnection, sandbox_id: str) -> KillResult:
    """Order one demo worker to crash, preferring one that is running this visitor's work."""
    await touch_sandbox(conn, sandbox_id)
    target = (
        await conn.execute(
            text(
                """
                SELECT w.id, w.hostname,
                       count(t.id) FILTER (WHERE r.sandbox_id = :sandbox) AS mine,
                       count(t.id) AS busy
                FROM workers w
                LEFT JOIN task_instances t ON t.lease_owner = w.id AND t.status = 'running'
                LEFT JOIN workflow_runs r ON r.id = t.run_id
                WHERE w.accepts_chaos AND w.status = 'active'
                  AND NOT EXISTS (SELECT 1 FROM worker_commands c
                                  WHERE c.worker_id = w.id AND c.consumed_at IS NULL)
                GROUP BY w.id, w.hostname
                ORDER BY mine DESC, busy DESC, w.started_at
                LIMIT 1
                """
            ),
            {"sandbox": sandbox_id},
        )
    ).one_or_none()
    if target is None:
        raise DemoUnavailableError("no demo worker is available to kill right now")
    await conn.execute(
        text(
            "INSERT INTO worker_commands (worker_id, command, sandbox_id) "
            "VALUES (:id, 'kill', :sandbox)"
        ),
        {"id": target.id, "sandbox": sandbox_id},
    )
    held = (
        await conn.execute(
            text(
                "SELECT id, run_id, node_key FROM task_instances "
                "WHERE lease_owner = :id AND status = 'running'"
            ),
            {"id": target.id},
        )
    ).all()
    for task in held:
        await record_event(
            conn,
            run_id=task.run_id,
            task_id=task.id,
            type=EventType.CHAOS_WORKER_KILLED,
            payload={"worker_id": str(target.id), "hostname": target.hostname},
        )
    return KillResult(target.id, target.hostname, [t.node_key for t in held])


@dataclass(frozen=True, slots=True)
class FloodResult:
    low: int
    high: int


async def flood(conn: AsyncConnection, sandbox_id: str, settings: Settings) -> FloodResult:
    """Enqueue a burst of low-priority jobs and a few high-priority ones."""
    await touch_sandbox(conn, sandbox_id)
    queued = (
        await conn.execute(text("SELECT count(*) FROM task_instances WHERE status = 'ready'"))
    ).scalar_one()
    if queued + settings.demo_flood_low + settings.demo_flood_high > settings.demo_max_queued:
        raise DemoLimitError("the queue is already deep; give the workers a moment")
    backoff = BackoffPolicy(base_s=0.5, max_s=2)
    for priority, count in ((-20, settings.demo_flood_low), (60, settings.demo_flood_high)):
        for _ in range(count):
            await task_store.enqueue_job(
                conn,
                task_store.TaskSpec(
                    task_type="sleep",
                    params={"seconds": 0.4},
                    priority=priority,
                    backoff=backoff,
                    sandbox_id=sandbox_id,
                ),
            )
    return FloodResult(settings.demo_flood_low, settings.demo_flood_high)


async def sandbox_state(conn: AsyncConnection, sandbox_id: str) -> dict[str, Any]:
    failures = (
        await conn.execute(
            text("SELECT failures_enabled FROM demo_sandboxes WHERE id = :id"), {"id": sandbox_id}
        )
    ).scalar_one_or_none()
    runs = await conn.execute(
        text(
            """
            SELECT id, status, created_at FROM workflow_runs
            WHERE sandbox_id = :id AND workflow_id IS NOT NULL
            ORDER BY created_at DESC LIMIT 10
            """
        ),
        {"id": sandbox_id},
    )
    flood_rows = await conn.execute(
        text(
            """
            SELECT CASE WHEN t.priority > 0 THEN 'high' ELSE 'low' END AS band,
                   t.status, count(*) AS n, max(t.effective_priority - t.priority) AS max_bonus
            FROM task_instances t JOIN workflow_runs r ON r.id = t.run_id
            WHERE r.sandbox_id = :id AND r.workflow_id IS NULL
            GROUP BY 1, 2
            """
        ),
        {"id": sandbox_id},
    )
    flood_state: dict[str, dict[str, int]] = {"low": {}, "high": {}}
    max_bonus = 0
    for row in flood_rows:
        flood_state[row.band][row.status] = int(row.n)
        max_bonus = max(max_bonus, int(row.max_bonus or 0))
    return {
        "sandbox_id": sandbox_id,
        "failures_enabled": bool(failures),
        "runs": [dict(r._mapping) for r in runs],
        "flood": {**flood_state, "max_aging_bonus": max_bonus},
    }


async def owns_run(conn: AsyncConnection, sandbox_id: str, run_id: UUID) -> bool:
    owner = (
        await conn.execute(
            text("SELECT sandbox_id FROM workflow_runs WHERE id = :id"), {"id": run_id}
        )
    ).scalar_one_or_none()
    return owner == sandbox_id


async def reset_demo_data(conn: AsyncConnection, *, retention_s: float) -> dict[str, int]:
    """Delete finished runs, idle sandboxes and departed workers older than the retention."""
    deleted: dict[str, int] = {}
    for name, sql in (
        (
            "runs",
            "DELETE FROM workflow_runs WHERE status <> 'running' "
            "AND COALESCE(finished_at, created_at) < now() - make_interval(secs => :r)",
        ),
        # Runs stuck on a dead letter nobody replayed are 'failed', so the line
        # above covers them; this catches anything wedged far beyond retention.
        (
            "stale_runs",
            "DELETE FROM workflow_runs WHERE created_at < now() - make_interval(secs => :r * 4)",
        ),
        (
            "sandboxes",
            "DELETE FROM demo_sandboxes WHERE last_seen_at < now() - make_interval(secs => :r)",
        ),
        (
            "workers",
            "DELETE FROM workers WHERE status IN ('dead', 'stopped') "
            "AND last_heartbeat_at < now() - interval '10 minutes'",
        ),
        (
            "commands",
            "DELETE FROM worker_commands WHERE requested_at < now() - interval '1 hour'",
        ),
    ):
        result = await conn.execute(text(sql), {"r": retention_s})
        deleted[name] = int(result.rowcount)
    return deleted
