#!/usr/bin/env bash

set -Eeuo pipefail

usage() {
  printf '用法: %s <release-tag> <output.tar.gz>\n' "$0" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

if (($# != 2)); then
  usage
  exit 2
fi

release_tag="$1"
output_path="$2"
[[ "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "release tag 必须是稳定版 vMAJOR.MINOR.PATCH: $release_tag"
[[ -n "$output_path" ]] || die "必须指定 bundle 输出路径"

for tool in cp mktemp tar; do
  command -v "$tool" >/dev/null 2>&1 || die "缺少必要工具: $tool"
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
bundle_files=(
  compose.yaml
  .env.example
  deploy/compose.release.yaml
  scripts/deploy.sh
  scripts/ops_common.sh
  scripts/start.sh
  scripts/stop.sh
  scripts/verify_ledger_backup_restore.sh
)

for file in "${bundle_files[@]}"; do
  [[ -f "$repo_root/$file" && ! -L "$repo_root/$file" ]] || die "bundle 文件缺失或不是普通文件: $file"
done

output_dir="$(dirname -- "$output_path")"
output_name="$(basename -- "$output_path")"
[[ "$output_name" != . && "$output_name" != / ]] || die "无效 bundle 输出路径: $output_path"
mkdir -p -- "$output_dir"
output_dir="$(cd -- "$output_dir" && pwd)"
output_path="$output_dir/$output_name"
[[ ! -e "$output_path" && ! -L "$output_path" ]] || die "bundle 输出文件已存在: $output_path"

staging_dir="$(mktemp -d)"
trap 'rm -rf -- "$staging_dir"' EXIT
bundle_root="$staging_dir/root"
mkdir -p "$bundle_root/deploy" "$bundle_root/scripts"

for file in "${bundle_files[@]}"; do
  cp -- "$repo_root/$file" "$bundle_root/$file"
done
printf '%s\n' "$release_tag" >"$bundle_root/RELEASE_TAG"

tar -czf "$output_path" -C "$bundle_root" \
  compose.yaml \
  .env.example \
  deploy/compose.release.yaml \
  scripts/deploy.sh \
  scripts/ops_common.sh \
  scripts/start.sh \
  scripts/stop.sh \
  scripts/verify_ledger_backup_restore.sh \
  RELEASE_TAG

[[ -s "$output_path" ]] || die "bundle 生成失败: $output_path"
printf '%s\n' "$output_path"
