from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parents[2]
SCRIPTS = {
    "deploy": PROJECT_ROOT / "scripts" / "deploy.sh",
    "start": PROJECT_ROOT / "scripts" / "start.sh",
    "stop": PROJECT_ROOT / "scripts" / "stop.sh",
}
COMMON = PROJECT_ROOT / "scripts" / "ops_common.sh"
SERVICES = (
    "postgres",
    "ledger-migrate",
    "ledger",
    "redis",
    "market",
    "notification-worker",
    "symbol-sync",
    "spike",
    "strategy_kline",
    "strategy_tick",
)


FAKE_DOCKER = r'''#!/usr/bin/env bash
set -eu

log_file="${FAKE_DOCKER_LOG:?}"
state_dir="${FAKE_STATE_DIR:?}"
mkdir -p "$state_dir"
printf '%s\n' "$*" >> "$log_file"
args="$*"

write_state() {
  printf '%s\n' "$2" > "$state_dir/$1.state"
  printf '%s\n' "${3:-}" > "$state_dir/$1.health"
  printf '%s\n' "${4:-}" > "$state_dir/$1.exit"
}

read_state() {
  local service="$1"
  if [[ -f "$state_dir/$service.state" ]]; then
    cat "$state_dir/$service.state"
  else
    case "$service" in
      spike) printf '%s\n' "${FAKE_SPIKE_STATE:-exited}" ;;
      postgres|redis|market|ledger) printf 'running\n' ;;
      notification-worker) printf '%s\n' "${FAKE_WORKER_STATE:-running}" ;;
      symbol-sync) printf 'running\n' ;;
      ledger-migrate|strategy_kline|strategy_tick) printf 'exited\n' ;;
      *) printf 'exited\n' ;;
    esac
  fi
}

read_health() {
  local service="$1"
  if [[ -f "$state_dir/$service.health" ]]; then
    cat "$state_dir/$service.health"
  elif [[ "$service" =~ ^(postgres|redis|market|ledger)$ ]]; then
    printf 'healthy\n'
  else
    printf '\n'
  fi
}

read_exit() {
  local service="$1"
  if [[ -f "$state_dir/$service.exit" ]]; then
    cat "$state_dir/$service.exit"
  elif [[ "$service" == ledger-migrate ]]; then
    printf '0\n'
  else
    printf '\n'
  fi
}

if [[ "$args" == *" compose version"* ]]; then
  printf 'Docker Compose version v2.40.3\n'
  exit 0
fi
if [[ "$args" == *" config --services"* ]]; then
  printf '%s\n' postgres ledger-migrate ledger redis market notification-worker symbol-sync spike strategy_kline strategy_tick
  exit 0
fi
if [[ "$args" == *" config --format json"* ]]; then
  case "${FAKE_STRATEGY_LABEL_MODE:-valid}" in
    valid)
      printf '%s\n' '{"services":{"spike":{"labels":{"trading-platform.role":"strategy"}},"strategy_kline":{"labels":{"trading-platform.role":"strategy"}},"strategy_tick":{"labels":{"trading-platform.role":"strategy"}}},"x-fake-secret":"'"${FAKE_COMPOSE_SECRET:-}"'"}'
      ;;
    wrong)
      printf '%s\n' '{"services":{"spike":{"labels":{"trading-platform.role":"worker"}},"strategy_kline":{"labels":{"trading-platform.role":"worker"}},"strategy_tick":{"labels":{"trading-platform.role":"worker"}}}}'
      ;;
    missing)
      printf '%s\n' '{"services":{"spike":{"labels":{}},"strategy_kline":{"labels":{}},"strategy_tick":{"labels":{}}}}'
      ;;
    cross)
      printf '%s\n' '{"services":{"spike":{"labels":{"trading-platform.role":"strategy"}},"strategy_kline":{"labels":{}},"strategy_tick":{"labels":{}}}}'
      ;;
    *)
      printf '%s\n' '{}'
      ;;
  esac
  exit 0
fi
if [[ "$args" == *" build"* ]]; then
  exit 0
fi
if [[ "$args" == *" up -d --wait postgres redis"* ]]; then
  write_state postgres running healthy
  write_state redis running healthy
  exit 0
fi
if [[ "$args" == *" up -d --wait ledger-migrate"* ]]; then
  if [[ "${FAKE_MIGRATION_FAIL:-0}" == 1 ]]; then
    write_state ledger-migrate exited '' 1
  else
    write_state ledger-migrate exited '' 0
  fi
  exit 0
fi
if [[ "$args" == *" up -d --wait market ledger notification-worker symbol-sync"* ]]; then
  write_state market running healthy
  write_state ledger running healthy
  write_state notification-worker running
  write_state symbol-sync running
  exit 0
fi
if [[ "$args" == *" up -d --wait"* ]]; then
  service="${args##*--wait }"
  service="${service##*--build }"
  if [[ "${FAKE_UP_FAIL:-0}" == 1 ]]; then
    write_state "$service" "${FAKE_UP_FAIL_STATE:-restarting}"
    exit 1
  fi
  write_state "$service" "${FAKE_UP_STATE:-running}"
  exit 0
fi
if [[ "$args" == *" stop --timeout 120 "* ]]; then
  service="${!#}"
  write_state "$service" exited
  exit 0
fi
if [[ "$args" == *" rm -f "* ]]; then
  service="${!#}"
  rm -f "$state_dir/$service.state" "$state_dir/$service.health" "$state_dir/$service.exit"
  : > "$state_dir/$service.removed"
  exit 0
fi
if [[ "$args" == *" ps -a --format "* ]]; then
  service="${!#}"
  if [[ "$service" == "" || "$service" == "'"* ]]; then
    exit 0
  fi
  if [[ -f "$state_dir/$service.removed" ]]; then
    exit 0
  fi
  case "$service" in
    spike) rows="${FAKE_SPIKE_ROWS:-1}" ;;
    *) rows=1 ;;
  esac
  if [[ "$rows" == 2 ]]; then
    printf 'id-%s-1|%s|%s|%s|%s\n' "$service" "$service" "$(read_state "$service")" "$(read_health "$service")" "$(read_exit "$service")"
    printf 'id-%s-2|%s|%s||0\n' "$service" "$service" exited
  else
    state="$(read_state "$service")"
    health="$(read_health "$service")"
    exit_code="$(read_exit "$service")"
    printf 'id-%s|%s|%s|%s|%s\n' "$service" "$service" "$state" "$health" "$exit_code"
  fi
  exit 0
fi
if [[ "$args" == *" logs "* ]]; then
  printf 'fake logs\n'
  exit 0
fi
if [[ "$args" == *" ps"* ]]; then
  exit 0
fi
exit 0
'''


FAKE_CURL = r'''#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "${FAKE_CURL_LOG:?}"
if [[ "${FAKE_CURL_FAIL:-0}" == 1 ]]; then
  exit 7
fi
if [[ "$*" == *"/api/v1/notifications/overview"* ]]; then
  if [[ -n "${FAKE_NOTIFICATION_OVERVIEW:-}" ]]; then
    printf '%s\n' "$FAKE_NOTIFICATION_OVERVIEW"
  else
    printf '%s\n' '{"enabled_connectors":0,"enabled_endpoints":0,"policies":0,"routable_policies":0,"critical_routes_ready":false,"critical_routes":{"risk.halted":false,"system.strategy.unhealthy":false}}'
  fi
else
  printf '{"status":"healthy"}\n'
fi
'''


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)
    return path


def prepare(tmp_path: Path, script_name: str, *, overview: str | None = None, **extra: str) -> dict[str, str]:
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    shutil.copy2(SCRIPTS[script_name], script_dir / f"{script_name}.sh")
    shutil.copy2(COMMON, script_dir / "ops_common.sh")
    env_file = tmp_path / ".env"
    env_file.write_text("BINANCE_TESTNET=true\nSPIKE_MODE=testnet\n", encoding="utf-8")
    env_file.chmod(0o600)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    if script_name == "start":
        (tmp_path / "logs").mkdir()
        (tmp_path / "data" / "wal").mkdir(parents=True)
    docker = _write_executable(tmp_path / "fake-docker", FAKE_DOCKER)
    curl = _write_executable(tmp_path / "fake-curl", FAKE_CURL)
    environment = os.environ.copy()
    environment.update(
        {
            "DEPLOY_DOCKER_BIN": str(docker),
            "START_DOCKER_BIN": str(docker),
            "STOP_DOCKER_BIN": str(docker),
            "DEPLOY_CURL_BIN": str(curl),
            "START_CURL_BIN": str(curl),
            "DEPLOY_PYTHON_BIN": os.sys.executable,
            "START_PYTHON_BIN": os.sys.executable,
            "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
            "FAKE_CURL_LOG": str(tmp_path / "curl.log"),
            "FAKE_STATE_DIR": str(state_dir),
            "DEPLOY_MIGRATION_INTERVAL": "0",
            "DEPLOY_MIGRATION_ATTEMPTS": "2",
        }
    )
    if overview is not None:
        environment["FAKE_NOTIFICATION_OVERVIEW"] = overview
    environment.update(extra)
    return environment


def run_script(tmp_path: Path, script_name: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = prepare(tmp_path, script_name, **({} if env is None else env))
    return subprocess.run(
        ["bash", str(tmp_path / "scripts" / f"{script_name}.sh"), *args],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
    )


def invoke(tmp_path: Path, script_name: str, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    return run_script(tmp_path, script_name, *args, env=overrides)


def docker_log(tmp_path: Path) -> list[str]:
    path = tmp_path / "docker.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def state(tmp_path: Path, service: str) -> str:
    path = tmp_path / "state" / f"{service}.state"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return "running" if service == "notification-worker" else "missing"


@pytest.mark.parametrize("script_name", SCRIPTS)
def test_scripts_have_valid_shell_syntax(script_name: str) -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPTS[script_name])], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_deploy_creates_runtime_directories_and_warns_for_empty_notifications(tmp_path: Path) -> None:
    result = invoke(tmp_path, "deploy")
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stdout
    assert "0/0/0" in result.stdout
    assert "risk.halted" in result.stdout
    assert "${OPS_CRITICAL_ROUTE_MISSING" not in result.stdout
    assert (tmp_path / "data" / "wal").is_dir()
    assert (tmp_path / "data" / "market" / "campaign_snapshots").is_dir()
    assert (tmp_path / "logs").is_dir()
    calls = docker_log(tmp_path)
    assert any("config --services" in call for call in calls)
    assert any("build" in call for call in calls)
    assert not any(" up -d --wait spike" in call for call in calls)


def test_deploy_requires_private_env(tmp_path: Path) -> None:
    environment = prepare(tmp_path, "deploy")
    (tmp_path / ".env").chmod(0o644)
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "deploy.sh")],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert "0600" in result.stderr or "权限" in result.stderr


def test_deploy_rejects_missing_env(tmp_path: Path) -> None:
    environment = prepare(tmp_path, "deploy")
    (tmp_path / ".env").unlink()
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "deploy.sh")],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert ".env" in result.stderr


def test_start_rejects_unconfigured_notifications_before_up(tmp_path: Path) -> None:
    result = invoke(tmp_path, "start")
    assert result.returncode == 2
    assert "关键通知路由未就绪" in result.stderr
    assert "risk.halted" in result.stderr
    assert "${OPS_CRITICAL_ROUTE_MISSING" not in result.stderr
    assert not any(" up -d --wait spike" in call for call in docker_log(tmp_path))


@pytest.mark.parametrize("service", ["postgres", "redis", "market", "ledger", "ledger-migrate", "notification-worker", "symbol-sync"])
def test_start_rejects_base_services(tmp_path: Path, service: str) -> None:
    result = invoke(tmp_path, "start", service)
    assert result.returncode == 2
    assert "基础服务" in result.stderr


def test_start_and_deploy_do_not_encode_strategy_mode_semantics() -> None:
    for name in ("deploy", "start"):
        source = SCRIPTS[name].read_text(encoding="utf-8")
        assert "BINANCE_TESTNET" not in source
        assert "SPIKE_MODE" not in source


def test_start_accepts_any_single_compose_service_after_notification_gate(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "start",
        "strategy_kline",
        FAKE_NOTIFICATION_OVERVIEW='{"enabled_connectors":0,"enabled_endpoints":0,"policies":0,"routable_policies":0,"critical_routes_ready":true,"critical_routes":{"risk.halted":true,"system.strategy.unhealthy":true}}',
    )
    assert result.returncode == 0, result.stderr
    assert state(tmp_path, "strategy_kline") == "running"
    calls = docker_log(tmp_path)
    assert any("config --services" in call for call in calls)
    assert any(call.endswith("up -d --wait strategy_kline") for call in calls)


def test_start_counts_all_critical_routes_returned_by_overview(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "start",
        FAKE_NOTIFICATION_OVERVIEW=(
            '{"enabled_connectors":0,"enabled_endpoints":0,"policies":0,'
            '"routable_policies":0,"critical_routes_ready":true,'
            '"critical_routes":{"risk.halted":true,"system.strategy.unhealthy":true,'
            '"operations.degraded":true}}'
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "critical routes = 3/3" in result.stdout


def test_start_failure_prints_logs_and_cleans_active_target(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "start",
        FAKE_NOTIFICATION_OVERVIEW='{"enabled_connectors":1,"enabled_endpoints":1,"policies":1,"routable_policies":1,"critical_routes_ready":true,"critical_routes":{"risk.halted":true,"system.strategy.unhealthy":true}}',
        FAKE_UP_FAIL="1",
        FAKE_UP_FAIL_STATE="restarting",
    )
    assert result.returncode != 0
    assert "fake logs" in result.stderr
    assert any("stop --timeout 120 spike" in call for call in docker_log(tmp_path))
    assert state(tmp_path, "spike") == "exited"


@pytest.mark.parametrize("failure_state", ["created", "removing"])
def test_start_failure_removes_non_running_target_without_touching_other_services(
    tmp_path: Path, failure_state: str
) -> None:
    result = invoke(
        tmp_path,
        "start",
        FAKE_NOTIFICATION_OVERVIEW='{"enabled_connectors":0,"enabled_endpoints":0,"policies":0,"routable_policies":1,"critical_routes_ready":true,"critical_routes":{"risk.halted":true,"system.strategy.unhealthy":true}}',
        FAKE_UP_FAIL="1",
        FAKE_UP_FAIL_STATE=failure_state,
    )
    assert result.returncode != 0
    calls = docker_log(tmp_path)
    assert any("rm -f spike" in call for call in calls)
    assert not any("stop --timeout" in call for call in calls)
    assert not any("rm -f" in call and "spike" not in call for call in calls)
    assert not any("volume" in call or " down" in call for call in calls)
    assert state(tmp_path, "spike") == "missing"


@pytest.mark.parametrize("label_mode", ["missing", "wrong", "cross"])
def test_start_requires_target_strategy_label(tmp_path: Path, label_mode: str) -> None:
    result = invoke(
        tmp_path,
        "start",
        "strategy_kline",
        FAKE_STRATEGY_LABEL_MODE=label_mode,
        FAKE_NOTIFICATION_OVERVIEW='{"enabled_connectors":0,"enabled_endpoints":0,"policies":0,"routable_policies":1,"critical_routes_ready":true,"critical_routes":{"risk.halted":true,"system.strategy.unhealthy":true}}',
    )
    assert result.returncode == 2
    assert "strategy" in result.stderr.lower()
    assert not any(" up -d --wait strategy_kline" in call for call in docker_log(tmp_path))


def test_start_rejects_unready_critical_route_even_with_routable_policy(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "start",
        FAKE_NOTIFICATION_OVERVIEW='{"enabled_connectors":1,"enabled_endpoints":1,"policies":1,"routable_policies":1,"critical_routes_ready":false,"critical_routes":{"risk.halted":false,"system.strategy.unhealthy":true}}',
    )
    assert result.returncode == 2
    assert "关键通知路由未就绪" in result.stderr
    assert "risk.halted" in result.stderr
    assert not any(" up -d --wait spike" in call for call in docker_log(tmp_path))


def test_start_rejects_inconsistent_critical_readiness_response(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "start",
        FAKE_NOTIFICATION_OVERVIEW='{"enabled_connectors":1,"enabled_endpoints":1,"policies":1,"routable_policies":1,"critical_routes_ready":true,"critical_routes":{"risk.halted":false,"system.strategy.unhealthy":true}}',
    )
    assert result.returncode == 2
    assert "响应格式无效" in result.stderr
    assert not any(" up -d --wait spike" in call for call in docker_log(tmp_path))


def test_start_rejects_existing_active_or_duplicate_target(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "start",
        FAKE_SPIKE_ROWS="2",
        FAKE_NOTIFICATION_OVERVIEW='{"enabled_connectors":1,"enabled_endpoints":1,"policies":1}',
    )
    assert result.returncode == 2
    assert "多个容器" in result.stderr
    assert not any(" up -d --wait spike" in call for call in docker_log(tmp_path))


def test_stop_uses_120_seconds_and_keeps_notification_worker_running(tmp_path: Path) -> None:
    result = invoke(tmp_path, "stop", FAKE_SPIKE_STATE="running")
    assert result.returncode == 0, result.stderr
    assert any("stop --timeout 120 spike" in call for call in docker_log(tmp_path))
    assert state(tmp_path, "spike") == "exited"
    assert state(tmp_path, "notification-worker") == "running"


def test_stop_removes_created_target_only(tmp_path: Path) -> None:
    result = invoke(tmp_path, "stop", FAKE_SPIKE_STATE="created")
    assert result.returncode == 0, result.stderr
    calls = docker_log(tmp_path)
    assert any("rm -f spike" in call for call in calls)
    assert not any("stop --timeout" in call for call in calls)
    assert not any("volume" in call or " down" in call for call in calls)
    assert state(tmp_path, "spike") == "missing"


def test_stop_fails_closed_for_removing_target(tmp_path: Path) -> None:
    result = invoke(tmp_path, "stop", FAKE_SPIKE_STATE="removing")
    assert result.returncode == 2
    assert "正在移除" in result.stderr
    calls = docker_log(tmp_path)
    assert not any("rm -f spike" in call for call in calls)
    assert not any("stop --timeout" in call for call in calls)
    assert not any("volume" in call or " down" in call for call in calls)


def test_strategy_label_validation_does_not_trace_interpolated_compose_secret(tmp_path: Path) -> None:
    environment = prepare(tmp_path, "stop", FAKE_COMPOSE_SECRET="fake-compose-secret")
    result = subprocess.run(
        ["bash", "-x", str(tmp_path / "scripts" / "stop.sh"), "spike"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "fake-compose-secret" not in result.stdout
    assert "fake-compose-secret" not in result.stderr


def test_stop_warns_but_continues_when_notification_worker_is_down(tmp_path: Path) -> None:
    result = invoke(tmp_path, "stop", FAKE_SPIKE_STATE="running", FAKE_WORKER_STATE="exited")
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr
    assert any("stop --timeout 120 spike" in call for call in docker_log(tmp_path))


def test_stop_warns_but_continues_when_notification_worker_has_no_container(tmp_path: Path) -> None:
    result = invoke(tmp_path, "stop", FAKE_SPIKE_STATE="running", FAKE_WORKER_STATE="missing")
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr
    assert any("stop --timeout 120 spike" in call for call in docker_log(tmp_path))


def test_stop_refuses_notification_worker(tmp_path: Path) -> None:
    result = invoke(tmp_path, "stop", "notification-worker")
    assert result.returncode == 2
    assert "notification-worker" in result.stderr
    assert not any("stop --timeout" in call for call in docker_log(tmp_path))


def test_stop_fails_closed_for_multiple_instances(tmp_path: Path) -> None:
    result = invoke(tmp_path, "stop", FAKE_SPIKE_ROWS="2", FAKE_SPIKE_STATE="running")
    assert result.returncode == 2
    assert "多个容器" in result.stderr
    assert not any("stop --timeout" in call for call in docker_log(tmp_path))
