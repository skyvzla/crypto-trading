#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_DOCKER_BIN="${DEPLOY_DOCKER_BIN:-${TRADING_OPS_DOCKER_BIN:-docker}}"
OPS_CURL_BIN="${DEPLOY_CURL_BIN:-${TRADING_OPS_CURL_BIN:-curl}}"
OPS_PYTHON_BIN="${DEPLOY_PYTHON_BIN:-${TRADING_OPS_PYTHON_BIN:-python3}}"
OPS_LEDGER_URL="${DEPLOY_LEDGER_URL:-http://127.0.0.1:8001}"
OPS_MARKET_URL="${DEPLOY_MARKET_URL:-http://127.0.0.1:8000}"
source "$SCRIPT_DIR/ops_common.sh"

usage() {
  cat <<'EOF'
用法: scripts/deploy.sh

部署数据库、缓存、行情、账本、通知 worker 和币种同步服务，不启动策略 service。
EOF
}

health_check() {
  local name="$1" url="$2"
  "$OPS_CURL_BIN" --fail --silent --show-error --connect-timeout 5 --max-time 15 \
    "$url" >/dev/null || ops_die "$name 健康接口失败: $url"
}

main() {
  case "${1:-}" in
    "") ;;
    --help|-h) usage; return 0 ;;
    *) usage >&2; ops_die "deploy.sh 不接受参数" ;;
  esac

  ops_require_host "$OPS_CURL_BIN" "$OPS_PYTHON_BIN" stat
  ops_require_env
  ops_prepare_paths
  ops_require_compose

  ops_compose build
  ops_compose up -d --wait postgres redis
  # Compose's --wait handles the one-shot migration lifecycle; the explicit
  # state/exit-code check below is the final gate used by later services.
  ops_compose up -d --wait ledger-migrate
  ops_require_state ledger-migrate exited
  [[ "$OPS_EXIT_CODE" == 0 ]] || ops_die "ledger-migrate 退出码不是 0: ${OPS_EXIT_CODE:-unknown}"
  ops_compose up -d --wait market ledger notification-worker symbol-sync

  ops_require_state postgres running healthy
  ops_require_state redis running healthy
  ops_require_state market running healthy
  ops_require_state ledger running healthy
  ops_require_state notification-worker running
  ops_require_state symbol-sync running
  health_check market "$OPS_MARKET_URL/health"
  health_check ledger "$OPS_LEDGER_URL/api/v1/health"

  ops_notification_overview
  if (( OPS_NOTIFICATION_READY == 1 )); then
    printf '关键通知路由已就绪: critical routes = %s/%s；routable policies（结构指标）= %s（enabled connectors/endpoints/policies = %s/%s/%s）\n' \
      "$OPS_CRITICAL_ROUTE_COUNT" "$OPS_CRITICAL_ROUTE_TOTAL" "$OPS_ROUTABLE_POLICIES" "$OPS_ENABLED_CONNECTORS" "$OPS_ENABLED_ENDPOINTS" "$OPS_POLICIES"
  else
    printf 'WARNING: 关键通知路由未就绪（缺失: %s；critical routes: %s/%s；routable policies（结构指标）: %s；enabled connectors/endpoints/policies: %s/%s/%s）；deploy 成功，但 start 仍会拒绝策略 service。\n' \
      "${OPS_CRITICAL_ROUTE_MISSING:-unknown}" "$OPS_CRITICAL_ROUTE_COUNT" "$OPS_CRITICAL_ROUTE_TOTAL" "$OPS_ROUTABLE_POLICIES" "$OPS_ENABLED_CONNECTORS" "$OPS_ENABLED_ENDPOINTS" "$OPS_POLICIES"
  fi
  printf '基础服务部署完成；策略 service 未启动。\n'
  ops_compose ps
}

main "$@"
