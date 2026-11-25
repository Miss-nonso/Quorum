"""Leader election with a PostgreSQL advisory lock.

The leader holds ``pg_try_advisory_lock(KEY)`` on a dedicated connection.
Postgres releases the lock the moment that session ends (process killed,
connection dropped, server restart), so a standby acquires it on its next
attempt. The leader probes its own connection every ``check_interval_s`` and
abandons its duties the instant the probe fails.

There is an unavoidable window where a partitioned old leader has not yet
noticed. Leader duties are therefore written to be safe even if two
instances run them at once (SKIP LOCKED everywhere, a unique index on
scheduled runs); leadership only avoids wasted duplicate work. See ADR 0004.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID, uuid4

import asyncpg

logger = logging.getLogger(__name__)

# "Cond" in ASCII: an arbitrary, fixed 64-bit key shared by every scheduler.
LEADER_LOCK_KEY = 0x436F6E64


class LeaderElector:
    """Campaign for leadership; while elected, run ``duties`` until it is lost."""

    def __init__(
        self,
        *,
        dsn: str,
        check_interval_s: float = 2.0,
        retry_interval_s: float = 2.0,
        key: int = LEADER_LOCK_KEY,
        instance_id: UUID | None = None,
    ) -> None:
        self.id = instance_id or uuid4()
        self._dsn = dsn
        self._check_interval_s = check_interval_s
        self._retry_interval_s = retry_interval_s
        self._key = key
        self._stopping = asyncio.Event()
        self.is_leader = False

    def request_stop(self) -> None:
        self._stopping.set()

    async def run(self, duties: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Campaign until stopped. ``duties`` runs only while this instance leads."""
        while not self._stopping.is_set():
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(self._dsn, timeout=5)
                if await conn.fetchval("SELECT pg_try_advisory_lock($1)", self._key):
                    await self._lead(conn, duties)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("leader election error", extra={"error": repr(exc)})
            finally:
                self.is_leader = False
                if conn is not None and not conn.is_closed():
                    # Closing the session releases the advisory lock.
                    with contextlib.suppress(Exception):
                        await conn.close(timeout=2)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._retry_interval_s):
                    await self._stopping.wait()

    async def _lead(
        self, conn: asyncpg.Connection, duties: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        self.is_leader = True
        await conn.execute(
            """
            INSERT INTO scheduler_state (id, leader_id, hostname, pid, elected_at, heartbeat_at)
            VALUES (1, $1, $2, $3, now(), now())
            ON CONFLICT (id) DO UPDATE SET leader_id = EXCLUDED.leader_id,
                hostname = EXCLUDED.hostname, pid = EXCLUDED.pid,
                elected_at = now(), heartbeat_at = now()
            """,
            self.id,
            socket.gethostname(),
            os.getpid(),
        )
        logger.warning("elected scheduler leader", extra={"leader_id": str(self.id)})
        work: asyncio.Task[None] = asyncio.create_task(duties(), name="leader-duties")
        try:
            while not self._stopping.is_set() and not work.done():
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(self._check_interval_s):
                        await self._stopping.wait()
                try:
                    async with asyncio.timeout(self._check_interval_s):
                        await conn.execute(
                            "UPDATE scheduler_state SET heartbeat_at = now() "
                            "WHERE id = 1 AND leader_id = $1",
                            self.id,
                        )
                except Exception as exc:
                    logger.error("lost scheduler leadership", extra={"error": repr(exc)})
                    return
            if work.done() and not work.cancelled() and work.exception() is not None:
                logger.error("leader duties crashed", exc_info=work.exception())
        finally:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            self.is_leader = False
