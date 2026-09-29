#!/usr/bin/env bash

set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
OPS_DOCKER_BIN="${TRADING_OPS_DOCKER_BIN:-docker}"

ops_compose() {
  "$OPS_DOCKER_BIN" compose "$@"
}

ops_pg_scalar() {
  ops_compose exec -T postgres sh -ceu '
    psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
  ' sh "$1"
}

backup_path="${1:-backups/ledger_$(date -u +%Y%m%dT%H%M%SZ).dump}"
if [[ "$backup_path" != /* ]]; then
  backup_path="$project_root/$backup_path"
fi
if [[ -e "$backup_path" ]]; then
  echo "backup target already exists: $backup_path" >&2
  exit 2
fi

umask 077
mkdir -p "$(dirname "$backup_path")"

verify_db="ledger_restore_verify_$(date -u +%Y%m%d%H%M%S)_$$"
if ! [[ "$verify_db" =~ ^[a-z0-9_]+$ ]]; then
  echo "generated restore database name is invalid" >&2
  exit 2
fi

postgres_container="$(ops_compose ps -q postgres)"
if [[ -z "$postgres_container" ]]; then
  echo "Compose PostgreSQL is not running" >&2
  exit 1
fi

migration_table_exists="$(ops_pg_scalar "SELECT to_regclass('public.ledger_schema_migrations') IS NOT NULL")"
if [[ "$migration_table_exists" != "t" ]]; then
  public_table_count="$(ops_pg_scalar "SELECT COUNT(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public'")"
  if [[ "$public_table_count" == "0" ]]; then
    echo "BACKUP_SKIPPED_EMPTY_DATABASE: no application tables exist yet"
    exit 0
  fi
  echo "database has application tables but no migration history; refusing deployment" >&2
  exit 1
fi

migration_row_count="$(ops_pg_scalar "SELECT COUNT(*) FROM ledger_schema_migrations")"
if [[ ! "$migration_row_count" =~ ^[1-9][0-9]*$ ]]; then
  echo "ledger migration history is empty or invalid; refusing deployment" >&2
  exit 1
fi

cleanup() {
  ops_compose exec -T postgres sh -ceu '
    dropdb --if-exists --force -U "$POSTGRES_USER" "$1"
  ' sh "$verify_db" >/dev/null 2>&1 || true
}
trap cleanup EXIT

current_version="$(ops_pg_scalar "SELECT COALESCE(MAX(version), 0) FROM ledger_schema_migrations")"
extra_count_sql=""
if (( current_version >= 5 )); then
  extra_count_sql="
  'exchange_categories', (SELECT COUNT(*) FROM exchange_categories),
  'exchange_symbol_categories', (SELECT COUNT(*) FROM exchange_symbol_categories),
  'exchange_symbol_sync_state', (SELECT COUNT(*) FROM exchange_symbol_sync_state),
  'symbol_global_admission', (SELECT COUNT(*) FROM symbol_global_admission),
  'symbol_global_admission_audit', (SELECT COUNT(*) FROM symbol_global_admission_audit),
  'strategy_category_admission', (SELECT COUNT(*) FROM strategy_category_admission),
  'strategy_category_admission_audit', (SELECT COUNT(*) FROM strategy_category_admission_audit),"
fi
if (( current_version >= 6 )); then
  extra_count_sql+="
  'backtest_researches', (SELECT COUNT(*) FROM backtest_researches),
  'backtest_runs', (SELECT COUNT(*) FROM backtest_runs),
  'backtest_trades', (SELECT COUNT(*) FROM backtest_trades),
  'backtest_orders', (SELECT COUNT(*) FROM backtest_orders),
  'backtest_fills', (SELECT COUNT(*) FROM backtest_fills),
  'backtest_events', (SELECT COUNT(*) FROM backtest_events),
  'backtest_reports', (SELECT COUNT(*) FROM backtest_reports),
  'backtest_report_rows', (SELECT COUNT(*) FROM backtest_report_rows),
  'backtest_strategy_schemas', (SELECT COUNT(*) FROM backtest_strategy_schemas),"
fi
if (( current_version >= 16 )); then
  extra_count_sql+="
  'execution_event_journal', (SELECT COUNT(*) FROM execution_event_journal),"
fi

count_sql="
SELECT jsonb_build_object(
  'orders', (SELECT COUNT(*) FROM orders),
  'trades', (SELECT COUNT(*) FROM trades),
  'positions', (SELECT COUNT(*) FROM positions),
  'subcategory_admission', (SELECT COUNT(*) FROM subcategory_admission),
  'subcategory_admission_audit', (SELECT COUNT(*) FROM subcategory_admission_audit),
  'strategy_audit_events', (SELECT COUNT(*) FROM strategy_audit_events),
  'strategy_runtime_status', (SELECT COUNT(*) FROM strategy_runtime_status),
  'exchange_symbols', (SELECT COUNT(*) FROM exchange_symbols),
  $extra_count_sql
  'ledger_schema_migrations', (SELECT COUNT(*) FROM ledger_schema_migrations)
)::text;
"
migration_sql="
SELECT COALESCE(
  jsonb_object_agg(version::text, filename || ':' || checksum ORDER BY version),
  '{}'::jsonb
)::text FROM ledger_schema_migrations;
"

source_counts="$(ops_compose exec -T postgres sh -ceu '
  psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
' sh "$count_sql")"
source_migrations="$(ops_compose exec -T postgres sh -ceu '
  psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
' sh "$migration_sql")"

ops_compose exec -T postgres sh -ceu '
  pg_dump --format=custom --no-owner --no-privileges \
    --serializable-deferrable -U "$POSTGRES_USER" -d "$POSTGRES_DB"
' >"$backup_path"

if [[ ! -s "$backup_path" ]]; then
  echo "backup archive is empty" >&2
  exit 1
fi
ops_compose exec -T postgres pg_restore --list <"$backup_path" >/dev/null

ops_compose exec -T postgres sh -ceu '
  createdb -U "$POSTGRES_USER" "$1"
' sh "$verify_db"
ops_compose exec -T postgres sh -ceu '
  pg_restore --exit-on-error --no-owner --no-privileges \
    -U "$POSTGRES_USER" -d "$1"
' sh "$verify_db" <"$backup_path"

restored_counts="$(ops_compose exec -T postgres sh -ceu '
  psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -c "$2"
' sh "$verify_db" "$count_sql")"
restored_migrations="$(ops_compose exec -T postgres sh -ceu '
  psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -c "$2"
' sh "$verify_db" "$migration_sql")"

if [[ "$source_counts" != "$restored_counts" ]]; then
  echo "restored row counts do not match source" >&2
  exit 1
fi
if [[ "$source_migrations" != "$restored_migrations" ]]; then
  echo "restored migration history does not match source" >&2
  exit 1
fi

echo "BACKUP_RESTORE_OK"
echo "archive=$backup_path"
echo "counts=$restored_counts"
echo "migrations=$restored_migrations"
