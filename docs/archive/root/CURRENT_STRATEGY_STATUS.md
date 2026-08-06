# 当前策略状态与开发交接

> 更新日期：2026-08-06  
> 项目目录：`/data/projects/quant/spike_trading_platform`  
> 当前结论：实验策略具有继续验证价值，新平台尚未形成可运行的回测或实盘闭环。

## 1. 文档范围与平台定位

本项目首先是一个**通用量化交易平台**。行情、数据存储、执行、风控、账本、回测和 Web 管理采用通用边界；数据层设计得较宽泛是有意的架构选择，不代表所有数据能力都由逼空策略直接需要。

但当前初期范围只有逼空插针策略。开发、测试、回测和上线验收都必须优先完成这一条策略闭环，不并行开发 K 线策略或其他策略功能，也不为未来复用提前增加抽象。平台总体设计以 `PLATFORM_ARCHITECTURE.md` 为准；未来其他策略可以复用平台中性的基础设施，但不属于当前阶段。

## 2. 当前首个策略目标

当前首个策略实现独立的 1s 级逼空插针做空流程：

1. 持续监听 aggTrade，并形成可审计的 1s/tick 事件。
2. 识别加速前起涨点、短时涨速和成交量放大。
3. 当前价格相对起涨点上涨至少 10% 后，才允许生成挂单计划。
4. 使用动能、波动率、近期极值和可选清算地图预测极端价格。
5. 生成多档限价做空订单，管理部分成交、撤单、重挂和失效。
6. 对已形成的仓位执行止损、止盈、超时和形态退出。
7. 实时与历史回放使用同一个策略核心和状态机。

策略规格以 `SPIKE_STRATEGY_SPEC.md` 和 `docs/spike_trader/` 为准。`SPIKE_STRATEGY_ARCHITECTURE.md` 是历史草案，仅用于追溯早期决策。

## 3. 两套策略实现必须区分

### 3.1 已产生基线结果的实验策略

2026 年 7 月的 100 标的批量回测实际使用以下脚本：

- `scripts/backtest_dynamic_spike.py`：1s 触发、动态预测价格、三档挂单和触达判断。
- `scripts/append_dynamic_spike_pnl.py`：实验性退出和收益计算。
- `scripts/batch_backtest_dynamic_spike.py`：批量调度。
- `scripts/summarize_dynamic_spike.py`：结果汇总。

这些脚本是当前结果的事实基线，但仍是研究工具，不是生产策略核心。退出规则、手续费和成交模型均需重新冻结和验证。

### 3.2 新平台中的迁移版本

`trading_platform/strategies/spike_short.py` 是另一份迁移实现。目前不能替代上述实验脚本，也不能沿用其回测结论，原因包括：

- 策略包装器接口与当前回测引擎不兼容。
- 实盘 tick 入口仍运行示例策略，没有加载插针策略。
- 缺少完整退出、保护单和实盘订单状态机。
- 起涨点、10% 硬条件和三档价格公式与实验模型存在偏差。
- 没有清算地图、混合估价和滚动重挂。
- 策略核心依赖回测引擎对象，尚未做到实时与回放共用。

因此，开发时应先冻结统一策略核心，不能继续分别维护“回测策略”和“实盘策略”。

## 4. 现有回测基线

数据范围：2026 年 7 月，100 个活跃非主流 USDT 永续标的。

| 指标 | 结果 |
|---|---:|
| 总触发数 | 717 |
| 允许挂单 | 141 |
| 至少一档成交 | 50 |
| 盈利 / 亏损 | 37 / 13 |
| 成交后胜率 | 74% |
| 归一化 Profit Factor | 10.6193 |
| 归一化平均盈亏比 | 3.7311:1 |
| 每笔等权平均收益率 | 4.77739% |
| 100U 固定计划名义金额收益 | 约 +123.19U |

主要限制：

- AKEUSDT 贡献约 84% 的总净收益，收益集中度很高。
- 当前成交模型不能证明真实限价单一定成交。
- 尚未完整模拟部分成交、盘口深度、下单延迟、滑点、撤单失败、资金费率和保证金。
- 1s 数据只保存有成交的秒，低流动性标的存在时间戳不连续问题。
- 实验脚本可能使用触发时尚未完成的 1m K 线信息，不能宣称已经消除未来函数。

基线文件：

- `reports/dynamic_final_run1/report.md`
- `reports/dynamic_final_run1/summary.json`
- `reports/dynamic_final_run1/all_trigger_orders_with_pnl.csv`
- `reports/dynamic_final_run1/batch_manifest.json`
- `reports/active_alt100_final.csv`

## 5. 历史数据位置

历史行情没有随代码移动，目前仍位于：

`/data/projects/quant/crypto/data/market/history.duckdb`

已知覆盖包括2026年7月100个标的的 1s、1m、5m、15m 和1h数据。1s表约9942万行，只包含有成交的秒。

新项目不应硬编码该路径。后续应通过配置指定外部数据目录；决定数据完全归属新项目后，再单独迁移或建立独立数据卷。

## 6. 当前平台完成度

已经存在的骨架：

- Binance 行情和账户协议客户端。
- aggTrade 到 1s Bar 的聚合器。
- Redis Pub/Sub 与 K线 latest 存储。
- PostgreSQL 订单、成交、持仓和控制表。
- tick/kline 策略基类。
- 虚拟时钟回测引擎和简化限价成交器。
- Docker Compose 部署结构。

尚未形成闭环的关键能力：

1. combined stream 消息解包、真实自动重连和数据断档恢复。
2. 插针策略的实时入口和启动历史预热。
3. 无引擎依赖的统一策略核心与逐币种 campaign 状态机。
4. 多档订单幂等、部分成交、撤单竞态、SUBMIT_UNKNOWN 和启动对账。
5. 交易所托管止损、止盈、超时退出和仓位生命周期。
6. 单币、账户、保证金、日亏损、数据延迟和紧急停止风控。
7. 无未来数据的逐事件回测及更可信的成交模型。
8. 完整触发、预测、订单、成交、退出和盈亏审计报告。

## 7. 已确认的高优先级缺陷

- `trading_platform/backtest/run_spike_short.py` 使用了错误的回测引擎构造参数。
- `DynamicSpikeBacktestStrategy` 没有实现引擎要求的 `on_bar1s/on_kline` 接口。
- 策略查询订单使用了不存在的 `engine.executor.orders`。
- 策略按 `client_order_id` 查询以内部订单 ID 为键的订单表，存在重复下单风险。
- 三档 ATR 公式实际得到 `0.75、0.35、-0.05`，与文档的 `0.75、1.15、1.55` 不一致。
- 触及无效价时只移除信号，没有确保撤销已经提交的挂单。
- 回测结束只是关闭仓位记录，没有按市场价格结算未平仓盈亏。
- 行情 API 重复注册相同路由，订阅成功后可能没有刷新 WebSocket。
- 1s 聚合器一次关闭多个窗口时只返回一根，其余窗口可能丢失。
- 实盘入口只启动示例 BTC/ETH 策略。

现有测试通过不代表这些路径可运行。多数集成测试只验证导入，策略测试也没有覆盖完整触发和交易生命周期。

## 8. 推荐开发顺序

### 阶段一：冻结策略事实标准

1. 用 AKEUSDT 关键事件逐字段对比实验脚本和目标规则。
2. 冻结起涨点算法、10% 硬条件、触发指标、预测价格、三档规则和失效条件。
3. 冻结退出、手续费、延迟和成交模型。
4. 为配置和策略规则建立明确版本号。

### 阶段二：建立统一策略核心

1. 策略核心只接收标准事件、时钟和账户/订单状态。
2. 输出订单意图，不直接访问回测引擎、Redis、数据库或 Binance。
3. 每个币种维护独立 campaign 状态机。
4. 实时适配器和回放适配器调用同一核心。

### 阶段三：先建立可信回测闭环

1. 修复行情时间语义和数据质量检查。
2. 按事件可用时间回放，禁止读取未完成 K 线。
3. 覆盖触发、挂单、部分成交、撤单、退出和盈亏完整测试。
4. 用原100币数据重新运行，并与旧基线逐笔解释差异。

### 阶段四：执行和实盘

1. 实现 WAL、幂等 clientOrderId、订单状态机和启动对账。
2. 实现交易所托管保护单和账户级风控。
3. 纸盘实时运行并与相同事件回放结果比对。
4. 测试网验证后，使用独立 API Key 或子账户小额灰度。

## 9. 开发入口

Docker 环境验证（宿主机不安装项目依赖）：

```bash
cd /data/projects/quant/spike_trading_platform
docker compose -f compose.test.yaml build
docker compose -f compose.test.yaml run --rm test
```

Dockerfile 默认使用 PyPI 并缓存依赖下载，构建时可用 `--build-arg PYPI_INDEX_URL=...` 覆盖。

当前不应直接启动实盘策略。开始开发前，先阅读：

1. `CURRENT_STRATEGY_STATUS.md`
2. `SPIKE_STRATEGY_SPEC.md`
3. `docs/spike_trader/decisions.md`
4. `docs/spike_trader/architecture/strategy.md`
5. `docs/spike_trader/architecture/execution-ledger-web.md`
6. `docs/spike_trader/phases/README.md`
