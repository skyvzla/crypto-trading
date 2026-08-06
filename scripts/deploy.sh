#!/usr/bin/env bash

set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker 未安装或不可用" >&2
  exit 1
fi

mkdir -p data logs

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已从 .env.example 创建 .env，请按需填写交易所凭据后重新执行。"
  exit 0
fi

docker compose config -q
docker compose build
docker compose up -d --wait postgres redis
docker compose exec -T postgres sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < src/trading_platform/ledger/db/schema.sql
docker compose up -d --wait market ledger
docker compose ps
