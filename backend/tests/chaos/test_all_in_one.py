"""`quorum all`: the whole backend in one container (free hosting tiers)."""

from __future__ import annotations

import os
import signal
import socket
import uuid

import asyncpg
import httpx

from tests.chaos.conftest import Cluster
from tests.conftest import eventually

SANDBOX = {"X-Sandbox-Id": "all-in-one-test-sandbox"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_all_in_one_runs_the_demo_and_respawns_killed_workers(
    cluster: Cluster, db: asyncpg.Connection
) -> None:
    port = free_port()
    cluster.extra_env.update(
        {
            "PORT": str(port),
            "QUORUM_DEMO_MODE": "true",
            "QUORUM_DEMO_CHAOS": "true",
            "QUORUM_DEMO_STEP_DELAY_S": "0.3",
            "QUORUM_ALL_IN_ONE_WORKERS": "2",
            "QUORUM_SHUTDOWN_GRACE_S": "3",
        }
    )
    proc = cluster._spawn("all-in-one", "all")
    base = f"http://127.0.0.1:{port}/api/v1"

    async with httpx.AsyncClient(timeout=10) as client:

        async def ready() -> bool:
            try:
                healthy = (await client.get(f"{base}/health")).status_code == 200
            except httpx.TransportError:
                return False
            active = await db.fetchval("SELECT count(*) FROM workers WHERE status = 'active'")
            return healthy and active == 2

        await eventually(ready, within=60, what="migrate, seed, API and two workers")
        assert (await client.get(f"{base}/meta")).json()["demo_mode"] is True

        # The seeded sample fetches from this same container, and runs to completion.
        run = (await client.post(f"{base}/demo/runs", headers=SANDBOX)).json()

        async def run_status(want: str) -> bool:
            status = await db.fetchval(
                "SELECT status FROM workflow_runs WHERE id = $1", uuid.UUID(run["id"])
            )
            return bool(status == want)

        await eventually(lambda: run_status("succeeded"), within=30, what="the sample run")

        # Kill a worker through the demo API: a real process dies, a new one replaces it.
        before = {r["id"] for r in await db.fetch("SELECT id FROM workers")}
        killed = (await client.post(f"{base}/demo/chaos/kill-worker", headers=SANDBOX)).json()
        victim_pid = await db.fetchval(
            "SELECT pid FROM workers WHERE id = $1", uuid.UUID(killed["worker_id"])
        )

        async def victim_gone() -> bool:
            return not alive(victim_pid)

        await eventually(victim_gone, within=10, what="the killed worker process to exit")

        async def replaced() -> bool:
            rows = await db.fetch("SELECT id FROM workers WHERE status = 'active'")
            fresh = {r["id"] for r in rows} - before
            return len(rows) >= 2 and bool(fresh)

        await eventually(replaced, within=20, what="a fresh worker to replace the dead one")

    worker_pids = [r["pid"] for r in await db.fetch("SELECT pid FROM workers")]
    proc.signal(signal.SIGTERM)
    # Uvicorn shuts down gracefully, then re-raises the signal it caught, so the
    # exit status is "terminated by SIGTERM"; what matters is that it exits
    # promptly and its lifespan stopped every worker it started.
    assert proc.popen.wait(timeout=20) in (0, -signal.SIGTERM)
    leftovers = [pid for pid in worker_pids if alive(pid)]
    assert leftovers == [], "no orphaned worker processes after shutdown"
