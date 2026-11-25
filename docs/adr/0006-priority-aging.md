# 0006. Priority aging by periodic update

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

Strict priority ordering starves low-priority work under sustained high-priority load. The spec
asks for `effective_priority = base_priority + aging_bonus`, where the bonus grows with time
spent waiting. It can be computed in one of two places:

- **A. A periodic job** rewrites the stored `effective_priority`, and the claim query orders by
  the column, using the partial index `(effective_priority DESC, created_at) WHERE status = 'ready'`.
- **B. The claim query** orders by the expression itself. That is always exactly fresh, but no
  index can serve it.

## Measurement

`backend/bench/claim_ordering.py`, run against PostgreSQL 16 with 50,000 ready tasks
(Intel i5-1135G7, 8 threads, Docker), 300 single-task claims per strategy, each rolled back:

| Strategy | p50 | p95 |
| --- | --- | --- |
| A: index-ordered claim | **0.19 ms** | **0.24 ms** |
| B: aging computed in the claim | 36.0 ms | 40.3 ms |
| A′: one full aging pass, every row changing (worst case, off the hot path) | 1.05 s | |

B sorts the whole ready set on every claim, so its cost grows with queue depth; the claim is
the hottest query in the system. A's background pass is the same order of work, but it runs
once per `aging_tick_s` instead of once per claim, rewrites only rows whose bonus actually
changed, and skips locked rows, so it never blocks a claimer.

## Decision

- **Strategy A.** The scheduler leader runs `store/aging.py` every `aging_tick_s` (default 5s).
- The formula: `effective = priority + min(max_bonus, floor(waited / aging_interval_s))`, where
  `waited` counts from `not_before`, the moment the task became claimable. A task waiting out its
  retry backoff doesn't age.
- **Defaults:** `aging_interval_s = 10`, `max_bonus = 200`. With priorities in −100…100, a cap of
  200 guarantees that any waiting task eventually outranks a fresh task of any priority. A task
  `g` levels below the competing load waits at most about `(g + 1) × aging_interval_s`, plus one
  tick.
- The domain function (`domain/priority.py`) and the SQL are pinned together by
  `test_sql_aging_matches_the_domain_formula`.

## Consequences

- Claims stay O(log n) on the index. Aging is up to one tick stale, which doesn't matter at
  10s granularity.
- Aged rows are rewritten, adding churn on long queues. The default interval keeps this to at
  most one write per waiting task every 10 seconds.
- **Starvation-freedom is tested**, not assumed.
  `test_low_priority_task_is_not_starved_by_sustained_high_priority_load` uses one worker and a
  never-empty stream of priority-50 tasks. A priority −50 task runs within seconds with aging,
  and the control run without aging shows it never runs at all.
