"""Realtime: notify on every run_events insert; indexes for metrics windows.

A statement-level trigger, not a row-level one, and an empty payload:
Postgres folds identical notifications within a transaction, so a claim
that inserts 50 events wakes listeners once. Listeners read new rows by id
(``id > last_seen``), so the notification is purely a wake-up and losing
one costs latency, never data.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION conductor_notify_events() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('conductor_events', '');
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER run_events_notify
        AFTER INSERT ON run_events
        FOR EACH STATEMENT EXECUTE FUNCTION conductor_notify_events()
        """
    )
    # Metrics and the overview chart look at recent windows of events by type.
    op.execute("CREATE INDEX run_events_type_at_idx ON run_events (type, at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS run_events_type_at_idx")
    op.execute("DROP TRIGGER IF EXISTS run_events_notify ON run_events")
    op.execute("DROP FUNCTION IF EXISTS conductor_notify_events()")
