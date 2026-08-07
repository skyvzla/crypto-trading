#!/usr/bin/env bash

set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

cleanup() {
  docker compose -f compose.test.yaml down >/dev/null
}
trap cleanup EXIT

docker compose -f compose.test.yaml build test
docker compose -f compose.test.yaml run --rm test uv run python scripts/verify_imports.py
docker compose -f compose.test.yaml run --rm test \
  uv run pytest tests/shared/execution/test_execution_layer.py -v
