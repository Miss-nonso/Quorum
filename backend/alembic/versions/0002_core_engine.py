"""Core engine schema: runs, tasks, workers, events, dead letters, notifications.

Design notes (see docs/adr/0001-0003):

* Statuses are ``text`` + CHECK rather than Postgres enum types, so adding a
  status later is a constraint swap, not an ``ALTER TYPE`` dance.
* JSON columns drop the spec's ``_jsonb`` suffix (``params`` not
  ``params_jsonb``); the type is in the schema, not the name.
* ``lease_owner`` deliberately has no FK to ``workers``: the hot claim path
  should not pay for FK checks, and worker rows are retained anyway.
* Every task belongs to a run. A standalone job is a one-node run with no
  workflow, so retries, events and the dashboard have a single code path.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASK_STATUSES = "'pending','ready','running','succeeded','failed','dead','cancelled'"
RUN_STATUSES = "'pending','running','succeeded','failed','cancelled'"
WORKER_STATUSES = "'active','unhealthy','dead','stopped'"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE workflows (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name        text NOT NULL,
            definition  jsonb NOT NULL DEFAULT '{}',
            version     integer NOT NULL DEFAULT 1 CHECK (version >= 1),
            timezone    text NOT NULL DEFAULT 'UTC',
            cron        text,
            enabled     boolean NOT NULL DEFAULT true,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE workflow_runs (
            id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- NULL for standalone jobs (one-node runs with no workflow).
            workflow_id    uuid REFERENCES workflows (id) ON DELETE CASCADE,
            version        integer,
            status         text NOT NULL DEFAULT 'pending' CHECK (status IN ({RUN_STATUSES})),
            trigger        text NOT NULL DEFAULT 'manual',
            scheduled_for  timestamptz,
            started_at     timestamptz,
            finished_at    timestamptz,
            created_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX workflow_runs_workflow_idx ON workflow_runs (workflow_id, created_at DESC)"
    )

    op.execute(
        f"""
        CREATE TABLE task_instances (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id              uuid NOT NULL REFERENCES workflow_runs (id) ON DELETE CASCADE,
            node_key            text NOT NULL,
            task_type           text NOT NULL,
            params              jsonb NOT NULL DEFAULT '{{}}',
            status              text NOT NULL DEFAULT 'ready' CHECK (status IN ({TASK_STATUSES})),
            priority            integer NOT NULL DEFAULT 0,
            effective_priority  integer NOT NULL DEFAULT 0,
            attempt             integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
            max_attempts        integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
            backoff_policy      jsonb NOT NULL DEFAULT '{{}}',
            timeout_s           double precision NOT NULL DEFAULT 60 CHECK (timeout_s > 0),
            not_before          timestamptz NOT NULL DEFAULT now(),
            lease_owner         uuid,
            lease_expires_at    timestamptz,
            -- Monotonic per task; bumped on every claim and never reset, so a
            -- write carrying an older token is provably from a superseded lease.
            fencing_token       bigint NOT NULL DEFAULT 0,
            idempotency_key     text NOT NULL,
            result              jsonb,
            error               jsonb,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now(),
            UNIQUE (run_id, node_key),
            -- A lease exists exactly while the task is running.
            CONSTRAINT task_lease_iff_running CHECK (
                (status = 'running') = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
            )
        )
        """
    )
    # Claim path. Matches the claim query's ORDER BY exactly, so Postgres walks
    # the index in order and stops after LIMIT rows; `not_before` is a cheap
    # filter on those rows. Partial: only 'ready' rows are ever candidates,
    # which keeps the index small while millions of finished rows accumulate.
    # (The spec suggested (status, effective_priority DESC, not_before); with
    # the partial predicate `status` is constant, and ordering by created_at
    # for FIFO-within-priority needs created_at in the key instead.)
    op.execute(
        "CREATE INDEX task_instances_claim_idx ON task_instances "
        "(effective_priority DESC, created_at) WHERE status = 'ready'"
    )
    # Reaper path: find running tasks whose lease has lapsed, oldest first.
    op.execute(
        "CREATE INDEX task_instances_lease_idx ON task_instances (lease_expires_at) "
        "WHERE status = 'running'"
    )
    # Effectively-once side effects hinge on this key being unique.
    op.execute(
        "CREATE UNIQUE INDEX task_instances_idempotency_key_idx ON task_instances (idempotency_key)"
    )

    op.execute(
        """
        CREATE TABLE task_dependencies (
            task_id             uuid NOT NULL REFERENCES task_instances (id) ON DELETE CASCADE,
            depends_on_task_id  uuid NOT NULL REFERENCES task_instances (id) ON DELETE CASCADE,
            PRIMARY KEY (task_id, depends_on_task_id),
            CHECK (task_id <> depends_on_task_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX task_dependencies_reverse_idx ON task_dependencies (depends_on_task_id)"
    )

    op.execute(
        f"""
        CREATE TABLE workers (
            id                 uuid PRIMARY KEY,
            hostname           text NOT NULL,
            pid                integer NOT NULL,
            capabilities       text[] NOT NULL DEFAULT '{{}}',
            status             text NOT NULL DEFAULT 'active' CHECK (status IN ({WORKER_STATUSES})),
            concurrency        integer NOT NULL CHECK (concurrency >= 1),
            current_load       integer NOT NULL DEFAULT 0 CHECK (current_load >= 0),
            started_at         timestamptz NOT NULL DEFAULT now(),
            last_heartbeat_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX workers_heartbeat_idx ON workers (last_heartbeat_at) "
        "WHERE status IN ('active','unhealthy')"
    )

    op.execute(
        """
        CREATE TABLE run_events (
            id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            run_id   uuid NOT NULL REFERENCES workflow_runs (id) ON DELETE CASCADE,
            task_id  uuid REFERENCES task_instances (id) ON DELETE CASCADE,
            type     text NOT NULL,
            payload  jsonb NOT NULL DEFAULT '{}',
            -- clock_timestamp(), not now(): several events in one transaction
            -- must keep their real order on the timeline.
            at       timestamptz NOT NULL DEFAULT clock_timestamp()
        )
        """
    )
    op.execute("CREATE INDEX run_events_run_idx ON run_events (run_id, id)")
    op.execute("CREATE INDEX run_events_task_idx ON run_events (task_id, id)")

    op.execute(
        """
        CREATE TABLE dead_letters (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id      uuid NOT NULL REFERENCES task_instances (id) ON DELETE CASCADE,
            reason       text NOT NULL,
            payload      jsonb NOT NULL DEFAULT '{}',
            created_at   timestamptz NOT NULL DEFAULT now(),
            replayed_at  timestamptz
        )
        """
    )
    op.execute("CREATE INDEX dead_letters_task_idx ON dead_letters (task_id)")
    op.execute(
        "CREATE INDEX dead_letters_open_idx ON dead_letters (created_at DESC) "
        "WHERE replayed_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE notifications (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            -- One notification per idempotency key, however often delivered.
            idempotency_key  text NOT NULL UNIQUE,
            run_id           uuid REFERENCES workflow_runs (id) ON DELETE CASCADE,
            task_id          uuid REFERENCES task_instances (id) ON DELETE CASCADE,
            channel          text NOT NULL,
            message          text NOT NULL,
            created_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    for table in (
        "notifications",
        "dead_letters",
        "run_events",
        "workers",
        "task_dependencies",
        "task_instances",
        "workflow_runs",
        "workflows",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
