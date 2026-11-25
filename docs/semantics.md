# Delivery semantics and failure model

This page states precisely what Conductor guarantees and what it does not. Each guarantee names
the test that fails if it stops being true. *Status: Phase 1. Workflow-level semantics (DAG
advancement, failure policies, restart recovery) are added in Phase 2.*

## Failure model

Conductor tolerates:

- **Worker crashes at any instant** (SIGKILL, OOM, host loss), including between a task's side
  effect and its completion report.
- **Worker stalls and partitions** (GC pauses, SIGSTOP, network loss) of any length, including
  stalls that outlive the lease.
- **Lost notifications.** `LISTEN/NOTIFY` is a latency optimisation only.
- **Concurrent schedulers and reapers.** Recovery is safe without a leader.
- **Restarts of any process.** No correctness-relevant state lives in process memory.

Conductor assumes:

- **PostgreSQL is durable and consistent.** It is a single primary; failover of the database
  itself is out of scope for v1.
- **Task handlers do not block the event loop for longer than `lease_ttl_s`.** A handler that
  does will lose its lease. Fencing still prevents a wrong outcome, but the work is wasted.

## Guarantees

| # | Guarantee | Proven by |
| --- | --- | --- |
| G1 | A `ready` task is leased to at most one worker at a time. | `test_concurrent_claimers_never_receive_the_same_task` (20 claimers, 400 tasks) |
| G2 | A task that is enqueued is eventually run to an outcome (`succeeded` or `dead`), as long as at least one worker and one reaper are running. | `test_killed_worker_task_completes_elsewhere_with_effect_once`, `test_killing_a_worker_under_load_loses_nothing_and_duplicates_nothing` |
| G3 | Only the current lease holder can record a task's outcome. Writes carrying an older fencing token are rejected and logged as `stale_write_rejected`. | `test_zombie_completion_is_rejected_by_fencing_token`, `test_frozen_zombie_worker_cannot_overwrite_the_new_owners_outcome` |
| G4 | A side effect recorded under the task's idempotency key with a uniqueness guarantee happens at most once per key, across retries, lease takeovers and dead-letter replays. Combined with G2, this means effectively once. | `test_killed_worker_…_effect_once`, `test_notify_suppresses_duplicate_delivery`, the chaos load test (45 tasks, 45 notifications) |
| G5 | A failed attempt is retried after a backoff of at most `min(max_s, base_s · factor^(attempt−1))`, until `max_attempts` attempts are used up; then the task is dead-lettered. A lease expiry counts as an attempt. | `test_failure_retries_with_backoff_then_dead_letters`, `test_reaper_dead_letters_a_task_that_keeps_killing_workers`, property tests in `test_domain.py` |
| G6 | Every state change is recorded in `run_events` in the same transaction as the change itself. The log never shows a transition that didn't happen, or omits one that did. | Event-sequence assertions throughout the integration suite |
| G7 | A lease renewed before the reaper locks the row is never reaped, and concurrent reapers recover each expired task exactly once. | `test_lease_renewed_at_the_last_moment_is_not_reaped`, `test_concurrent_reapers_recover_each_task_exactly_once` |
| G8 | Correctness does not depend on `LISTEN/NOTIFY`. | The kill test, parametrised `listen` / `poll-only` |

## What is *not* guaranteed

- **Exactly-once execution.** A handler may run more than once for the same task: after a crash,
  a stall, a timeout or a replay. Design handlers to tolerate this.
- **Exactly-once external effects without a dedupe key.** If a handler calls an external system
  that does not deduplicate (for example, a webhook receiver that ignores idempotency keys), that
  effect is at-least-once.
- **Strict ordering.** Claims are ordered by effective priority, then creation time, but tasks
  run concurrently across workers and retries re-enter the queue. Order of *completion* is not
  guaranteed. Express real ordering requirements as workflow dependencies (Phase 2).
- **Bounded recovery time under database unavailability.** While Postgres is down nothing
  progresses. Workers stop all work once they can't renew for `lease_ttl_s`, and resume when it
  returns.
- **Precise timing.** `not_before` is a lower bound. A retry runs no earlier than its backoff, and
  later if workers are busy.

## Recovery timeline after a worker dies

```
t0                   worker killed while holding a lease
t0 + ≤ lease_ttl_s   lease lapses (last renewal + TTL)
  + ≤ reaper_interval_s    reaper records lease_expired, schedules the retry
  + backoff                 task becomes claimable
  + ≤ poll_interval_s       a worker claims it (immediately, if NOTIFY arrives)
```

With the defaults (30s TTL, 5s reaper interval, 2s poll) the worst case is about 37s plus
backoff. The chaos suite uses a 1.5s TTL and observes detection in about 1.5s and full recovery
(including re-running a 2s task) in about 3.5s.

## Attempts, replays and tokens

| Operation | `attempt` | `fencing_token` |
| --- | --- | --- |
| claim | +1 | +1 |
| fail → retry / lease expired | unchanged (already counted at claim) | unchanged |
| graceful release on shutdown | −1 (not the task's fault) | unchanged |
| dead-letter replay | reset to 0 (fresh budget) | unchanged (never reset) |

Because the token never decreases, every past lease holder stays fenced off for good.
