# Spike 策略版本说明

本文记录 Spike 做空策略的可复现版本基线。策略实现通过模块路径注入，版本名称只是展示标签；回测报告中的 `strategy_path`、完整参数和 Git 提交共同构成复现事实。

## 策略模块装配

回测入口接受 `module:attribute` 形式的完整策略声明，例如：

```bash
uv run spike-backtest --strategy \
  trading_platform.strategies.spike.v2_1:V21 ...
```

策略声明包含完整生命周期：入场、挂单、持仓管理、止盈止损和退出；共享的事件回放、订单撮合、费用和报告仍由 base/engine 提供。策略还声明 `market_timeframes`、`execution_timeframe` 和指标需求。引擎只读取并投递声明的数据，因此纯 `1m/5m` 策略不需要加载 1s 行情。

当前模块：

| 声明 | 用途 |
|---|---|
| `trading_platform.strategies.spike.v1:V1` | v1 三档、4h 前高、无 OI/连阳过滤 |
| `trading_platform.strategies.spike.v2:V2` | 改动前冻结的 v2，7天低点上涨24h、第三档全仓、无 OI/连阳过滤 |
| `trading_platform.strategies.spike.v2_1:V21` | 当前保存的 v2.1，在 v2 基础上增加连阳/OI/多空比规则 |

新增策略只需新增声明模块并实现生命周期方法，不修改旧策略模块；报告中的 `strategy_path` 用于恢复对应实现。

## 共用基础逻辑

v1 和 v2 均使用 `DynamicSpikeShortStrategy` 的同一套基础信号：

- 5 秒涨幅、成交量倍数和 12 小时低点涨幅筛选。
- 短期回调后重新突破的信号形态。
- ATR 计算三档候选价格。
- 最低档必须高于指定窗口内的前高、起涨点下限和触发价。
- `candidate-v1` 退出策略。

`strategy_version` 仅保留为报告展示字段，实际策略切换使用 `strategy_path`，不再依赖公共类中的版本分支。

## 已完成基线

| 项目 | v1：6h 前高基线 | v2：固定参数基线 |
|---|---|---|
| 代表报告 | `reports/spike-july-parameter-sweep`，6h 行 | `reports/spike-v2-parameter-sweep` |
| 期间 | 2025-08-01 至 2026-08-01 | 2025-08-01 至 2026-08-01 |
| 交易对 | 494 个可回测标的 | v1 6h 结果中实际有交易的 92 个标的 |
| 前高窗口 | 6h | 6h |
| 7 天低点至信号最短时间 | 不限制 | 至少 24h |
| 预热 | 16h | 168h（满足 7 天低点窗口） |
| 挂单方式 | 三档，名义金额按 30% / 40% / 30% 分配 | 仅第三档，100% 名义金额 |
| 盈利解锁首 90 秒风险保护 | 不启用 | 浮盈严格大于 1.5% 后解除 |
| 退出策略 | `candidate-v1` | `candidate-v1` |
| 连阳、OI、多空比过滤 | 均未启用 | 均未启用 |

因此，v2 的信号集合是 v1 6h 基线的子集：v1 有 140 笔，v2 有 93 笔，没有 v2 独有信号。v2 额外的 7 天低点上涨持续时间条件过滤了 47 笔。

同一信号的损益不应直接横向相减：v2 改成第三档全仓，并可能因盈利解锁改变退出时点，所以它不是“v1 删除 47 笔”后的同一成交结果。

## v2.1 已运行变体

`reports/spike-v2.1-parameter-sweep` 使用和 v2 相同的信号、挂单及退出基线，仅将 `profit_unlock_percent` 从 1.5 改为 3。该报告当前结果与 v2 相同，但仍应以各自报告的 `parameters` 字段作为判断依据。

工作区的 `experiments/spike_sweep_v2.example.toml` 是下一次运行的示例配置，不自动代表既有 v2 或 v2.1 报告的参数。

## 可选实验维度

以下参数可用于 v1 或 v2，不构成独立策略版本；必须在报告名称和参数中明确记录。

| 参数 | 含义 | 当前状态 |
|---|---|---|
| `max_consecutive_up_minutes` | 信号前已完成的 1m K 线连续收阳根数上限；0 表示关闭 | v2 专项 sweep 已完成；不应直接迁移为 v1 结论 |
| `max_oi_change_pct` | 信号时刻 OI 相较上一 5m 快照的涨幅上限；0 表示关闭 | 指标 sweep 进行中 |
| `max_ls_ratio` | 信号时刻全市场多空比上限；0 表示关闭 | 指标 sweep 进行中 |
| `entry_tier_mode` | `three-tier` 或 `tier3-only` | v1 / v2 的核心差异之一 |
| `prior_high_lookback_hours` | 入场前高窗口 | v1 参数扫描已覆盖 0/4/6/8/12/24h |
| `rise_low_lookback_hours` 与 `min_rise_duration_hours` | 近期低点窗口及距信号最短时间 | v2 固定为 168h / 24h |
| `profit_unlock_percent` | 浮盈达到阈值后解除首 90 秒风险保护 | v2 为 1.5，v2.1 已测试 3 |

## 命名和复核约定

- 基线报告名称应包含版本，例如 `spike-v1-*`、`spike-v2-*`。
- 添加或修改任一可选实验参数时，应使用独立输出目录，不能覆盖基线报告。
- 比较两份报告前，先核对 `parameters`、交易对集合、时间范围和数据归档版本。
- “信号子集”只比较 `(symbol, signal_time)`；收益比较还必须同时核对挂单、成交和退出参数。
