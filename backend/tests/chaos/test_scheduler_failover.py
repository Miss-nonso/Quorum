"""SIGKILL the leading scheduler process; a standby must take over its duties."""

from __future__ import annotations

import signal
import time
from collections.abc import Callable

import asyncpg
from sqlalchemy.ext.asyncio import AsyncEngine

from quorum.domain.dag import WorkflowDefinition
from quorum.store import workflows as wf_store
from tests.chaos.conftest import Cluster
from tests.conftest import eventually

LEADER_ENV = {
    "QUORUM_LEADER_CHECK_INTERVAL_S": "0.3",
    "QUORUM_LEADER_RETRY_INTERVAL_S": "0.3",
    "QUORUM_CRON_TICK_S": "0.2",
}


async def test_standby_scheduler_takes_over_when_the_leader_is_killed(
    cluster: Cluster,
    engine: AsyncEngine,
    db: asyncpg.Connection,
    record_property: Callable[[str, object], None],
) -> None:
    cluster.extra_env.update(LEADER_ENV)
    async with engine.begin() as conn:
        workflow = await wf_store.create_workflow(
            conn,
            name="every-minute",
            definition=WorkflowDefinition.model_validate(
                {"nodes": [{"key": "a", "task_type": "sleep"}]}
            ),
            cron="* * * * *",
        )
    schedulers = [cluster.scheduler(), cluster.scheduler()]

    async def leader_pid() -> int | None:
        pid = await db.fetchval(
            "SELECT pid FROM scheduler_state WHERE heartbeat_at > now() - interval '2 seconds'"
        )
        return int(pid) if pid else None

    first_pid = await eventually(leader_pid, within=15, what="a leader to be elected")
    leader = next(p for p in schedulers if p.pid == first_pid)
    standby = next(p for p in schedulers if p.pid != first_pid)

    leader.signal(signal.SIGKILL)
    killed_at = time.monotonic()

    async def new_leader() -> int | None:
        pid = await leader_pid()
        return pid if pid == standby.pid else None

    await eventually(new_leader, within=10, what="the standby to take over")
    record_property("failover_seconds", round(time.monotonic() - killed_at, 2))

    # The new leader runs the duties: make the schedule due and watch it fire.
    await db.execute(
        "UPDATE schedules SET next_fire_at = now() - interval '1 second' WHERE workflow_id = $1",
        workflow.id,
    )

    async def fired() -> bool:
        count = await db.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE workflow_id = $1 AND trigger = 'schedule'",
            workflow.id,
        )
        return bool(count)

    await eventually(fired, within=10, what="the new leader to fire the schedule")
