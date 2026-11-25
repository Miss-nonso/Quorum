"""Shared dependencies for route handlers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from conductor.api.hub import EventHub
from conductor.worker.registry import TaskRegistry


async def read_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """A connection for read-only handlers."""
    async with request.app.state.engine.connect() as conn:
        yield conn


async def write_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """A connection inside a transaction, committed when the handler returns normally."""
    async with request.app.state.engine.begin() as conn:
        yield conn


def registry(request: Request) -> TaskRegistry:
    reg: TaskRegistry = request.app.state.registry
    return reg


def hub(request: Request) -> EventHub:
    h: EventHub = request.app.state.hub
    return h


ReadConn = Annotated[AsyncConnection, Depends(read_conn)]
WriteConn = Annotated[AsyncConnection, Depends(write_conn)]
Registry = Annotated[TaskRegistry, Depends(registry)]
Hub = Annotated[EventHub, Depends(hub)]
Limit = Annotated[int, Query(ge=1, le=100, description="Page size.")]
Offset = Annotated[int, Query(ge=0, le=100_000, description="Items to skip.")]
