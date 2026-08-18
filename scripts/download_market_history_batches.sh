#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TARGETS_FILE="${TARGETS_FILE:-/tmp/target_symbols.tsv}"
ARCHIVE_DIR="${ARCHIVE_DIR:-data/market/history-parquet}"
CATALOG="${CATALOG:-data/market/history-batch.duckdb}"
LOG_DIR="${LOG_DIR:-data/market/download-logs}"
BATCH_SIZE="${BATCH_SIZE:-20}"
WORKERS="${WORKERS:-4}"
RESERVE_KIB="${RESERVE_KIB:-15728640}"
START="${START:-2026-01-01T00:00:00Z}"
END="${END:-2026-08-01T00:00:00Z}"

mkdir -p "$LOG_DIR"
mapfile -t symbols < <(awk 'NF { print $1 }' "$TARGETS_FILE")

for ((offset=0, batch=1; offset < ${#symbols[@]}; offset += BATCH_SIZE, batch++)); do
  avail_kib="$(df --output=avail -k . | tail -1 | tr -d ' ')"
  if (( avail_kib <= RESERVE_KIB )); then
    printf '15GiB reserve reached: avail_kib=%s\n' "$avail_kib"
    break
  fi

  group=("${symbols[@]:offset:BATCH_SIZE}")
  log_file="$LOG_DIR/2026-01_07_1s_batch20_batch_${batch}.log"
  printf 'batch=%s symbols=%s\n' "$batch" "${group[*]}" | tee "$log_file"

  uv run market-history "$ARCHIVE_DIR" \
    --catalog "$CATALOG" \
    --symbols "${group[@]}" \
    --timeframes 1s \
    --start "$START" \
    --end "$END" \
    --attempts 2 \
    --timeout 120 \
    --workers "$WORKERS" 2>&1 | tee -a "$log_file"

  avail_kib="$(df --output=avail -k . | tail -1 | tr -d ' ')"
  printf 'batch=%s avail_kib=%s\n' "$batch" "$avail_kib" | tee -a "$log_file"
done

df -h .
du -sh "$ARCHIVE_DIR"
