"""Workflows: per-node failure policy, unique workflow names, stored records.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE task_instances
        ADD COLUMN failure_policy text NOT NULL DEFAULT 'retry_then_dead'
            CHECK (failure_policy IN ('retry_then_dead', 'fail_run', 'continue'))
        """
    )
    op.execute("ALTER TABLE workflows ADD CONSTRAINT workflows_name_key UNIQUE (name)")
    # History view: newest runs first, optionally filtered by status.
    op.execute("CREATE INDEX workflow_runs_created_idx ON workflow_runs (created_at DESC)")
    op.execute("CREATE INDEX workflow_runs_status_idx ON workflow_runs (status, created_at DESC)")
    # Run advancement: "which pending tasks in this run depend on the one that just finished?"
    op.execute("CREATE INDEX task_instances_run_idx ON task_instances (run_id, status)")
    op.execute(
        """
        CREATE TABLE stored_records (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            -- One write per idempotency key, however many times `store` runs.
            idempotency_key  text NOT NULL UNIQUE,
            run_id           uuid REFERENCES workflow_runs (id) ON DELETE CASCADE,
            task_id          uuid REFERENCES task_instances (id) ON DELETE CASCADE,
            collection       text NOT NULL,
            records          jsonb NOT NULL,
            record_count     integer NOT NULL,
            created_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX stored_records_collection_idx ON stored_records (collection, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS stored_records")
    op.execute("DROP INDEX IF EXISTS task_instances_run_idx")
    op.execute("DROP INDEX IF EXISTS workflow_runs_status_idx")
    op.execute("DROP INDEX IF EXISTS workflow_runs_created_idx")
    op.execute("ALTER TABLE workflows DROP CONSTRAINT IF EXISTS workflows_name_key")
    op.execute("ALTER TABLE task_instances DROP COLUMN IF EXISTS failure_policy")
