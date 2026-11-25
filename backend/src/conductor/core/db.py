"""Database engine and session factory.

PostgreSQL is Conductor's only data store. One async engine is created per
process and shared; sessions are short-lived and scoped to a unit of work.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from conductor.core.config import Settings

# libpq-style query parameters that hosted providers (Neon, Supabase, Render)
# put in connection strings. asyncpg either takes them as keyword arguments
# under another name (sslmode -> ssl) or must not receive them at all
# (channel_binding would be forwarded to the server as an unknown setting).
_LIBPQ_ONLY = frozenset({"sslmode", "channel_binding", "sslrootcert", "sslcert", "sslkey"})


def engine_args(database_url: str) -> tuple[str, dict[str, Any]]:
    """Split a URL into (URL asyncpg-safe for SQLAlchemy, extra connect_args)."""
    url = make_url(database_url)
    connect_args: dict[str, Any] = {}
    sslmode = url.query.get("sslmode")
    if isinstance(sslmode, str):
        connect_args["ssl"] = False if sslmode == "disable" else sslmode
    clean = url.difference_update_query(_LIBPQ_ONLY)
    return clean.render_as_string(hide_password=False), connect_args


def create_engine(settings: Settings, *, pool_size: int | None = None) -> AsyncEngine:
    """Create the process-wide async engine. No connection is opened yet."""
    url, connect_args = engine_args(settings.database_url)
    return create_async_engine(
        url,
        pool_size=pool_size or settings.db_pool_size,
        pool_pre_ping=True,
        connect_args={"timeout": settings.db_connect_timeout_s, **connect_args},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to ``engine``.

    ``expire_on_commit=False`` lets callers read attributes after commit
    without an implicit (and surprising) extra round trip.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def ping(engine: AsyncEngine) -> None:
    """Run a trivial query; raises if the database is unreachable."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


def asyncpg_dsn(database_url: str) -> str:
    """Convert a SQLAlchemy URL to a plain DSN for direct asyncpg connections.

    Used for long-lived dedicated connections (LISTEN, and advisory locks
    later) that must not be borrowed from, or returned to, the pool.
    """
    # asyncpg understands sslmode in a DSN but would send channel_binding to the
    # server as a runtime setting, which fails; drop it (asyncpg negotiates SCRAM itself).
    url = make_url(database_url).set(drivername="postgresql")
    return url.difference_update_query(["channel_binding"]).render_as_string(hide_password=False)
