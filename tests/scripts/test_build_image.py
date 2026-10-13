from __future__ import annotations

import os
from pathlib import Path
import subprocess

PROJECT_ROOT = Path(__file__).parents[2]
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_image.sh"


def _write_fake_docker(bin_dir: Path) -> Path:
    docker = bin_dir / "docker"
    docker.write_text(
        r"""#!/usr/bin/env bash
set -eu
log="${FAKE_DOCKER_LOG:?}"
printf '%s\n' "$*" >>"$log"
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o700)
    return docker


def _run_build_image(
    tmp_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _write_fake_docker(bin_dir)
    docker_log = tmp_path / "docker.log"

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_DOCKER_LOG"] = str(docker_log)
    env["DOCKER_BIN"] = str(bin_dir / "docker")
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    calls = []
    if docker_log.exists():
        calls = [line.strip() for line in docker_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return result, calls


def test_build_image_help() -> None:
    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "用法: scripts/build_image.sh [TAG] [--push]" in result.stdout


def test_build_image_defaults_to_latest_without_push(tmp_path: Path) -> None:
    result, calls = _run_build_image(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "构建完成" in result.stdout
    assert len(calls) == 1
    assert calls[0].startswith("build -t qianxiy/crypto:latest")
    assert not any("push" in call for call in calls)


def test_build_image_with_custom_version_tags_both_version_and_latest(tmp_path: Path) -> None:
    result, calls = _run_build_image(tmp_path, "v1.0.0")
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    assert "build -t qianxiy/crypto:v1.0.0 -t qianxiy/crypto:latest" in calls[0]
    assert not any("push" in call for call in calls)


def test_build_image_with_version_and_push(tmp_path: Path) -> None:
    result, calls = _run_build_image(tmp_path, "v1.0.0", "--push")
    assert result.returncode == 0, result.stderr
    assert "镜像推送完成" in result.stdout
    assert len(calls) == 3
    assert "build -t qianxiy/crypto:v1.0.0 -t qianxiy/crypto:latest" in calls[0]
    assert calls[1] == "push qianxiy/crypto:v1.0.0"
    assert calls[2] == "push qianxiy/crypto:latest"


def test_build_image_latest_with_push(tmp_path: Path) -> None:
    result, calls = _run_build_image(tmp_path, "--push")
    assert result.returncode == 0, result.stderr
    assert "镜像推送完成" in result.stdout
    assert len(calls) == 2
    assert "build -t qianxiy/crypto:latest" in calls[0]
    assert calls[1] == "push qianxiy/crypto:latest"


def test_build_image_custom_repository_override(tmp_path: Path) -> None:
    result, calls = _run_build_image(
        tmp_path,
        "v2.1.0",
        "--push",
        extra_env={"TRADING_PLATFORM_IMAGE_REPO": "myregistry/myimage"},
    )
    assert result.returncode == 0, result.stderr
    assert "build -t myregistry/myimage:v2.1.0 -t myregistry/myimage:latest" in calls[0]
    assert calls[1] == "push myregistry/myimage:v2.1.0"
    assert calls[2] == "push myregistry/myimage:latest"


def test_build_image_rejects_invalid_tag(tmp_path: Path) -> None:
    result, calls = _run_build_image(tmp_path, "invalid tag with spaces")
    assert result.returncode == 2
    assert "无效的镜像标签" in result.stderr
    assert len(calls) == 0


def test_build_image_rejects_multiple_tags(tmp_path: Path) -> None:
    result, calls = _run_build_image(tmp_path, "v1.0.0", "v2.0.0")
    assert result.returncode == 2
    assert "只能指定一个 TAG" in result.stderr
    assert len(calls) == 0


def test_build_image_rejects_unknown_option(tmp_path: Path) -> None:
    result, calls = _run_build_image(tmp_path, "--unknown-option")
    assert result.returncode == 2
    assert "未知选项" in result.stderr
    assert len(calls) == 0
