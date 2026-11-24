# Conductor developer commands. Run `make help` for the list.
SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose -f deploy/docker-compose.yml
UV      := cd backend && uv run

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Install backend dependencies (incl. dev tools) into backend/.venv
	cd backend && uv sync --locked

.PHONY: dev
dev: ## Build and run the full local stack (DB, migrations, API) in the foreground
	$(COMPOSE) up --build

.PHONY: down
down: ## Stop the local stack (keeps the database volume)
	$(COMPOSE) down

.PHONY: db
db: ## Start only PostgreSQL and wait until it is healthy
	$(COMPOSE) up -d --wait db

.PHONY: migrate
migrate: db ## Apply migrations to the local database from the host
	$(UV) conductor migrate

.PHONY: fmt
fmt: ## Auto-format and apply safe lint fixes
	$(UV) ruff format .
	$(UV) ruff check --fix .

.PHONY: lint
lint: ## Lint and check formatting (no changes)
	$(UV) ruff check .
	$(UV) ruff format --check .

.PHONY: typecheck
typecheck: ## Type-check the backend (mypy strict)
	$(UV) mypy

.PHONY: test-unit
test-unit: ## Run tests that need no database
	$(UV) pytest tests/unit

.PHONY: test
test: db ## Run the full test suite against the Compose PostgreSQL
	$(UV) pytest

.PHONY: check
check: lint typecheck test ## Everything CI runs on the backend

.PHONY: bench demo
bench demo:
	@echo "'make $@' is not available yet: it arrives with the phase that builds it (see README roadmap)." >&2
	@exit 1
