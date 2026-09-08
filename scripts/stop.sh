#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_DOCKER_BIN="${STOP_DOCKER_BIN:-${TRADING_OPS_DOCKER_BIN:-docker}}"
OPS_PYTHON_BIN="${STOP_PYTHON_BIN:-${TRADING_OPS_PYTHON_BIN:-python3}}"
source "$SCRIPT_DIR/ops_common.sh"

usage() {
  cat <<'EOF'
用法: scripts/stop.sh [SERVICE]

SERVICE 默认为 spike。此入口只接受策略 service，使用 120 秒宽限期停止。
EOF
}

main() {
  local service="${1:-spike}" state
  (($# <= 1)) || { usage >&2; ops_die "只能指定一个 Compose service"; }
  case "$service" in
    --help|-h) usage; return 0 ;;
    --*) usage >&2; ops_die "未知选项: $service" ;;
  esac

  ops_require_host "$OPS_PYTHON_BIN"
  ops_require_compose
  ops_require_strategy_service "$service"

  if ! ops_single_state notification-worker true || \
    [[ "$OPS_STATE_FOUND" != 1 || "$OPS_STATE" != running ]]; then
    printf 'WARNING: notification-worker 当前未确认 running，继续停止目标 service。\n' >&2
  fi

  ops_require_single_state "$service" true || ops_die "$service 状态无法安全确认"
  if [[ "$OPS_STATE_FOUND" != 1 ]]; then
    printf '%s 没有容器，视为已经停止。\n' "$service"
    return 0
  fi
  case "$OPS_STATE" in
    exited) printf '%s 已经停止。\n' "$service" ;;
    running|restarting|paused) ops_compose stop --timeout 120 "$service" ;;
    created) ops_compose rm -f "$service" ;;
    removing) ops_die "$service 正在移除，拒绝并等待其状态稳定后重试" ;;
    *) ops_die "$service 状态无法安全判断: ${OPS_STATE:-unknown}" ;;
  esac

  ops_require_single_state "$service" true || ops_die "$service 停止后状态无法确认"
  if [[ "$OPS_STATE_FOUND" == 1 && "$OPS_STATE" =~ ^(running|restarting|paused|created|removing)$ ]]; then
    ops_die "$service 停止后仍为 $OPS_STATE"
  fi
  if ! ops_single_state notification-worker true || \
    [[ "$OPS_STATE_FOUND" != 1 || "$OPS_STATE" != running ]]; then
    printf 'WARNING: notification-worker 停止后未确认 running。\n' >&2
  fi
  printf '服务已停止: %s\n' "$service"
  printf "日志: docker compose --profile '*' logs %s；文件日志: logs/；WAL: data/wal/；事件记录: 账本数据库。\n" "$service"
}

main "$@"
