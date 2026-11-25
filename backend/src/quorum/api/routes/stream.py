"""``GET /stream``: Server-Sent Events for run events, workers and overview stats.

Events:

* ``run_event``: one row of ``run_events`` (with ``node_key``). Carries an
  SSE ``id``; on reconnect the browser sends ``Last-Event-ID`` and the
  missed events are replayed from the table, so reconnects are lossless.
* ``workers`` / ``stats``: snapshots every couple of seconds (no ``id``:
  only the latest one matters).
* ``hello``: sent first, with the server time, so clients can correct for
  clock skew when showing heartbeat ages.

A comment line is sent every ``keepalive_s`` so proxies don't close idle
connections.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from quorum.api.deps import Hub
from quorum.api.hub import Message, dumps

router = APIRouter(tags=["realtime"])
KEEPALIVE_S = 15.0


@router.get(
    "/stream",
    summary="Live events (Server-Sent Events)",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream(
    request: Request,
    hub: Hub,
    run_id: Annotated[UUID | None, Query(description="Only this run's events.")] = None,
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
    after: Annotated[
        int | None, Query(ge=0, description="Replay events after this id (EventSource polyfills).")
    ] = None,
    recent: Annotated[
        int,
        Query(ge=0, le=100, description="On a fresh connect, first send this many past events."),
    ] = 0,
) -> StreamingResponse:
    run_filter = str(run_id) if run_id else None
    resume_from = last_event_id if last_event_id is not None else after

    async def body() -> AsyncIterator[bytes]:
        async with hub.subscribe(run_filter) as sub:
            hello = {"server_time": datetime.now(UTC), "resumed_from": resume_from}
            yield Message("hello", dumps(hello)).encode()
            # Subscribe first, then replay: anything published meanwhile is
            # queued, and duplicates are dropped by id below.
            replay_from = resume_from
            if replay_from is None and recent:
                replay_from = await hub.recent_start(recent, run_id=run_filter)
            last_sent = replay_from or 0
            if replay_from is not None:
                for missed in await hub.backlog(after_id=replay_from, run_id=run_filter):
                    last_sent = missed.id or last_sent
                    yield missed.encode()
            for snapshot in await hub.snapshot_messages():
                yield snapshot.encode()
            while True:
                try:
                    async with asyncio.timeout(KEEPALIVE_S):
                        message = await sub.queue.get()
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield b": keepalive\n\n"
                    continue
                if message is None:  # hub shutting down, or we fell behind
                    return
                if message.id is not None:
                    if message.id <= last_sent:
                        continue
                    last_sent = message.id
                yield message.encode()

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    return StreamingResponse(body(), media_type="text/event-stream", headers=headers)
