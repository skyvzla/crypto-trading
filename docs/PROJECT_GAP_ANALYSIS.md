# 项目功能盘点与实施前决策

> 盘点日期：2026-08-07
> 盘点依据：当前源码、容器测试、`docs/spike_trader/decisions.md` 与已归档状态快照
> 状态：实施中；testnet 完整成交链路已验收，策略依 D-028 冻结并等待逐笔数据评审

## 1. 当前结论

项目已形成“历史数据预热 -> Spike 信号 -> 三档订单 -> 成交/持仓 -> 报告”的
可运行 replay 入场链路；账本查询与最小 Web 控制闭环已经可用。Spike 的 testnet/live
进程已装配行情预热、User Stream、WAL、PostgreSQL、Redis Campaign、subcategory、TTL
撤单、启动对账和周期安全扫描。candidate-v1 可在 replay/testnet 作为候选执行验证，
D-007 保留为简单执行测试；D-028 的逐笔数据评审和后续人工决策完成前，
`live` 会拒绝启动。

当前宿主机全量为 `431 passed, 33 skipped, 1 warning`；Compose 真实 PostgreSQL/Redis
全量为 `460 passed, 1 skipped, 1 warning`。测试已按
`backtest/market/strategies/shared/ledger/integration/research/scripts` 归档，并覆盖 Spike 两个 replay CLI、16 小时预热、
正向信号至三档成交、全局交易准入、必需数据集缺失拒绝、期末未平仓标记、testnet URL
切换、combined stream 解包、自动重连、订阅刷新、多 Bar 发布、真实 Redis 分发、真实
PostgreSQL CRUD/API/PnL/subcategory 审计及 Web 静态资源。外部 smoke 已在 Binance Futures
testnet 真实接收完成 1s Bar 与新完成 1m Kline。`demo-fapi` 真实鉴权已成功；执行 smoke
首次提交前曾发现账户为 Hedge Mode 并 fail-closed。经用户明确授权后，旧 AKEUSDT SHORT
和 BTCUSDT LONG 已平仓，账户切换为 one-way；AKEUSDT 已真实完成预挂撤单、限价成交、
reduce-only 退出和紧急清仓。完整 Spike 进程已真实覆盖三档预挂、部分成交、全部成交、
User Stream `TRADE`、账户仓位确认、剩余档撤单、外部 reduce-only 清仓及空仓重启恢复。
空仓启动与人工重启也已通过：User Stream listenKey 正常关闭/重建，市场订阅卸载/恢复后
1s/1m/5m 三个流重新 ready。独立 User Stream harness 又通过了主动关闭 WebSocket 的真实
断流演练：观察到 disconnect、恢复对账、重新连接和 listenKey 轮换，最终 0 挂单、0 仓位；
报告为 `reports/testnet_user_stream_reconnect_20260807.json`。该结果不替代外部长时间运行验证。

外部 DuckDB 历史源已完成时间一致性审计。固定范围为 AKEUSDT、UTC `2026-07-01` 至
`2026-08-01`、从 `2026-06-30 08:00 UTC` 开始 16 小时预热、DuckDB 只读源、
总名义 `1000 USDT`。审计发现 1s 序列相对 Kline 整体早 8 小时；旧报告混合了不同时刻
的信号指标与执行价格，已全部归档为无效。runner 现支持显式 1s 时间修正并写入
`run_meta.json`；未带 `bar1s_time_shift_ms: 28800000` 的旧 AKE 报告不得引用。
candidate-v1 已接 replay/testnet 共用策略和 Redis/WAL 恢复，但旧阈值来源同步失效。
当前仅作为固定数据观察基线，暂停重新标定、walk-forward 和收益寻优；先与用户
共同审阅逐笔交易事实，live 仍无条件拒绝。

当前文档优先级：

1. `docs/spike_trader/decisions.md`
2. `docs/ARCHITECTURE.md`
3. `docs/PROJECT_IMPLEMENTATION_PLAN.md`
4. `docs/PROJECT_GAP_ANALYSIS.md`

旧完成总结、策略规格、执行协议、模块设计、阶段计划、迁移报告和历史架构文档已经移动到
`docs/archive/`，只能作为历史记录。

## 2. 已实现并可保留的基础能力

| 区域 | 当前能力 | 当前判断 |
|---|---|---|
| 工程结构 | `src/`、`tests/`、Dockerfile、Compose、pytest | 已建立 |
| 行情客户端 | Binance WebSocket 客户端、aggTrade/Kline 解析器 | testnet 短时外部 smoke 已通过，待长时间运行验证 |
| 行情聚合 | aggTrade 到 `Bar1s` 的内存聚合器 | 已实现基础逻辑 |
| 行情分发 | Redis Pub/Sub、Kline latest Hash | 已通过真实 Redis 服务级集成 |
| 订阅管理 | consumer 声明式订阅、引用计数、instance epoch | 刷新、断线重连和完整进程重启恢复已验证，待外部长时间验证 |
| 执行客户端 | Binance REST、签名、限速、User Data Stream、规则量化、WAL | live/test 进程已接入；终态 WAL 补账、回调 fatal/排空、账户单实例和外部订单拒绝已覆盖 |
| 账本 | PostgreSQL 订单/成交/持仓、PnL、subcategory/策略审计和 FastAPI 查询 | 已通过真实 PostgreSQL 服务级集成；Campaign 生命周期可按审计事件查询 |
| 风控 | 总持仓价值、币种数量、杠杆上限、未知订单币种阻塞 | 仅最小基础能力 |
| 回测 | UTC 虚拟时钟、16h 预热、部分成交、交易所量化、持仓、费用和策略审计报告 | AKEUSDT 已发现并显式修正 1s `+8h`；对齐后的退出与绩效口径仍未冻结 |
| Web | 运行状态、订单、成交、持仓、PnL、subcategory 控制 | V1 已实现，身份权限待确认 |
| 实验基线 | 100 标的历史研究脚本和报告 | 可作事实对照，不能作生产结论 |

## 3. P0 修复记录

### 3.1 测试网端点隔离（已修复）

- `BINANCE_TESTNET=true` 会自动选择可鉴权的 `demo-fapi` REST 与 testnet WS URL。
- 行情服务使用统一 `BinanceConfig`，不再硬编码生产 WS URL。
- Compose 默认 testnet，不再用生产 URL 环境默认值抵消配置切换。

正式账户仍受 Phase 3-6 的执行恢复和人工门禁约束。

### 3.2 行情订阅刷新（已修复）

- 删除重复路径注册，PUT/DELETE 由唯一 handler 更新状态并刷新 WebSocket。
- FastAPI 回归测试验证 PUT 会调用 `refresh_ws_streams()`。

### 3.3 Combined Stream 消息解包（已修复）

- WebSocket 客户端兼容 raw 与 `{ "stream": ..., "data": ... }` 两种消息。
- 单元测试验证 combined stream 会向下游交付真实事件 payload。

### 3.4 Spike 策略与账户接口已解耦（2026-08-07）

**已修复**：
- `DynamicSpikeBacktestStrategy` 现在实现 `on_bar1s(bar)` / `on_kline(kline)` 协议
- 删除了旧的 `on_event` 包装器
- 策略通过最小 `StrategyAccount` 协议查询订单、持仓和撤单
- `BacktestEngine` 实现该协议，旧 `bind_engine()` 仅保留第三方兼容入口

**已补齐的实时适配**：
- `BinanceStrategyAccount` 将 WAL、订单回报和仓位快照投影为同步 `StrategyAccount`；
- `spike-live` 进程已装配历史 Kline 预热、实时 1m/5m/15m 行情、User Stream、账本、Campaign 和准入；
- 启动强制专用账户、one-way 模式、交易所规则快照和订单/仓位一致性；关机先撤销未终态入场单；
- 本地与 Compose 已覆盖部分成交、撤单竞态、重连顺序、迟到回报、成交后仓位确认和全局 halt；
  one-way testnet 已验证完整策略进程成交和保护退出。User Stream 断流会立即关闭执行门禁，
  只有重连对账完成才恢复；带仓重启从 Redis/WAL/PostgreSQL 恢复 timing、手续费和成交幂等。
  Candidate 退出前会按 Campaign 撤净全部入场单并刷新交易所仓位；未知入场单继续阻挡退出，
  已终态但仍有残仓的退出允许补清，candidate 与轮换不会并发生成两张退出单。
  首次停机发现“交易所已成交但本地 WAL
  尚为部分成交”的撤单竞态，现会在撤单异常后按原 client ID 查询交易所，明确终态才消解。
- 订单 TTL 使用首次 intent 不可变时间；终态 WAL 在 PostgreSQL 订单、成交和仓位
  全部补齐后才记账本 ack。User Stream 回调异常不再被吞掉，关机会有界排空。
- 同一账户只允许一个执行进程持有 PostgreSQL advisory lock；专用账户出现未归属
  订单或非托管 symbol 仓位回报时立即 fatal，不写入 Spike 账本。
- 策略审计已写入 PostgreSQL 并提供 `/api/v1/strategy-audit-events`；本地 Redis 1s Bar
  任一托管 symbol 超过 10 秒静默时关闭新入场。
- 明确的交易所业务拒单写入不可逆终态 `REJECTED`，保留交易所错误码，不会被恢复流程误判为
  `SUBMIT_UNKNOWN` 或自动更换 client ID 重试；网络超时等模糊结果仍保持
  `SUBMIT_UNKNOWN` 并 fail-closed。
- PostgreSQL 迁移 `0003` 增加策略运行状态；Spike 每 5 秒写入实例心跳、门禁和 halt 事实，
  15 秒未更新时 API 显示 `stale`。`/api/v1/strategy-runtime-status` 与 Web 将账本数据库健康
  和策略是否运行分开展示，无运行记录不会显示为正常。

### 3.5 订单幂等与失效撤单（Phase 2 修复）

**订单幂等问题**：
- 策略使用 `client_order_id` 标记已下单
- 新代码已在 `SpikeSignal.placed_client_order_ids` 中维护幂等集合
- WAL 已接入 Binance REST 提交适配器：提交前记录意图；明确业务拒绝记录为 `REJECTED`，
  网络状态不明时保持 `SUBMIT_UNKNOWN`。两者不会互相误分类，重复 client ID 不重下单；
  启动恢复及后台轮询会持续阻塞对应 symbol，直到全部未知订单解析

**失效撤单已实现**：
- 新代码通过 `_cancel_signal_orders()` 正确撤销未成交订单
- 通过账户抽象调用 `cancel_order()`，回测模式立即生效

**仍缺失**（Phase 3 范围）：
- User Stream 主动断流重连已通过真实 testnet 演练；仍缺外部长时间运行与持续未知回报处置演练
- 定义 `SUBMIT_UNKNOWN` 达到 12 次上限后的人工处置流程

### 3.6 数据质量检查已加强（2026-08-06）

**已实现**：
- 5 秒窗口连续性检查：`current.timestamp - bar_5s_ago.timestamp == 5000ms`
- 60 秒窗口连续性检查：`current.timestamp - bar_60s_ago.timestamp == 60000ms`
- 缺口或断线时不触发信号

**已补齐**（2026-08-07）：
- Redis 发布器记录每个已发布通道的订阅者数量、零订阅次数和最近发布时间；零订阅者时记录告警，恢复时记录恢复日志。
- `/health`、`/quality` 暴露 Pub/Sub 交付状态；存在活跃行情流且最近一次发布无消费者时 fail-closed（503），避免下游误认为行情已送达。

**已补齐**（2026-08-07）：
- `/health` 和 `/quality` 暴露逐流质量、连接代次和降级原因
- WebSocket 重连后先进入 `awaiting_data`，aggTrade/Kline 缺口、重复和乱序会粘性降级
- 降级期间停止继续发布不完整行情；未实现未经确认的 REST 回补或对账
- Kline/Tick 实时策略基类消费 `/quality`，质量未知或降级时不处理旧快照和 Pub/Sub Bar

**已修复**（2026-08-06）：新代码中的 `range(1,4)` bug 已改为 `range(3)`，与实验脚本对齐。

三档价格公式（已冻结，来自实验脚本）：
```python
tier_prices = [spike_high - atr * (0.75 - (n - 1) * 0.40) for n in range(3)]
# n=0: spike_high - 1.15*atr  （第一档，最低价，深度回调）
# n=1: spike_high - 0.75*atr  （第二档，主力，40% 权重）
# n=2: spike_high - 0.35*atr  （第三档，最高价，浅回调优先）
```

此前文档中出现的"0.75、1.15、1.55"无出处，属笔误，已更正。

## 4. P1：闭环缺失能力（Phase 2-5 范围）

| 区域 | 缺失能力 | 目标阶段 |
|---|---|---|
| **策略核心** | ✅ `StrategyAccount` 已解耦；⏳ Clock 与实时账户适配 | Phase 2/3 |
| **Campaign** | ✅ 全局入场互斥、第一笔成交计时、D-009 盈利轮换、Redis 原子租约、成交后仓位确认门禁、真实成交后恢复及空仓释放 | Phase 2 |
| **入场订单（已部分实现）** | ✅ 固定总名义金额、三档幂等、部分成交/撤单竞态/迟到回报自动化及完整进程外部验收 | Phase 2/3 |
| **执行恢复** | ✅ WAL/REST/User Stream/未知单恢复/`REJECTED` 分流/订单仓位启动门禁/规则量化/主动断流重连/重连失败清理/持续未知 fatal/外部部分成交和保护退出；⏳外部长时间运行 | Phase 3 |
| **持仓退出** | ✅ candidate-v1 状态机、特征、origin 减半、趋势/动能/时间退出及 Redis/WAL 恢复；⏸ 依 D-028 暂停调参，先逐笔数据评审；live 禁止启动 | Phase 2/3 |
| **风控** | ✅ 总持仓/币种数/杠杆/未知订单阻塞/全局 halt；⏳保证金、日亏损、数据延迟 | Phase 3 |
| **监听池** | ✅ 5 分钟扫描编排及 subcategory 同节拍刷新；⏳ 真实扫描器、监听租约、保护性监听 | Phase 1/4 |
| **回测可信度** | ✅ 无未来数据/预热/缺失拒绝/窗口缺口/部分成交/显式 1s 时间修正；⏳ 滑点、同秒顺序及多数据源一致性门禁 | Phase 2 |
| **审计** | ✅ 信号/计划/失效/首成交/退出及订单/成交/持仓；✅ Campaign 身份显式贯穿 WAL/账本并提供实际买卖均价、手续费和净 PnL API | Phase 2/4 |
| **Web** | ✅ subcategory 控制已接具体进程、fail-closed 刷新与关闭撤单、账本、PnL、策略运行状态；账本健康与策略状态已分离；⏳ 身份权限 | Phase 4 |
| **运维** | ✅ testnet/live 隔离、专用账户紧急清仓脚本、User Stream 主动断流演练和运行手册；⏳外部告警通道、凭据、回滚及长时间运行 | Phase 5-6 |

## 5. 测试现状与缺口

已验证：

- 宿主机 `uv run pytest -q`：`431 passed, 33 skipped, 1 warning`
- Compose 真实 PostgreSQL/Redis：`460 passed, 1 skipped, 1 warning`
- testnet harness 自动化覆盖预挂后撤单、意外/部分成交后的只减仓清理、显式成交后 reduce-only 退出、仓位快照延迟和未知订单不宣称风险已解析
- AKEUSDT 外部执行追加 1 轮非市价 LIMIT 撤单和 3 轮可成交 LIMIT 开空/reduce-only MARKET 平仓；最终独立检查为 0 挂单、0 仓位
- BTCUSDT 追加 `SELL LIMIT 0.001 @ 100000` 从 `NEW` 到 `CANCELED`，以及
  `SELL LIMIT 0.001 @ 65000` 成交后 reduce-only 平仓；独立复核 AKEUSDT/BTCUSDT
  均为 0 挂单、0 仓位
- 执行器 100 轮 soak：每 10 轮注入一次“交易所已接单但 REST 响应超时”，100 个 client ID 均只 POST 一次并完成查回
- Binance `demo-fapi` 真实鉴权成功；账户已切换 one-way，AKEUSDT 真实 `SELL LIMIT` 成交
  1300 后以 reduce-only `BUY MARKET` 成交 1300 并归零
- AKEUSDT 非市价化 `SELL LIMIT` 真实进入 `NEW` 后撤为 `CANCELED`，成交量为 0 且未产生仓位
- 测试 Compose 使用独立项目名，不会重建默认 PostgreSQL/Redis；默认容器 ID 隔离回归已通过
- `scripts/verify_ledger_dependency_recovery.sh`：PostgreSQL 重建后账本先降级并在 4 秒内恢复
- `scripts/market_smoke.py e2e`：真实 testnet WS 接收 11 条完成 1s Bar 和一条新完成 1m Kline，质量状态 ready
- 真实 PostgreSQL 组合回归：`ORDER_TRADE_UPDATE` 同步 WAL、未知订单风险门禁、订单和成交，
  `ACCOUNT_UPDATE` 随后写入同账户持仓
- 真实 PostgreSQL 准入回归：Web/数据库开关关闭后策略门禁 fail-closed，并只撤销已知未终态
  入场单；退出单、已有仓位和未知提交保持各自管理路径
- AKEUSDT DuckDB 时间一致性审计：1s 与 1m 同分钟中位相对误差约 `2.47%`；1s 显式
  `+8h` 后降至 `0%`。旧 replay/标定已归档，对齐回放必须记录该修正
- Python 编译检查通过
- Compose 配置解析通过
- 核心模块导入通过
- `binance_testnet_flatten.py` 离线单测及真实写入均通过；对 AKEUSDT 空仓使用 reduce-only
  `BUY MARKET`，退出单明确为 `FILLED`、成交 1300，最终无挂单、无持仓
- 完整 Spike Compose profile 真实连接 User Stream；修复 Redis 消费者启动顺序后，
  `bar1s:AKEUSDT` 从启动起即有 1 个消费者；修复未完成 Kline 的传输质量判定后，
  aggTrade、1m 和 5m 三个流均 healthy，`/quality` 返回 200
- Spike 人工重启时旧 listenKey 正常关闭、新 listenKey 成功连接；市场订阅卸载后重新注册，
  `connection_generation=2` 并恢复 ready；验收后 subcategory 已关闭、策略容器已停止
- 受控测试信号经完整 Spike 进程生成三档 AKEUSDT `SELL LIMIT`，交易所数量为
  `1316/1750/1310`、状态均为 `NEW`；WAL 顺序记录 intent、REST `NEW` 和 User Stream
  `NEW`。关闭准入并优雅停止后，三档均由 User Stream 确认为 `CANCELED`、成交量 0，
  Campaign 已释放
- 带上述终态 WAL 再次启动时没有重下订单，subcategory version 4 保持 disabled；最终仍为
  0 个挂单、0 个非零仓位
- AKEUSDT 每档最小名义金额为 5 USDT；进程新增启动前门禁，拒绝总金额 10 导致的
  3/4/3 USDT 无效配置，Compose testnet 默认总金额调整为 20 USDT
- 完整进程受控成交中，e1 数量 1437 先成交 1200 后成交 237，e2 数量 1911 先成交 1201
  后成交 710，均由 User Stream 从 `PARTIALLY_FILLED` 进入 `FILLED`；e3 数量 1429 撤销，
  账户确认 AKEUSDT 空头 3348。外部紧急工具以 reduce-only `BUY MARKET 3348` 成交并归零
- 首次停机因本地部分成交状态落后于交易所终态而 fail-closed；新增撤单异常后的 REST 终态
  查询回归。清理后在 subcategory version 6 disabled 状态重启，Campaign 释放、无重下单，
  最终优雅停止为 `Exited (0)`
- 数据库迁移至 `0002` 后，完整 Spike profile 曾通过迁移 runner、真实 User Stream 连接和
  健康检查；验收后先停止 Spike，再独立确认 AKEUSDT/BTCUSDT 均为 0 挂单、0 持仓。
  新增迁移 `0003` 的策略运行状态、5 秒心跳、15 秒 stale 判定和 API/Web 展示已通过
  Compose 真实 PostgreSQL 回归；默认数据库已从 `0002` 升至 `0003`，准入关闭的真实
  Spike testnet 进程写入 `running` 且 `entry_enabled=false`，优雅停止后写入 `stopped`，
  启停前后均复核为 0 挂单、0 持仓
- 真实 User Stream 主动断流重连演练观察到 disconnect、恢复对账、reconnected 和 listenKey
  轮换，最终 0 挂单、0 非零仓位；报告为
  `reports/testnet_user_stream_reconnect_20260807.json`

尚未验证：

- Spike 由自然策略信号触发的正式保护性退出和盈利管理；执行器驱动的完整 Campaign 已平仓
  PnL 已通过真实 testnet 验证
- subcategory 准入服务的持续未知回报外部故障验证
- Web 浏览器视觉与兼容性验收（当前环境无法安装受支持的 Playwright 浏览器）
- Binance 外部 WS 长时间运行验证；受控 User Stream 主动断流故障注入已通过，但不代表
  长时间稳定性已验收
- Binance Futures testnet 的持续未知回报故障注入及人工处置；完整进程真实部分成交、
  成交仓位确认、剩余档撤单、持仓 Campaign 恢复/释放、空仓重启和主动断流重连已完成
- 外部告警通道、Web 身份权限、正式 live 风控/验收阈值及自然策略信号退出验收

## 6. 明确不做

- 清算地图当前不实施，也不阻塞 replay、testnet 或 live。
- 未经确认不新增其他策略。
- 未经确认不把实时纸盘、滚动重挂或热更新策略参数加入 V1。
- 未经确认不根据旧回测结果扩大仓位或启动实盘。

## 7. 需要用户确认的决策

1. 现有 PostgreSQL、Redis、FastAPI、Compose 和“执行层为库”已作为当前实现基线，是否冻结为 V1 技术方案？
2. V1 挂单失败或撤销后是否允许本轮重挂？
3. 部分成交、手续费、滑点、同秒事件、未平仓结算采用什么回测口径？
4. 动能指标组合已确认进入测试；仍需根据结果冻结阈值、90 秒后的时间收严曲线、5m/15m 趋势线、下跌通道及“站稳”定义。
5. 监听租约的入池条件、确认次数、回吐、期限和重入规则是什么？扫描周期已确认为 5 分钟。
6. Web V1 的身份认证、角色和敏感操作范围是什么？
7. replay、testnet、live 各自的验收阈值和人工审批条件是什么？
8. 外部 DuckDB 继续只读挂载，还是迁移为本项目独立数据卷？
9. `SUBMIT_UNKNOWN` 已确认先按 5 秒一次、最多 12 次进行 testnet 验证；达到上限后持续未知状态的运维处置流程仍需定义。

## 8. 当前代码状态总结（2026-08-07 更新）

| 模块 | 状态 | 备注 |
|---|---|---|
| 信号检测逻辑 | 已冻结 | 参数与实验脚本对齐，消除未来数据泄漏 |
| 三档挂单 | 已修复 | `range(3)` 修复，价格计算正确 |
| 数据连续性检查 | 已实现 | 5s/60s 窗口检查及行情层质量门禁已接入 |
| 订单幂等 | 已接入实时进程 | WAL、User Stream、启动恢复、5秒×12轮询、client ID 身份冲突门禁；撤单锁和迟到回报已有自动化覆盖 |
| 失效撤单 | 已接入实时进程 | TTL、失效、subcategory 关闭及关机均撤销入场单；部分成交/异常撤单竞态已回归 |
| Campaign | 已接入实时进程 | Redis 原子租约、恢复一致性、成交后仓位确认和订单终态+空仓释放已装配；生命周期由 PostgreSQL `strategy_audit_events` 按 Campaign 查询，不新增独立状态表 |
| 持仓管理 | 部分完成 | D-007 仅为执行测试；最新 origin/动能/趋势方案待标定，live 已硬阻断 |
| 环境解耦 | 已完成进程适配 | replay 与 BinanceStrategyAccount 共用策略接口，testnet/live 共用执行进程 |
| 账本查询 | 已完成执行闭环 | PostgreSQL CRUD/PnL/API、具体进程入账及 Campaign 生命周期审计已有；新 Campaign 可查询逐轮执行 PnL，缺失归属的旧成交不猜测回填 |
| Web V1 | 部分完成 | 账本、PnL、subcategory 和独立策略运行状态已有，缺身份权限和浏览器视觉验收 |

**Phase 0 剩余工作**：
- 固定案例已完成 5/5；仍需结合逐笔数据解释对照脚本 CSV 差异，策略参数继续冻结

**Phase 1 剩余项**：
- Binance testnet 公共流短时连通和进程重启订阅恢复已验证；仍需长时间运行验收
- 外部告警通道
- 待确认的监听租约规则
