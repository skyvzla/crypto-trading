# Binance Futures Testnet 执行 Smoke Runbook

`scripts/binance_testnet_execution_smoke.py` 用于单独验证执行器所依赖的交易所写路径，不等待策略自然产生信号。它只允许 Binance USD-M Futures testnet，默认仅做公开 `exchangeInfo` 读取和订单规则量化。

完整 Spike 进程使用显式 Compose profile 启动，默认 `docker compose up` 不会启动交易进程：

```bash
docker compose --profile spike up --build
```

启动前应先运行本 smoke；账户不是 one-way 或存在旧订单/仓位时不要启动 profile。

## 安全边界

- 必须设置 `BINANCE_TESTNET=true`。
- `BINANCE_BASE_URL` 必须严格为 `https://demo-fapi.binance.com`。
- 默认 dry-run，不读取账户、不提交或撤销订单。
- 真实 testnet 写操作必须同时提供 `--execute` 和确认短语。
- SELL LIMIT 必须至少高于最近 1m close 100 bps，避免测试单立即成为可成交单。
- 账户必须为 one-way position mode；V1 依赖 `reduceOnly`，Hedge Mode 在写入前拒绝。
- 指定 symbol 在测试前必须空仓且无挂单，client ID 必须从未使用。
- 每个入场 `clientOrderId` 只提交一次。提交结果未知时仅用该 ID 查回，绝不重下。
- 撤单无法确认时 fail-closed，报告 `CANCEL_UNKNOWN`，需要人工对账。
- 仅支持单向持仓模式。本轮产生仓位时，撤销测试挂单后使用反向 `reduceOnly MARKET` 清仓；对冲模式直接拒绝。
- 流程中途失败会再次按本轮 client ID 做 best-effort 撤单/清仓；清理状态不明时仍需人工对账，绝不换 ID 重下。
- JSON 报告不包含 API key、secret、签名、请求头或完整账户响应。

## 离线自检

不读取环境变量、不联网：

```bash
uv run python scripts/binance_testnet_execution_smoke.py --self-check
```

预期 `result` 为 `SELF_CHECK_OK`。

## Dry-run

加载 `.env` 后执行。以下命令只读取 testnet 的公开交易规则：

```bash
set -a
source .env
set +a
uv run python scripts/binance_testnet_execution_smoke.py \
  --symbol BTCUSDT \
  --quantity 0.001 \
  --limit-price 100000
```

先检查报告中的 `normalized_entry`，确认价格、数量和交易对符合预期。

## 真实 Testnet Smoke

选择明显高于当前价格的限价，避免意外成交。`clientOrderId` 应为本轮唯一值，发生 `SUBMIT_UNKNOWN` 后严禁换 ID 重下，必须先在 Binance testnet 查清该 ID。

```bash
uv run python scripts/binance_testnet_execution_smoke.py \
  --symbol BTCUSDT \
  --quantity 0.001 \
  --limit-price 100000 \
  --client-order-id tp_smoke_manual_001 \
  --execute \
  --confirm I_UNDERSTAND_TESTNET_ORDERS_ARE_REAL \
  --report reports/testnet_execution_smoke.json
```

执行顺序为：量化意图、提交一次 SELL LIMIT、按 `clientOrderId` 查单、未成交则撤单、检查仓位、存在单向仓位则 `reduceOnly MARKET` 退出、复查仓位归零。

成功结果是 `EXECUTION_OK`。任何 `FAIL_CLOSED` 都不应自动重跑；先依据错误码和 `client_order_id` 完成交易所侧对账。特别是 `SUBMIT_UNKNOWN`、`CANCEL_UNKNOWN`、`POSITION_NOT_FLAT` 必须人工确认订单及仓位后才能开始下一轮。`HEDGE_MODE_UNSUPPORTED` 需要换用专用 one-way testnet 账户，不能用 `positionSide` 绕过。
