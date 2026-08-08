# 项目完整实施计划

> 版本：v1.23
> 更新日期：2026-08-07
> 状态：执行中
> 事实来源：当前源码、自动化测试、`ARCHITECTURE.md` 与 `spike_trader/decisions.md`

## 1. 目标

在现有三层架构上完成一条可回放、可恢复、可审计的 Spike 做空交易闭环：

```text
行情数据层 -> 策略执行层 -> 账本与 Web 控制层
行情接入      信号/Campaign    PostgreSQL 账本
完成 1s Bar   下单/成交/退出    FastAPI/Web 控制
K 线/质量     风控/恢复         审计/运行状态
```

V1 只实现上涨尖峰后的做空策略，运行模式仅为 `replay`、`testnet`、`live`。
清算地图、其他策略、实时纸盘和策略参数热更新不在当前范围内。

## 2. 文档与决策规则

当前实现只服从以下文档，优先级从高到低：

1. `docs/spike_trader/decisions.md`：已确认业务规则和待确认问题
2. `docs/ARCHITECTURE.md`：三层职责与依赖方向
3. 本文：实施顺序、交付物和验收门禁
4. `docs/PROJECT_GAP_ANALYSIS.md`：基于当前代码的状态快照

`docs/archive/` 中的内容仅供追溯，不作为实现或验收依据。未写入决策记录“已确认”
部分的规则，不得根据旧文档或常见做法自行补全。

### 2.1 策略冻结与数据评审门禁

自 2026-08-07 起，策略层暂时冻结为 `candidate-v1`。在用户根据具体交易数据
确认问题之前，不进行参数搜索、收益寻优、新退出指标猜测或多样本
walk-forward。每次策略评审必须先提供可追溯的逐轮事实：

- 信号时间、三档预挂价和每笔实际成交；
- 首次成交、origin、减仓/清仓时间及触发原因；
- 触发时的价格路径、候选指标快照和执行状态；
- 卖出/买回均价、数量、手续费和净 PnL。

只有在用户确认具体问题和待验证规则后，才新增一个单变量实验，并同时
保留基线逐笔对照。执行安全、故障注入、恢复和账本一致性测试不属于
策略实验，可继续实施。

## 3. 当前基线

截至 2026-08-07：

- 已建立 Git 仓库并提交初始版本；
- 已确认三层业务架构；
- 当前宿主机全量为 `431 passed, 33 skipped, 1 warning`；Compose 真实
  PostgreSQL/Redis 全量为 `460 passed, 1 skipped, 1 warning`；
- Spike replay 已跑通“预热 -> 信号 -> 三档挂单 -> 成交 -> OPEN 持仓 -> 报告”；
- replay 数据范围固定为 AKEUSDT：UTC `2026-07-01` 至 `2026-08-01`，从
  `2026-06-30 08:00 UTC` 开始 16 小时预热，使用只读 DuckDB 历史源和
  `1000 USDT` 总名义。2026-08-07 数据对账证实 DuckDB 的 1s 序列相对 1m Kline
  整体早 8 小时；同时间对齐的价格中位相对误差从约 `2.47%` 降至 `0%`。旧 replay、
  legacy 对照和退出标定报告均已归档为无效；新 runner 只在显式传入
  `--bar1s-time-shift-hours 8` 时修正，不自动猜测数据时区；
- candidate-v1 已接入 replay/testnet 共用策略核心，Kline 只更新指标，退出订单统一由
  下一根完成 1s Bar 按可执行价格产生；原 candidate-v1 阈值来源受错位数据污染，当前
  只能作为执行候选和数据观察基线；先完成人工逐笔评审，后续实验需用户再次确认，
  当前不能作为收益或生产结论；
- 行情层已完成 testnet 隔离、订阅刷新、combined stream、重连、多 Bar 发布、Redis
  Pub/Sub/Kline Store 服务级集成和依赖健康检查；Pub/Sub 零订阅者检测、状态 API 和告警日志已补齐；
- Binance Futures testnet 公共行情短时 smoke 已真实接收 11 条完成 1s Bar 和一条新完成 1m Kline，
  Redis 交付及质量门禁均为 healthy；该结果不包含鉴权 REST 或订单执行；
- 账本层已完成订单、成交、持仓 CRUD/PnL 查询、subcategory 准入审计、策略审计
  （含按 Campaign 查询生命周期）及 Web V1；
- D-010/D-011 准入服务已实现：按显式周期读取 PostgreSQL，关闭或数据源故障时禁止
  新信号，撤销 `NEW`/`PARTIALLY_FILLED` 入场单，保留退出单和已有仓位；未知提交继续
  fail-closed，解析为已知未终态后再撤销。真实 PostgreSQL 开关到策略撤单已通过组合测试；
- Spike 已通过 `StrategyAccount` 接口与回测引擎内部结构解耦，并输出基础策略审计事件；
- 默认 Compose 已验证 PostgreSQL、Redis、行情和账本服务可健康启动；未确认的示例策略仅在
  `--profile examples` 下启动；
- User Stream、启动时未知 WAL 单次对账、有限次数后台查单和停止/重连已形成可复用运行时；
  `ORDER_TRADE_UPDATE` 会严格同步 WAL/RiskGuard，并与订单/成交、`ACCOUNT_UPDATE` 持仓
  共同写入 PostgreSQL。启动及重连会先按 WAL 所有权从 REST 回补错过的订单、成交与管理
  标的仓位，再执行严格快照对账；组合链路已通过真实数据库测试。具体 `spike-live` 进程已装配交易
  规则量化、one-way 模式门禁、行情预热、Redis Campaign、subcategory、TTL 撤单和关机
  撤单；`demo-fapi` 鉴权成功，现有 testnet 账户已在清理旧仓位后切换为 one-way。AKEUSDT
  已真实完成限价入场/撤单、限价成交/reduce-only 退出和人工紧急清仓三类验证，最终无挂单、
  无持仓。完整 Spike profile 已通过空仓启动和人工重启，User Stream listenKey、市场订阅和
  三类行情流均恢复；真实订单已覆盖 `PARTIALLY_FILLED`、`FILLED`、TRADE、仓位确认、剩余档
  撤单、reduce-only 清仓及 Campaign 恢复/释放。
- User Stream 启动和重连会等待 WebSocket 实际打开；断流立即关闭 execution gate，完成
  REST 对账且无未知订单后才恢复。带仓重启从 Redis Campaign、WAL 身份和 PostgreSQL
  成交交叉恢复首成交时间、累计手续费和成交幂等状态，事实缺失时启动失败。
- AKEUSDT testnet 已追加完成 1 轮非市价 LIMIT 挂单撤销及 3 轮可成交 LIMIT 开空、
  reduce-only MARKET 平仓；每轮使用唯一 client order ID，最终独立检查为 0 挂单、0 仓位。
- 账户级回报不得隐式归属策略：运行时工厂要求显式声明专用策略账户；共享账户在缺少
  client-order-id 和仓位路由规则时直接拒绝启动。
- Compose 使用显式 `spike` profile 装配交易进程；默认服务启动不承担下单风险。
- testnet 执行 harness 已区分默认预挂撤单 `cancel-open` 与显式成交退出 `fill-and-exit`；
  后者需要额外开仓确认，并验证 LIMIT 成交、仓位可见、reduce-only MARKET 退出和最终空仓。
- 可交易池扫描节拍已冻结为 5 分钟，subcategory 准入刷新复用同一节拍；扫描编排组件
  已完成，接入具体行情/策略进程后再进行外部验收。
- Redis 全局 Campaign 互斥存储组件已完成：原子获取、不自动过期、仅持有者可释放；
  已接入 Spike 进程。真实成交回报先于 `ACCOUNT_UPDATE` 时保持 pending，只有交易所
  订单、成交确认和仓位事实均终态后才释放；撤单竞态、迟到成交和重连顺序已有回归覆盖。
- Candidate 退出状态已写入 Redis Campaign；退出意图先进入 WAL/提交器，再持久化状态。
  重启时 Redis、WAL、PostgreSQL 实际成交和当前仓位必须互相印证，不一致直接 fail-closed；
  残余入场单全部终态并刷新交易所仓位前不生成退出，candidate 与轮换共用单一退出在途约束。
- 订单 TTL 已绑定不可变的首次 intent 时间，状态更新和重启 REST 对账不会延长
  预挂单寿命；旧 WAL 从追加日志的首条 intent 兼容恢复。
- 终态 WAL 新增账本 ack 确认点；`FILLED/CANCELLED/EXPIRED` 在订单、成交数量和
  仓位全部同步成功前不会 ack，崩溃后仍会重试补账。
- User Stream 会跟踪所有账本/仓位回调；回调异常立即关闭 execution gate 并终止
  Spike 进程，关机在有界时间内排空回调，超时会显式失败。
- 账户级 PostgreSQL session advisory lock 保证同一交易账户仅有一个执行进程；
  锁会话丢失会在有界探测后关闭执行门禁并终止进程。
- 专用账户的未知 client order id 回报和非托管 symbol 仓位回报会立即 fatal，
  且不会写入 Spike 账本；人工 testnet harness 不得与 Spike 进程并发运行。
- 策略审计事件已以确定性键幂等写入 PostgreSQL，可通过
  `/api/v1/strategy-audit-events` 按账户、策略、symbol、事件类型和 Campaign 查询；
  写入失败时保留待写事件并 fail-closed。
- Spike 本地检查每个托管 symbol 的 Redis 1s Bar 交付新鲜度；任一流超过 10 秒
  静默就关闭入场门禁，不依赖上游健康声明。
- 独立真实 testnet harness 已主动关闭 User Stream WebSocket，并观察到 disconnect、恢复对账、
  reconnected 和 listenKey 轮换；最终 0 挂单、0 仓位，报告为
  `reports/testnet_user_stream_reconnect_20260807.json`。该短时演练不替代外部长时间运行验收。
- 明确业务拒单会写入不可逆终态 `REJECTED` 并保留交易所错误码，不进入
  `SUBMIT_UNKNOWN`、不更换 client ID 重试；超时等结果模糊错误仍保持未知并 fail-closed。
- 迁移 `0003` 增加策略运行状态。Spike 每 5 秒发布实例、门禁和 halt 心跳，15 秒未更新时
  `/api/v1/strategy-runtime-status` 返回 `stale`；Web 将账本数据库健康与策略运行状态分开，
  无运行记录不会冒充策略正常。

当前结果证明离线入场链路、Redis/PostgreSQL 内部服务集成、Binance testnet 公共行情短时
链路、独立 REST harness、完整策略进程成交恢复和受控 User Stream 主动断流恢复可用；
外部长时间运行、外部告警、正式阈值和自然信号退出仍未验收，也不能据此启动正式账户。

## 4. 功能范围与状态

状态含义：`完成` 表示已有实现和自动化验证；`部分完成` 表示已有骨架或局部闭环；
`未开始` 表示尚无可验收实现；`待确认` 表示业务规则未确定，禁止实施。

### 4.1 行情数据层

| 能力 | 状态 | 剩余工作 | 验收 |
|---|---|---|---|
| Binance REST/WS testnet 隔离 | 完成 | 鉴权 REST 需随 testnet 执行验证 | 健康 API 暴露环境；外部 smoke 会拒绝非 testnet 服务 |
| aggTrade/Kline 接入与 combined stream 解包 | 完成 | 外部流长时间运行验证 | testnet 短时 smoke 已接收完成 Bar/Kline，原始与 combined 消息均可解析 |
| aggTrade 聚合完成 1s Bar | 完成 | 外部流长时间运行验证 | 不重复、不丢失跨多秒完成 Bar |
| 动态订阅、引用计数、刷新和重连 | 部分完成 | 租约规则、缺口 REST 回补决策 | 长稳发现 26 秒公共 WS 中断；重连恢复 streams，但确定 gap 保持粘性降级 |
| 可交易池扫描编排 | 部分完成 | 接入真实扫描器和运行进程 | 固定每 5 分钟扫描，subcategory 在同一节拍刷新 |
| Redis 分发和 Kline Store | 完成 | 外部告警通道 | 真实 Redis 读写通过；零订阅者发布会告警，活跃流无消费者时健康检查 503；长稳故障期间正确关闭策略门禁 |
| 历史 Parquet 数据读取 | 部分完成 | 归档边界、缺口报告 | replay 不联网补数据，缺数据直接拒绝 |
| 监听池发现与租约 | 待确认 | 入池、回吐、期限、重入规则 | 规则冻结后补状态机测试 |

### 4.2 策略执行层

| 能力 | 状态 | 剩余工作 | 验收 |
|---|---|---|---|
| Spike 信号与三档价格 | 完成 | 与历史脚本固定案例对账 | 参数与冻结脚本一致，无未来数据 |
| 16h 预热和连续性检查 | 完成 | 外部质量长时间运行验证 | 预热不下单，窗口缺口阻止信号 |
| replay runner 与报告 | 部分完成 | 滑点、同秒顺序及对齐后绩效口径 | 已输出订单、成交、持仓、汇总、策略审计；LIMIT 支持跨 Bar 部分成交，MARKET 使用 Taker 费用，可选交易所 tick/step 快照 |
| 全局交易准入与首成交计时 | 完成 | 持续做外部故障回归 | 已验证全局互斥、Redis 原子租约、带仓重启首成交/手续费恢复及 D-009 |
| 入场幂等与失效撤单 | 完成 | 持续做交易所回归 | replay 与 AKEUSDT testnet 已验证预挂、幂等、部分成交、全部成交、撤单和终态 WAL 重启 |
| User Stream 与启动对账 | 部分完成 | 持续未知外部处置演练 | 长稳中多次真实断流均在同一实例约 2 秒恢复；已完成真实回报、REST 回补、终态 WAL 补账 ack、主动断流重连、listenKey 轮换、回调 fatal/有界排空、持续未知 fatal、TRADE 幂等及带仓 Campaign timing 恢复 |
| 持仓保护与退出 | 部分完成 | 先与用户逐轮评审买卖点、指标快照和 PnL；未确认前暂停调参 | candidate-v1 已实现 origin 减半、90s 后时间/动能退出及 5m/15m 突破退出；参数仅为候选，旧标定已失效，live 继续拒绝 |
| 账户级风控 | 部分完成 | 保证金、日亏损、数据延迟、急停 | 已限制持仓价值、币种数、杠杆并阻塞未知订单 symbol；关键事实不一致会全局 halt 新开仓，但保留 reduce-only 退出 |
| testnet/live 适配 | 部分完成 | 完整策略进程真实多轮验证、最新退出策略 | 已有正式进程与 `BinanceStrategyAccount`；REST harness 已在 one-way testnet 多轮写入；未冻结退出前 live 拒绝启动 |

### 4.3 账本与 Web 控制层

| 能力 | 状态 | 剩余工作 | 验收 |
|---|---|---|---|
| PostgreSQL schema 和模型 | 完成 | 后续变更逐版本追加迁移 | 有序迁移、事务回滚、并发锁、校验和及既有数据接管已通过真实 PostgreSQL 测试；启动会迁移并验证当前版本 |
| 订单/成交/持仓与 Campaign 生命周期审计 | 完成 | 旧成交不猜测回填 | `campaign_id` 显式贯穿意图、WAL、User Stream、启动回补和账本；`/api/v1/campaigns/{campaign_id}/pnl` 返回实际卖出/买回均价、数量、USDT 手续费及净 PnL；事实不完整或方向矛盾时拒绝计算 |
| FastAPI 查询 API | 完成 | 认证确定后补访问控制 | 订单、成交、持仓、PnL、策略审计、策略运行状态分页查询和真实数据库健康检查已验证 |
| subcategory 准入控制 | 部分完成 | 接入真实可交易池扫描器并外部验证 | 已接入 Spike 进程；乐观并发、追加审计、fail-closed 刷新和关闭撤单已通过真实 PostgreSQL 测试 |
| Web 页面 | 完成 | 浏览器兼容性视觉验收 | V1 提供独立的账本健康和策略运行状态、账本、PnL 与 subcategory 控制 |
| 权限与操作审计 | 待确认 | 身份、角色、敏感操作范围 | 所有控制变更可追责 |
| 监控、告警、备份恢复 | 部分完成 | SLO、外部告警通道 | runtime heartbeat/API/Web、只读 soak、迁移校验和及 PostgreSQL 备份恢复演练已完成；报告保留失败进度且脱敏 |

## 5. 分阶段实施

### Phase 0：基线与文档治理

**状态：部分完成（内部自动化已覆盖，外部账户待验收）**

交付物：冻结脚本参数、当前三层架构、功能差距、权威计划、决策记录、旧文档归档、
固定 replay 案例。

固定案例当前完成 5/5：无成交、三档全成交、部分成交、失效/TTL、冷却。对齐后的旧脚本
与 candidate 已有 39 轮可按开仓直接配对；剩余是与用户共同核对差异集中轮次的价格路径、
指标快照和执行事实。Phase 0 尚未通过验收。

退出条件：文档无冲突；每项规则能追溯到确认记录；固定输入可重复得到相同输出。

### Phase 1：行情层运行闭环

**状态：完成**

交付物：可靠的完成 1s Bar、完成 K 线、动态订阅、重连、Redis 分发、依赖健康检查、
数据质量门禁。

已完成 Pub/Sub 零消费者告警、行情质量状态 API、实时策略 fail-closed 消费，以及 testnet
公共 WS 到 Redis 的短时外部 smoke，完整 Spike 进程重启后的订阅恢复也已验证。剩余：
外部告警通道仍属于部署运维项。行情层采用 WS+HTTP 双栈：WS 负责实时订阅、连续性水位和
质量门禁；`GET /klines/{symbol}/{interval}` 负责策略按范围读取已完成 K 线。WS 重连会从
Redis 持久水位用 REST `aggTrades fromId` / Kline 时间范围回补，先整批校验再重放；缺口、
满页截断、冲突重复、超时或副作用失败均保持 degraded。2026-08-08 的 3900 秒正式长稳取得
676 个样本并通过，期间同一实例完成多次真实 User Stream 恢复；全程 0 挂单、0 仓位。

退出条件：断线、乱序、迟到、跨秒、多订阅和依赖故障场景均有自动化验证；故障期间
不会产生新交易信号。

### Phase 2：统一策略核心与可信 replay

**状态：部分完成**

交付物：环境无关策略核心、逐币种 Campaign、全局协调器、确定性虚拟时钟、订单撮合、
成交/费用/持仓/PnL 审计报告。

已完成策略对 `BacktestEngine`/executor 内部结构的解耦、信号/入场计划/失效/首成交审计、
D-007、candidate-v1 状态机、部分成交、交易所量化和 Maker/Taker 费用。AKEUSDT DuckDB
对账证实 1s 事件整体早 8 小时，旧端到端 replay 和退出标定已归档为无效；runner 现要求
显式时间修正并写入运行元数据，对齐回放和 39 个同开仓轮次配对已完成。剩余：先共同查看
差异集中轮次的价格路径、指标快照和执行事实，再由用户确认需要验证的规则。
多样本 walk-forward、滑点参数实验和新退出规则当前暂停。

退出条件：同一事件序列在 replay 与实时适配器中产生相同订单意图；所有固定案例通过；
任何 PnL 均能追溯到订单、成交和费用。

### Phase 3：执行、恢复与风险控制

**状态：部分完成**

交付物：订单 WAL、`SUBMIT_UNKNOWN` 解析、User Stream、启动对账、撤单竞态处理、交易所
托管保护单、账户级风控和紧急停止。

已完成订单 WAL、Binance REST 可靠提交适配、明确业务拒单 `REJECTED` 与结果模糊的
`SUBMIT_UNKNOWN` 分离、启动时逐订单单次 `SUBMIT_UNKNOWN` 查单状态解析、
未知订单 symbol 风险阻塞与全部解析后解锁、显式参数的后台查单编排、WAL 所属订单/成交与
管理标的仓位的 REST 启动回补、订单/仓位启动快照一致性门禁，以及 User Stream
订单/账户回报投递。运行时已组合启动对账、后台轮询、停止和重连；账户级订单回报会严格
校验并同步 WAL/RiskGuard，随后与成交和仓位事实写入 PostgreSQL。后台查单已冻结为 5 秒
一次、最多 12 次；具体 testnet 进程、交易规则量化和 one-way 模式门禁已完成。撤单锁、部分
成交、迟到回报、成交后仓位确认、重连顺序和全局 halt 已通过本地与 Compose 回归。现有
one-way testnet 账户已补预挂撤单、3 轮成交/reduce-only 退出、紧急清仓，以及完整进程
`NEW/PARTIALLY_FILLED/FILLED/CANCELED`、TRADE、成交仓位确认、Campaign 恢复/释放和终态
WAL 空仓重启。撤单异常后会按原 client ID 查询交易所，只有明确终态才解除门禁；User Stream
断流立即关闭执行门禁，重连对账后才恢复；带仓重启恢复 timing、手续费和 trade-id 幂等。
订单 TTL 现固定为首次 intent 时间，不会因重启延长；未获账本 ack 的终态 WAL 会继续
回补 PostgreSQL；User Stream 业务回调失败会进入进程级 fatal，关机必须排空或显式报超时。
同一 account id 的执行进程由 PostgreSQL session lock 单实例保护；未归属 WAL 的订单回报
和非托管 symbol 仓位不再写入策略账本，而是直接停止进程。真实 testnet 主动断流演练已观察到
disconnect、恢复对账、reconnected 和 listenKey 轮换，最终空仓空单。3900.224 秒正式长稳
已取得 676 个样本并通过，覆盖同一实例 runtime 恢复和多次真实 listenKey 重建。
剩余持续未知回报外部处置演练，以及正式保护退出规则验收。

退出条件：REST 超时不会重复下单；未知状态持续阻塞新增风险；进程重启后可恢复所有
未终态轮次；本地状态以交易所订单、成交和仓位事实为准。

### Phase 4：账本与 Web 最小闭环

**状态：部分完成**

交付物：数据库迁移、完整账本写入、查询 API、subcategory 控制、Web 交易池/账本/PnL/
运行状态、控制审计。

已完成订单/成交/持仓 CRUD、Binance 订单/成交回报与 `ACCOUNT_UPDATE` 仓位快照原子幂等入账、数据库聚合 PnL、
分页查询、subcategory 乐观并发与追加审计、依赖健康检查和 Web V1；subcategory
fail-closed 轮询及关闭撤销未成交入场单已完成，User Stream 到 WAL、
风险门禁和账本的组合回调已通过真实 PostgreSQL 验证，具体 Spike 进程已接入准入状态。
Campaign 运行时权威状态保留在 Redis 原子租约中；其持久历史已由 PostgreSQL
`strategy_audit_events` 幂等记录，并可按 `campaign_id` 查询。这里不再新增独立 Campaign
状态表，避免与 Redis 运行态形成双写权威。新 Campaign 的订单和成交通过 WAL 中显式
`campaign_id` 归属，并提供逐 Campaign 执行 PnL 查询；旧成交不按时间猜测回填。剩余：
迁移 `0003` 已增加策略运行状态，Spike 每 5 秒写心跳，15 秒未更新显示为 `stale`；
API/Web 已将账本健康与策略状态分离。仍待确认身份认证和权限。

退出条件：控制变更和完整交易生命周期均可查询；并发修改不会静默覆盖；Web 不可绕过
策略准入和风控；数据库或 Redis 故障时默认禁止新增风险。

### Phase 5：测试网端到端验证

**状态：部分完成**

交付物：Compose 服务级集成环境、Binance Futures testnet 小额测试、故障注入、运行手册、
告警和恢复演练。

Compose 已覆盖真实 Redis/PostgreSQL 服务级集成，测试编排已使用独立项目隔离且不会重建默认依赖；
默认行情/账本服务首次部署健康检查及 PostgreSQL 重建后账本恢复脚本已验证；
Binance testnet 已完成预挂撤单、4 轮开平仓和最终空仓检查；完整策略链路此前已覆盖部分成交
和 Campaign 释放。迁移 `0002` 后曾再次启动完整 Spike profile，迁移 runner 与 ledger 使用同一
镜像、User Stream 真实连接且进程 healthy；subcategory version 6 保持 disabled。停止 Spike
后独立 dry-run 再次确认 AKEUSDT/BTCUSDT 均为空仓空单。User Stream 主动断流恢复也已通过
真实 testnet 演练，报告为 `reports/testnet_user_stream_reconnect_20260807.json`。正式
3900.224 秒监督也已 `SOAK_OK`：676 个样本、最大心跳年龄 4.984 秒、一次 5.458 秒
runtime 恢复，全程 0 挂单、0 仓位。新增迁移
`0003` 的策略运行状态和 API/Web 已通过 Compose 真实 PostgreSQL 回归；默认数据库也已从
`0002` 升至 `0003`，准入关闭的 Spike testnet 进程实际写入 `running`、
`entry_enabled=false`，优雅停止后写入 `stopped`，全程空仓空单。仍缺持续未知回报处置、
外部告警通道和正式退出规则验收。
最新一轮 BTCUSDT 使用 `SELL LIMIT 0.001 @ 65000` 成交，随后 `BUY MARKET reduceOnly`
全部成交；独立 dry-run 复核 AKEUSDT/BTCUSDT 均为 0 挂单、0 持仓。最新报告为
`reports/testnet_20260807_post_migration_flat.json`。
新增 Campaign 账本 roundtrip 通过与正式 Spike 相同的 PostgreSQL 账户锁、执行器、WAL、
User Stream 和账本链路完成 BTCUSDT `SELL LIMIT 0.001` 与 `BUY MARKET reduceOnly`
清仓。Campaign `spike_short:BTCUSDT:1786108785578` 聚合 2 笔成交，净已实现 PnL
`-0.08006859 USDT`（含手续费 `0.05216860 USDT`），最终空仓空单；报告为
`reports/testnet_campaign_roundtrip_20260807.json`。这只证明执行与会计一致性，不代表策略退出
规则已经冻结或通过盈利验收。

退出条件：至少覆盖下单、部分成交、撤单、拒单、超时、断流、重启、对账、保护退出和
subcategory 关闭；验收阈值由用户确认后写入决策记录。

### Phase 6：正式账户灰度

**状态：未开始，受人工门禁约束**

前置：Phase 0-5 全部通过；资金上限、允许标的、值守、告警、回滚、紧急平仓方案和审批人
全部书面确认。

正式账户不得由自动流程直接开启。旧研究报告的收益、胜率或 PF 不作为上线门槛。

## 6. 实施顺序与并行边界

```text
Phase 0 ─┬─> Phase 1 ───────────────┐
         └─> Phase 2 replay 基础 ───┼─> Phase 3 ─> Phase 4 ─> Phase 5 ─> Phase 6
业务确认 ─> Phase 2 成交/退出规则 ──┘
```

- 行情可靠性、Redis/PostgreSQL 集成和纯查询 API 可并行；
- Campaign、撮合口径和退出状态机共享业务语义，确认前不能并行猜测实现；
- Web 页面必须等待 API 与控制范围冻结；
- live 必须等待 testnet 门禁，不得并行提前接入正式密钥。

## 7. 每批交付的质量门禁

每批变更至少执行：

```bash
uv run --extra dev python -m pytest -q
python -m compileall -q src scripts tests
docker compose config -q
docker compose -f compose.test.yaml config -q
git diff --check
```

涉及 Redis/PostgreSQL/外部测试网的阶段必须增加服务级验证；不能用 mock 单元测试代替。
每批完成后同步本文和功能差距文档，并建立独立 Git 提交。

当前宿主机全量基线为 `431 passed, 33 skipped, 1 warning`；Compose 真实 PostgreSQL/Redis
基线为 `460 passed, 1 skipped, 1 warning`。

## 8. 风险与停止条件

- 任何配置可能连接生产端点时，停止测试；
- 订单或仓位无法与交易所事实对齐时，阻止新增风险；
- 行情缺口、预热不足、时钟倒退或依赖不健康时，阻止新信号；
- 未冻结退出规则前，只允许受控 testnet 候选验证，不允许进入 live；
- 无备份恢复和紧急操作演练，不进入正式账户灰度。

## 9. 待用户确认

以下问题会改变交易结果或系统边界，当前不做假设：

| 编号 | 需要确认 | 建议 |
|---|---|---|
| Q-002 | 三档 SELL limit 的“仅最高档成交”与价格穿透矛盾如何解释 | 建议先确认档位命名或是否分阶段挂单 |
| Q-003 | 已确认起涨点为第一检查位、动能衰减时减半；仍需标定专业动能指标、时间收严曲线和剩余半仓趋势退出 | 按 D-027 以版本化候选做 replay/testnet 迭代，证据达标后冻结生产参数 |
| Q-004 | replay 触价/穿透、部分成交、滑点、同秒顺序 | Maker/Taker 已区分；其余建议先冻结保守口径，再做敏感性对照 |
| Q-005 | replay 期末 OPEN 仓位是否强平 | 建议默认保持 OPEN 并按末价 MTM，除非明确要求结算 |
| Q-006 | PostgreSQL、Redis、FastAPI、Compose 是否正式保留 | 建议保留，现有实现与三层边界匹配 |
| Q-007 | 监听租约的入池、回吐、期限和重入规则 | 扫描周期已冻结为 5 分钟；其余参数建议用状态机表达并由业务确认 |
| Q-008 | Web 身份认证、角色及敏感操作范围 | 建议 V1 最小只读 + subcategory 开关并保留审计 |

确认结果统一写入 `docs/spike_trader/decisions.md`，再进入对应实现阶段。
