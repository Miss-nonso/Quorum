# 0003. Leases and fencing tokens

- **Status:** Accepted
- **Date:** 2026-11-24

## Context

A claimed task must be recoverable if its worker disappears, so ownership has to expire. Expiry
alone is unsafe, though. A worker can be paused rather than dead (a GC pause, a CPU-starved
container, a laptop lid, a network partition). It wakes up after its lease has been given to
someone else, still believing it owns the task, and writes a stale result over the new owner's
outcome. This is the classic problem from Martin Kleppmann's "How to do distributed locking":
a lease without fencing is not a lock.

## Decision

**Leases.**
- A claim sets `lease_owner` and `lease_expires_at = now() + lease_ttl_s`, using the database's
  clock, so clock skew between workers is irrelevant.
- Workers renew every held lease in one statement every `heartbeat_interval_s`. Startup
  validation enforces `heartbeat_interval_s ≤ lease_ttl_s / 2`, so a single missed heartbeat
  never costs a lease.
- The reaper requeues (with backoff) or dead-letters any `running` task whose lease has lapsed.

**Fencing tokens.**
- `fencing_token` is a per-task `bigint`, incremented on every claim and never reset, not even on
  dead-letter replay.
- Every worker write (renew, complete, fail, release) is conditional on
  `status = 'running' AND lease_owner = :me AND fencing_token = :my_token`. A write with a stale
  token matches zero rows. It raises `StaleLeaseError`, and a `stale_write_rejected` event is
  recorded so that the dashboard can show the zombie being fenced off.
- The token, not the worker id, proves ownership: a worker that re-claims its own task gets a new
  token and its old writes are still rejected.

**Cooperative abandonment.** When a renewal reports that a lease is no longer held, the worker
cancels that execution and records `lease_lost`. This saves wasted work. Safety does not depend
on it: fencing alone guarantees the correct outcome.

**Safe concurrency.** The reaper locks candidate rows with `SKIP LOCKED`, and Postgres re-checks
the expiry predicate under the lock. A lease renewed at the last moment is therefore not reaped,
and concurrent reapers never recover the same task twice. Recovery does not depend on leader
election.

## Consequences

- **Recovery time** after a worker dies is at most `lease_ttl_s + reaper_interval_s` plus the
  retry backoff. The defaults (30s + 5s) favour fewer false positives over fast recovery. The demo
  uses short values so recovery is visible within seconds.
- **The lease TTL sets the patience for slow-but-alive workers.** A worker that can't heartbeat
  for `lease_ttl_s` loses its tasks even if it would have finished them.
- **Fencing protects only writes that go through Quorum.** A zombie's external side effect
  (say, an HTTP call made after it lost its lease) can't be fenced by our database. That is what
  idempotency keys are for (ADR 0002).
- **Tested by** `tests/integration/test_task_store.py` (stale tokens rejected, last-moment
  renewal, concurrent reapers) and `tests/chaos/test_worker_failures.py`, which freezes a real
  worker with SIGSTOP, lets another finish the task, thaws the zombie, and asserts that its write
  never lands.
