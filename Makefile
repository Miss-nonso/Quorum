# Quorum developer commands. Run `make help` for the list.
SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose -f deploy/docker-compose.yml
UV      := cd backend && uv run
# The engine's core packages must stay at or above this line coverage.
COV_CORE := src/quorum/domain/*,src/quorum/worker/*,src/quorum/scheduler/*,src/quorum/store/*

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Install backend dependencies (incl. dev tools) into backend/.venv
	cd backend && uv sync --locked

.PHONY: dev
dev: ## Build and run the full local stack (DB, migrations, API, scheduler, worker)
	$(COMPOSE) up --build

.PHONY: dev-host
dev-host: db ## Backend on the host (no containers), demo mode, 3 respawning chaos workers
	./scripts/dev-host.sh

.PHONY: down
down: ## Stop the local stack (keeps the database volume)
	$(COMPOSE) down

.PHONY: db
db: ## Start only PostgreSQL and wait until it is healthy
	$(COMPOSE) up -d --wait db

.PHONY: migrate
migrate: db ## Apply migrations to the local database from the host
	$(UV) quorum migrate

.PHONY: fmt
fmt: ## Auto-format and apply safe lint fixes
	$(UV) ruff format .
	$(UV) ruff check --fix .

.PHONY: lint
lint: ## Lint and check formatting (no changes)
	$(UV) ruff check .
	$(UV) ruff format --check .

.PHONY: typecheck
typecheck: ## Type-check the backend and its tests (mypy strict)
	$(UV) mypy
	$(UV) mypy tests

.PHONY: test-unit
test-unit: ## Run tests that need no database
	$(UV) pytest tests/unit

.PHONY: test
test: db ## Run every test (unit, integration, chaos) with the coverage gate
	$(UV) pytest --cov --cov-report=term
	$(UV) coverage report --include='$(COV_CORE)' --fail-under=85

.PHONY: test-chaos
test-chaos: db ## Run only the chaos tests (real worker processes, SIGKILL/SIGSTOP)
	$(UV) pytest tests/chaos -v

.PHONY: check
check: lint typecheck test ## Everything CI runs on the backend

# ---------------------------------------------------------------- frontend
FE := cd frontend && npm run
E2E_DATABASE_URL ?= postgresql+asyncpg://quorum:quorum@localhost:5433/quorum

.PHONY: fe-install
fe-install: ## Install dashboard dependencies
	cd frontend && npm ci

.PHONY: fe-dev
fe-dev: ## Dashboard dev server on :3000 (expects the API on :8000)
	$(FE) dev

.PHONY: fe-check
fe-check: ## Dashboard lint, format, types, unit/component tests, production build
	$(FE) lint
	$(FE) format:check
	$(FE) typecheck
	$(FE) test
	$(FE) build

.PHONY: e2e
e2e: db ## Playwright end to end against a real backend (starts API, scheduler, workers)
	QUORUM_DATABASE_URL=$(E2E_DATABASE_URL) ./scripts/e2e.sh

.PHONY: bench
bench: db ## Run the benchmarks (see docs/benchmarks.md)
	$(UV) python bench/engine.py
	$(UV) python bench/claim_ordering.py

.PHONY: demo
demo:
	@echo "'make $@' is not available yet: it arrives with the phase that builds it (see README roadmap)." >&2
	@exit 1
