#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_DOCKER_BIN="${START_DOCKER_BIN:-${TRADING_OPS_DOCKER_BIN:-docker}}"
OPS_CURL_BIN="${START_CURL_BIN:-${TRADING_OPS_CURL_BIN:-curl}}"
OPS_PYTHON_BIN="${START_PYTHON_BIN:-${TRADING_OPS_PYTHON_BIN:-python3}}"
OPS_LEDGER_URL="${START_LEDGER_URL:-http://127.0.0.1:8001}"
source "$SCRIPT_DIR/ops_common.sh"

target_service=""
start_attempted=0
start_succeeded=0

usage() {
  cat <<'EOF'
用法: scripts/start.sh [--build] [SERVICE]

SERVICE 默认为 spike。此入口只接受策略 service，不接受基础服务。
EOF
}

base_gate() {
  ops_require_state postgres running healthy
  ops_require_state redis running healthy
  ops_require_state market running healthy
  ops_require_state ledger running healthy
  ops_require_state ledger-migrate exited
  [[ "$OPS_EXIT_CODE" == 0 ]] || ops_die "ledger-migrate 退出码不是 0: ${OPS_EXIT_CODE:-unknown}"
  ops_require_state notification-worker running
  ops_require_state symbol-sync running
  ops_require_paths
}

cleanup_after_failure() {
  local status=$? cleanup_failed=0
  if (( start_attempted == 1 && start_succeeded == 0 )); then
    printf '启动未完成，目标 service 状态和末尾日志如下: %s\n' "$target_service" >&2
    ops_compose ps -a "$target_service" >&2 || true
    ops_compose logs --tail 80 "$target_service" >&2 || true
    if ops_single_state "$target_service" true; then
      case "$OPS_STATE" in
        running|restarting|paused)
          ops_compose stop --timeout 120 "$target_service" >&2 || cleanup_failed=1
          ;;
        created|removing)
          ops_compose rm -f "$target_service" >&2 || cleanup_failed=1
          ;;
      esac
    else
      cleanup_failed=1
    fi
    if ! ops_single_state "$target_service" true; then
      printf 'ERROR: 清理后无法确认 %s 状态\n' "$target_service" >&2
      cleanup_failed=1
    elif [[ "$OPS_STATE_FOUND" == 1 && "$OPS_STATE" =~ ^(running|restarting|paused|created|removing)$ ]]; then
      printf 'ERROR: 清理后 %s 仍为 %s\n' "$target_service" "$OPS_STATE" >&2
      cleanup_failed=1
    elif [[ "$OPS_STATE_FOUND" == 1 && "$OPS_STATE" != exited ]]; then
      printf 'ERROR: 清理后 %s 留有未知状态 %s\n' "$target_service" "$OPS_STATE" >&2
      cleanup_failed=1
    fi
  fi
  if (( cleanup_failed == 1 && status == 0 )); then status=2; fi
  return "$status"
}

main() {
  local build=0 service="spike" service_given=0 arg
  while (($#)); do
    arg="$1"
    case "$arg" in
      --build) build=1 ;;
      --help|-h) usage; return 0 ;;
      --*) usage >&2; ops_die "未知选项: $arg" ;;
      *) ((service_given == 0)) || ops_die "只能指定一个 Compose service"; service="$arg"; service_given=1 ;;
    esac
    shift
  done

  ops_require_host "$OPS_CURL_BIN" "$OPS_PYTHON_BIN" stat
  ops_require_env
  ops_require_compose
  ops_require_strategy_service "$service"
  target_service="$service"
  ops_require_single_state "$service" true || ops_die "$service 状态无法安全确认"
  if [[ "$OPS_STATE_FOUND" == 1 && "$OPS_STATE" != exited ]]; then
    ops_die "$service 已有活动或未清理容器（状态: ${OPS_STATE:-unknown}）"
  fi
  base_gate
  ops_notification_overview
  (( OPS_NOTIFICATION_READY == 1 )) || ops_die "关键通知路由未就绪；缺失: ${OPS_CRITICAL_ROUTE_MISSING:-unknown}"
  printf '关键通知路由门禁通过: critical routes = %s/%s；routable policies（结构指标）= %s（enabled connectors/endpoints/policies = %s/%s/%s）\n' \
    "$OPS_CRITICAL_ROUTE_COUNT" "$OPS_CRITICAL_ROUTE_TOTAL" "$OPS_ROUTABLE_POLICIES" "$OPS_ENABLED_CONNECTORS" "$OPS_ENABLED_ENDPOINTS" "$OPS_POLICIES"

  start_attempted=1
  if (( build == 1 )); then
    ops_compose up -d --wait --build "$service"
  else
    ops_compose up -d --wait "$service"
  fi
  ops_require_state "$service" running
  start_succeeded=1
  printf '服务已启动: %s\n' "$service"
  ops_compose ps "$service"
}

trap cleanup_after_failure EXIT
main "$@"
