# 项目功能盘点与实施前决策

> 盘点日期：2026-08-07
> 盘点依据：当前源码、容器测试、`docs/spike_trader/decisions.md` 与已归档状态快照
> 状态：实施中；成交与退出规则待用户确认

## 1. 当前结论

项目已形成“历史数据预热 -> Spike 信号 -> 三档订单 -> 成交/持仓 -> 报告”的
可运行 replay 入场链路；账本查询与最小 Web 控制闭环已经可用。持仓保护与退出、测试网
执行，以及执行回报到账本的事务闭环尚未完成。

当前本地全量测试为 `75 passed, 5 skipped`，Compose 真实 Redis/PostgreSQL 环境为
`78 passed`。测试已覆盖 Spike 两个 replay CLI、16 小时预热、
正向信号至三档成交、全局交易准入、必需数据集缺失拒绝、期末未平仓标记、testnet URL
切换、combined stream 解包、自动重连、订阅刷新、多 Bar 发布、真实 Redis 分发、真实
PostgreSQL CRUD/API/PnL/subcategory 审计及 Web 静态资源；仍不能证明 Binance testnet
执行或实盘流程可用。

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
| 行情客户端 | Binance WebSocket 客户端、aggTrade/Kline 解析器 | P0 已修复，待外部集成验证 |
| 行情聚合 | aggTrade 到 `Bar1s` 的内存聚合器 | 已实现基础逻辑 |
| 行情分发 | Redis Pub/Sub、Kline latest Hash | 已通过真实 Redis 服务级集成 |
| 订阅管理 | consumer 声明式订阅、引用计数、instance epoch | 刷新路由已修复，待断线恢复验证 |
| 执行客户端 | Binance REST、签名、限速、User Data Stream | 基础客户端已实现 |
| 账本 | PostgreSQL 订单/成交/持仓、PnL、subcategory 审计和 FastAPI 查询 | 已通过真实 PostgreSQL 服务级集成 |
| 风控 | 总持仓价值、币种数量、币种阻塞 | 仅最小基础能力 |
| 回测 | UTC 虚拟时钟、16h 预热、简化限价成交、持仓、费用和策略审计报告 | replay 入场链路可运行 |
| Web | 运行状态、订单、成交、持仓、PnL、subcategory 控制 | V1 已实现，身份权限待确认 |
| 实验基线 | 100 标的历史研究脚本和报告 | 可作事实对照，不能作生产结论 |

## 3. P0 修复记录

### 3.1 测试网端点隔离（已修复）

- `BINANCE_TESTNET=true` 会自动选择 Futures testnet REST/WS URL。
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

**仍缺失的实时适配**（Phase 3 修复范围）：
- testnet/live 账户实现尚未接入 `StrategyAccount`
- User Stream 基础回报投递与重连调度已有实现和 mock 覆盖；WAL、启动对账、迟到回报及完整账本对账尚未形成闭环

### 3.5 订单幂等与失效撤单（Phase 2 修复）

**订单幂等问题**：
- 策略使用 `client_order_id` 标记已下单
- 新代码已在 `SpikeSignal.placed_client_order_ids` 中维护幂等集合
- 仍需 Phase 3 实现 WAL 和 `SUBMIT_UNKNOWN` 对账

**失效撤单已实现**：
- 新代码通过 `_cancel_signal_orders()` 正确撤销未成交订单
- 通过账户抽象调用 `cancel_order()`，回测模式立即生效

**仍缺失**（Phase 3 范围）：
- 订单 WAL（写前日志）
- `SUBMIT_UNKNOWN` 后台查询确认
- 启动对账
- User Stream 回执处理基础调度（线程回调回主事件循环、重连去重、停止取消）
- 迟到回报处理

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
| **Campaign** | ✅ 全局入场互斥、第一笔成交计时；⏳ 恢复与终态 | Phase 2 |
| **入场订单（已部分实现）** | ✅ 固定总名义金额、✅ 三档幂等、⏳ 部分成交、⏳ 撤单竞态 | Phase 2/3 |
| **执行恢复** | WAL、`SUBMIT_UNKNOWN` 查询确认、启动对账、迟到回报 | Phase 3 |
| **持仓退出** | ✅ D-007 900 秒非正收益退出；保护单、止损、止盈、盈利管理、最终结算 | Phase 2/3 |
| **风控** | 单币、账户、保证金、杠杆、日亏损、数据延迟、紧急停止 | Phase 3 |
| **监听池** | subcategory、低频发现扫描、监听租约、保护性监听 | Phase 1/4 |
| **回测可信度** | ✅ 无未来数据/预热/数据集缺失拒绝/窗口缺口门禁/未平仓 MTM；⏳ 部分成交、滑点、同秒顺序最终口径 | Phase 2 |
| **审计** | ✅ 信号/计划/失效/首成交/基础退出及订单/成交/持仓；⏳ 完整 PnL 链路 | Phase 2/4 |
| **Web** | ✅ subcategory 控制、账本、PnL、运行状态；⏳ 身份与权限 | Phase 4 |
| **运维** | testnet/live 隔离、监控、告警、凭据、回滚、紧急平仓 | Phase 5-6 |

## 5. 测试现状与缺口

已验证：

- `uv run --extra dev pytest -q`：`75 passed, 5 skipped`
- `docker compose -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from test`：`78 passed`
- Python 编译检查通过
- Compose 配置解析通过
- 核心模块导入通过

尚未验证：

- Spike 部分成交、保护性退出、盈利管理、完整已平仓 PnL
- subcategory 关闭后策略实时轮询并撤销未成交入场单
- Web 浏览器视觉与兼容性验收（当前环境无法安装受支持的 Playwright 浏览器）
- Binance HTTP/WS 重连和 User Stream 对账
- Compose 全服务健康与 testnet 端点隔离
- 外部 DuckDB 历史数据上的新平台端到端回测

## 6. 明确不做

- 清算地图当前不实施，也不阻塞 replay、testnet 或 live。
- 未经确认不新增其他策略。
- 未经确认不把实时纸盘、滚动重挂或热更新策略参数加入 V1。
- 未经确认不根据旧回测结果扩大仓位或启动实盘。

## 7. 需要用户确认的决策

1. 是否保留现有 PostgreSQL、Redis、FastAPI、Compose 和“执行层为库”的技术方案？
2. V1 挂单失败或撤销后是否允许本轮重挂？
3. Campaign 是否采用“逐币种状态对象 + 全局协调器只允许一个交易状态”？
4. 部分成交、手续费、滑点、同秒事件、未平仓结算采用什么回测口径？
5. 盈利仓位超过 900 秒后的止盈、动能衰减、回撤和分批退出规则是什么？
6. 监听租约的入池条件、扫描周期、确认次数、回吐、期限和重入规则是什么？
7. Web V1 的身份认证、角色和敏感操作范围是什么？
8. replay、testnet、live 各自的验收阈值和人工审批条件是什么？
9. 外部 DuckDB 继续只读挂载，还是迁移为本项目独立数据卷？

## 8. 当前代码状态总结（2026-08-07 更新）

| 模块 | 状态 | 可用性 | 备注 |
|---|---|---|---|
| 信号检测逻辑 | ✅ 已冻结 | 90% | 参数与实验脚本对齐，消除未来数据泄漏 |
| 三档挂单 | ✅ 已修复 | 90% | `range(3)` 修复，价格计算正确 |
| 数据连续性检查 | ✅ 已实现 | 80% | 5s/60s 窗口检查，缺行情层级标记 |
| 订单幂等 | ✅ 已实现 | 70% | `placed_client_order_ids` 幂等集合，需 WAL |
| 失效撤单 | ✅ 已实现 | 70% | `_cancel_signal_orders()` 正确撤单 |
| Campaign | ⚠️ 已有全局准入锁和首成交时钟 | 40% | 缺退出、恢复与持久化 |
| 持仓管理 | ⚠️ D-007 已实现 | 25% | 保护退出、盈利管理和完整已平仓 PnL 待确认 |
| 环境解耦 | ✅ 依赖最小账户协议 | 70% | 缺 testnet/live 账户适配器 |
| 账本查询 | ✅ PostgreSQL CRUD/PnL/API | 80% | 缺 Campaign 和执行回报接线 |
| Web V1 | ✅ 账本、PnL、状态、subcategory | 80% | 缺身份权限和浏览器视觉验收 |

**Phase 0 剩余工作**：
- 固定案例已完成 2/5（无成交、三档全成交）；待补失效、冷却和部分成交
- 对照脚本 CSV 验证差异可解释

**Phase 1 剩余项**：
- 真实 Binance testnet 流的外部连通和长时间运行验证
- Redis Pub/Sub 断流检测和告警（Redis/WS readiness 已接入 `/health`）
- 数据质量状态传递给实时策略
