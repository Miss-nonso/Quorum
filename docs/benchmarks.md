# Benchmarks

Numbers you can reproduce, with the method and their limits stated. Everything here runs on one
laptop, so read it as "what one modest machine does with durable settings", not as a capacity
plan.

## Environment

| | |
| --- | --- |
| Host | Intel Core i5-1135G7 (4 cores / 8 threads), 16 GB RAM, 512 GB SATA SSD, Linux 7.0 |
| Database | PostgreSQL 16.16 in Docker, **default configuration** (`synchronous_commit=on`, fsync on), reached through Docker's port mapping |
| Workers | Real `conductor worker` processes, 8 task slots each, `poll_interval_s=0.05` |
| Run date | 2026-11-25. The laptop was also running an IDE, a browser and a desktop session |

## Method

`backend/bench/engine.py` creates a throwaway database, migrates it, runs each scenario, prints
Markdown, and drops the database:

- **Throughput.** 5,000 standalone no-op jobs (`sleep 0`) are inserted in one statement, then
  1, 2 or 4 worker processes start. The rate is tasks / wall time until all have succeeded. Each
  task goes through the full production path: claim (lease, fencing token, event), execute,
  completion (run lock, conditional update, event, dependent release, run status refresh).
- **Claim latency.** 1, 4 or 16 concurrent claimers call the real `claim_tasks` (SQLAlchemy,
  one task each) until the queue is empty. Round trips are timed client-side, including the commit.
- **Recovery time.** One scheduler and two workers. A 60-second task is enqueued, the worker
  holding it is SIGKILLed, and the timer stops when another worker claims attempt 2. That covers
  lease expiry, the reaper sweep, the retry and the re-claim. Three runs per configuration.

`backend/bench/claim_ordering.py` compares two ways to implement priority aging
([ADR 0006](adr/0006-priority-aging.md)).

## Results

### Throughput (no-op tasks, durable commits)

| Workers × slots | Tasks/s |
| --- | --- |
| 1 × 8 | 239 |
| 2 × 8 | 229 |
| 4 × 8 | 320 |

### Claim latency (claim statement round trip, including commit)

| Concurrent claimers | p50 ms | p95 ms |
| --- | --- | --- |
| 1 | 4.96 | 10.59 |
| 4 | 8.23 | 15.53 |
| 16 | 15.00 | 31.36 |

### Recovery after SIGKILL (kill → re-claimed by another worker)

| Lease TTL / heartbeat / reaper interval | Runs | Median | Max |
| --- | --- | --- | --- |
| 6 s / 1 s / 1 s (public demo settings) | 3 | 6.8 s | 6.9 s |
| 30 s / 5 s / 5 s (production defaults) | 3 | 34.9 s | 35.0 s |

### Priority aging strategy (50,000 ready tasks, 300 single-task claims each)

| Strategy | p50 | p95 |
| --- | --- | --- |
| Periodic aging job + index-ordered claim (chosen) | 0.19 ms | 0.25 ms |
| Aging computed inside the claim query | 35.6 ms | 42.0 ms |
| One worst-case full aging pass (off the hot path) | 1.2 s | |

## What the numbers mean

**Throughput is bound by durable commits, not by workers.** Adding workers barely helps (1 → 4
workers: 239 → 320 tasks/s). Each task needs at least two durable commits (claim, completion),
and each commit waits for an fsync on a laptop SSD behind Docker's storage driver. To check this
rather than assume it, the same benchmark ran with `synchronous_commit=off` on the throwaway
database (`--no-fsync-wait`, diagnostic only; Conductor never runs that way):

| Durable commits? | Claim p50 (1 claimer) | Tasks/s (1 worker) | Tasks/s (2 workers) |
| --- | --- | --- | --- |
| Yes (default) | 4.96 ms | 239 | 229 |
| No (diagnostic) | 1.03 ms | 385 | 533 |

The claim statement itself takes about 0.2 ms of database work (see the aging benchmark); the
rest of the default-mode latency is waiting for the disk. On a server with a battery-backed or
enterprise SSD (fsync in tens of microseconds), the same code should land much nearer the
diagnostic row, but that is an expectation, not a measurement. The ways to raise the ceiling
without weakening durability are batching (claim and complete several tasks per commit) and
group commit tuning (`commit_delay`). [ADR 0001](adr/0001-postgres-as-the-queue.md) lists
when a broker becomes the right tool.

**Recovery time is the lease TTL plus about one reaper tick**, exactly as designed
([semantics](semantics.md#recovery-timeline-after-a-worker-dies)). It is a dial: the public demo
uses a 6 s lease so visitors see recovery quickly; production defaults favour fewer false
alarms for slow-but-alive workers.

**Claim latency grows with concurrency**, because claimers contend on the commit path, not on
row locks: `SKIP LOCKED` means claimers never wait for each other's rows (the concurrency test
proves no task is claimed twice).

## Reproduce

```bash
make db                                        # PostgreSQL 16 on localhost:5433
cd backend
uv run python bench/engine.py --jobs 5000      # throughput, claim latency, recovery
uv run python bench/engine.py --jobs 3000 --quick --no-fsync-wait   # commit-ceiling diagnostic
uv run python bench/claim_ordering.py          # aging strategy (also `make bench`)
```

Point `TEST_DATABASE_URL` at another server to benchmark it; the scripts create and drop their
own database.
