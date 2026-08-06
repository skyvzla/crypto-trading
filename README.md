# Quant Trading Platform

通用量化交易平台，当前以逼空插针做空策略为主要开发、测试和验收对象。

当前文档入口：

- `docs/README.md`：文档优先级和阅读入口
- `docs/ARCHITECTURE.md`：三层架构边界
- `docs/PROJECT_IMPLEMENTATION_PLAN.md`：完整实施计划与验收条件
- `docs/PROJECT_GAP_ANALYSIS.md`：已完成、缺失和 P0/P1 问题

Spike replay 回测入口已经可运行；测试网执行、持仓退出和账本闭环尚未完成，
不应直接启动正式账户。

## 目录约定

- `src/trading_platform/`：唯一的 Python 源码根目录
- `tests/`：自动化测试
- `scripts/`：回测、部署和验证脚本
- `docs/`：架构、策略和运维文档
- `Dockerfile`：统一应用镜像
- `compose.yaml`：正式服务编排
- `compose.test.yaml`：隔离测试编排

## Docker 工作流

宿主机不需要安装 Python、uv 或项目依赖。测试、回测和正式服务都在容器内执行。

```bash
# 构建应用镜像
docker compose -f compose.test.yaml build

# 执行全量测试
docker compose -f compose.test.yaml run --rm test

# 启动当前开发服务骨架（不得使用正式账户密钥）
cp .env.example .env
docker compose -f compose.yaml up -d --build
docker compose -f compose.yaml ps
```

开发 API：行情层 `http://localhost:8000`，账本层 `http://localhost:8001`。
历史行情数据不随代码仓迁移，回测时通过 Compose 挂载的 `data/` 目录提供。

当前已修复 testnet URL 隔离、行情订阅刷新和 combined stream 解包，并有回归测试。
执行恢复、保护退出和启动对账仍未完成，因此不要填入正式账户 API Key。

Spike replay 示例（历史 Parquet 数据需预先放入 `data/market/`）：

```bash
uv run --extra dev python -m trading_platform.backtest.runner \
  --strategy spike --symbols BTCUSDT \
  --start 2026-06-01 --end 2026-06-02 \
  --data-dir data/market --total-notional 1000
```

常用停止命令：

```bash
docker compose -f compose.yaml down
```
