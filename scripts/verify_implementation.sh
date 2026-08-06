#!/usr/bin/env bash

set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

docker compose -f compose.test.yaml run --rm test uv run python scripts/verify_imports.py
docker compose -f compose.test.yaml run --rm test uv run pytest tests/test_execution_layer.py -v
