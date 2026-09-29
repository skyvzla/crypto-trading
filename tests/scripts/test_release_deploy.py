from __future__ import annotations

import hashlib
from io import BytesIO
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

PROJECT_ROOT = Path(__file__).parents[2]
DEPLOY_SCRIPT = PROJECT_ROOT / "deploy" / "deploy-release.sh"
BUNDLE_SCRIPT = PROJECT_ROOT / "deploy" / "create-release-bundle.sh"
BUNDLE_ENTRIES = (
    "RELEASE_TAG",
    ".env.example",
    "compose.yaml",
    "deploy/compose.release.yaml",
    "scripts/deploy.sh",
    "scripts/ops_common.sh",
    "scripts/start.sh",
    "scripts/stop.sh",
    "scripts/verify_ledger_backup_restore.sh",
)


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _release_assets(tmp_path: Path, *, extra_entry: str | None = None) -> Path:
    bundle = tmp_path / "deploy-bundle.tar.gz"
    contents = {
        "RELEASE_TAG": "v1.2.3\n",
        ".env.example": "DB_PASSWORD=postgres\n",
        "compose.yaml": "services: {}\n",
        "deploy/compose.release.yaml": "services: {}\n",
        "scripts/deploy.sh": (
            "#!/usr/bin/env bash\n"
            "printf 'DEPLOY_OK:%s:%s:%s\\n' \"$TRADING_PLATFORM_IMAGE\" "
            "\"$OPS_PROJECT_ROOT\" \"$TRADING_PLATFORM_RELEASE_COMPOSE_DIR\"\n"
        ),
        "scripts/ops_common.sh": "#!/usr/bin/env bash\n",
        "scripts/start.sh": "#!/usr/bin/env bash\nprintf 'START_OK:%s\\n' \"$1\"\n",
        "scripts/stop.sh": "#!/usr/bin/env bash\nprintf 'STOP_OK:%s\\n' \"$1\"\n",
        "scripts/verify_ledger_backup_restore.sh": "#!/usr/bin/env bash\n",
    }
    with tarfile.open(bundle, "w:gz") as archive:
        for name, content in contents.items():
            encoded = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(encoded)
            info.mode = 0o700 if name.startswith("scripts/") else 0o600
            archive.addfile(info, BytesIO(encoded))
        if extra_entry is not None:
            encoded = b"unexpected"
            info = tarfile.TarInfo(extra_entry)
            info.size = len(encoded)
            archive.addfile(info, BytesIO(encoded))
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    (tmp_path / "deploy-bundle.tar.gz.sha256").write_text(
        f"{digest}  deploy-bundle.tar.gz\n", encoding="utf-8"
    )
    return bundle


FAKE_GH = r'''#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >>"${FAKE_GH_LOG:?}"
case "$1 ${2:-}" in
  "auth status")
    [[ "${FAKE_GH_AUTH_FAIL:-0}" == 0 ]] && exit 0
    exit 1
    ;;
  "release view")
    if [[ "${3:-}" == "--repo" ]]; then
      printf '%s\n' "${FAKE_RELEASE_TAG:-v1.2.3}"
    fi
    exit 0
    ;;
  "release download")
    release_tag="$3"
    [[ "$release_tag" == "${FAKE_RELEASE_TAG:-v1.2.3}" ]]
    while (($#)); do
      if [[ "$1" == --dir ]]; then destination="$2"; shift 2; else shift; fi
    done
    cp "$FAKE_RELEASE_ASSETS/deploy-bundle.tar.gz" "$destination/"
    cp "$FAKE_RELEASE_ASSETS/deploy-bundle.tar.gz.sha256" "$destination/"
    exit 0
    ;;
esac
exit 1
'''


FAKE_DOCKER = r'''#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >>"${FAKE_DOCKER_LOG:?}"
if [[ "$1 ${2:-} ${3:-}" == "compose version " ]]; then
  printf 'Docker Compose version v2.40.0\n'
  exit 0
fi
if [[ "$1 ${2:-}" == "info" ]]; then exit 0; fi
if [[ "$1 ${2:-}" == "manifest inspect" ]]; then exit 0; fi
if [[ "$*" == *"config --format json"* ]]; then
  image="${TRADING_PLATFORM_IMAGE:?}"
  printf '{"name":"%s","services":{' "${FAKE_COMPOSE_PROJECT_NAME:-trading_platform}"
  separator=""
  for service in market ledger-migrate ledger notification-worker symbol-sync spike long_breakout strategy_kline strategy_tick; do
    if [[ "$service" == spike || "$service" == long_breakout || "$service" == strategy_kline || "$service" == strategy_tick ]]; then
      printf '%s"%s":{"image":"%s","labels":{"trading-platform.role":"strategy"}}' "$separator" "$service" "$image"
    else
      printf '%s"%s":{"image":"%s"}' "$separator" "$service" "$image"
    fi
    separator=,
  done
  printf '}}\n'
  exit 0
fi
exit 0
'''

FAKE_CURL = "#!/usr/bin/env bash\nexit 0\n"


def _run_bootstrap(
    tmp_path: Path,
    *args: str,
    release_tag: str = "v1.2.3",
    extra_entry: str | None = None,
    env_file: bool = True,
    env_symlink: bool = False,
    home_from_script: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    assets = tmp_path / "assets"
    assets.mkdir()
    _release_assets(assets, extra_entry=extra_entry)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    docker = fake_bin / "docker"
    curl = fake_bin / "curl"
    _write_executable(gh, FAKE_GH)
    _write_executable(docker, FAKE_DOCKER)
    _write_executable(curl, FAKE_CURL)
    deploy_root = tmp_path / "instance"
    deploy_root.mkdir()
    if env_file:
        env_path = deploy_root / ".env"
        env_path.write_text(f"DB_PASSWORD={'d' * 32}\n", encoding="utf-8")
        env_path.chmod(0o600)
    elif env_symlink:
        target_env = tmp_path / "outside.env"
        target_env.write_text(f"DB_PASSWORD={'d' * 32}\n", encoding="utf-8")
        target_env.chmod(0o600)
        (deploy_root / ".env").symlink_to(target_env)
    environment = os.environ.copy()
    environment.update(
        {
            "TRADING_PLATFORM_GH_BIN": str(gh),
            "TRADING_PLATFORM_DOCKER_BIN": str(docker),
            "TRADING_PLATFORM_HOME": str(deploy_root),
            "TRADING_PLATFORM_REPOSITORY": "example/trading-platform",
            "FAKE_RELEASE_TAG": release_tag,
            "FAKE_RELEASE_ASSETS": str(assets),
            "FAKE_GH_LOG": str(tmp_path / "gh.log"),
            "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )
    script_path = DEPLOY_SCRIPT
    if home_from_script:
        script_path = deploy_root / "deploy-release.sh"
        shutil.copy2(DEPLOY_SCRIPT, script_path)
        script_path.chmod(0o700)
        environment.pop("TRADING_PLATFORM_HOME", None)
    environment.update(extra_env or {})
    return subprocess.run(
        ["bash", str(script_path), *args],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
    )


def test_deploy_release_script_has_valid_shell_and_help() -> None:
    syntax = subprocess.run(
        ["bash", "-n", str(DEPLOY_SCRIPT)], capture_output=True, text=True
    )
    assert syntax.returncode == 0, syntax.stderr
    help_result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--help"], capture_output=True, text=True
    )
    assert help_result.returncode == 0
    assert "latest|vMAJOR.MINOR.PATCH" in help_result.stdout


def test_release_bundle_rejects_prerelease_tags(tmp_path: Path) -> None:
    output = tmp_path / "deploy-bundle.tar.gz"
    result = subprocess.run(
        ["bash", str(BUNDLE_SCRIPT), "v1.2.3-rc.1", str(output)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert not output.exists()


def test_deploy_release_uses_latest_and_runs_source_free_bundle(tmp_path: Path) -> None:
    result = _run_bootstrap(tmp_path, "latest", "--start", "spike")
    assert result.returncode == 0, result.stderr
    assert "DEPLOY_OK:ghcr.io/example/trading-platform:v1.2.3" in result.stdout
    assert f"{tmp_path}/instance" in result.stdout
    assert "START_OK:spike" in result.stdout
    gh_calls = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "release view --repo example/trading-platform" in gh_calls
    assert "release download v1.2.3" in gh_calls
    docker_calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "compose version" in docker_calls
    assert "manifest inspect ghcr.io/example/trading-platform:v1.2.3" in docker_calls
    installed = tmp_path / "instance" / "releases" / "v1.2.3"
    assert (installed / "compose.yaml").is_file()
    assert (tmp_path / "instance" / "CURRENT_RELEASE").read_text(encoding="utf-8").strip() == "v1.2.3"
    assert not (installed / "src").exists()
    assert not (installed / "Dockerfile").exists()


def test_deploy_release_rejects_a_different_resolved_project_name(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        extra_env={"FAKE_COMPOSE_PROJECT_NAME": "alternate_project"},
    )
    assert result.returncode == 2
    assert "Release Compose configuration is invalid" in result.stderr
    docker_calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "config --format json" in docker_calls
    assert " up " not in docker_calls


def test_deploy_release_defaults_to_the_script_directory(tmp_path: Path) -> None:
    result = _run_bootstrap(tmp_path, home_from_script=True)
    assert result.returncode == 0, result.stderr
    expected_root = tmp_path / "instance"
    assert "DEPLOY_OK:ghcr.io/example/trading-platform:v1.2.3" in result.stdout
    assert str(expected_root) in result.stdout
    assert (expected_root / "CURRENT_RELEASE").read_text(encoding="utf-8").strip() == "v1.2.3"


def test_deploy_release_accepts_an_explicit_tag(tmp_path: Path) -> None:
    result = _run_bootstrap(tmp_path, "v1.2.3")
    assert result.returncode == 0, result.stderr
    gh_calls = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "release view v1.2.3 --repo example/trading-platform" in gh_calls


def test_deploy_release_rejects_prerelease_tag(tmp_path: Path) -> None:
    result = _run_bootstrap(tmp_path, "v1.2.3-rc.1")
    assert result.returncode == 2
    assert "version tag" in result.stderr
    assert not (tmp_path / "gh.log").exists()


def test_deploy_release_can_stop_a_strategy_without_deploying(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    _release_assets(assets)
    deploy_root = tmp_path / "instance"
    release_dir = deploy_root / "releases" / "v1.2.3"
    release_dir.mkdir(parents=True)
    with tarfile.open(assets / "deploy-bundle.tar.gz", "r:gz") as archive:
        archive.extractall(release_dir, filter="data")
    (release_dir / ".bundle.sha256").write_text("stored-digest\n", encoding="utf-8")
    (deploy_root / "CURRENT_RELEASE").write_text("v1.2.3\n", encoding="utf-8")
    env_path = deploy_root / ".env"
    env_path.write_text(f"DB_PASSWORD={'d' * 32}\n", encoding="utf-8")
    env_path.chmod(0o600)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    docker = fake_bin / "docker"
    curl = fake_bin / "curl"
    _write_executable(gh, FAKE_GH)
    _write_executable(docker, FAKE_DOCKER)
    _write_executable(curl, FAKE_CURL)
    environment = os.environ.copy()
    environment.update(
        {
            "TRADING_PLATFORM_GH_BIN": str(gh),
            "TRADING_PLATFORM_DOCKER_BIN": str(docker),
            "TRADING_PLATFORM_HOME": str(deploy_root),
            "TRADING_PLATFORM_REPOSITORY": "example/trading-platform",
            "FAKE_GH_AUTH_FAIL": "1",
            "FAKE_GH_LOG": str(tmp_path / "gh.log"),
            "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        }
    )
    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--stop", "spike"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "STOP_OK:spike" in result.stdout
    assert "DEPLOY_OK" not in result.stdout
    docker_calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "manifest inspect" not in docker_calls
    assert not (tmp_path / "gh.log").exists()


def test_deploy_release_creates_private_env_template_and_stops(tmp_path: Path) -> None:
    result = _run_bootstrap(tmp_path, env_file=False)
    assert result.returncode == 2
    env_path = tmp_path / "instance" / ".env"
    assert env_path.is_file()
    assert env_path.stat().st_mode & 0o777 == 0o600
    assert "then rerun" in result.stdout
    assert "DEPLOY_OK" not in result.stdout


def test_deploy_release_refuses_env_symlink(tmp_path: Path) -> None:
    result = _run_bootstrap(tmp_path, env_file=False, env_symlink=True)
    assert result.returncode == 2
    assert "regular file" in result.stderr
    assert (tmp_path / "outside.env").read_text(encoding="utf-8").startswith(
        "DB_PASSWORD="
    )


def test_deploy_release_rejects_bundle_paths_outside_allowlist(tmp_path: Path) -> None:
    result = _run_bootstrap(tmp_path, extra_entry="src/trading_platform/__init__.py")
    assert result.returncode == 2
    assert "allowlist" in result.stderr
    assert "DEPLOY_OK" not in result.stdout


def test_deploy_release_detects_missing_github_cli_auth(tmp_path: Path) -> None:
    result = _run_bootstrap(tmp_path, extra_env={"FAKE_GH_AUTH_FAIL": "1"})
    assert result.returncode == 2
    assert "not authenticated" in result.stderr
    assert "DEPLOY_OK" not in result.stdout
