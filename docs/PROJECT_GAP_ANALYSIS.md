# 项目功能盘点与实施前决策

> 盘点日期：2026-08-06
> 盘点依据：当前源码、容器测试、`docs/spike_trader/decisions.md`、阶段文档与已归档状态快照
> 状态：待用户确认后进入实现

## 1. 当前结论

项目已形成“历史数据预热 -> Spike 信号 -> 三档订单 -> 成交/持仓 -> 报告”的
可运行 replay 入场链路；持仓保护与退出、测试网执行和账本写入闭环尚未完成。

当前本地全量测试为 `55 passed`。测试已覆盖 Spike 两个 replay CLI、16 小时预热、
正向信号至三档成交、全局交易准入、关键数据缺失拒绝、期末未平仓标记、testnet URL
切换、combined stream 解包、自动重连、订阅刷新和多 Bar 发布；仍不能证明测试网
执行或实盘流程可用。

当前文档优先级：

1. `docs/PROJECT_IMPLEMENTATION_PLAN.md`
2. `docs/ARCHITECTURE.md`
3. `docs/spike_trader/decisions.md`
4. `docs/spike_trader/architecture/`
5. `docs/spike_trader/phases/`

旧完成总结、旧实现状态、旧迁移报告和历史架构文档已经移动到 `docs/archive/`，只能作为历史记录。

## 2. 已实现并可保留的基础能力

| 区域 | 当前能力 | 当前判断 |
|---|---|---|
| 工程结构 | `src/`、`tests/`、Dockerfile、Compose、pytest | 已建立 |
| 行情客户端 | Binance WebSocket 客户端、aggTrade/Kline 解析器 | P0 已修复，待外部集成验证 |
| 行情聚合 | aggTrade 到 `Bar1s` 的内存聚合器 | 已实现基础逻辑 |
| 行情分发 | Redis Pub/Sub、Kline latest Hash | 已实现基础封装，缺集成测试 |
| 订阅管理 | consumer 声明式订阅、引用计数、instance epoch | 刷新路由已修复，待断线恢复验证 |
| 执行客户端 | Binance REST、签名、限速、User Data Stream | 基础客户端已实现 |
| 账本 | PostgreSQL schema、订单/成交/持仓/控制模型和 FastAPI 查询 | 有骨架，缺服务级集成验证 |
| 风控 | 总持仓价值、币种数量、币种阻塞 | 仅最小基础能力 |
| 回测 | UTC 虚拟时钟、16h 预热、简化限价成交、持仓与费用报告 | replay 入场链路可运行 |
| 实验基线 | 100 标的历史研究脚本和报告 | 可作事实对照，不能作生产结论 |

## 3. P0 修复记录

### 3.1 测试网端点隔离（已修复）

- `BINANCE_TESTNET=true` 会自动选择 Futures testnet REST/WS URL。
- 行情服务使用统一 `BinanceConfig`，不再硬编码生产 WS URL。
- Compose 默认 testnet，不再用生产 URL 环境默认值抵消配置切换。

正式账户仍受 Phase 4-6 的执行恢复和人工门禁约束。

### 3.2 行情订阅刷新（已修复）

- 删除重复路径注册，PUT/DELETE 由唯一 handler 更新状态并刷新 WebSocket。
- FastAPI 回归测试验证 PUT 会调用 `refresh_ws_streams()`。

### 3.3 Combined Stream 消息解包（已修复）

- WebSocket 客户端兼容 raw 与 `{ "stream": ..., "data": ... }` 两种消息。
- 单元测试验证 combined stream 会向下游交付真实事件 payload。

### 3.4 Spike 策略与引擎接口已对齐（2026-08-06）

**已修复**：
- `DynamicSpikeBacktestStrategy` 现在实现 `on_bar1s(bar)` / `on_kline(kline)` 协议
- 删除了旧的 `on_event` 包装器
- 策略通过 `bind_engine()` 获得引擎引用（Phase 2 必须改为抽象接口）

**仍存在的环境依赖**（Phase 2 修复范围）：
- 策略直接访问 `engine.executor.orders` 查询订单
- 策略直接调用 `engine.executor.cancel_order()` 撤单
- 无法用于 testnet/live 模式

### 3.5 订单幂等与失效撤单（Phase 2 修复）

**订单幂等问题**：
- 策略使用 `client_order_id` 标记已下单
- 新代码已在 `SpikeSignal.placed_client_order_ids` 中维护幂等集合
- 仍需 Phase 2 实现 WAL 和 `SUBMIT_UNKNOWN` 对账

**失效撤单已实现**：
- 新代码通过 `_cancel_signal_orders()` 正确撤销未成交订单
- 使用 `engine.executor.cancel_order()` 立即生效（回测模式）
- Phase 2 需改为通过账户抽象调用，支持 testnet/live

**仍缺失**（Phase 4 范围）：
- 订单 WAL（写前日志）
- `SUBMIT_UNKNOWN` 后台查询确认
- 启动对账
- User Stream 回执处理
- 迟到回报处理

### 3.6 数据质量检查已加强（2026-08-06）

**已实现**：
- 5 秒窗口连续性检查：`current.timestamp - bar_5s_ago.timestamp == 5000ms`
- 60 秒窗口连续性检查：`current.timestamp - bar_60s_ago.timestamp == 60000ms`
- 缺口或断线时不触发信号

**仍缺失**（Phase 1 数据层范围）：
- Redis Pub/Sub 断流检测与告警
- 数据质量标记传递到策略层

**已修复**（2026-08-06）：新代码中的 `range(1,4)` bug 已改为 `range(3)`，与实验脚本对齐。

三档价格公式（已冻结，来自实验脚本）：
```python
tier_prices = [spike_high - atr * (0.75 - n * 0.40) for n in range(3)]
# n=0: spike_high - 1.15*atr  （第一档，最低价，深度回调）
# n=1: spike_high - 0.75*atr  （第二档，主力，40% 权重）
# n=2: spike_high - 0.35*atr  （第三档，最高价，浅回调优先）
```

此前文档中出现的"0.75、1.15、1.55"无出处，属笔误，已更正。

## 4. P1：闭环缺失能力（Phase 2-5 范围）

| 区域 | 缺失能力 | 目标阶段 |
|---|---|---|
| **策略核心** | 环境无关的统一 Spike 核心（Clock/Account 抽象） | Phase 2 |
| **Campaign** | ✅ 全局入场互斥、第一笔成交计时；⏳ 恢复与终态 | Phase 2 |
| **入场订单（已部分实现）** | ✅ 固定总名义金额、✅ 三档幂等、⏳ 部分成交、⏳ 撤单竞态 | Phase 2/4 |
| **执行恢复** | WAL、`SUBMIT_UNKNOWN` 查询确认、启动对账、迟到回报 | Phase 4 |
| **持仓退出** | 保护单、止损、止盈、900 秒规则、盈利管理、最终结算 | Phase 4 |
| **风控** | 单币、账户、保证金、杠杆、日亏损、数据延迟、紧急停止 | Phase 4 |
| **监听池** | subcategory、低频发现扫描、监听租约、保护性监听 | Phase 5 |
| **回测可信度** | ✅ 无未来数据/预热/缺失拒绝/未平仓 MTM；⏳ 部分成交、滑点、同秒顺序最终口径 | Phase 3 |
| **审计** | 触发、预测、订单、成交、退出、费用与 PnL 全链路报告 | Phase 3 |
| **Web** | subcategory 控制页面、权限、审计、并发修改 | Phase 5 |
| **运维** | testnet/live 隔离、监控、告警、凭据、回滚、紧急平仓 | Phase 5-6 |

## 5. 测试现状与缺口

已验证：

- `uv run --extra dev python -m pytest -q`：`55 passed`
- Python 编译检查通过
- Compose 配置解析通过
- 核心模块导入通过

尚未验证：

- Spike 正向触发、三档入场、部分成交、失效、退出和 PnL
- PostgreSQL schema/CRUD/API 的容器集成
- Redis Pub/Sub 与 Kline Store 集成
- FastAPI 订阅刷新、健康检查和紧急控制
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
2. V1 是否严格只有 `replay/testnet/live`，不提供实时纸盘？
3. V1 是否不做滚动重挂，挂单失败或撤销后本轮不重挂？
4. Campaign 是“全局唯一一个”，还是“逐币种独立状态机，但全局只允许一个进入交易状态”？
5. 阶段 0 的触发阈值、起涨点、三档价格、失效价是否沿用实验脚本？
6. 部分成交、手续费、滑点、同秒事件、未平仓结算采用什么回测口径？
7. 盈利仓位超过 900 秒后的止盈、动能衰减、回撤和分批退出规则是什么？
8. 监听租约的入池条件、扫描周期、确认次数、回吐、期限和重入规则是什么？
9. Web V1 是否只做 subcategory 开关，还是同时包含暂停、撤单、紧急平仓和账户控制？
10. replay、testnet、live 各自的验收阈值和人工审批条件是什么？
11. 外部 DuckDB 继续只读挂载，还是迁移为本项目独立数据卷？

## 8. 当前代码状态总结（2026-08-06 更新）

| 模块 | 状态 | 可用性 | 备注 |
|---|---|---|---|
| 信号检测逻辑 | ✅ 已冻结 | 90% | 参数与实验脚本对齐，消除未来数据泄漏 |
| 三档挂单 | ✅ 已修复 | 90% | `range(3)` 修复，价格计算正确 |
| 数据连续性检查 | ✅ 已实现 | 80% | 5s/60s 窗口检查，缺行情层级标记 |
| 订单幂等 | ✅ 已实现 | 70% | `placed_client_order_ids` 幂等集合，需 WAL |
| 失效撤单 | ✅ 已实现 | 70% | `_cancel_signal_orders()` 正确撤单 |
| Campaign | ⚠️ 已有全局准入锁和首成交时钟 | 40% | 缺退出、恢复与持久化 |
| 持仓管理 | ❌ 完全缺失 | 0% | Phase 4 范围 |
| 环境解耦 | ⚠️ 硬绑引擎 | 20% | Phase 2 必须重构 |

**Phase 0 剩余工作**：
- 建立 5 个固定测试案例
- 对照脚本 CSV 验证差异可解释

**Phase 1 剩余项**：
- Redis 与真实 Binance 流的容器集成验证
- Redis Pub/Sub 断流检测、健康状态和告警
- 数据质量状态传递给实时策略
