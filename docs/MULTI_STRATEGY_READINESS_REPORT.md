# 多策略配置与上线评估

评估日期：2026-09-15

## 结论

当前代码和隔离测试已经达到“多策略 testnet 上线准备”标准：两个策略以独立 Compose
service 运行，每个策略拥有独立 process、strategy_id、逻辑 account_id、Binance 凭证、
WAL、User Stream、执行 worker 和账本身份。`long_breakout` 是验证这条通用运行路径的
testnet 做多策略。

这不等于已经完成 testnet 业务验收：仍需部署人员配置第二个真实 testnet 子账户，并完成
小额订单 roundtrip、断流和停机演练。真实资金上线不在本次批准范围内。long_breakout
主动限制为 Binance Futures testnet；本次没有配置私有凭证、启动策略容器或发送订单。

| 检查项 | 结论 | 说明 |
| --- | --- | --- |
| 多策略进程隔离 | 通过 | `spike` 与 `long_breakout` 是不同 service/profile |
| 账本身份隔离 | 通过 | `spike_short` 与 `long_breakout` 使用不同 strategy_id/account_id |
| worker 隔离 | 通过 | 每个 service 内有自己的有界优先执行队列和 worker |
| WAL 隔离 | 通过 | 两个 preflight 双向拒绝规范化后相同的订单 WAL 路径 |
| 账户并发保护 | 通过 | PostgreSQL advisory lease 按逻辑 account_id 独占 |
| 下单与信号解耦 | 通过 | 网络执行在独立 worker，入场在入队及执行前二次检查门禁 |
| 重启/断流恢复 | 通过 | 重连回调先关闭 execution gate 和回调屏障，对账与账户事实重建后才恢复 |
| 安全停机 | 通过 | 提交先收敛；超时提交落 SUBMIT_UNKNOWN、查单后重扫并撤销活动 BUY |
| 行情连续性 | 通过（demo 语义） | 只读 Market 已完成 K 线；缺口当轮禁止交易并审计，缺失信号不回放 |
| 做多方向约束 | 通过 | BUY 开仓，SELL reduce-only 平仓；反向意图和 SHORT 仓位 fail-closed |
| 内网访问安全 | 接受风险 | 按当前部署前提不作为本次阻断项 |
| 真实资金策略批准 | 未通过 | 仅 testnet；参数、退出规则和绩效基线仍需后续冻结 |
| 私有 testnet 端到端发单 | 待人工验收 | 没有使用或要求真实凭证，本次不会触发订单 |

## 配置模型

必须遵守以下一一对应关系：

```text
一个策略 = 一个 Compose service/process
         = 一个 strategy_id
         = 一个逻辑 account_id
         = 一个专用 Binance testnet 子账户/凭证
         = 一套独立订单 WAL 和 event WAL
         = 一个独立 execution worker
```

不能让两个策略共享 Binance 账户。PostgreSQL 租约能够阻止相同逻辑 account_id 的并发
占用，但无法从两个不同 API key 自动证明它们是否属于同一个 Binance 子账户；部署时
必须使用两个明确分离的 testnet 子账户，并核对权限和持仓。

建议的账户配置：

| 配置 | Spike | Long breakout |
| --- | --- | --- |
| 逻辑账户 | `SPIKE_ACCOUNT_ID=spike_testnet` | `LONG_BREAKOUT_ACCOUNT_ID=long_breakout_testnet` |
| 凭证 | `BINANCE_API_KEY/SECRET` | `LONG_BREAKOUT_BINANCE_API_KEY/SECRET` |
| WAL | `data/wal/spike_short.jsonl` | `data/wal/long_breakout.jsonl` |
| 默认 symbol | `AKEUSDT` | `BTCUSDT` |

`.env` 中至少需要形成以下分离配置；不要把真实密钥写入仓库：

```dotenv
SPIKE_ACCOUNT_ID=spike_testnet
BINANCE_API_KEY=<spike-testnet-key>
BINANCE_API_SECRET=<spike-testnet-secret>
SPIKE_WAL_PATH=/app/data/wal/spike_short.jsonl

LONG_BREAKOUT_ACCOUNT_ID=long_breakout_testnet
LONG_BREAKOUT_BINANCE_API_KEY=<long-testnet-key>
LONG_BREAKOUT_BINANCE_API_SECRET=<long-testnet-secret>
LONG_BREAKOUT_WAL_PATH=/app/data/wal/long_breakout.jsonl
```

Compose 会把另一个策略的 account ID 和 WAL 路径注入各自 preflight；相同 account ID、
相同路径或包含 `..` 后解析为相同路径都会拒绝启动。代码无法证明两组不同 API key
是否实际属于同一个 Binance 子账户，因此子账户归属仍是部署人工门禁。

long_breakout 的关键参数：

```dotenv
LONG_BREAKOUT_TIMEFRAME=4h
LONG_BREAKOUT_LOOKBACK_BARS=42
# 或 TIMEFRAME=1d、LOOKBACK_BARS=7
LONG_BREAKOUT_ENTRY_NOTIONAL_USDT=10
LONG_BREAKOUT_LEVERAGE=1
LONG_BREAKOUT_ENTRY_ENABLED=false
```

当 `LONG_BREAKOUT_LOOKBACK_BARS` 留空时，运行时自动按 `4h -> 42`、`1d -> 7` 推导；
若显式配置成其他根数，core、settings 和 preflight 都会拒绝。demo 规则使用前一完整
7 日窗口最高价：收盘价严格突破才 BUY 市价入场，默认收盘价亏损 1% SELL
reduce-only 止损、盈利 2% SELL reduce-only 止盈。

## Worker 配置与启停

`spike` 和 `long_breakout` 是两个策略 profile/service。每个 service 内只创建自己的
execution queue、worker、REST client 和 User Stream；PostgreSQL、Ledger、Market 是共享
基础设施，不是共享交易账户。

只启动其中一个策略：

```bash
scripts/start.sh --build spike
# 或
scripts/start.sh --build long_breakout
```

并行运行两个策略时，分别通过同一安全入口启动；第二次调用会保留第一个已运行策略：

```bash
scripts/start.sh --build spike
scripts/start.sh --build long_breakout
docker compose --profile '*' ps spike long_breakout
```

独立观察或停止某个 worker：

```bash
docker compose --profile '*' logs --tail 100 spike
docker compose --profile '*' logs --tail 100 long_breakout
docker compose --profile '*' stop --timeout 120 long_breakout
```

不要用裸 `docker compose up` 绕过 `scripts/start.sh` 的基础服务、通知和重复实例门禁。

## 启用与门禁

先保留 `LONG_BREAKOUT_ENTRY_ENABLED=false` 启动只管理模式。配置好独立 testnet 凭证、
one-way position mode、cross margin 和指定杠杆后，构建但不要直接绕过运维入口：

```bash
scripts/start.sh --build long_breakout
docker compose --profile '*' ps long_breakout
docker compose --profile '*' logs --tail 100 long_breakout
```

运行健康只要求 `lease/execution/market/worker` 四个 operational gates。允许新增仓位还需
同时满足：

- `LONG_BREAKOUT_ENTRY_ENABLED=true`
- Ledger `long_breakout` subcategory 为 enabled
- testnet 账户可交易、余额足够
- symbol 为 cross margin 且实际杠杆与配置一致
- Market testnet、执行流、账户租约和 execution worker 均健康

首次创建 Ledger 准入记录时使用 version 0；建议先创建 disabled 记录，再在观察运行状态
后显式启用：

```bash
curl -X PUT http://127.0.0.1:8001/api/v1/subcategory-admissions/long_breakout \
  -H 'Content-Type: application/json' \
  -d '{"enabled":false,"expected_version":0,"updated_by":"operator","reason":"testnet warmup"}'
```

后续启用必须读取当前 version，并以该 version 作为 `expected_version`，否则 Ledger 返回
409，避免并发覆盖。

## 上线门禁

进入 testnet 观察前还需要人工完成：

1. 为 long_breakout 配置与 Spike 不同的 testnet 子账户和 API key/secret。
2. 确认两个账户均无未知挂单和非预期仓位，且 WAL 文件可写、路径不相同。
3. 确认通知关键路由已就绪，再通过 `scripts/start.sh --build long_breakout` 启动。
4. 保持 entry disabled，观察 runtime status、Market 连续性和 User Stream 重连。
5. 小额启用后完成一次 BUY 入场、SELL reduce-only 退出、账本 Campaign/PnL 对账。
6. 演练断流、重启、未知提交恢复和停机撤销未成交入场。

真实资金上线前仍需另外冻结策略参数、绩效与最大回撤基线、仓位规模、退出规则、告警
责任人和应急平仓预案，并移除当前 testnet-only 限制；这不是本次 demo 的完成条件。

## 验证证据

2026-09-15 最终结果：

- Docker 定向测试：`109 passed, 3 skipped`
- Docker 全量测试：`1729 passed, 5 skipped, 5 subtests passed`，`pytest -n 13`
- `docker compose config --quiet`、long_breakout profile 和全部 profile 渲染通过
- `spike`、`long_breakout` 交付镜像构建通过；两个策略容器均未启动
- `compileall`、`git diff --check` 通过；现有基础 Compose 服务保持健康
- 两轮 SubAgent 架构/代码复审；最终 Sol-medium 复核无阻断问题

仍未执行的外部验证只有需要私有凭证的双 testnet 子账户联调：真实 User Stream 断流、
小额 BUY/SELL reduce-only roundtrip，以及 REST 提交期间停机演练。这些是启动 entry 前的
人工上线门禁，不是代码测试可以替代的项目。
