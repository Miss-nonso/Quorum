#!/usr/bin/env bash
# Run the backend on the host (no containers): API, scheduler and N chaos-enabled
# workers in demo mode. Killed workers are respawned as brand-new workers, like the
# containers' restart policy. Ctrl-C stops everything.
#   WORKERS=3 ./scripts/dev-host.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS="${LOG_DIR:-$ROOT/.dev-logs}"
mkdir -p "$LOGS"
export CONDUCTOR_DATABASE_URL="${CONDUCTOR_DATABASE_URL:-postgresql+asyncpg://conductor:conductor@localhost:5433/conductor}"
export CONDUCTOR_LOG_FORMAT="${CONDUCTOR_LOG_FORMAT:-text}"
export CONDUCTOR_DEMO_MODE="${CONDUCTOR_DEMO_MODE:-true}"
export CONDUCTOR_DEMO_CHAOS="${CONDUCTOR_DEMO_CHAOS:-true}"
# Demo-friendly timings: a killed worker's task is recovered within seconds.
export CONDUCTOR_LEASE_TTL_S="${CONDUCTOR_LEASE_TTL_S:-6}"
export CONDUCTOR_HEARTBEAT_INTERVAL_S="${CONDUCTOR_HEARTBEAT_INTERVAL_S:-1}"
export CONDUCTOR_REAPER_INTERVAL_S="${CONDUCTOR_REAPER_INTERVAL_S:-1}"
export CONDUCTOR_WORKER_UNHEALTHY_AFTER_S="${CONDUCTOR_WORKER_UNHEALTHY_AFTER_S:-3}"
export CONDUCTOR_WORKER_DEAD_AFTER_S="${CONDUCTOR_WORKER_DEAD_AFTER_S:-8}"
export CONDUCTOR_AGING_INTERVAL_S="${CONDUCTOR_AGING_INTERVAL_S:-1}"
export CONDUCTOR_AGING_TICK_S="${CONDUCTOR_AGING_TICK_S:-1}"

pids=()
cleanup() {
  trap - EXIT INT TERM
  # Each service runs in its own session (setsid), so signal the whole group:
  # that reaches uv, the Python process under it, and any respawn loop.
  for pid in "${pids[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || true; done
  sleep 2
  for pid in "${pids[@]}"; do kill -KILL -- "-$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

cd "$ROOT/backend"
uv run conductor migrate
uv run conductor seed
setsid uv run conductor api >"$LOGS/api.log" 2>&1 & pids+=($!)
setsid uv run conductor scheduler >"$LOGS/scheduler.log" 2>&1 & pids+=($!)
for n in $(seq 1 "${WORKERS:-3}"); do
  # Respawn loop: a demo "kill" exits with 137 and comes back as a new worker.
  setsid bash -c "while true; do uv run conductor worker >>'$LOGS/worker-$n.log' 2>&1; sleep 1; done" & pids+=($!)
done
echo "backend running (logs in $LOGS); API on http://localhost:8000. Ctrl-C to stop."
wait
