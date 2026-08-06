# 项目功能盘点与实施前决策

> 盘点日期：2026-08-06
> 盘点依据：当前源码、容器测试、`docs/spike_trader/decisions.md`、阶段文档与已归档状态快照
> 状态：待用户确认后进入实现

## 1. 当前结论

项目已具备行情、账本、执行客户端、策略基类和回测引擎的基础骨架，但尚未形成可运行、可验收的逼空插针策略闭环。

当前容器测试为 `30 passed`，实测覆盖率约 `32%`。现有测试主要证明基础类型、简化回测和模块导入可用，不能证明 Spike 回测、测试网执行或实盘流程可用。

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
| 行情客户端 | Binance WebSocket 客户端、aggTrade/Kline 解析器 | 有骨架，真实链路有 P0 缺陷 |
| 行情聚合 | aggTrade 到 `Bar1s` 的内存聚合器 | 已实现基础逻辑 |
| 行情分发 | Redis Pub/Sub、Kline latest Hash | 已实现基础封装，缺集成测试 |
| 订阅管理 | consumer 声明式订阅、引用计数、instance epoch | 已实现基础模型，刷新链路有 P0 缺陷 |
| 执行客户端 | Binance REST、签名、限速、User Data Stream | 基础客户端已实现 |
| 账本 | PostgreSQL schema、订单/成交/持仓/控制模型和 FastAPI 查询 | 有骨架，缺服务级集成验证 |
| 风控 | 总持仓价值、币种数量、币种阻塞 | 仅最小基础能力 |
| 回测 | available time 虚拟时钟、简化限价成交、持仓记录 | 通用骨架可运行 |
| 实验基线 | 100 标的历史研究脚本和报告 | 可作事实对照，不能作生产结论 |

## 3. P0：不依赖产品决策的明确缺陷

### 3.1 测试网配置可能仍连接生产端点

- `BINANCE_TESTNET=true` 没有自动切换 REST/WS URL。
- 行情服务硬编码 `wss://fstream.binance.com`。
- Compose 只传 testnet 布尔值，没有传入测试网 base URL。

在修复并增加配置测试前，不应启动任何带凭据的策略服务。

### 3.2 行情订阅成功但不会刷新 WebSocket

- FastAPI 先挂载通用 router，之后又注册相同路径的扩展 PUT/DELETE 路由。
- 实测 PUT 返回成功，但命中先注册路由，`refresh_ws_streams()` 没有执行。
- 因此订阅状态可以变化，但 Binance WebSocket 不会按订阅开启。

### 3.3 Combined Stream 消息未解包

- Binance combined stream 返回 `{ "stream": ..., "data": ... }`。
- 当前解析器直接读取顶层事件字段，真实 aggTrade/Kline 消息会被忽略。

### 3.4 Spike 回测入口不可运行

`backtest/run_spike_short.py` 使用了已经失效的 Loader、Engine 和 Result API；通用 runner 的 `--strategy spike` 也直接抛出 `NotImplementedError`。

### 3.5 Spike 策略与回测引擎接口不兼容

- 引擎要求 `on_bar1s(bar)` / `on_kline(kline)`。
- Spike 包装器只实现 `on_event(event, engine)`。
- 策略内部依赖不存在的 `engine.virtual_clock` 和 `engine.executor.orders`。

### 3.6 下单幂等与失效撤单错误

- 引擎订单表以内部 `order_id` 为键，策略使用 `client_order_id` 查询，可能重复产生订单意图。
- 信号过期或触及失效价时只删除内存信号，没有撤销已经提交的未成交订单。

### 3.7 三档价格公式与文档目标不一致

当前公式的 ATR 系数为 `0.75、0.35、-0.05`，不是文档描述的 `0.75、1.15、1.55`。最终公式需要用户冻结后再修复。

## 4. P1：闭环缺失能力

| 区域 | 缺失能力 |
|---|---|
| 策略核心 | 与引擎、Redis、Binance 解耦的统一 Spike 核心 |
| Campaign | 全局互斥、逐币种状态、第一笔成交计时、恢复与终态 |
| 入场订单 | 固定总名义金额、三档幂等、部分成交、撤单竞态 |
| 执行恢复 | WAL、`SUBMIT_UNKNOWN` 查询确认、启动对账、迟到回报 |
| 持仓退出 | 保护单、止损、止盈、900 秒规则、盈利管理、最终结算 |
| 风控 | 单币、账户、保证金、杠杆、日亏损、数据延迟、紧急停止 |
| 监听池 | subcategory、低频发现扫描、监听租约、保护性监听 |
| 回测可信度 | 无未来数据、未完成 Kline 排除、部分成交、手续费、滑点、同秒顺序 |
| 审计 | 触发、预测、订单、成交、退出、费用与 PnL 全链路报告 |
| Web | subcategory 控制页面、权限、审计、并发修改 |
| 运维 | testnet/live 隔离、监控、告警、凭据、回滚、紧急平仓 |

## 5. 测试现状与缺口

已验证：

- `docker compose -f compose.test.yaml run --rm --build test`：`30 passed`
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

## 8. 建议实施顺序

1. 修复测试网端点、行情订阅刷新、combined stream 解包，并补回归测试。
2. 确认并冻结阶段 0 规则，建立固定事件案例。
3. 建立与运行环境无关的统一 Spike 核心和 Campaign 状态机。
4. 接通统一回测 runner，完成可信 replay 闭环。
5. 实现订单 WAL、幂等、对账、保护和账户级风控。
6. 完成 subcategory 控制和监听租约。
7. 通过 testnet 验收后再讨论 live 灰度。
