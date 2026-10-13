#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKER_BIN="${DOCKER_BIN:-${TRADING_OPS_DOCKER_BIN:-docker}}"
IMAGE_REPO="${TRADING_PLATFORM_IMAGE_REPO:-qianxiy/crypto}"

usage() {
  cat <<'EOF'
用法: scripts/build_image.sh [TAG] [--push]

便捷构建并发布 Docker 镜像。
默认 TAG 为 latest；如果传入版本号（如 v1.0.0），同时打上该版本标签和 latest 标签。
如果带有 --push 参数，构建完成后推送到 Docker Hub。

示例:
  scripts/build_image.sh                 # 构建 qianxiy/crypto:latest
  scripts/build_image.sh v1.0.0          # 构建 qianxiy/crypto:v1.0.0 和 qianxiy/crypto:latest
  scripts/build_image.sh v1.0.0 --push   # 构建并推送到 Docker Hub
  scripts/build_image.sh --push          # 构建 latest 并推送到 Docker Hub
EOF
}

tag="latest"
push=0
tag_given=0

while (($#)); do
  case "$1" in
    --push)
      push=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      usage >&2
      printf 'ERROR: 未知选项: %s\n' "$1" >&2
      exit 2
      ;;
    *)
      if (( tag_given == 0 )); then
        tag="$1"
        tag_given=1
      else
        usage >&2
        printf 'ERROR: 只能指定一个 TAG，多余参数: %s\n' "$1" >&2
        exit 2
      fi
      ;;
  esac
  shift
done

if [[ ! "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
  printf 'ERROR: 无效的镜像标签: %s\n' "$tag" >&2
  exit 2
fi

command -v "$DOCKER_BIN" >/dev/null 2>&1 || {
  printf 'ERROR: 未找到 Docker 可执行文件: %s\n' "$DOCKER_BIN" >&2
  exit 2
}

[[ -f "$PROJECT_ROOT/Dockerfile" ]] || {
  printf 'ERROR: 未找到 Dockerfile: %s/Dockerfile\n' "$PROJECT_ROOT" >&2
  exit 2
}

primary_image="$IMAGE_REPO:$tag"
build_args=(-t "$primary_image")
tags_to_push=("$primary_image")

if [[ "$tag" != "latest" ]]; then
  latest_image="$IMAGE_REPO:latest"
  build_args+=(-t "$latest_image")
  tags_to_push+=("$latest_image")
fi

printf '==> 开始构建镜像: %s\n' "${build_args[*]}"
"$DOCKER_BIN" build "${build_args[@]}" -f "$PROJECT_ROOT/Dockerfile" "$PROJECT_ROOT"
printf '==> 镜像构建完成: %s\n' "${build_args[*]}"

if (( push == 1 )); then
  for img in "${tags_to_push[@]}"; do
    printf '==> 推送镜像: %s\n' "$img"
    "$DOCKER_BIN" push "$img"
  done
  printf '==> 镜像推送完成！\n'
fi
