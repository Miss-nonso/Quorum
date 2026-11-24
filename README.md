# Conductor

**A distributed job scheduling and workflow engine built on PostgreSQL alone, with failure recovery you can watch happen.**

<!-- Badges and the live demo link are added in Phase 7 (Ship). -->

> **Status:** Phase 0 (foundation) of 7 is complete. The engine itself, the dashboard and the
> public demo are still in progress. See the [roadmap](#roadmap).

## Quick start

Requirements: Docker with Compose v2, plus [uv](https://docs.astral.sh/uv/) and GNU make for the
host-side tooling.

```bash
make dev        # builds the image and starts PostgreSQL, runs migrations, serves the API
```

- API health: <http://localhost:8000/api/v1/health>
- OpenAPI docs: <http://localhost:8000/docs>

The Compose database publishes on host port **5433**, so it won't clash with a locally
installed PostgreSQL.

## Development

```bash
make install    # backend deps + dev tools into backend/.venv
make check      # lint + format check + mypy (strict) + full test suite
make test       # starts the Compose DB if needed, then runs pytest
make help       # every target
```

Integration tests run against a **real PostgreSQL**, never a mock. Each test session creates a
throwaway database on the server named by `TEST_DATABASE_URL`, migrates it to head and drops it
afterwards. If the server is unreachable the suite fails instead of skipping.

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
| 1 | Core engine: SKIP LOCKED claiming, leases, fencing tokens, retries, DLQ, reaper | ⏳ |
| 2 | Workflows: DAG validation, dependency resolution, failure policies, restart recovery | |
| 3 | Scheduling: cron + IANA time zones, leader election, priority aging | |
| 4 | API + realtime: full REST surface, SSE, Prometheus metrics | |
| 5 | Dashboard: DAG view, timeline, workers, DLQ, history | |
| 6 | Public demo: sandbox, chaos controls, narrated event feed | |
| 7 | Ship: production deploy, benchmarks, docs | |

## Design decisions

Architecture decisions are recorded in [`docs/adr/`](docs/adr/).
