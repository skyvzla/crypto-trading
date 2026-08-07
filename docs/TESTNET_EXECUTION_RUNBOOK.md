# Binance Futures Testnet 执行 Smoke Runbook

`scripts/binance_testnet_execution_smoke.py` 用于单独验证执行器所依赖的交易所写路径，不等待策略自然产生信号。它只允许 Binance USD-M Futures testnet，默认仅读取公开 `exchangeInfo` 和最新 1m K 线，用于订单规则量化及价格距离校验。

专用 testnet 账户需要人工紧急清仓时，使用
`scripts/binance_testnet_flatten.py`。该命令必须显式列出要处理的 symbol，只接受
`demo-fapi.binance.com`、one-way 账户和固定确认短语；执行时先撤销指定 symbol 的全部挂单，
再次确认没有挂单后重新读取仓位，再提交 reduce-only 市价单并复核为空。它不会处理未列出的
symbol，也不会把撤单前的旧仓位快照当成清仓结果。

完整 Spike 进程使用显式 Compose profile 启动，默认 `docker compose up` 不会启动交易进程：

```bash
docker compose --profile spike up --build
```

启动前应先运行本 smoke；账户不是 one-way 或存在旧订单/仓位时不要启动 profile。

## 安全边界

- 必须设置 `BINANCE_TESTNET=true`。
- 最终解析的 `BINANCE_BASE_URL` 必须严格为 `https://demo-fapi.binance.com`；未显式配置时
  由 `BINANCE_TESTNET=true` 推导，任何生产端点都会拒绝。
- 默认 dry-run，不读取账户、不提交或撤销订单；会读取公开最新 1m close，验证场景价格是否满足要求。
- 真实 testnet 写操作必须同时提供 `--execute` 和确认短语。
- 默认 `cancel-open` 场景的 SELL LIMIT 必须至少高于最近 1m close 100 bps，避免测试单立即成为可成交单。
- `fill-and-exit` 场景会真实建立 testnet 空仓，除通用确认外还必须提供独立的开仓确认短语；该场景的 SELL LIMIT 必须位于最近 1m close 到其下方 20 bps 范围内。
- 账户必须为 one-way position mode；V1 依赖 `reduceOnly`，Hedge Mode 在写入前拒绝。
- 指定 symbol 在测试前必须空仓且无挂单，client ID 必须从未使用。
- 人工 smoke/flatten/reconcile 写入与 Spike 共用 PostgreSQL advisory lock；锁键由
  `--account-id`/`SPIKE_ACCOUNT_ID` 统一配置，默认 `spike_testnet`；
  Spike 或其他 harness 已持锁时返回 `EXECUTION_LEASE_UNAVAILABLE`，不进入写路径。
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
docker compose --profile spike run --rm --no-deps \
  -v "$PWD/reports:/app/reports" spike \
  python scripts/binance_testnet_execution_smoke.py \
  --symbol BTCUSDT \
  --quantity 0.001 \
  --limit-price 100000
```

先检查报告中的 `normalized_entry`，确认价格、数量和交易对符合预期。

## 默认 Testnet Smoke：预挂后撤单

选择明显高于当前价格的限价，避免意外成交。`clientOrderId` 应为本轮唯一值，发生 `SUBMIT_UNKNOWN` 后严禁换 ID 重下，必须先在 Binance testnet 查清该 ID。

```bash
docker compose --profile spike run --rm --no-deps \
  -v "$PWD/reports:/app/reports" spike \
  python scripts/binance_testnet_execution_smoke.py \
  --scenario cancel-open \
  --symbol BTCUSDT \
  --quantity 0.001 \
  --limit-price 100000 \
  --client-order-id tp_smoke_manual_001 \
  --execute \
  --confirm I_UNDERSTAND_TESTNET_ORDERS_ARE_REAL \
  --report reports/testnet_execution_smoke.json
```

执行顺序为：量化意图、提交一次 SELL LIMIT、按 `clientOrderId` 查单、未成交则撤单、检查仓位、存在单向仓位则 `reduceOnly MARKET` 退出、复查仓位归零。

`cancel-open` 是默认场景，命令中的 `--scenario cancel-open` 可以省略。即使远端价格突变导致限价单意外成交，脚本仍会走同一 `reduceOnly MARKET` 清理路径。

## 显式 Testnet Smoke：成交后退出

该场景用于验证完整的入场成交和只减仓退出路径。先读取最新 1m close，再选择不高于该 close、且最多低 20 bps 的 `--limit-price`；脚本仍提交 `SELL LIMIT`，不会把入场替换成市价单。

```bash
uv run python scripts/binance_testnet_execution_smoke.py \
  --scenario fill-and-exit \
  --symbol BTCUSDT \
  --quantity 0.001 \
  --limit-price <latest-close-or-up-to-20bps-below> \
  --client-order-id tp_smoke_fill_001 \
  --execute \
  --confirm I_UNDERSTAND_TESTNET_ORDERS_ARE_REAL \
  --confirm-position I_UNDERSTAND_THIS_OPENS_A_TESTNET_POSITION \
  --report reports/testnet_execution_fill_exit.json
```

成功必须同时确认：入场 LIMIT 状态为 `FILLED`、空仓在账户快照中可见、退出单为反向 `reduceOnly MARKET` 且状态为 `FILLED`、最终仓位归零。入场超时会先撤单再返回 `ENTRY_NOT_FILLED`；成交事实或仓位无法确认时 fail-closed，并执行本轮 client ID 范围内的 best-effort 清理。

成功结果是 `EXECUTION_OK`。任何 `FAIL_CLOSED` 都不应自动重跑；先依据错误码和 `client_order_id` 完成交易所侧对账。最终清理中的 `risk_resolved=false` 表示即使当前暂时空仓，订单事实仍未知，不能开始下一轮。特别是 `SUBMIT_UNKNOWN`、`CANCEL_UNKNOWN`、`POSITION_NOT_FLAT` 必须人工确认订单及仓位后才能继续。`HEDGE_MODE_UNSUPPORTED` 需要换用专用 one-way testnet 账户，不能用 `positionSide` 绕过。

## SUBMIT_UNKNOWN 人工对账

进程轮询默认为 5 秒一次、最多 12 次。超过上限后不重下单，Campaign 和 symbol 风险门禁保持阻塞。先停止同账户其他写入流程，再用以下命令只读查询：

```bash
docker compose --profile spike run --rm --no-deps spike \
  python scripts/binance_testnet_reconcile_wal.py \
  --account-id spike_testnet \
  --wal-path /app/data/wal/spike_short.jsonl
```

只有核对 symbol、clientOrderId 和交易所状态后，才能显式追加 WAL 事实；该操作不会向交易所写入：

```bash
docker compose --profile spike run --rm --no-deps spike \
  python scripts/binance_testnet_reconcile_wal.py \
  --account-id spike_testnet \
  --wal-path /app/data/wal/spike_short.jsonl \
  --execute \
  --confirm I_UNDERSTAND_WAL_RECONCILIATION_WRITES_LOCAL_STATE
```

任一条 `resolved=false` 或 `FAIL_CLOSED` 都不得重跑、改 client ID 重下或手工解除风险门禁；必须保留账户锁定并进行订单、成交和仓位三方对账。

明确的交易所业务拒单记录为不可逆终态 `REJECTED`，保留交易所错误码，不进入上述
`SUBMIT_UNKNOWN` 查单循环，也不得更换 client ID 自动重试。网络超时等无法确认交易所是否
接单的模糊错误仍记录为 `SUBMIT_UNKNOWN`，继续 fail-closed。

## User Stream 主动断流恢复

`scripts/binance_testnet_user_stream_reconnect.py` 用于受控验证真实 testnet User Stream 的
断流门禁与恢复对账。它会占用 `spike_testnet` 的同一 advisory lock，Spike 或其他人工
harness 运行时必须拒绝执行；开始和结束都要求全账户 0 挂单、0 非零仓位。

```bash
docker compose --profile spike run --rm --no-deps \
  -v "$PWD/reports:/app/reports" spike \
  python scripts/binance_testnet_user_stream_reconnect.py \
  --symbol BTCUSDT \
  --confirm I_UNDERSTAND_THIS_DISCONNECTS_THE_TESTNET_USER_STREAM \
  --report reports/testnet_user_stream_reconnect.json
```

成功结果必须为 `RECONNECT_OK`，并同时观察到 disconnect、恢复对账、reconnected 和
listenKey 轮换；最终仍须为 0 挂单、0 非零仓位。该演练会主动关闭 WebSocket，不提交订单，
不能替代外部长时间运行验证。

## Spike 长时间只读监督

先确认 `spike` subcategory 为 disabled，再启动正式 Compose Spike。观察器不启动、不停止、
不重启 Spike，也不提交订单；它固定启动时的 `instance_id`，每 5 秒核对策略心跳、
execution/market/bar_stream 门禁、账本与行情健康、one-way 模式及全账户订单/仓位：

```bash
uv run --env-file .env python scripts/binance_testnet_spike_soak.py \
  --duration-seconds 3900 \
  --sample-seconds 5 \
  --runtime-recovery-seconds 15 \
  --require-flat \
  --expect-entry-enabled false \
  --confirm I_UNDERSTAND_THIS_OBSERVES_THE_TESTNET_ACCOUNT \
  --report reports/testnet_spike_soak.json
```

正式控制面验收建议至少 3900 秒，跨过两次 30 分钟 listenKey keepalive 边界。任一
`instance_id` 变化、`stale/fatal/stopped`、halt、心跳倒退/停止推进、生产模式、Hedge Mode
或控制面 soak 出现挂单/仓位都会返回 `FAIL_CLOSED`。默认严格模式下 runtime degraded 也立即
失败；正式恢复验收可显式设置 `--runtime-recovery-seconds 15`，只允许未 halt 的同一实例在
窗口内恢复，并在报告记录每次恢复耗时，超时仍失败。Compose 自动重启产生的新实例
不能被结束时的健康状态掩盖。自然策略/持仓 soak 不得使用 `--require-flat`，必须单独审批，
本命令不定义策略收益或退出参数。

## Campaign 执行与账本闭环

`scripts/binance_testnet_campaign_roundtrip.py` 不测试策略信号和退出参数，只验证 testnet/live
共用执行链路：PostgreSQL 账户锁、`BinanceOrderExecutor`、WAL、User Stream、PostgreSQL
订单/成交和 Campaign PnL。入场仍为可成交 `SELL LIMIT`，仅退出使用 `BUY MARKET
reduceOnly`。脚本使用 `spike_testnet` 的同一 advisory lock，Spike 正在运行时会拒绝执行。

真实写入前必须确保 Spike 已停止。脚本还会拒绝 Hedge Mode、账户任意非零仓位和目标 symbol
已有挂单：

```bash
docker compose --profile spike run --rm --no-deps \
  -v "$PWD/reports:/app/reports" spike \
  python scripts/binance_testnet_campaign_roundtrip.py \
  --symbol BTCUSDT \
  --quantity 0.001 \
  --execute \
  --confirm I_UNDERSTAND_TESTNET_ORDERS_ARE_REAL \
  --confirm-position I_UNDERSTAND_THIS_OPENS_A_TESTNET_POSITION \
  --report reports/testnet_campaign_roundtrip.json
```

成功结果必须为 `ROUNDTRIP_OK`，`campaign_pnl.pnl_facts_complete=true`、
`remaining_quantity=0`，并且 `final_cleanup.flat=true`、`open_orders=0`。报告中的
`net_realized_pnl` 是扣除 USDT 手续费后的已实现收益，不包含本金。

紧急清仓示例（先 dry-run，再执行）：

```bash
uv run --env-file .env python scripts/binance_testnet_flatten.py --symbols AKEUSDT,BTCUSDT
docker compose --profile spike run --rm --no-deps \
  -v "$PWD/reports:/app/reports" spike \
  python scripts/binance_testnet_flatten.py \
  --symbols AKEUSDT,BTCUSDT \
  --execute \
  --confirm I_UNDERSTAND_TESTNET_EMERGENCY_FLATTEN \
  --report reports/testnet_emergency_flatten.json
```

`OPEN_ORDERS_REMAIN`、`POSITION_NOT_FLAT` 或任意未知异常都表示清仓未完成，必须根据报告和
交易所实际状态人工复核，不得自动重复执行。

## 真实验收记录

2026-08-07 使用现有 Binance Futures testnet 账户完成以下验证：

- 用户授权清理原有 `AKEUSDT SHORT -2791` 和 `BTCUSDT LONG 0.001`，逐笔成交并确认空仓后，
  账户从 Hedge Mode 切换为 one-way；
- `AKEUSDT` 预挂 `SELL LIMIT 1300` 进入 `NEW`，随后撤单为 `CANCELED`，成交量为 0；
- `AKEUSDT` `SELL LIMIT 1300` 成交后出现 `BOTH -1300` 仓位，随后 reduce-only
  `BUY MARKET 1300` 明确为 `FILLED`，最终空仓；
- 人工紧急清仓再次以 AKEUSDT 小仓验证，工具查询退出单到 `FILLED` 后才确认仓位为空；
- 完整 Spike profile 启动时发现并修复两个循环门禁问题：Redis bar 消费者必须先订阅再等待
  Pub/Sub 质量；未完成 Kline 只能证明传输健康，不得写入策略 Kline 存储。修复后 aggTrade、
  1m、5m 均为 healthy，`/quality` 返回 200；
- 人工重启 Spike 后旧 listenKey 正常关闭、新 listenKey 成功连接，市场订阅重新注册并在
  `connection_generation=2` 恢复 ready；
- 受控测试信号通过完整 Spike 进程真实提交三档 AKEUSDT `SELL LIMIT`，数量分别为
  `1316/1750/1310`，三档均收到 REST 与 User Stream `NEW`；优雅停止后均收到
  User Stream `CANCELED`、成交量为 0，Campaign 释放；
- 保留终态 WAL 再次启动，三档没有被重复提交，disabled subcategory 继续阻止新入场；
- 第二轮受控信号真实产生部分成交：e1 数量 1437 依次成交 1200、237，e2 数量 1911
  依次成交 1201、710，两单均经 User Stream `PARTIALLY_FILLED`、TRADE 到 `FILLED`；
  e3 数量 1429 保持 `NEW` 后撤为 `CANCELED`，账户确认空头仓位为 3348；
- 首次停止时，本地 WAL 尚为部分成交而交易所已终态，撤单返回 unknown order，进程按
  fail-closed 退出。实现已补充撤单异常后的 REST 查单：仅明确 `FILLED/CANCELED/EXPIRED`
  才消解，查询失败或非终态继续阻塞；
- 使用紧急清仓工具提交 reduce-only `BUY MARKET 3348`，订单 299557055 明确 `FILLED`，
  随后确认 0 个挂单、0 个非零仓位；
- 覆盖测试注入的 1m Kline 后，在 subcategory version 6 disabled 状态重启；终态 WAL 未重下单，
  空仓事实使 Campaign 释放，最终优雅停止为 `Exited (0)`；
- 验收结束时账户为 one-way、0 个挂单、0 个非零仓位。
- 追加覆盖 `tp_cov_cancel_20260807b`：BTCUSDT `SELL LIMIT 0.001 @ 100000`
  从 `NEW` 到 `CANCELED`，`executedQty=0`；报告为
  `reports/testnet_20260807_cancel_open_b.json`。
- 追加覆盖 `tp_cov_fill_20260807b`：BTCUSDT `SELL LIMIT 0.001 @ 65000`
  成交并形成 `BOTH -0.001`，随后 `BUY MARKET reduceOnly 0.001` 成交；报告为
  `reports/testnet_20260807_fill_exit_b.json`。独立 dry-run 复核报告
  `reports/testnet_20260807_final_flat_b.json` 确认 AKEUSDT/BTCUSDT 均无挂单、无持仓。
- 新版 Spike profile 启动时第一次遇到 Binance 公共 WS TLS 握手重置，进程 fail-closed 关闭
  listenKey 和订阅；Compose 自动重启后第二次连接成功，market `/health`/`/quality`
  ready，`bar1s:AKEUSDT` 有 1 个消费者。
- 同账户第二个 Spike 实例在创建 Redis/Binance 资源前被 PostgreSQL advisory lock 拒绝；
  主实例停止后独立复核 AKEUSDT/BTCUSDT 仍为 0 挂单、0 持仓（`final_flat_c`）。
- Campaign 账本闭环 `spike_short:BTCUSDT:1786108785578` 通过正式执行器提交
  `SELL LIMIT 0.001`，实际卖出均价 `65196.8`；随后通过同一执行器提交 `BUY MARKET
  reduceOnly 0.001`，实际买回均价 `65224.7`。User Stream 写入 2 笔成交，gross PnL
  `-0.02789999 USDT`、手续费 `0.05216860 USDT`、net PnL `-0.08006859 USDT`，最终
  0 挂单、0 仓位。净化报告为 `reports/testnet_campaign_roundtrip_20260807.json`。
- User Stream 主动断流演练观察到 disconnect、恢复对账、reconnected 和 listenKey 轮换，
  最终 0 挂单、0 非零仓位；报告为
  `reports/testnet_user_stream_reconnect_20260807.json`。
- 31 分钟只读监督首次运行在外部网络断流后发现旧、新 WebSocket 生命周期竞态，日志出现
  `NoneType.sock`；修复后旧连接的回调和 task 完成事件不能再清理或重连当前连接。
  修复后的主动断流演练再次返回 `RECONNECT_OK`，最终 0 挂单、0 非零仓位；报告为
  `reports/testnet_user_stream_reconnect_post_race_fix_20260807.json`。
- 修复后的同一 Spike 实例完成 120 秒只读监督，共 23 个样本，最大心跳延迟 4.894 秒，
  execution/market/bar_stream 门禁始终开启，账户始终 0 挂单、0 非零仓位且无瞬时错误；报告为
  `reports/testnet_spike_soak_post_reconnect_fix_20260807.json`。该结果仅为短时回归，不替代
  3900 秒正式长稳验收。
- 3900 秒严格监督运行 1212.575 秒、取得 220 个健康样本后，第二次真实 User Stream
  断流使 runtime 短暂 degraded，observer 按严格模式立即失败；User Stream 随后约 2 秒内
  以同一实例恢复，未再出现旧连接竞态。报告为
  `reports/testnet_spike_soak_3900s_20260807.json`。
- 启用 15 秒有界 runtime 恢复窗口后的正式监督在 348.065 秒后再次失败：Market 公共 WS
  ping timeout，26 秒后才完成第三次重连；缺失的 aggTrade 和 1m Kline 分别形成确定 gap，
  质量按设计粘性降级，策略关闭 market/bar 门禁且账户始终为空。报告为
  `reports/testnet_spike_soak_3900s_recovery_20260807.json`。未实现 REST 回补前不得通过重连
  清除该事实；3900 秒验收仍未通过。
- PostgreSQL 迁移 `0003` 的策略运行状态已通过 Compose 回归：Spike 每 5 秒写入心跳，
  15 秒未更新显示为 `stale`；API/Web 分开呈现账本健康与策略状态。默认数据库已从
  `0002` 升到 `0003`；准入关闭的 Spike testnet 实例实际写入 `running` 且
  `entry_enabled=false`，优雅停止后写入 `stopped`，启停前后均为空仓空单。
- 本轮宿主机全量结果为 `431 passed, 33 skipped, 1 warning`。
- 本轮 Compose 相关组合回归为 `174 passed, 1 warning`；最终全量回归为
  `490 passed, 34 skipped, 1 warning`。

当前 `spike` subcategory 为 disabled。正式 soak 后 Market 再次出现确定 gap，Spike 已优雅
停止，不通过重启清除质量事实；最终 dry-run 确认 AKEUSDT/BTCUSDT 均为 0 挂单、0 非零仓位，
报告为 `reports/testnet_final_flat_after_soak_20260808.json`。启用交易准入前必须先确定并验收
Market 缺口恢复方式，再经过新的人工操作。

AKEUSDT 的 `MIN_NOTIONAL` 为 5 USDT。三档权重为 30/40/30，因此 10 USDT 总金额会形成
3/4/3 USDT 的必然无效订单。进程会在连接交易所、读取 symbol rules 后验证最小档必须严格
高于交易所最小名义金额；当前 Compose testnet 默认使用 20 USDT。

上述结果已证明 REST harness、紧急清仓以及完整策略进程的 User Stream、Campaign、部分成交、
启动恢复和受控主动断流恢复路径；3900 秒外部长时间运行已验证正确 fail-closed，但受 Market
缺口后的恢复方式阻塞。持续未知回报外部处置、外部告警通道、
Web 身份权限、正式 live 阈值和自然策略信号退出仍未验收。`candidate-v1` 保持冻结，
正式退出参数冻结前不得启动 live。
