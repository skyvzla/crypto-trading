#!/usr/bin/env bash

set -Eeuo pipefail

REPOSITORY="${TRADING_PLATFORM_REPOSITORY:-skyvzla/crypto-trading}"
GH_BIN="${TRADING_PLATFORM_GH_BIN:-gh}"
DOCKER_BIN="${TRADING_PLATFORM_DOCKER_BIN:-docker}"
DEPLOY_ROOT="${TRADING_PLATFORM_HOME:-${XDG_DATA_HOME:-${HOME:?HOME is required}/.local/share}/trading-platform}"
RELEASES_DIR="$DEPLOY_ROOT/releases"
RELEASE_TAG="latest"
START_SERVICE=""
STOP_SERVICE=""
tag_given=0

usage() {
  cat <<'EOF'
Usage: deploy-release.sh [latest|vMAJOR.MINOR.PATCH] [--start SERVICE|--stop SERVICE]

Download a source-free deployment bundle and deploy its matching GHCR image.
Default: latest stable GitHub Release. Strategies remain stopped unless --start is given.

Environment:
  TRADING_PLATFORM_HOME                 Persistent deployment/data directory
  TRADING_PLATFORM_REPOSITORY            GitHub owner/repository
  TRADING_PLATFORM_GH_BIN                GitHub CLI executable
  TRADING_PLATFORM_DOCKER_BIN            Docker executable
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

while (($#)); do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --start)
      (($# >= 2)) || die "--start requires a Compose strategy service"
      [[ -z "$START_SERVICE" && -z "$STOP_SERVICE" ]] || die \
        "Choose one of --start or --stop"
      START_SERVICE="$2"
      shift 2
      ;;
    --stop)
      (($# >= 2)) || die "--stop requires a Compose strategy service"
      [[ -z "$START_SERVICE" && -z "$STOP_SERVICE" ]] || die \
        "Choose one of --start or --stop"
      STOP_SERVICE="$2"
      shift 2
      ;;
    --*)
      usage >&2
      die "Unknown option: $1"
      ;;
    *)
      ((tag_given == 0)) || die "Specify at most one release tag"
      RELEASE_TAG="$1"
      tag_given=1
      shift
      ;;
  esac
done

[[ "$RELEASE_TAG" == latest || "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
  die "Release must be 'latest' or a version tag such as v1.2.3"
if [[ -n "$START_SERVICE" && ! "$START_SERVICE" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
  die "Invalid strategy service name"
fi
if [[ -n "$STOP_SERVICE" && ! "$STOP_SERVICE" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
  die "Invalid strategy service name"
fi

run_release_script() (
  local script="$1"
  shift
  cd "$DEPLOY_ROOT"
  export OPS_PROJECT_ROOT="$DEPLOY_ROOT"
  export OPS_ENV_FILE="$DEPLOY_ROOT/.env"
  export TRADING_PLATFORM_PROJECT_ROOT="$DEPLOY_ROOT"
  export TRADING_PLATFORM_ENV_FILE="$DEPLOY_ROOT/.env"
  export TRADING_PLATFORM_RELEASE_COMPOSE_DIR="$release_dir"
  export TRADING_PLATFORM_IMAGE="$image"
  export TRADING_OPS_DOCKER_BIN="$DOCKER_BIN"
  export DEPLOY_DOCKER_BIN="$DOCKER_BIN"
  export START_DOCKER_BIN="$DOCKER_BIN"
  export STOP_DOCKER_BIN="$DOCKER_BIN"
  bash "$release_dir/scripts/$script" "$@"
)

if [[ -n "$STOP_SERVICE" ]]; then
  (( tag_given == 0 )) || die "--stop uses the locally recorded active release; do not specify a tag"
  [[ -z "$START_SERVICE" ]] || die "Choose one of --start or --stop"
  require_command "$DOCKER_BIN"
  require_command stat
  require_command python3
  "$DOCKER_BIN" compose version >/dev/null 2>&1 || die "Docker Compose plugin is unavailable"
  "$DOCKER_BIN" info >/dev/null 2>&1 || die "Docker daemon is unavailable to the current user"
  [[ ! -L "$DEPLOY_ROOT" ]] || die "Deployment directory must not be a symlink: $DEPLOY_ROOT"
  [[ -d "$DEPLOY_ROOT" ]] || die "Deployment directory does not exist: $DEPLOY_ROOT"
  DEPLOY_ROOT="$(cd -- "$DEPLOY_ROOT" && pwd -P)"
  RELEASES_DIR="$DEPLOY_ROOT/releases"
  current_file="$DEPLOY_ROOT/CURRENT_RELEASE"
  [[ -f "$current_file" && ! -L "$current_file" ]] || die \
    "No locally recorded deployment release; cannot safely select stop configuration"
  RELEASE_TAG="$(<"$current_file")"
  [[ "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
    die "Locally recorded release tag is invalid"
  release_dir="$RELEASES_DIR/$RELEASE_TAG"
  [[ -d "$RELEASES_DIR" && ! -L "$RELEASES_DIR" ]] || die \
    "Release directory is unavailable: $RELEASES_DIR"
  [[ -d "$release_dir" && ! -L "$release_dir" ]] || die \
    "Installed release is unavailable: $release_dir"
  [[ -f "$release_dir/RELEASE_TAG" && ! -L "$release_dir/RELEASE_TAG" ]] || die \
    "Installed release marker is unavailable"
  [[ "$(<"$release_dir/RELEASE_TAG")" == "$RELEASE_TAG" ]] || die \
    "Installed release marker does not match CURRENT_RELEASE"
  for entry in compose.yaml deploy/compose.release.yaml scripts/ops_common.sh scripts/stop.sh; do
    [[ -f "$release_dir/$entry" && ! -L "$release_dir/$entry" ]] || die \
      "Installed stop configuration is unavailable: $entry"
  done
  [[ -f "$DEPLOY_ROOT/.env" && ! -L "$DEPLOY_ROOT/.env" ]] || die \
    "Environment file must be a regular file: $DEPLOY_ROOT/.env"
  env_mode="$(stat -c '%a' "$DEPLOY_ROOT/.env" 2>/dev/null)" || die \
    "Could not inspect .env permissions"
  [[ "$env_mode" == 600 ]] || die ".env must have mode 0600"
  image="ghcr.io/${REPOSITORY,,}:$RELEASE_TAG"
  printf 'Stopping strategy service using locally installed release %s\n' "$RELEASE_TAG"
  run_release_script stop.sh "$STOP_SERVICE"
  exit 0
fi

require_command "$GH_BIN"
require_command "$DOCKER_BIN"
require_command tar
require_command sha256sum
require_command python3
require_command curl
require_command stat
require_command awk

"$GH_BIN" auth status --hostname github.com >/dev/null 2>&1 || die \
  "GitHub CLI is not authenticated for github.com; configure its existing read access first"
"$DOCKER_BIN" compose version >/dev/null 2>&1 || die "Docker Compose plugin is unavailable"
"$DOCKER_BIN" info >/dev/null 2>&1 || die "Docker daemon is unavailable to the current user"

if [[ -L "$DEPLOY_ROOT" ]]; then
  die "Deployment directory must not be a symlink: $DEPLOY_ROOT"
fi
mkdir -p "$RELEASES_DIR"
[[ -d "$DEPLOY_ROOT" && -w "$DEPLOY_ROOT" ]] || die \
  "Deployment directory is not writable: $DEPLOY_ROOT"
[[ ! -L "$RELEASES_DIR" ]] || die "Release directory must not be a symlink"
DEPLOY_ROOT="$(cd -- "$DEPLOY_ROOT" && pwd -P)"
RELEASES_DIR="$DEPLOY_ROOT/releases"

if [[ "$RELEASE_TAG" == latest ]]; then
  RELEASE_TAG="$("$GH_BIN" release view --repo "$REPOSITORY" --json tagName --jq .tagName)" || \
    die "Could not resolve the latest GitHub Release for $REPOSITORY"
else
  "$GH_BIN" release view "$RELEASE_TAG" --repo "$REPOSITORY" --json tagName --jq .tagName \
    >/dev/null || die "GitHub Release not found: $RELEASE_TAG"
fi
[[ "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
  die "Resolved release tag is invalid"
[[ ! -L "$DEPLOY_ROOT/CURRENT_RELEASE" ]] || die \
  "Current release marker must not be a symlink"

image_repository="ghcr.io/${REPOSITORY,,}"
image="$image_repository:$RELEASE_TAG"
download_dir="$(mktemp -d "$DEPLOY_ROOT/.download.XXXXXX")"
extract_dir="$(mktemp -d "$RELEASES_DIR/.extract.XXXXXX")"
cleanup() {
  rm -rf -- "$download_dir" "$extract_dir"
}
trap cleanup EXIT

"$GH_BIN" release download "$RELEASE_TAG" \
  --repo "$REPOSITORY" \
  --pattern deploy-bundle.tar.gz \
  --pattern deploy-bundle.tar.gz.sha256 \
  --dir "$download_dir" || die "Could not download deployment bundle for $RELEASE_TAG"
bundle="$download_dir/deploy-bundle.tar.gz"
checksum_file="$download_dir/deploy-bundle.tar.gz.sha256"
[[ -s "$bundle" && -s "$checksum_file" ]] || die "Release bundle or checksum is missing"
bundle_size="$(stat -c '%s' "$bundle")" || die "Could not inspect release bundle size"
(( bundle_size <= 104857600 )) || die "Deployment bundle exceeds the 100 MiB limit"
expected_digest="$(awk 'length($1) == 64 && $1 ~ /^[0-9a-f]+$/ && $2 == "deploy-bundle.tar.gz" { print $1; count++ } END { if (count != 1) exit 1 }' "$checksum_file")" || \
  die "Deployment bundle checksum file is invalid"
actual_digest="$(sha256sum "$bundle" | awk '{print $1}')" || die \
  "Could not calculate deployment bundle checksum"
[[ "$actual_digest" == "$expected_digest" ]] || die \
  "Deployment bundle checksum verification failed"

EXPECTED_RELEASE_TAG="$RELEASE_TAG" python3 - "$bundle" <<'PY' || die \
  "Release bundle contains invalid paths, file types, or version marker"
import os
import sys
import tarfile

expected = {
    "RELEASE_TAG",
    ".env.example",
    "compose.yaml",
    "deploy/compose.release.yaml",
    "scripts/deploy.sh",
    "scripts/ops_common.sh",
    "scripts/start.sh",
    "scripts/stop.sh",
    "scripts/verify_ledger_backup_restore.sh",
}
try:
    with tarfile.open(sys.argv[1], "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or set(names) != expected:
            raise ValueError("bundle file list does not match allowlist")
        if any(not member.isfile() for member in members):
            raise ValueError("bundle may contain regular files only")
        if sum(member.size for member in members) > 262144000:
            raise ValueError("uncompressed bundle exceeds the 250 MiB limit")
        marker = archive.extractfile("RELEASE_TAG")
        if marker is None or marker.read().decode("utf-8").strip() != os.environ[
            "EXPECTED_RELEASE_TAG"
        ]:
            raise ValueError("bundle release tag mismatch")
except (OSError, tarfile.TarError, UnicodeDecodeError, ValueError, KeyError) as error:
    print(error, file=sys.stderr)
    raise SystemExit(1)
PY

tar -xzf "$bundle" --no-same-owner --no-same-permissions -C "$extract_dir"
for entry in RELEASE_TAG .env.example compose.yaml deploy/compose.release.yaml \
  scripts/deploy.sh scripts/ops_common.sh scripts/start.sh scripts/stop.sh \
  scripts/verify_ledger_backup_restore.sh; do
  [[ -f "$extract_dir/$entry" && ! -L "$extract_dir/$entry" ]] || die \
    "Release bundle path is not a regular file: $entry"
done
release_dir="$RELEASES_DIR/$RELEASE_TAG"
[[ ! -L "$release_dir" ]] || die "Release path must not be a symlink: $release_dir"
if [[ -e "$release_dir" ]]; then
  [[ -d "$release_dir" && ! -L "$release_dir" ]] || die \
    "Existing release path is not a real directory: $release_dir"
  [[ -f "$release_dir/.bundle.sha256" && ! -L "$release_dir/.bundle.sha256" ]] || die \
    "Existing release bundle has no integrity marker: $release_dir"
  [[ "$(<"$release_dir/.bundle.sha256")" == "$expected_digest" ]] || die \
    "Release asset changed after installation; refusing to replace $release_dir"
  [[ "$(<"$release_dir/RELEASE_TAG")" == "$RELEASE_TAG" ]] || die \
    "Installed release marker does not match $RELEASE_TAG"
  for entry in RELEASE_TAG .env.example compose.yaml deploy/compose.release.yaml \
    scripts/deploy.sh scripts/ops_common.sh scripts/start.sh scripts/stop.sh \
    scripts/verify_ledger_backup_restore.sh; do
    [[ -f "$release_dir/$entry" && ! -L "$release_dir/$entry" ]] || die \
      "Installed release path is not a regular file: $entry"
  done
else
  mv -- "$extract_dir" "$release_dir"
  extract_dir="$(mktemp -d "$RELEASES_DIR/.extract.XXXXXX")"
  printf '%s\n' "$expected_digest" >"$release_dir/.bundle.sha256"
fi

if [[ ! -e "$DEPLOY_ROOT/.env" && ! -L "$DEPLOY_ROOT/.env" ]]; then
  cp -- "$release_dir/.env.example" "$DEPLOY_ROOT/.env"
  chmod 600 "$DEPLOY_ROOT/.env"
  printf 'Created %s/.env with mode 0600. Set a strong DB_PASSWORD and the intended exchange credentials, then rerun.\n' \
    "$DEPLOY_ROOT"
  exit 2
fi
[[ -f "$DEPLOY_ROOT/.env" && ! -L "$DEPLOY_ROOT/.env" ]] || die \
  "Environment file must be a regular file: $DEPLOY_ROOT/.env"
env_mode="$(stat -c '%a' "$DEPLOY_ROOT/.env" 2>/dev/null)" || die \
  "Could not inspect .env permissions"
[[ "$env_mode" == 600 ]] || die ".env must have mode 0600; run chmod 600 '$DEPLOY_ROOT/.env'"
awk -F= '$1 == "DB_PASSWORD" { password=substr($0, index($0, "=") + 1); found=1 } END { exit !(found && length(password) >= 24) }' \
  "$DEPLOY_ROOT/.env" || die "Set a strong DB_PASSWORD (at least 24 characters) in .env"
if [[ -z "$STOP_SERVICE" ]]; then
  "$DOCKER_BIN" manifest inspect "$image" >/dev/null 2>&1 || die \
    "Cannot read $image from GHCR; check the existing Docker login and package read access"
fi

compose_args=(
  compose
  --project-directory "$DEPLOY_ROOT"
  --env-file "$DEPLOY_ROOT/.env"
  -f "$release_dir/compose.yaml"
  -f "$release_dir/deploy/compose.release.yaml"
  --profile '*'
)
TRADING_PLATFORM_IMAGE="$image" "$DOCKER_BIN" "${compose_args[@]}" config --format json |
  EXPECTED_RELEASE_IMAGE="$image" python3 -c '
import json
import os
import sys

try:
    services = json.load(sys.stdin)["services"]
    names = (
        "market", "ledger-migrate", "ledger", "notification-worker",
        "symbol-sync", "spike", "long_breakout", "strategy_kline", "strategy_tick",
    )
    images = set()
    for name in names:
        service = services[name]
        if "build" in service:
            raise ValueError(f"{name} still has a source build context")
        images.add(service["image"])
    if images != {os.environ["EXPECTED_RELEASE_IMAGE"]}:
        raise ValueError("application services do not share one image")
    print(f"Deployment config validated for {images.pop()}")
except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
    print(f"Invalid release Compose config: {error}", file=sys.stderr)
    raise SystemExit(1)
' || die "Release Compose configuration is invalid"

printf 'Deploying %s from GitHub Release %s\n' "$image" "$RELEASE_TAG"
run_release_script deploy.sh
current_file="$DEPLOY_ROOT/CURRENT_RELEASE"
current_tmp="$DEPLOY_ROOT/.CURRENT_RELEASE.$$"
if [[ -e "$current_file" || -L "$current_file" ]]; then
  [[ -f "$current_file" && ! -L "$current_file" ]] || die \
    "Current release marker must be a regular file"
fi
[[ ! -e "$current_tmp" && ! -L "$current_tmp" ]] || die \
  "Current release marker path is not safe to update"
(umask 077; printf '%s\n' "$RELEASE_TAG" >"$current_tmp") || die \
  "Could not write current release marker"
mv -fT -- "$current_tmp" "$current_file" || die "Could not update current release marker"
if [[ -n "$START_SERVICE" ]]; then
  run_release_script start.sh "$START_SERVICE"
fi
