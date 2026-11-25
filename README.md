# Conductor

**A distributed job scheduling and workflow engine built on PostgreSQL alone, with failure recovery you can watch happen.**

<!-- Badges and the live demo link are added in Phase 7 (Ship). -->

> **Status:** Phases 0 to 2 of 7 are complete: the queue engine and DAG workflows work and are
> proven under chaos tests, including a kill of every process mid-run. Scheduling, the API
> surface, the dashboard and the public demo are next. See the [roadmap](#roadmap).

## Quick start

Requirements: Docker with Compose v2, plus [uv](https://docs.astral.sh/uv/) and GNU make for the
host-side tooling.

```bash
make dev        # PostgreSQL, migrations, API, scheduler (reaper) and a worker
docker compose -f deploy/docker-compose.yml up --scale worker=5   # more workers
```

- API health: <http://localhost:8000/api/v1/health>
- OpenAPI docs: <http://localhost:8000/docs>

The Compose database publishes on host port **5433**, so it won't clash with a locally
installed PostgreSQL.

## Development

```bash
make install    # backend deps + dev tools into backend/.venv
make check      # lint + format check + mypy (strict) + full test suite
make test       # starts the Compose DB if needed; unit + integration + chaos, coverage gate
make test-chaos # only the tests that SIGKILL/SIGSTOP real worker processes
make help       # every target
```

Integration tests run against a **real PostgreSQL**, never a mock. Each test session creates a
throwaway database on the server named by `TEST_DATABASE_URL`, migrates it to head and drops it
afterwards. If the server is unreachable the suite fails instead of skipping.

## Reliability, in one table

| Problem | How it's solved | Proven by |
| --- | --- | --- |
| Two workers grabbing the same task | One-statement claim with `FOR UPDATE SKIP LOCKED` | 20 concurrent claimers over 400 tasks: each task is claimed once |
| A worker dies mid-task | Leases renewed by heartbeat; the reaper requeues lapsed leases with backoff | Chaos test: SIGKILL a real worker process, and the task finishes on another |
| A frozen "zombie" worker wakes up and writes a stale result | Monotonic fencing token checked on every write | Chaos test: SIGSTOP, takeover, SIGCONT, and the zombie's write is rejected |
| Retries duplicating side effects | At-least-once delivery plus idempotency keys behind unique constraints | Kill after the effect but before completion, and exactly one notification exists |
| Two parents finish at once and both miss the child | Every outcome transaction locks the run row first (run, then task) | 25-iteration race test, which fails on iteration 2 without the lock |
| Every process killed mid-workflow | All state in Postgres; leases expire; the reaper recovers | Chaos test: SIGKILL everything twice (once after a side effect), restart, the run succeeds, effects happen once |
| A task that crashes every worker it touches | Lease expiry consumes an attempt, so it eventually goes to the dead-letter queue | `test_reaper_dead_letters_a_task_that_keeps_killing_workers` |
| Lost wake-up notifications | `NOTIFY` is only a hint; workers always poll | Chaos tests run with LISTEN on and off |

Exact guarantees and non-guarantees are in [`docs/semantics.md`](docs/semantics.md).

## Repository layout

| Path | Contents |
| --- | --- |
| `backend/src/conductor/` | API, scheduler and worker. One codebase, one image |
| `backend/alembic/` | Hand-written migrations |
| `backend/tests/` | `unit/` (no I/O) and `integration/` (real PostgreSQL) |
| `frontend/` | Next.js dashboard (Phase 5) |
| `deploy/` | Docker Compose files and the Caddy config |
| `docs/` | Architecture, ADRs, delivery semantics, benchmarks |

## Roadmap

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Foundation: scaffold, tooling, Compose, CI, health endpoint, first migration | ✅ |
| 1 | Core engine: SKIP LOCKED claiming, leases, fencing tokens, retries, DLQ, reaper | ✅ |
| 2 | Workflows: DAG validation, dependency resolution, failure policies, restart recovery | ✅ |
| 3 | Scheduling: cron + IANA time zones, leader election, priority aging | ⏳ |
| 4 | API + realtime: full REST surface, SSE, Prometheus metrics | |
| 5 | Dashboard: DAG view, timeline, workers, DLQ, history | |
| 6 | Public demo: sandbox, chaos controls, narrated event feed | |
| 7 | Ship: production deploy, benchmarks, docs | |

## Design decisions

Architecture decisions are recorded in [`docs/adr/`](docs/adr/):

- [0001 PostgreSQL as the queue](docs/adr/0001-postgres-as-the-queue.md), including when a broker would be the right call
- [0002 At-least-once delivery with idempotency keys](docs/adr/0002-at-least-once-with-idempotency-keys.md)
- [0003 Leases and fencing tokens](docs/adr/0003-leases-and-fencing-tokens.md)
- [0007 No arbitrary code execution](docs/adr/0007-no-arbitrary-code-execution.md)
- [0008 In-house cron parser and JSON Schema subset](docs/adr/0008-in-house-cron-and-json-schema.md)
