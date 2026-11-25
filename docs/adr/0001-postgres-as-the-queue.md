# 0001. PostgreSQL as the queue

- **Status:** Accepted
- **Date:** 2026-11-24

## Context

Quorum needs durable task storage, competing consumers, delayed retries, priorities,
dead-lettering, an audit trail and realtime updates. The usual architecture puts a broker
(RabbitMQ, Redis Streams, SQS) next to a database: the broker moves work, and the database holds
workflow state.

That split creates a dual-write problem. "Mark the step done and enqueue its dependents" spans two
systems that can't commit atomically, so every edge case (crash between the writes, broker
redelivery, lost acks) needs reconciliation code, and that code is where workflow engines
accumulate bugs. It also doubles the operational surface on a budget of about $5–10 a month.

PostgreSQL 9.5+ provides the primitives a queue needs:

- `SELECT … FOR UPDATE SKIP LOCKED` for contention-free competing consumers;
- transactional `LISTEN/NOTIFY` for low-latency wake-ups;
- advisory locks for leader election;
- partial indexes, so that only claimable rows cost index space.

## Decision

PostgreSQL 16 is the only data store and the only message transport in v1.

- Tasks are rows in `task_instances`. A worker claims them with one `UPDATE … FROM (SELECT … FOR
  UPDATE SKIP LOCKED)` statement, ordered by `effective_priority DESC, created_at`.
- Every state change, its `run_events` row, any dead-letter row and its `NOTIFY` commit in one
  transaction.
- `NOTIFY` is a hint only. Workers poll at `poll_interval_s` regardless, so a lost notification
  costs latency, never correctness. The chaos suite runs with LISTEN both on and off.

## Consequences

- **Atomicity for free.** A task completion and the readiness of its dependents (Phase 2) are a
  single commit. There's no outbox and no reconciliation job.
- **One system to run, back up and reason about.** `pg_dump` captures queue state and workflow
  state at the same instant.
- **Throughput ceiling.** Each claim and each completion is a write with its WAL cost, so
  sustained throughput is bounded by a single primary's write capacity. On modest hardware that
  is typically thousands of tasks per second (measured in `docs/benchmarks.md`, Phase 7), far
  above what this project needs.
- **Bloat.** Hot rows churn through `ready → running → succeeded`. The partial claim index stays
  small, but tables need autovacuum to keep up, and finished rows need a retention policy (demo
  auto-reset in Phase 6, archival later).
- **Connection budget.** Every worker holds pooled connections plus one LISTEN connection.

### When a broker becomes the right call

Revisit this decision if any of these become true:

- sustained throughput above what one primary can absorb after tuning (batching claims,
  `fillfactor`, partitioning `task_instances` by status or time);
- fan-out to many independent consumer groups (pub/sub), which tables model poorly;
- consumers in other languages or teams that need a standard protocol (AMQP, Kafka) rather than
  a SQL contract;
- message retention or replay measured in days of high-volume streams.

Even then, the likely next step is a transactional outbox from Postgres into the broker, so the
atomicity argument above still holds for workflow state.
