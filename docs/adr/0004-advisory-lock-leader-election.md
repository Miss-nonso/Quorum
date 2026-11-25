# 0004. Leader election with a PostgreSQL advisory lock

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

Some periodic duties should run in one place at a time: firing cron schedules, reaping expired
leases and aging priorities. Several scheduler instances must be able to run for availability.
Adding a coordinator (ZooKeeper, etcd, Consul) for this would break the "PostgreSQL only" rule
(ADR 0001).

## Decision

- Each scheduler connects with a **dedicated** asyncpg connection (never a pooled one) and calls
  `pg_try_advisory_lock(0x436F6E64)`. Whoever gets it leads. The others retry every
  `leader_retry_interval_s`.
- Postgres ties the lock to the session. When the leader's process dies, or its connection
  drops, the server releases the lock immediately, and a standby takes over on its next attempt.
  The chaos test measures this at about 0.1s with a 0.3s retry interval.
- The leader probes its own connection every `leader_check_interval_s` by updating its heartbeat
  row in `scheduler_state`, which the dashboard displays. If the probe fails, it cancels its
  duties at once and campaigns again.

### Safety does not depend on a single leader

There is an unavoidable window in which a partitioned old leader hasn't yet noticed that its
session is gone, while a new leader is already running. Every duty is therefore safe to run
concurrently:

- the reaper and the aging job lock rows with `SKIP LOCKED` and re-check conditions under the
  lock (ADR 0003);
- cron firing claims schedule rows with `SKIP LOCKED`, and a unique index on
  `workflow_runs (workflow_id, scheduled_for) WHERE trigger = 'schedule'` makes a duplicate run
  for the same instant impossible.

Leader election saves duplicated work. It is not what keeps the system correct.

## Consequences

- No extra infrastructure. Failover time is bounded by the retry interval, plus TCP detection of
  the dead session for partitions rather than crashes.
- A leader stalled for longer than its check interval keeps running duties until it notices.
  This is harmless by the argument above.
- Advisory locks are per-database and invisible to connection poolers in transaction mode. A
  deployment behind PgBouncer must give schedulers a session-mode connection. This is documented
  in the deployment guide.
- **Tested by** `test_exactly_one_leader_and_failover_when_its_session_dies` (session terminated
  with `pg_terminate_backend`) and `test_standby_scheduler_takes_over_when_the_leader_is_killed`
  (two real processes, SIGKILL the leader, and the standby fires the next schedule).
