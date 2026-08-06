# Docker 部署检查清单

## 目录与镜像

- [x] Python 源码位于 `src/trading_platform/`
- [x] `Dockerfile` 位于项目根目录
- [x] 正式服务使用根目录 `compose.yaml`
- [x] 测试使用根目录 `compose.test.yaml`
- [x] PostgreSQL schema 位于 `src/trading_platform/ledger/db/schema.sql`
- [x] 测试镜像包含 `src/`、`tests/` 和 `scripts/`

## 测试

```bash
docker compose -f compose.test.yaml config
docker compose -f compose.test.yaml build
docker compose -f compose.test.yaml run --rm test
```

## 正式服务

```bash
cp .env.example .env
docker compose -f compose.yaml config
docker compose -f compose.yaml up -d --build
docker compose -f compose.yaml ps
```

预期状态：`postgres`、`redis`、`market`、`ledger` 以及两个策略服务均为运行状态；`postgres`、`redis`、`market`、`ledger` 应显示健康状态。

## 服务检查

```bash
docker compose -f compose.yaml exec redis redis-cli ping
docker compose -f compose.yaml exec postgres pg_isready -U postgres -d trading_platform
docker compose -f compose.yaml run --rm --no-deps market \
  python -c "import urllib.request; urllib.request.urlopen('http://market:8000/health', timeout=5)"
docker compose -f compose.yaml run --rm --no-deps ledger \
  python -c "import urllib.request; urllib.request.urlopen('http://ledger:8001/api/v1/health', timeout=5)"
```

## 停止与排查

```bash
docker compose -f compose.yaml logs --tail=100 market ledger
docker compose -f compose.yaml ps
docker compose -f compose.yaml down
```

不要在宿主机直接运行 `python`、`pytest`、`uv run` 或项目服务；所有这些命令都应通过 Compose 容器执行。
