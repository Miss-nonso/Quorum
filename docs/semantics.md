# Delivery semantics and failure model

This page states precisely what Quorum guarantees and what it does not. Each guarantee names
the test that fails if it stops being true. *Status: covers Phases 1–3 (queue, workflows, scheduling).*

## Failure model

Quorum tolerates:

- **Worker crashes at any instant** (SIGKILL, OOM, host loss), including between a task's side
  effect and its completion report.
- **Worker stalls and partitions** (GC pauses, SIGSTOP, network loss) of any length, including
  stalls that outlive the lease.
- **Lost notifications.** `LISTEN/NOTIFY` is a latency optimisation only.
- **Concurrent schedulers and reapers.** Recovery is safe without a leader.
- **Restarts of any process.** No correctness-relevant state lives in process memory.

Quorum assumes:

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

## Workflow runs

| # | Guarantee | Proven by |
| --- | --- | --- |
| W1 | A workflow can't be saved if it has a cycle, a dangling or duplicate dependency, an unknown task type, invalid params, or a reference to a node that is not upstream. Every problem is reported at once. | `test_workflow_domain.py` (incl. property tests: random DAGs order correctly; any added back edge is caught) |
| W2 | A task becomes `ready` only when every dependency is satisfied: it `succeeded`, or `failed` under the `continue` policy. Its release happens in the same transaction as the last dependency's completion. | `test_parallel_branches_are_released_together`, `test_concurrent_parent_completions_always_release_the_child` (fails within two iterations if the run lock is removed) |
| W3 | Outcome changes within one run are serialised by a run-row lock, taken before the task lock on every path, so they can't deadlock each other. The reaper takes run locks with `SKIP LOCKED`, so it never waits on a worker. | same; reaper and chaos suites |
| W4 | Run status follows its tasks. `running` while anything is ready or running; then `succeeded` if every task succeeded, else `failed`. | `test_run_status_follows_task_states` |
| W5 | A run that stopped on a dead-lettered task (`retry_then_dead`) resumes when that task is replayed, and continues to completion. | `test_replaying_the_dead_task_resumes_the_run` |
| W6 | `fail_run` cancels every unfinished task in the run. A running task's worker is fenced off and stops. | `test_fail_run_policy_cancels_everything_else` |
| W7 | Killing **every** process mid-run, even twice and once after a side effect, then restarting, completes the run with each side effect exactly once. | `test_full_system_restart_mid_run_resumes_with_effects_exactly_once` |

**Failure policies**, applied once a node's attempts are exhausted (or immediately, for a
permanent failure such as invalid data):

| Policy | Task ends | Dependents | Run |
| --- | --- | --- | --- |
| `retry_then_dead` (default) | `dead` + dead-letter entry | wait (`pending`) | `failed`, resumes on replay |
| `fail_run` | `failed` | `cancelled`, with the rest of the run | `failed` |
| `continue` | `failed` | run normally | finishes, as `failed` |

**Data between nodes.** A param `{"$from": "node", "path": "a.b"}` is resolved by the worker
from the upstream node's stored result just before execution. If the path doesn't exist, the
task fails permanently instead of running with missing input.

## Scheduling

| # | Guarantee | Proven by |
| --- | --- | --- |
| S1 | A schedule's local wall-clock times map to instants with explicit DST rules. A skipped local time fires at the instant the clock jumps, and several skipped times collapse into that one firing. A repeated local time fires once, on its first occurrence. | `test_cron.py`: America/New_York, Europe/London and Africa/Lagos across 2026 transitions, plus property tests (UTC agrees with a minute-by-minute scan; firings strictly increase) |
| S2 | Each scheduled instant produces at most one run, even with concurrent or failing-over schedulers. | `test_concurrent_schedulers_fire_each_instant_once`, `test_unique_index_blocks_a_duplicate_run_for_the_same_instant` |
| S3 | **Misfires:** after downtime spanning several fire times, a schedule fires once (for the earliest missed instant), records how many it skipped, and resumes from the next future time. | `test_due_schedule_fires_once_and_skips_missed_instants` |
| S4 | At most one scheduler holds leadership at a time, and a standby takes over when the leader's session ends. | `test_exactly_one_leader_and_failover_when_its_session_dies`, `test_standby_scheduler_takes_over_when_the_leader_is_killed` |
| S5 | No task starves. Waiting raises effective priority, and with the default cap any task eventually outranks fresh work of any priority. | `test_low_priority_task_is_not_starved_by_sustained_high_priority_load` (with a no-aging control) |

## What is *not* guaranteed

- **Exactly-once execution.** A handler may run more than once for the same task: after a crash,
  a stall, a timeout or a replay. Design handlers to tolerate this.
- **Exactly-once external effects without a dedupe key.** If a handler calls an external system
  that does not deduplicate (for example, a webhook receiver that ignores idempotency keys), that
  effect is at-least-once.
- **Strict ordering.** Claims are ordered by effective priority, then creation time, but tasks
  run concurrently across workers and retries re-enter the queue. Order of *completion* is not
  guaranteed. Express real ordering requirements as workflow dependencies.
- **Isolation from edits.** Editing a workflow creates a new version for future runs. Runs that
  already started keep the tasks they were created with.
- **Bounded recovery time under database unavailability.** While Postgres is down nothing
  progresses. Workers stop all work once they can't renew for `lease_ttl_s`, and resume when it
  returns.
- **Single-leader execution of scheduler duties.** During a failover, two schedulers may
  briefly both run duties. Each duty is written to be safe under that (ADR 0004).
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
backoff. Measured (docs/benchmarks.md): 6.8 s median from SIGKILL to re-claim with a 6 s lease, and
34.9 s with the 30 s default. The chaos suite uses a 1.5 s TTL and recovers in about 1.5 s.

## Attempts, replays and tokens

| Operation | `attempt` | `fencing_token` |
| --- | --- | --- |
| claim | +1 | +1 |
| fail → retry / lease expired | unchanged (already counted at claim) | unchanged |
| graceful release on shutdown | −1 (not the task's fault) | unchanged |
| dead-letter replay | reset to 0 (fresh budget) | unchanged (never reset) |

Because the token never decreases, every past lease holder stays fenced off for good.
