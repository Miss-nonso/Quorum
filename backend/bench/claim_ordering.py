"""Benchmark: where should priority aging be computed?

A. **Periodic update** (what Quorum does): a leader job rewrites
   ``effective_priority``; the claim query orders by the stored column and
   walks the partial index ``(effective_priority DESC, created_at)``.
B. **At claim time**: the claim query orders by
   ``priority + LEAST(cap, floor(age / interval))``, which no index can serve,
   so every claim sorts the whole ready set.

Usage (against any PostgreSQL 16 you can create databases on)::

    uv run python bench/claim_ordering.py [--rows 50000] [--claims 300]

Each claim runs in a transaction that is rolled back, so the data set stays
identical across iterations and strategies.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import statistics
import time

import asyncpg

from quorum.core.db import asyncpg_dsn

CLAIM_A = """
WITH c AS (
  SELECT id FROM task_instances
  WHERE status = 'ready' AND not_before <= now()
  ORDER BY effective_priority DESC, created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
UPDATE task_instances t SET status = 'running', lease_owner = gen_random_uuid(),
  lease_expires_at = now() + interval '30 seconds' FROM c WHERE t.id = c.id RETURNING t.id
"""

CLAIM_B = """
WITH c AS (
  SELECT id FROM task_instances
  WHERE status = 'ready' AND not_before <= now()
  ORDER BY priority + LEAST(200, floor(EXTRACT(EPOCH FROM now() - not_before) / 10))::int DESC,
           created_at
  LIMIT 1 FOR UPDATE SKIP LOCKED)
UPDATE task_instances t SET status = 'running', lease_owner = gen_random_uuid(),
  lease_expires_at = now() + interval '30 seconds' FROM c WHERE t.id = c.id RETURNING t.id
"""

AGE = """
UPDATE task_instances SET effective_priority =
  priority + LEAST(200, floor(EXTRACT(EPOCH FROM now() - not_before) / 10))::int
WHERE status = 'ready'
"""


async def timed(conn: asyncpg.Connection, sql: str, n: int) -> list[float]:
    samples = []
    for _ in range(n):
        tx = conn.transaction()
        await tx.start()
        start = time.perf_counter()
        await conn.fetchval(sql)
        samples.append((time.perf_counter() - start) * 1000)
        await tx.rollback()
    return samples


def summary(name: str, samples: list[float]) -> str:
    p = statistics.quantiles(samples, n=100)
    return f"{name:<34} p50 {p[49]:7.3f} ms   p95 {p[94]:7.3f} ms   max {max(samples):7.3f} ms"


async def main(rows: int, claims: int) -> None:
    server = asyncpg_dsn(
        os.environ.get(
            "TEST_DATABASE_URL", "postgresql+asyncpg://quorum:quorum@localhost:5433/quorum"
        )
    )
    name = f"bench_{secrets.token_hex(4)}"
    admin = await asyncpg.connect(server.rsplit("/", 1)[0] + "/postgres")
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        dsn = server.rsplit("/", 1)[0] + f"/{name}"
        os.environ["QUORUM_DATABASE_URL"] = "postgresql+asyncpg" + dsn[len("postgresql") :]
        proc = await asyncio.create_subprocess_exec("alembic", "upgrade", "head", cwd=".")
        await proc.wait()
        conn = await asyncpg.connect(dsn)
        run_id = "00000000-0000-0000-0000-000000000001"
        await conn.execute("INSERT INTO workflow_runs (id, status) VALUES ($1, 'running')", run_id)
        await conn.execute(
            """
            INSERT INTO task_instances (run_id, node_key, task_type, idempotency_key, priority,
                                        effective_priority, not_before)
            SELECT $1, 'n' || g, 'sleep', 'k' || g,
                   (g % 21) - 10, (g % 21) - 10, now() - (g % 3600) * interval '1 second'
            FROM generate_series(1, $2::int) g
            """,
            run_id,
            rows,
        )
        await conn.execute("ANALYZE task_instances")
        start = time.perf_counter()
        await conn.execute(AGE)
        age_ms = (time.perf_counter() - start) * 1000
        await conn.execute("ANALYZE task_instances")

        a = await timed(conn, CLAIM_A, claims)
        b = await timed(conn, CLAIM_B, claims)
        print(f"ready rows: {rows:,}   claims per strategy: {claims}")
        print(summary("A  index-ordered claim", a))
        print(summary("B  aging computed in claim", b))
        print(f"A' one full aging pass (worst case)  {age_ms:8.1f} ms")
        await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--claims", type=int, default=300)
    args = parser.parse_args()
    asyncio.run(main(args.rows, args.claims))
