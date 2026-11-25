#!/usr/bin/env bash
# End-to-end run: real Postgres, real API/scheduler/workers, production dashboard build, Playwright.
# Used by `make e2e` and by CI. Expects CONDUCTOR_DATABASE_URL to point at a migratable database.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS="${E2E_LOG_DIR:-$ROOT/frontend/test-results/backend-logs}"
mkdir -p "$LOGS"
export CONDUCTOR_LOG_FORMAT="${CONDUCTOR_LOG_FORMAT:-json}"
# Short timings so chaos scenarios recover within a test's patience.
export CONDUCTOR_LEASE_TTL_S="${CONDUCTOR_LEASE_TTL_S:-4}"
export CONDUCTOR_HEARTBEAT_INTERVAL_S="${CONDUCTOR_HEARTBEAT_INTERVAL_S:-1}"
export CONDUCTOR_REAPER_INTERVAL_S="${CONDUCTOR_REAPER_INTERVAL_S:-1}"
export CONDUCTOR_WORKER_UNHEALTHY_AFTER_S="${CONDUCTOR_WORKER_UNHEALTHY_AFTER_S:-3}"
export CONDUCTOR_WORKER_DEAD_AFTER_S="${CONDUCTOR_WORKER_DEAD_AFTER_S:-8}"
# The suite exercises the public demo (sandboxes, kill-worker, failure injection).
export CONDUCTOR_DEMO_MODE=true
export CONDUCTOR_DEMO_CHAOS=true
export CONDUCTOR_DEMO_STEP_DELAY_S="${CONDUCTOR_DEMO_STEP_DELAY_S:-0.8}"

pids=()
cleanup() {
  trap - EXIT INT TERM
  # Each service runs in its own session (setsid), so signal the whole group:
  # that reaches uv, the Python process under it, and any respawn loop.
  for pid in "${pids[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || true; done
  sleep 2
  for pid in "${pids[@]}"; do kill -KILL -- "-$pid" 2>/dev/null || true; done
}
trap cleanup EXIT

cd "$ROOT/backend"
uv run conductor migrate
uv run conductor seed
setsid uv run conductor api >"$LOGS/api.log" 2>&1 & pids+=($!)
setsid uv run conductor scheduler >"$LOGS/scheduler.log" 2>&1 & pids+=($!)
for n in 1 2 3; do
  # Respawn loop, like a container restart policy: a demo "kill" comes back as a new worker.
  setsid bash -c "while true; do uv run conductor worker >>'$LOGS/worker-$n.log' 2>&1; sleep 1; done" & pids+=($!)
done

for _ in $(seq 1 60); do
  curl -sf http://localhost:8000/api/v1/health >/dev/null && break
  sleep 0.5
done
curl -sf http://localhost:8000/api/v1/health >/dev/null || { echo "API did not become healthy" >&2; cat "$LOGS/api.log" >&2; exit 1; }

cd "$ROOT/frontend"
if [ "${E2E_SKIP_BUILD:-0}" != "1" ]; then npm run build; fi
npx playwright test "$@"
