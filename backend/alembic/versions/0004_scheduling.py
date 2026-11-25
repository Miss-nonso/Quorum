"""Scheduling: cron schedules, a duplicate-firing guard, and leader state.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE schedules (
            id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workflow_id    uuid NOT NULL UNIQUE REFERENCES workflows (id) ON DELETE CASCADE,
            cron           text NOT NULL,
            timezone       text NOT NULL,
            next_fire_at   timestamptz NOT NULL,
            last_fired_at  timestamptz
        )
        """
    )
    # The scheduler's poll: "which schedules are due?"
    op.execute("CREATE INDEX schedules_due_idx ON schedules (next_fire_at)")
    # Belt and braces for leader failover: even if two schedulers briefly both
    # believe they lead, a scheduled instant can produce only one run.
    op.execute(
        "CREATE UNIQUE INDEX workflow_runs_schedule_once_idx "
        "ON workflow_runs (workflow_id, scheduled_for) WHERE trigger = 'schedule'"
    )
    # One row: who leads right now (for the dashboard; the advisory lock is the authority).
    op.execute(
        """
        CREATE TABLE scheduler_state (
            id            smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            leader_id     uuid NOT NULL,
            hostname      text NOT NULL,
            pid           integer NOT NULL,
            elected_at    timestamptz NOT NULL DEFAULT now(),
            heartbeat_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scheduler_state")
    op.execute("DROP INDEX IF EXISTS workflow_runs_schedule_once_idx")
    op.execute("DROP TABLE IF EXISTS schedules")
