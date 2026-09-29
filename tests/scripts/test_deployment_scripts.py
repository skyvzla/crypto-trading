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
if [[ "$args" == *" ps -q postgres"* ]]; then
  printf 'id-postgres\n'
  exit 0
fi
if [[ "$args" == *"to_regclass('public.ledger_schema_migrations')"* ]]; then
  case "${FAKE_BACKUP_DB_STATE:-empty}" in
    empty|partial) printf 'f\n' ;;
    migrated|history-empty) printf 't\n' ;;
    *) exit 1 ;;
  esac
  exit 0
fi
if [[ "$args" == *"pg_catalog.pg_tables"* ]]; then
  case "${FAKE_BACKUP_DB_STATE:-empty}" in
    empty) printf '0\n' ;;
    partial) printf '2\n' ;;
    *) exit 1 ;;
  esac
  exit 0
fi
if [[ "$args" == *"COUNT(*) FROM ledger_schema_migrations"* ]]; then
  case "${FAKE_BACKUP_DB_STATE:-empty}" in
    migrated) printf '4\n' ;;
    history-empty) printf '0\n' ;;
    *) exit 1 ;;
  esac
  exit 0
fi
if [[ "$args" == *" config --services"* ]]; then
  printf '%s\n' postgres ledger-migrate ledger redis market notification-worker symbol-sync spike strategy_kline strategy_tick
  exit 0
fi
if [[ "$args" == *" config --format json"* ]]; then
  image="${TRADING_PLATFORM_IMAGE:-trading_platform-ledger}"
  case "${FAKE_STRATEGY_LABEL_MODE:-valid}" in
    valid)
      spike_label='{"trading-platform.role":"strategy"}'
      long_label='{"trading-platform.role":"strategy"}'
      kline_label='{"trading-platform.role":"strategy"}'
      tick_label='{"trading-platform.role":"strategy"}'
      ;;
    wrong)
      spike_label='{"trading-platform.role":"worker"}'
      long_label='{"trading-platform.role":"worker"}'
      kline_label='{"trading-platform.role":"worker"}'
      tick_label='{"trading-platform.role":"worker"}'
      ;;
    missing)
      spike_label='{}'
      long_label='{}'
      kline_label='{}'
      tick_label='{}'
      ;;
    cross)
      spike_label='{"trading-platform.role":"strategy"}'
      long_label='{}'
      kline_label='{}'
      tick_label='{}'
      ;;
    *)
      exit 1
      ;;
  esac
  printf '{"services":{"market":{"image":"%s"},"ledger-migrate":{"image":"%s"},"ledger":{"image":"%s"},"notification-worker":{"image":"%s"},"symbol-sync":{"image":"%s"},"spike":{"image":"%s","labels":%s},"long_breakout":{"image":"%s","labels":%s},"strategy_kline":{"image":"%s","labels":%s},"strategy_tick":{"image":"%s","labels":%s}},"x-fake-secret":"%s"}\n' \
    "$image" "$image" "$image" "$image" "$image" "$image" "$spike_label" \
    "$image" "$long_label" "$image" "$kline_label" "$image" "$tick_label" \
    "${FAKE_COMPOSE_SECRET:-}"
  exit 0
fi
if [[ "$args" == *" build"* ]]; then
  exit 0
fi
if [[ "$args" == *" pull market ledger-migrate ledger notification-worker symbol-sync spike long_breakout strategy_kline strategy_tick"* ]]; then
  exit 0
fi
if [[ "$args" == *" up -d --wait --no-build --pull missing postgres redis"* ]]; then
  write_state postgres running healthy
  write_state redis running healthy
  exit 0
fi
if [[ "$args" == *" up -d --wait postgres redis"* ]]; then
  write_state postgres running healthy
  write_state redis running healthy
  exit 0
fi
if [[ "$args" == *" up -d ledger-migrate"* || "$args" == *" up -d --no-build --pull never ledger-migrate"* ]]; then
  if [[ "${FAKE_MIGRATION_UP_FAIL:-0}" == 1 ]]; then
    exit 1
  fi
  if [[ "${FAKE_MIGRATION_FAIL:-0}" == 1 ]]; then
    write_state ledger-migrate exited '' 1
  elif [[ "${FAKE_MIGRATION_TRANSITION:-0}" == 1 || "${FAKE_MIGRATION_STAY_RUNNING:-0}" == 1 ]]; then
    write_state ledger-migrate running ''
  else
    write_state ledger-migrate exited '' 0
  fi
  exit 0
fi
if [[ "$args" == *" up -d --wait --no-build --pull never market ledger notification-worker symbol-sync"* || "$args" == *" up -d --wait market ledger notification-worker symbol-sync"* ]]; then
  write_state market running healthy
  write_state ledger running healthy
  write_state notification-worker running
  write_state symbol-sync running
  exit 0
fi
if [[ "$args" == *" up -d --wait --no-build --pull never "* ]]; then
  service="${args##*--pull never }"
  if [[ "${FAKE_UP_FAIL:-0}" == 1 ]]; then
    write_state "$service" "${FAKE_UP_FAIL_STATE:-restarting}"
    exit 1
  fi
  write_state "$service" "${FAKE_UP_STATE:-running}"
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
  if [[ "$service" == ledger-migrate && "${FAKE_MIGRATION_PS_FAIL:-0}" == 1 ]]; then
    exit 1
  fi
  if [[ "$service" == ledger-migrate && "${FAKE_MIGRATION_TRANSITION:-0}" == 1 ]]; then
    transition_file="$state_dir/ledger-migrate.polls"
    polls=0
    if [[ -f "$transition_file" ]]; then
      polls="$(cat "$transition_file")"
    fi
    polls=$((polls + 1))
    printf '%s\n' "$polls" > "$transition_file"
    if (( polls >= ${FAKE_MIGRATION_TRANSITION_POLLS:-2} )); then
      write_state ledger-migrate exited '' 0
    fi
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
    backup = script_dir / "verify_ledger_backup_restore.sh"
    _write_executable(
        backup,
        '#!/usr/bin/env bash\nif [[ "${FAKE_BACKUP_FAIL:-0}" == 1 ]]; then echo "backup failed" >&2; exit 1; fi\nprintf "BACKUP_RESTORE_OK\\n"\nprintf "backup\\n" >> "${FAKE_BACKUP_LOG:?}"\n',
    )
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
            "FAKE_BACKUP_LOG": str(tmp_path / "backup.log"),
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


def run_backup_script(
    tmp_path: Path,
    *,
    database_state: str,
    release_compose_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = prepare(tmp_path, "deploy")
    backup = tmp_path / "scripts" / "verify_ledger_backup_restore.sh"
    shutil.copy2(PROJECT_ROOT / "scripts" / "verify_ledger_backup_restore.sh", backup)
    environment.update(
        {
            "TRADING_OPS_DOCKER_BIN": environment["DEPLOY_DOCKER_BIN"],
            "FAKE_BACKUP_DB_STATE": database_state,
        }
    )
    if release_compose_dir is not None:
        environment.update(
            {
                "TRADING_PLATFORM_PROJECT_ROOT": str(tmp_path),
                "TRADING_PLATFORM_ENV_FILE": str(tmp_path / ".env"),
                "TRADING_PLATFORM_RELEASE_COMPOSE_DIR": str(release_compose_dir),
            }
        )
    return subprocess.run(
        ["bash", str(backup)],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
    )


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


def test_deploy_pulls_fixed_ghcr_image_after_backup_without_building(tmp_path: Path) -> None:
    image = "ghcr.io/example/trading-platform:v1.2.3"
    result = invoke(tmp_path, "deploy", TRADING_PLATFORM_IMAGE=image)
    assert result.returncode == 0, result.stderr
    assert "BACKUP_RESTORE_OK" in result.stdout
    assert image in result.stdout
    assert (tmp_path / "backup.log").read_text(encoding="utf-8").strip() == "backup"
    calls = docker_log(tmp_path)
    assert any("--pull missing postgres redis" in call for call in calls)
    assert any(" pull market ledger-migrate ledger" in call for call in calls)
    assert not any(" build" in call for call in calls)
    assert any("--no-build --pull never ledger-migrate" in call for call in calls)


def test_deploy_uses_release_compose_files_and_persistent_project_directory(
    tmp_path: Path,
) -> None:
    release_dir = tmp_path / "release-v1"
    result = invoke(
        tmp_path,
        "deploy",
        TRADING_PLATFORM_IMAGE="ghcr.io/example/trading-platform:v1.2.3",
        TRADING_PLATFORM_RELEASE_COMPOSE_DIR=str(release_dir),
    )
    assert result.returncode == 0, result.stderr
    calls = [call for call in docker_log(tmp_path) if call.startswith("compose ")]
    relevant_calls = [
        call
        for call in calls
        if "config --services" in call or " up -d" in call or " pull " in call
    ]
    assert relevant_calls
    assert all(
        f"--project-directory {tmp_path} --env-file {tmp_path}/.env "
        f"-f {release_dir}/compose.yaml "
        f"-f {release_dir}/deploy/compose.release.yaml" in call
        for call in relevant_calls
    )


def test_backup_restore_uses_release_compose_and_persistent_project_directory(
    tmp_path: Path,
) -> None:
    release_dir = tmp_path / "releases" / "v1.2.3"
    result = run_backup_script(
        tmp_path,
        database_state="empty",
        release_compose_dir=release_dir,
    )
    assert result.returncode == 0, result.stderr
    calls = [call for call in docker_log(tmp_path) if call.startswith("compose ")]
    assert calls
    assert all(
        f"--project-directory {tmp_path} --env-file {tmp_path}/.env "
        f"-f {release_dir}/compose.yaml "
        f"-f {release_dir}/deploy/compose.release.yaml" in call
        for call in calls
    ), calls


def test_deploy_rejects_running_strategy_before_backup_or_pull(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "deploy",
        TRADING_PLATFORM_IMAGE="ghcr.io/example/trading-platform:v1.2.3",
        FAKE_SPIKE_STATE="running",
    )
    assert result.returncode == 2
    assert "关闭准入" in result.stderr
    assert not (tmp_path / "backup.log").exists()
    assert not any(" pull " in call for call in docker_log(tmp_path))


def test_deploy_backup_failure_stops_before_application_pull_and_migration(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "deploy",
        TRADING_PLATFORM_IMAGE="ghcr.io/example/trading-platform:v1.2.3",
        FAKE_BACKUP_FAIL="1",
    )
    assert result.returncode != 0
    calls = docker_log(tmp_path)
    assert any("--pull missing postgres redis" in call for call in calls)
    assert not any(" pull market ledger-migrate ledger" in call for call in calls)
    assert not any("ledger-migrate" in call and "up -d" in call for call in calls)


def test_deploy_rejects_floating_or_non_ghcr_image_tags(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "deploy",
        TRADING_PLATFORM_IMAGE="ghcr.io/example/trading-platform:latest",
    )
    assert result.returncode == 2
    assert "固定 tag" in result.stderr
    assert not (tmp_path / "backup.log").exists()


def test_verified_backup_skips_a_truly_empty_database(tmp_path: Path) -> None:
    result = run_backup_script(tmp_path, database_state="empty")
    assert result.returncode == 0, result.stderr
    assert "BACKUP_SKIPPED_EMPTY_DATABASE" in result.stdout
    assert not list((tmp_path / "backups").glob("*.dump"))


@pytest.mark.parametrize("database_state", ["partial", "history-empty"])
def test_verified_backup_rejects_schema_without_valid_migration_history(
    tmp_path: Path, database_state: str
) -> None:
    result = run_backup_script(tmp_path, database_state=database_state)
    assert result.returncode == 1
    assert "migration history" in result.stderr.lower()


def test_deploy_waits_for_one_shot_migration_to_exit(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "deploy",
        FAKE_MIGRATION_TRANSITION="1",
        FAKE_MIGRATION_TRANSITION_POLLS="2",
    )
    assert result.returncode == 0, result.stderr
    calls = docker_log(tmp_path)
    assert any(call.endswith("up -d ledger-migrate") for call in calls)
    assert not any("up -d --wait ledger-migrate" in call for call in calls)
    assert sum(
        "ps -a --format" in call and "ledger-migrate" in call
        for call in calls
    ) >= 2


def test_deploy_fails_closed_when_migration_does_not_exit_before_timeout(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "deploy",
        FAKE_MIGRATION_STAY_RUNNING="1",
        DEPLOY_MIGRATION_ATTEMPTS="2",
        DEPLOY_MIGRATION_INTERVAL="0",
    )
    assert result.returncode == 2
    assert "状态等待超时" in result.stderr
    calls = docker_log(tmp_path)
    assert any(call.endswith("up -d ledger-migrate") for call in calls)
    assert not any("up -d --wait ledger-migrate" in call for call in calls)
    assert not any(" up -d --wait market ledger" in call for call in calls)


def test_deploy_fails_closed_for_migration_state_query_error(tmp_path: Path) -> None:
    result = invoke(tmp_path, "deploy", FAKE_MIGRATION_PS_FAIL="1")
    assert result.returncode == 2
    assert "状态查询失败" in result.stderr
    assert not any(" up -d --wait market ledger" in call for call in docker_log(tmp_path))


def test_deploy_fails_closed_when_migration_up_fails(tmp_path: Path) -> None:
    result = invoke(tmp_path, "deploy", FAKE_MIGRATION_UP_FAIL="1")
    assert result.returncode == 2
    assert "启动失败" in result.stderr
    assert not any(" up -d --wait market ledger" in call for call in docker_log(tmp_path))


def test_deploy_rejects_nonzero_migration_exit_code(tmp_path: Path) -> None:
    result = invoke(tmp_path, "deploy", FAKE_MIGRATION_FAIL="1")
    assert result.returncode == 2
    assert "退出码不是 0" in result.stderr
    assert not any(" up -d --wait market ledger" in call for call in docker_log(tmp_path))


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


def test_start_uses_configured_ghcr_image_without_building(tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "start",
        TRADING_PLATFORM_IMAGE="ghcr.io/example/trading-platform:v1.2.3",
        FAKE_NOTIFICATION_OVERVIEW='{"enabled_connectors":1,"enabled_endpoints":1,"policies":1,"routable_policies":1,"critical_routes_ready":true,"critical_routes":{"risk.halted":true,"system.strategy.unhealthy":true}}',
    )
    assert result.returncode == 0, result.stderr
    calls = docker_log(tmp_path)
    assert any(call.endswith("up -d --wait --no-build --pull never spike") for call in calls)
    assert not any(" build" in call for call in calls)


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


def test_start_image_inspection_does_not_trace_compose_environment(tmp_path: Path) -> None:
    environment = prepare(
        tmp_path,
        "start",
        FAKE_COMPOSE_SECRET="fake-compose-secret",
        FAKE_NOTIFICATION_OVERVIEW=(
            '{"enabled_connectors":1,"enabled_endpoints":1,"policies":1,'
            '"routable_policies":1,"critical_routes_ready":true,'
            '"critical_routes":{"risk.halted":true,"system.strategy.unhealthy":true}}'
        ),
    )
    result = subprocess.run(
        ["bash", "-x", str(tmp_path / "scripts" / "start.sh")],
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
