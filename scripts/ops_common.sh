#!/usr/bin/env bash

# Shared, deliberately small operator primitives.  Callers set the optional
# OPS_*_BIN variables before sourcing this file when tests or a non-default
# installation need alternate host tools.

OPS_SCRIPT_DIR="${OPS_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
OPS_PROJECT_ROOT="${OPS_PROJECT_ROOT:-$(cd "$OPS_SCRIPT_DIR/.." && pwd)}"
OPS_ENV_FILE="${OPS_ENV_FILE:-${TRADING_PLATFORM_ENV_FILE:-$OPS_PROJECT_ROOT/.env}}"
OPS_DOCKER_BIN="${OPS_DOCKER_BIN:-docker}"
OPS_CURL_BIN="${OPS_CURL_BIN:-curl}"
OPS_PYTHON_BIN="${OPS_PYTHON_BIN:-python3}"
OPS_BASE_SERVICES="postgres ledger-migrate ledger redis market notification-worker symbol-sync"

ops_die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

ops_compose() {
  "$OPS_DOCKER_BIN" compose --profile '*' "$@"
}

ops_require_host() {
  local tool
  command -v "$OPS_DOCKER_BIN" >/dev/null 2>&1 || ops_die "未找到 Docker: $OPS_DOCKER_BIN"
  "$OPS_DOCKER_BIN" compose version >/dev/null 2>&1 || ops_die "Docker Compose 不可用"
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || ops_die "未找到宿主机工具: $tool"
  done
}

ops_require_env() {
  [[ -f "$OPS_ENV_FILE" ]] || ops_die "缺少 $OPS_ENV_FILE；请先准备 .env"
  [[ ! -L "$OPS_ENV_FILE" ]] || ops_die ".env 不得是符号链接"
  [[ -r "$OPS_ENV_FILE" ]] || ops_die "$OPS_ENV_FILE 不可读"
  local mode
  mode="$(stat -c '%a' "$OPS_ENV_FILE" 2>/dev/null)" || ops_die "无法读取 .env 权限"
  [[ "$mode" == "600" ]] || ops_die ".env 必须是 0600 权限"
}

ops_require_compose() {
  OPS_COMPOSE_SERVICES="$(ops_compose config --services 2>/dev/null)" || \
    ops_die "Compose 配置校验失败"
  [[ -n "$OPS_COMPOSE_SERVICES" ]] || ops_die "Compose 没有返回服务列表"
}

ops_require_service() {
  local service="$1" item
  [[ "$service" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || ops_die "非法 Compose service: $service"
  while IFS= read -r item; do
    [[ "$item" == "$service" ]] && return 0
  done <<<"$OPS_COMPOSE_SERVICES"
  ops_die "未知 Compose service: $service"
}

ops_require_strategy_service() {
  local service="$1" base
  ops_require_service "$service"
  for base in $OPS_BASE_SERVICES; do
    [[ "$service" != "$base" ]] || ops_die "$service 是基础服务；此入口只接受策略 service"
  done
  if ! (
    set -o pipefail
    ops_compose config --format json 2>/dev/null |
      "$OPS_PYTHON_BIN" -c '
import json
import sys

try:
    document = json.load(sys.stdin)
    service = document["services"][sys.argv[1]]
    labels = service.get("labels", {})
    if isinstance(labels, list):
        labels = {
            item.split("=", 1)[0]: item.split("=", 1)[1]
            for item in labels
            if isinstance(item, str) and "=" in item
        }
    if not isinstance(labels, dict):
        raise ValueError("labels must be an object")
    if labels.get("trading-platform.role") != "strategy":
        raise ValueError("missing strategy role")
except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
' "$service"
  ); then
    ops_die "$service 未标记为策略 service（trading-platform.role=strategy）"
  fi
}

# Read one Compose row into OPS_STATE_FOUND/OPS_STATE/OPS_HEALTH/OPS_EXIT_CODE.
# A missing row is allowed only when the second argument is true.  Multiple
# rows return failure so callers can choose whether to fail closed or warn.
ops_single_state() {
  local service="$1" allow_empty="${2:-false}"
  local rows row id name state health exit_code count=0
  OPS_STATE_FOUND=0
  OPS_STATE=""
  OPS_HEALTH=""
  OPS_EXIT_CODE=""
  rows="$(ops_compose ps -a --format '{{.ID}}|{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' "$service" 2>/dev/null)" || return 1
  if [[ -z "$rows" ]]; then
    [[ "$allow_empty" == true ]] || return 1
    return 0
  fi
  while IFS= read -r row; do
    [[ -n "$row" ]] || continue
    count=$((count + 1))
    IFS='|' read -r id name state health exit_code <<<"$row"
    [[ "$count" -eq 1 ]] || return 1
    OPS_STATE_FOUND=1
    OPS_STATE="$state"
    OPS_HEALTH="$health"
    OPS_EXIT_CODE="$exit_code"
  done <<<"$rows"
  [[ "$count" -eq 1 ]]
}

ops_require_single_state() {
  local service="$1" allow_empty="${2:-false}"
  if ! ops_single_state "$service" "$allow_empty"; then
    ops_die "$service 状态无法安全确认（可能没有容器、多个容器实例或 Compose 查询失败）"
  fi
}

ops_require_state() {
  local service="$1" expected="$2" health="${3:-}" allow_empty="${4:-false}"
  ops_require_single_state "$service" "$allow_empty"
  [[ "$OPS_STATE_FOUND" == 1 ]] || ops_die "$service 没有容器"
  [[ "$OPS_STATE" == "$expected" ]] || ops_die "$service 状态为 ${OPS_STATE:-unknown}，期望 $expected"
  if [[ -n "$health" ]]; then
    [[ "$OPS_HEALTH" == "$health" ]] || ops_die "$service 健康状态为 ${OPS_HEALTH:-unknown}，期望 $health"
  fi
}

# Poll a single Compose service until its state (and optional health) matches.
# The bounded attempt count makes one-shot services deterministic even when
# Compose returns before the container's final state is visible to `ps`.
# Query errors fail closed instead of being treated as a transient mismatch.
ops_wait_for_state() {
  local service="$1" expected="$2" health="${3:-}"
  local attempts="${4:-60}" interval="${5:-0.5}"
  local attempt
  OPS_WAIT_REASON=""

  if [[ ! "$attempts" =~ ^[1-9][0-9]*$ ]]; then
    OPS_WAIT_REASON="$service 状态等待参数无效: attempts=$attempts"
    return 1
  fi
  if [[ ! "$interval" =~ ^([0-9]+([.][0-9]+)?|[.][0-9]+)$ ]]; then
    OPS_WAIT_REASON="$service 状态等待参数无效: interval=$interval"
    return 1
  fi

  for ((attempt = 1; attempt <= attempts; attempt++)); do
    # A just-created one-shot container may briefly have no `ps` row. Keep
    # polling that case; Compose/query errors and duplicate rows still fail.
    if ! ops_single_state "$service" true; then
      OPS_WAIT_REASON="$service 状态查询失败，拒绝继续部署"
      return 1
    fi
    if [[ "$OPS_STATE_FOUND" == 1 && "$OPS_STATE" == "$expected" && \
      ( -z "$health" || "$OPS_HEALTH" == "$health" ) ]]; then
      return 0
    fi
    if (( attempt < attempts )); then
      if ! sleep "$interval"; then
        OPS_WAIT_REASON="$service 状态等待休眠失败"
        return 1
      fi
    fi
  done

  OPS_WAIT_REASON="$service 状态等待超时（当前 ${OPS_STATE:-unknown}，期望 $expected${health:+，健康状态期望 $health}）"
  return 1
}

ops_prepare_paths() {
  mkdir -p "$OPS_PROJECT_ROOT/data/wal" \
    "$OPS_PROJECT_ROOT/data/market/campaign_snapshots" \
    "$OPS_PROJECT_ROOT/logs"
  ops_require_paths
}

ops_require_paths() {
  local path
  for path in "$OPS_PROJECT_ROOT/logs" "$OPS_PROJECT_ROOT/data/wal"; do
    [[ -d "$path" && -w "$path" ]] || ops_die "$path 不存在或不可写"
  done
}

ops_notification_overview() {
  local body counts
  body="$("$OPS_CURL_BIN" --fail --silent --show-error --connect-timeout 5 --max-time 15 \
    "${OPS_LEDGER_URL:-http://127.0.0.1:8001}/api/v1/notifications/overview")" || \
    ops_die "无法读取 notification overview"
  counts="$(printf '%s' "$body" | "$OPS_PYTHON_BIN" -c '
import json
import sys

try:
    value = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
if not isinstance(value, dict):
    raise SystemExit(1)
ready = value.get("critical_routes_ready")
routes = value.get("critical_routes")
if not isinstance(ready, bool) or not isinstance(routes, dict) or not routes:
    raise SystemExit(1)
missing = []
ready_count = 0
for event_type, route in routes.items():
    if not isinstance(route, bool):
        raise SystemExit(1)
    if not route:
        missing.append(event_type)
    else:
        ready_count += 1
if ready != (ready_count == len(routes)):
    raise SystemExit(1)
result = []
for key in ("enabled_connectors", "enabled_endpoints", "policies", "routable_policies"):
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise SystemExit(1)
    result.append(str(item))
result.append("1" if ready else "0")
result.append(str(ready_count))
result.append(str(len(routes)))
result.append(",".join(missing))
print(" ".join(result))
')" || ops_die "notification overview 响应格式无效"
  read -r OPS_ENABLED_CONNECTORS OPS_ENABLED_ENDPOINTS OPS_POLICIES OPS_ROUTABLE_POLICIES \
    OPS_CRITICAL_ROUTES_READY OPS_CRITICAL_ROUTE_COUNT OPS_CRITICAL_ROUTE_TOTAL OPS_CRITICAL_ROUTE_MISSING <<<"$counts"
  OPS_NOTIFICATION_READY="$OPS_CRITICAL_ROUTES_READY"
}
