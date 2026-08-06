#!/usr/bin/env bash

set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

ledger_health_url="${LEDGER_HEALTH_URL:-http://127.0.0.1:8001/api/v1/health}"
recovery_timeout_seconds="${LEDGER_RECOVERY_TIMEOUT_SECONDS:-60}"

if ! [[ "$recovery_timeout_seconds" =~ ^[1-9][0-9]*$ ]]; then
  echo "LEDGER_RECOVERY_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
fi

initial_status="$(curl --max-time 2 -sS -o /dev/null -w '%{http_code}' "$ledger_health_url" || true)"
if [[ "$initial_status" != "200" ]]; then
  echo "ledger must be healthy before recovery verification (HTTP $initial_status)" >&2
  exit 1
fi

echo "Recreating PostgreSQL without removing its volume..."
docker compose up -d --force-recreate postgres >/dev/null &
recreate_pid=$!
start_seconds="$(date +%s)"
saw_dependency_outage=false

while true; do
  status="$(curl --max-time 2 -sS -o /dev/null -w '%{http_code}' "$ledger_health_url" || true)"
  elapsed_seconds="$(( $(date +%s) - start_seconds ))"

  if [[ "$status" != "200" ]]; then
    saw_dependency_outage=true
  elif [[ "$saw_dependency_outage" == "true" ]]; then
    wait "$recreate_pid"
    echo "ledger recovered after ${elapsed_seconds}s"
    exit 0
  elif ! kill -0 "$recreate_pid" 2>/dev/null; then
    wait "$recreate_pid"
    echo "ledger remained healthy while PostgreSQL was recreated"
    exit 0
  fi

  if (( elapsed_seconds >= recovery_timeout_seconds )); then
    wait "$recreate_pid" || true
    echo "ledger did not recover within ${recovery_timeout_seconds}s (HTTP $status)" >&2
    exit 1
  fi

  sleep 1
done
