"""End-to-end engine benchmarks: throughput, claim latency, recovery time.

Runs against a throwaway database on the server in TEST_DATABASE_URL (the
Compose database by default) with real `quorum worker` processes:

    uv run python bench/engine.py [--jobs 5000] [--quick]

Prints a Markdown report; docs/benchmarks.md records a run and its hardware.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import secrets
import signal
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine

from quorum.core.db import asyncpg_dsn
from quorum.store import tasks as task_store

BACKEND = Path(__file__).resolve().parents[1]
SERVER = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://quorum:quorum@localhost:5433/quorum"
)


def pct(samples: list[float], p: int) -> float:
    return statistics.quantiles(samples, n=100)[p - 1] if len(samples) > 1 else samples[0]


@contextmanager
def processes(
    url: str, env_extra: dict[str, str], *commands: list[str]
) -> Iterator[list[subprocess.Popen[bytes]]]:
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("QUORUM_")},
        "QUORUM_DATABASE_URL": url,
        "QUORUM_LOG_LEVEL": "WARNING",
        **env_extra,
    }
    procs = [
        subprocess.Popen(  # noqa: S603 - our own module, fixed argv
            [sys.executable, "-m", "quorum", *cmd],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for cmd in commands
    ]
    try:
        yield procs
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()


async def fresh_db() -> tuple[str, str]:
    base = SERVER.rsplit("/", 1)[0]
    name = f"bench_{secrets.token_hex(4)}"
    admin = await asyncpg.connect(asyncpg_dsn(base + "/postgres"))
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    url = f"{base}/{name}"
    env = {**os.environ, "QUORUM_DATABASE_URL": url}
    migrate = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "quorum",
        "migrate",
        cwd=BACKEND,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await migrate.wait() != 0:
        raise RuntimeError("migration of the benchmark database failed")
    return url, name


async def drop_db(name: str) -> None:
    admin = await asyncpg.connect(asyncpg_dsn(SERVER.rsplit("/", 1)[0] + "/postgres"))
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.close()


async def bulk_jobs(conn: asyncpg.Connection, n: int, seconds: float = 0.0) -> None:
    await conn.execute("TRUNCATE workflow_runs, workers CASCADE")
    await conn.execute(
        """
        WITH runs AS (
            INSERT INTO workflow_runs (status, trigger) SELECT 'running', 'api'
            FROM generate_series(1, $1) RETURNING id
        )
        INSERT INTO task_instances (run_id, node_key, task_type, params, idempotency_key)
        SELECT id, 'job', 'sleep', jsonb_build_object('seconds', $2::float), id::text FROM runs
        """,
        n,
        seconds,
    )


async def throughput(url: str, jobs: int, workers: int, concurrency: int) -> float:
    conn = await asyncpg.connect(asyncpg_dsn(url))
    await bulk_jobs(conn, jobs)
    env = {"QUORUM_WORKER_CONCURRENCY": str(concurrency), "QUORUM_POLL_INTERVAL_S": "0.05"}
    started = time.perf_counter()
    with processes(url, env, *[["worker"]] * workers):
        while True:
            done = await conn.fetchval(
                "SELECT count(*) FROM task_instances WHERE status = 'succeeded'"
            )
            if done >= jobs:
                break
            await asyncio.sleep(0.05)
    elapsed = time.perf_counter() - started
    await conn.close()
    return jobs / elapsed


async def claim_latency(url: str, jobs: int, claimers: int) -> tuple[float, float]:
    """Round trip of the real claim statement (one task), under concurrent claimers."""
    conn = await asyncpg.connect(asyncpg_dsn(url))
    await bulk_jobs(conn, jobs)
    await conn.close()
    engine = create_async_engine(url, pool_size=claimers + 2)
    samples: list[float] = []

    async def claimer() -> None:
        worker = uuid.uuid4()
        while True:
            t0 = time.perf_counter()
            async with engine.begin() as c:
                got = await task_store.claim_tasks(c, worker_id=worker, limit=1, lease_ttl_s=30)
            samples.append((time.perf_counter() - t0) * 1000)
            if not got:
                return

    await asyncio.gather(*(claimer() for _ in range(claimers)))
    await engine.dispose()
    return pct(samples, 50), pct(samples, 95)


async def recovery(
    url: str, ttl: float, heartbeat: float, reaper: float, rounds: int
) -> list[float]:
    """Seconds from SIGKILL of the worker holding a task to another worker re-claiming it."""
    env = {
        "QUORUM_LEASE_TTL_S": str(ttl),
        "QUORUM_HEARTBEAT_INTERVAL_S": str(heartbeat),
        "QUORUM_REAPER_INTERVAL_S": str(reaper),
        "QUORUM_POLL_INTERVAL_S": "0.2",
        "QUORUM_WORKER_UNHEALTHY_AFTER_S": str(max(ttl, 3)),
        "QUORUM_WORKER_DEAD_AFTER_S": str(max(ttl, 3) * 3),
        # The retry backoff after a lost lease is not part of detection: keep it tiny.
    }
    conn = await asyncpg.connect(asyncpg_dsn(url))
    results = []
    for _ in range(rounds):
        await conn.execute("TRUNCATE workflow_runs, workers CASCADE")
        with processes(url, env, ["scheduler"], ["worker"], ["worker"]) as procs:
            while await conn.fetchval("SELECT count(*) FROM workers WHERE status = 'active'") < 2:
                await asyncio.sleep(0.1)
            task_id = await conn.fetchval(
                """
                WITH r AS (INSERT INTO workflow_runs (status, trigger) VALUES ('running', 'api') RETURNING id)
                INSERT INTO task_instances (run_id, node_key, task_type, params, idempotency_key,
                                            backoff_policy)
                SELECT id, 'job', 'sleep', '{"seconds": 60}', id::text,
                       '{"base_s": 0.01, "max_s": 0.01, "jitter": "none"}' FROM r
                RETURNING id
                """
            )
            while (
                owner := await conn.fetchval(
                    "SELECT lease_owner FROM task_instances WHERE id = $1 AND status = 'running'",
                    task_id,
                )
            ) is None:
                await asyncio.sleep(0.05)
            pid = await conn.fetchval("SELECT pid FROM workers WHERE id = $1", owner)
            victim = next(p for p in procs if p.pid == pid)
            killed = time.perf_counter()
            victim.kill()
            while (
                await conn.fetchval(
                    "SELECT attempt FROM task_instances WHERE id = $1 AND status = 'running'",
                    task_id,
                )
                != 2
            ):
                await asyncio.sleep(0.02)
            results.append(time.perf_counter() - killed)
    await conn.close()
    return results


async def main(jobs: int, quick: bool, no_fsync_wait: bool = False) -> None:
    url, name = await fresh_db()
    if no_fsync_wait:
        admin = await asyncpg.connect(asyncpg_dsn(url))
        await admin.execute(f'ALTER DATABASE "{name}" SET synchronous_commit = off')
        await admin.close()
    try:
        print("# Quorum benchmark run\n")
        print(
            f"- Host: {platform.processor() or platform.machine()}, {os.cpu_count()} threads, "
            f"Python {platform.python_version()}, {platform.system()} {platform.release()}"
        )
        server_version = await (await asyncpg.connect(asyncpg_dsn(url))).fetchval(
            "SHOW server_version"
        )
        print(f"- PostgreSQL {server_version} (Docker, default config), jobs per run: {jobs}\n")

        print("## Throughput (no-op tasks)\n\n| Workers × slots | Tasks/s |\n| --- | --- |")
        for workers in [1, 2] if quick else [1, 2, 4]:
            rate = await throughput(url, jobs, workers, 8)
            print(f"| {workers} × 8 | {rate:,.0f} |")

        print(
            "\n## Claim latency (claim statement round trip)\n\n| Concurrent claimers | p50 ms | p95 ms |\n| --- | --- | --- |"
        )
        for claimers in [1, 8] if quick else [1, 4, 16]:
            p50, p95 = await claim_latency(url, min(jobs, 3000), claimers)
            print(f"| {claimers} | {p50:.2f} | {p95:.2f} |")

        print(
            "\n## Recovery after SIGKILL (kill → re-claimed by another worker)\n\n| Lease TTL / heartbeat / reaper | Runs | Median s | Max s |\n| --- | --- | --- | --- |"
        )
        configs = [(6, 1, 1)] if quick else [(6, 1, 1), (30, 5, 5)]
        for ttl, hb, rp in configs:
            times = await recovery(url, ttl, hb, rp, 3)
            print(
                f"| {ttl} s / {hb} s / {rp} s | {len(times)} | {statistics.median(times):.1f} | {max(times):.1f} |"
            )
    finally:
        await drop_db(name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--jobs", type=int, default=5000)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument(
        "--no-fsync-wait",
        action="store_true",
        help="Diagnostic only: synchronous_commit=off on the throwaway DB, to show the commit ceiling.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.jobs, args.quick, args.no_fsync_wait))
