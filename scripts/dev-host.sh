#!/usr/bin/env bash
# Run the backend on the host (no containers): API, scheduler and N chaos-enabled
# workers in demo mode. Killed workers are respawned as brand-new workers, like the
# containers' restart policy. Ctrl-C stops everything.
#   WORKERS=3 ./scripts/dev-host.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS="${LOG_DIR:-$ROOT/.dev-logs}"
mkdir -p "$LOGS"
export QUORUM_DATABASE_URL="${QUORUM_DATABASE_URL:-postgresql+asyncpg://quorum:quorum@localhost:5433/quorum}"
export QUORUM_LOG_FORMAT="${QUORUM_LOG_FORMAT:-text}"
export QUORUM_DEMO_MODE="${QUORUM_DEMO_MODE:-true}"
export QUORUM_DEMO_CHAOS="${QUORUM_DEMO_CHAOS:-true}"
# Demo-friendly timings: a killed worker's task is recovered within seconds.
export QUORUM_LEASE_TTL_S="${QUORUM_LEASE_TTL_S:-6}"
export QUORUM_HEARTBEAT_INTERVAL_S="${QUORUM_HEARTBEAT_INTERVAL_S:-1}"
export QUORUM_REAPER_INTERVAL_S="${QUORUM_REAPER_INTERVAL_S:-1}"
export QUORUM_WORKER_UNHEALTHY_AFTER_S="${QUORUM_WORKER_UNHEALTHY_AFTER_S:-3}"
export QUORUM_WORKER_DEAD_AFTER_S="${QUORUM_WORKER_DEAD_AFTER_S:-8}"
export QUORUM_AGING_INTERVAL_S="${QUORUM_AGING_INTERVAL_S:-1}"
export QUORUM_AGING_TICK_S="${QUORUM_AGING_TICK_S:-1}"

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
uv run quorum migrate
uv run quorum seed
setsid uv run quorum api >"$LOGS/api.log" 2>&1 & pids+=($!)
setsid uv run quorum scheduler >"$LOGS/scheduler.log" 2>&1 & pids+=($!)
for n in $(seq 1 "${WORKERS:-3}"); do
  # Respawn loop: a demo "kill" exits with 137 and comes back as a new worker.
  setsid bash -c "while true; do uv run quorum worker >>'$LOGS/worker-$n.log' 2>&1; sleep 1; done" & pids+=($!)
done
echo "backend running (logs in $LOGS); API on http://localhost:8000. Ctrl-C to stop."
wait
