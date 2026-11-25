"""Alembic environment: runs migrations over the async engine.

Migrations are written by hand (no autogenerate) so every index and
constraint is deliberate and reviewed; there is therefore no target
metadata here.
"""

from __future__ import annotations

import asyncio
import logging.config

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from quorum.core.config import get_settings
from quorum.core.db import engine_args

config = context.config

# `quorum migrate` configures JSON logging itself; only fall back to the
# ini-file logging when alembic is invoked directly from the CLI.
if config.config_file_name is not None and not logging.getLogger().handlers:
    logging.config.fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    # Tests set sqlalchemy.url explicitly to point at a throwaway database.
    explicit = config.get_main_option("sqlalchemy.url")
    if explicit:
        return explicit
    return get_settings().database_url


def _run(connection: Connection) -> None:
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    url, connect_args = engine_args(_database_url())
    engine = create_async_engine(url, connect_args=connect_args)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_run)
            await conn.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
