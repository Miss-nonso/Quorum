"""Baseline: require PostgreSQL 16 and fix the database's session time zone.

The engine's tables arrive in 0002 alongside the code that uses them. This
revision anchors the migration chain and enforces two assumptions the rest
of the schema relies on:

* PostgreSQL 16+ (SKIP LOCKED, advisory locks and NOTIFY payload semantics
  are all relied upon; 16 is the version CI and production run).
* Sessions default to UTC, so ``now()`` and timestamptz rendering never
  depend on the server's local zone. Schedules carry their own IANA zone.

Revision ID: 0001
Revises:
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIN_SERVER_VERSION_NUM = 160000


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF current_setting('server_version_num')::int < {MIN_SERVER_VERSION_NUM} THEN
                RAISE EXCEPTION 'Quorum requires PostgreSQL 16 or newer (found %)',
                    current_setting('server_version');
            END IF;
            EXECUTE format('ALTER DATABASE %I SET timezone TO ''UTC''', current_database());
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "EXECUTE format('ALTER DATABASE %I RESET timezone', current_database()); "
        "END $$;"
    )
