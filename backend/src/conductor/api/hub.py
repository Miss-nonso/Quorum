"""Fan-out of live events to SSE clients, one database listener per API process.

The hub keeps **one** LISTEN connection and **one** set of polls, however
many browsers are connected, so database load is independent of audience
size. It is driven the same way workers are:

* ``NOTIFY conductor_events`` (sent by a trigger on ``run_events``) is only
  a wake-up. The hub always reads rows with ``id > last_seen``, so a lost
  notification delays an event; it never drops one.
* A poll every ``poll_interval_s`` is the safety net.
* Worker and overview snapshots are published every ``snapshot_interval_s``.

Each subscriber has a bounded queue. A client too slow to keep up is
disconnected (it will reconnect with ``Last-Event-ID`` and catch up from the
table) rather than letting memory grow without bound.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
from sqlalchemy.ext.asyncio import AsyncEngine

from conductor.store import queries

logger = logging.getLogger(__name__)

EVENTS_CHANNEL = "conductor_events"
QUEUE_SIZE = 1000


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Message:
    """One SSE message. ``id`` is set only for replayable run events."""

    event: str
    data: str
    id: int | None = None
    run_id: str | None = None

    def encode(self) -> bytes:
        lines = []
        if self.id is not None:
            lines.append(f"id: {self.id}")
        lines.append(f"event: {self.event}")
        lines.extend(f"data: {line}" for line in self.data.splitlines() or [""])
        return ("\n".join(lines) + "\n\n").encode()


@dataclass(eq=False)
class Subscriber:
    run_id: str | None
    queue: asyncio.Queue[Message | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE)
    )

    def wants(self, message: Message) -> bool:
        return self.run_id is None or message.run_id is None or message.run_id == self.run_id


class EventHub:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        dsn: str,
        poll_interval_s: float = 1.0,
        snapshot_interval_s: float = 2.0,
    ) -> None:
        self._engine = engine
        self._dsn = dsn
        self._poll_interval_s = poll_interval_s
        self._snapshot_interval_s = snapshot_interval_s
        self._subscribers: set[Subscriber] = set()
        self._wake = asyncio.Event()
        self._last_id = 0
        self._tasks: list[asyncio.Task[None]] = []
        self.connected = False  # is the LISTEN connection up (for /health detail)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        async with self._engine.connect() as conn:
            self._last_id = await queries.latest_event_id(conn)
        self._tasks = [
            asyncio.create_task(self._listen(), name="hub-listen"),
            asyncio.create_task(self._pump(), name="hub-pump"),
            asyncio.create_task(self._snapshots(), name="hub-snapshots"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for sub in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(None)

    # ------------------------------------------------------------------ subscribe

    @contextlib.asynccontextmanager
    async def subscribe(self, run_id: str | None = None) -> AsyncIterator[Subscriber]:
        sub = Subscriber(run_id=run_id)
        self._subscribers.add(sub)
        try:
            yield sub
        finally:
            self._subscribers.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def backlog(self, *, after_id: int, run_id: str | None) -> list[Message]:
        """Events a reconnecting client missed (from the table, not memory)."""
        async with self._engine.connect() as conn:
            rows = await queries.events(
                conn, after_id=after_id, run_id=UUID(run_id) if run_id else None, limit=1000
            )
        return [self._event_message(r) for r in rows]

    async def recent_start(self, count: int, *, run_id: str | None) -> int:
        """The event id to replay from so that the last ``count`` events are sent."""
        async with self._engine.connect() as conn:
            return await queries.nth_latest_event_id(
                conn, count, run_id=UUID(run_id) if run_id else None
            )

    async def snapshot_messages(self) -> list[Message]:
        """Current workers and stats, so a new client paints immediately."""
        async with self._engine.connect() as conn:
            workers = await queries.list_workers(conn)
            stats = await queries.stats(conn)
        return [Message("workers", dumps(workers)), Message("stats", dumps(stats))]

    # ------------------------------------------------------------------ internals

    def _publish(self, message: Message) -> None:
        for sub in list(self._subscribers):
            if not sub.wants(message):
                continue
            try:
                sub.queue.put_nowait(message)
            except asyncio.QueueFull:
                # Too slow: cut it loose; it reconnects and catches up by id.
                self._subscribers.discard(sub)
                with contextlib.suppress(asyncio.QueueEmpty):
                    sub.queue.get_nowait()
                sub.queue.put_nowait(None)

    @staticmethod
    def _event_message(row: dict[str, Any]) -> Message:
        return Message(
            event="run_event", data=dumps(row), id=int(row["id"]), run_id=str(row["run_id"])
        )

    async def _listen(self) -> None:
        backoff = 0.5
        while True:
            conn: asyncpg.Connection | None = None
            closed = asyncio.Event()
            try:
                conn = await asyncpg.connect(self._dsn, timeout=5)
                conn.add_termination_listener(lambda _, done=closed: done.set())
                await conn.add_listener(EVENTS_CHANNEL, lambda *_: self._wake.set())
                self.connected = True
                backoff = 0.5
                self._wake.set()
                await closed.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("event listener failed; polling only", extra={"error": repr(exc)})
            finally:
                self.connected = False
                if conn is not None and not conn.is_closed():
                    with contextlib.suppress(Exception):
                        await conn.close(timeout=2)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10)

    async def _pump(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._poll_interval_s):
                    await self._wake.wait()
            self._wake.clear()
            if not self._subscribers:
                # Nobody listening: just keep the cursor current.
                try:
                    async with self._engine.connect() as conn:
                        self._last_id = await queries.latest_event_id(conn)
                except Exception:
                    logger.exception("event cursor refresh failed")
                continue
            try:
                while True:
                    async with self._engine.connect() as conn:
                        rows = await queries.events(conn, after_id=self._last_id, limit=500)
                    for row in rows:
                        self._publish(self._event_message(row))
                        self._last_id = int(row["id"])
                    if len(rows) < 500:
                        break
            except Exception:
                logger.exception("event pump failed")

    async def _snapshots(self) -> None:
        while True:
            await asyncio.sleep(self._snapshot_interval_s)
            if not self._subscribers:
                continue
            try:
                for message in await self.snapshot_messages():
                    self._publish(message)
            except Exception:
                logger.exception("snapshot failed")
