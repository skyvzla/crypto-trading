# Quant Trading Platform

通用量化交易平台，当前以逼空插针做空策略为主要开发、测试和验收对象。

当前文档入口：

- `docs/README.md`：文档优先级和阅读入口
- `docs/ARCHITECTURE.md`：三层架构边界
- `docs/PROJECT_IMPLEMENTATION_PLAN.md`：完整实施计划与验收条件
- `docs/PROJECT_GAP_ANALYSIS.md`：已完成、缺失和 P0/P1 问题

Spike replay、testnet 进程和执行/账本闭环已经可运行；盈利持仓退出规则仍是待数据评审的
`candidate-v1`，未冻结为正式策略，不应直接启动正式账户。

## 目录约定

- `src/trading_platform/`：唯一的 Python 源码根目录
- `tests/`：自动化测试
- `scripts/`：回测、部署和验证脚本
- `docs/`：架构、策略和运维文档
- `Dockerfile`：统一应用镜像
- `compose.yaml`：默认服务编排
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
scripts/deploy.sh
```

开发 API：行情层 `http://localhost:8000`，账本层 `http://localhost:8001`。
前端开发服务器监听 `0.0.0.0:5173`，内网设备访问
`http://<宿主机内网IP>:5173/#/backtests`；部署后可直接访问
`http://<宿主机内网IP>:8001/`。`/api` 请求由 Vite 代理到宿主机账本服务。
历史行情数据不随代码仓迁移，回测时通过 Compose 挂载的 `data/` 目录提供。

当前已修复 testnet URL 隔离、行情订阅刷新和 combined stream 解包，并有回归测试。
执行恢复基础（WAL、User Stream 状态同步、未知提交启动/后台恢复、风险阻塞、启动快照对账
及账本回调生命周期）、具体 Spike testnet 进程和真实 Campaign 执行/账本闭环已经实现；
真实 User Stream 主动断流演练已验证断开检测、listenKey 轮换、REST 恢复对账和重新连接，
最终保持 0 挂单、0 仓位。明确业务拒单会记录为 `REJECTED`，与网络结果不明的
`SUBMIT_UNKNOWN` 分离；模糊错误仍保持未知并 fail-closed。

账本迁移 `0003` 增加策略运行状态：Spike 每 5 秒写入心跳，15 秒未更新显示为 `stale`；
`/api/v1/strategy-runtime-status` 和 Web 将账本数据库健康与策略实例状态分开展示。
当前 Compose 真实 PostgreSQL/Redis 全量回归为 `737 passed, 1 skipped, 1 warning`。
外部告警通道、Web 身份权限、
正式 live 阈值以及自然策略信号下的退出仍未完成，`candidate-v1` 继续冻结；
自然策略信号下的保护退出与盈利管理仍需依据具体数据评审，因此不要填入正式账户 API Key。

公开 testnet 行情闭环验收（不需要 API Key，且会拒绝非 testnet 行情服务）：

```bash
docker compose exec -T market python scripts/market_smoke.py e2e
```

Spike replay 示例（使用只读 DuckDB candles 归档）：

```bash
uv run --extra dev python -m trading_platform.backtest.runner \
  --strategy spike --symbols BTCUSDT \
  --start 2026-06-01 --end 2026-06-02 \
  --duckdb-path data/market/candles/candles.duckdb --total-notional 1000
```

已验证的 AKEUSDT 2026 年 7 月只读 DuckDB replay：

```bash
uv run --extra dev python -m trading_platform.backtest.runner \
  --strategy spike --symbols AKEUSDT \
  --start 2026-07-01 --end 2026-08-01 \
  --duckdb-path /data/projects/quant/crypto/data/market/candles/candles.duckdb \
  --output reports/akeusdt_2026_07_replay \
  --total-notional 1000
```

该命令从 `2026-06-30 08:00 UTC` 开始默认 16 小时预热；DuckDB 以只读模式打开。
当前固定结果只有一个 OPEN Campaign，期末未实现 PnL 仅用于核对末价计价，不作为
策略绩效基线。

常用停止命令：

```bash
docker compose -f compose.yaml down
```
