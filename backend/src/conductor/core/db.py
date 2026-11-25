"""Database engine and session factory.

PostgreSQL is Conductor's only data store. One async engine is created per
process and shared; sessions are short-lived and scoped to a unit of work.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from conductor.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the process-wide async engine. No connection is opened yet."""
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
        connect_args={"timeout": settings.db_connect_timeout_s},
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
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
