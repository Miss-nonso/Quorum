"""Public demo: visitor sandboxes, injected chaos, worker commands.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Optional per-task fault injection, honoured only by workers started with
    # demo chaos enabled: {"delay_s": 1.5, "fail_attempts": 2}. Cleared on replay.
    op.execute("ALTER TABLE task_instances ADD COLUMN chaos jsonb")
    # Which visitor's sandbox a demo run belongs to (NULL for everything else).
    op.execute("ALTER TABLE workflow_runs ADD COLUMN sandbox_id text")
    op.execute(
        "CREATE INDEX workflow_runs_sandbox_idx ON workflow_runs (sandbox_id, created_at DESC) "
        "WHERE sandbox_id IS NOT NULL"
    )
    # Workers that will obey demo kill commands (the demo pool only).
    op.execute("ALTER TABLE workers ADD COLUMN accepts_chaos boolean NOT NULL DEFAULT false")
    op.execute(
        """
        CREATE TABLE demo_sandboxes (
            id                text PRIMARY KEY,
            failures_enabled  boolean NOT NULL DEFAULT false,
            created_at        timestamptz NOT NULL DEFAULT now(),
            last_seen_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE worker_commands (
            id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            worker_id     uuid NOT NULL,
            command       text NOT NULL CHECK (command IN ('kill')),
            sandbox_id    text,
            requested_at  timestamptz NOT NULL DEFAULT now(),
            consumed_at   timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX worker_commands_pending_idx ON worker_commands (worker_id) "
        "WHERE consumed_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS worker_commands")
    op.execute("DROP TABLE IF EXISTS demo_sandboxes")
    op.execute("ALTER TABLE workers DROP COLUMN IF EXISTS accepts_chaos")
    op.execute("DROP INDEX IF EXISTS workflow_runs_sandbox_idx")
    op.execute("ALTER TABLE workflow_runs DROP COLUMN IF EXISTS sandbox_id")
    op.execute("ALTER TABLE task_instances DROP COLUMN IF EXISTS chaos")
