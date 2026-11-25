"""Kill every Quorum process mid-run, restart, and require a correct finish."""

from __future__ import annotations

import signal
from collections.abc import Iterator

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quorum.domain.dag import WorkflowDefinition
from quorum.store import runs as run_store
from quorum.store import workflows as wf_store
from tests.chaos.conftest import Cluster
from tests.conftest import eventually
from tests.integration.http_fixture import local_http_server

FAST_BACKOFF = {"base_s": 0.05, "factor": 1, "max_s": 0.05, "jitter": "none"}


@pytest.fixture
def base_url() -> Iterator[str]:
    with local_http_server() as url:
        yield url


async def _status(db: asyncpg.Connection, run_id: object, node: str) -> str:
    return str(
        await db.fetchval(
            "SELECT status FROM task_instances WHERE run_id = $1 AND node_key = $2", run_id, node
        )
    )


async def test_full_system_restart_mid_run_resumes_with_effects_exactly_once(
    cluster: Cluster, engine: AsyncEngine, db: asyncpg.Connection, base_url: str
) -> None:
    cluster.extra_env["QUORUM_HTTP_ALLOWLIST"] = "127.0.0.1"
    definition = WorkflowDefinition.model_validate(
        {
            "nodes": [
                {
                    "key": "fetch",
                    "task_type": "http_fetch",
                    "params": {"url": f"{base_url}/orders"},
                },
                {
                    "key": "store",
                    "task_type": "store",
                    "depends_on": ["fetch"],
                    "params": {
                        "collection": "orders",
                        "records": {"$from": "fetch", "path": "json"},
                    },
                },
                {
                    "key": "slow",
                    "task_type": "sleep",
                    "depends_on": ["fetch"],
                    "params": {"seconds": 3},
                    "backoff": FAST_BACKOFF,
                },
                {
                    "key": "notify",
                    "task_type": "notify",
                    "depends_on": ["store", "slow"],
                    "params": {
                        "message": "stored ${n} orders",
                        "vars": {"n": {"$from": "store", "path": "stored"}},
                        "hold_s": 2,
                    },
                    "backoff": FAST_BACKOFF,
                },
            ]
        }
    )
    async with engine.begin() as conn:
        workflow = await wf_store.create_workflow(conn, name="restart", definition=definition)
        run = await run_store.create_workflow_run(conn, workflow.id)

    async def start_everything() -> None:
        cluster.scheduler()
        for n in range(2):
            await cluster.worker(db, f"worker-{len(cluster.procs)}-{n}")

    def kill_everything() -> None:
        for proc in cluster.procs:
            if proc.popen.poll() is None:
                proc.signal(signal.SIGKILL)
                proc.popen.wait()

    # --- Crash 1: one branch finished, the other mid-flight.
    await start_everything()

    async def mid_branch() -> bool:
        return (
            await _status(db, run.run_id, "store") == "succeeded"
            and await _status(db, run.run_id, "slow") == "running"
        )

    await eventually(mid_branch, within=20, what="store done while slow is running")
    kill_everything()

    # --- Crash 2: notify has delivered but not yet completed.
    await start_everything()

    async def delivered() -> bool:
        return bool(await db.fetchval("SELECT count(*) FROM notifications"))

    await eventually(delivered, within=30, what="notification delivered")
    assert await _status(db, run.run_id, "notify") == "running"
    kill_everything()

    # --- Recovery.
    await start_everything()

    async def finished() -> str | None:
        status = await db.fetchval("SELECT status FROM workflow_runs WHERE id = $1", run.run_id)
        return status if status in ("succeeded", "failed") else None

    assert await eventually(finished, within=30, what="run to finish") == "succeeded"
    assert await db.fetchval("SELECT count(*) FROM stored_records") == 1
    assert await db.fetchval("SELECT count(*) FROM notifications") == 1
    assert await db.fetchval("SELECT message FROM notifications") == "stored 8 orders"
    attempts = {
        r["node_key"]: r["attempt"]
        for r in await db.fetch(
            "SELECT node_key, attempt FROM task_instances WHERE run_id = $1", run.run_id
        )
    }
    assert attempts["slow"] >= 2, "the killed sleep was re-run"
    assert attempts["notify"] >= 2, "the killed notify was re-run (and deduplicated)"
    expired = await db.fetchval(
        "SELECT count(*) FROM run_events WHERE run_id = $1 AND type = 'lease_expired'", run.run_id
    )
    assert expired >= 2
