# Docker 部署

项目的运行边界是 Docker：宿主机只需要 Docker Engine 和 Compose，不直接安装或运行 Python、uv、PostgreSQL、Redis 或项目服务。

## 文件位置

- `Dockerfile`：应用镜像，使用 `src/` 布局安装项目。
- `compose.yaml`：正式服务编排，包括 PostgreSQL、Redis、行情层、账本层和两个策略进程。
- `compose.test.yaml`：隔离测试编排，只构建测试镜像并运行 `pytest`。
- `.env.example`：Compose 环境变量模板。

## 测试

```bash
docker compose -f compose.test.yaml build
docker compose -f compose.test.yaml run --rm test
```

测试镜像会复制 `src/`、`tests/` 和 `scripts/`，使用锁定依赖执行全量测试。宿主机不需要安装 Python 或测试依赖。

## 启动正式服务

```bash
cp .env.example .env
# 按需填写 BINANCE_API_KEY、BINANCE_API_SECRET 等配置
docker compose -f compose.yaml up -d --build
docker compose -f compose.yaml ps
```

Compose 会等待 PostgreSQL、Redis、行情层和账本层健康后再启动依赖它们的服务。API 端口是行情层 `8000` 和账本层 `8001`；数据库和 Redis 只加入内部 Compose 网络。

## Docker 内验证

不要在宿主机直接调用 Python 服务。可以从一次性容器访问正式服务：

```bash
docker compose -f compose.yaml run --rm --no-deps market \
  python -c "import urllib.request; urllib.request.urlopen('http://market:8000/health', timeout=5)"

docker compose -f compose.yaml run --rm --no-deps ledger \
  python -c "import urllib.request; urllib.request.urlopen('http://ledger:8001/api/v1/health', timeout=5)"
```

Redis 和 PostgreSQL 的检查也通过容器执行：

```bash
docker compose -f compose.yaml exec redis redis-cli ping
docker compose -f compose.yaml exec postgres pg_isready -U postgres -d trading_platform
```

## 回测

回测使用测试镜像运行，历史行情目录只读挂载：

```bash
docker compose -f compose.test.yaml run --rm \
  -v "$PWD/data:/app/data:ro" \
  -v "$PWD/reports:/app/reports" \
  test uv run python -m trading_platform.backtest.runner --help
```

## 停止与日志

```bash
docker compose -f compose.yaml logs -f market
docker compose -f compose.yaml ps
docker compose -f compose.yaml down
```

生产环境请使用独立的 `.env`，不要把交易所密钥提交到代码仓库；首次使用建议保持 `BINANCE_TESTNET=true`。
