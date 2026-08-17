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
| `trading_platform.strategies.spike.v1_1:V11` | v1 实验基线，固定 4h 前高，支持前高偏差、5 秒涨幅、连阳、OI 和多空比组合 |

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

### V2 指标研究候选

OI / 多空比矩阵已完成全周期与时间切分复核。`max_ls_ratio=1.5` 仅保留为 v2 的离线研究候选，`max_oi_change_pct=15` 未通过时间切分的有效性检查，不纳入候选。候选依赖完整的 `profit_unlock_percent=1.5` 研究基线，不能改写 `V21` 的默认值或线上配置；详细证据、限制和升级门禁见 [Spike V2 指标过滤研究候选](research/SPIKE_V2_METRICS_LS_1_5_CANDIDATE.md)。

### V2 深回撤保护候选

`profit_drawdown_peak_ratio=0.20` + `profit_drawdown_ratio=0.10`（1m close
粒度，粘滞 arm，峰值浮盈 ≥20% 后从峰值反弹 ≥10% 止盈）：全量 92 币验证
+527.3U，胜率 77.1% 不变，仅 2 笔命中（AKE/BANK 反弹 50%+ 才走的极端单）。
登记为 D-027 replay/testnet 候选，不改写 `V21` 默认值或线上配置；详细证据
见 [Spike V2 深回撤保护研究候选](research/SPIKE_V2_DEEP_DRAWDOWN_CANDIDATE.md)。

### V2 静态强弱分桶候选

入场按 `rise_from_12h_low >= 1.0` 静态定强弱桶，`strong_bucket_strict_age_ms`
（强桶 25min）+ `weak_bucket_strict_age_ms`（弱桶 10min）：全量 92 币验证
+293.4U，超过全桶静态 25min（+214.5）；与每秒重评的动态 decay 分档
（44% 笔强弱来回切换）解耦，无抖动。登记为 D-027 replay/testnet 候选，
不改写 `V21` 默认值；详细证据见
[Spike V2 退出候选合集](research/SPIKE_V2_DEEP_DRAWDOWN_CANDIDATE.md)。

## 可选实验维度

## v1.1 实验配置

`v1.1` 保留 v1 的三档挂单、12 小时低点和基础上涨信号，固定前高窗口为 4h，并复用已有 `candidate-v1` 退出状态机。该状态机从 90 秒开始检查动能与时间风险，在 90/300/900 秒节点逐步收严，并在下跌动能衰减或趋势突破时退出。实验矩阵比较 5 秒涨幅 3%/5%、前高偏差 0%/5%、连阳上限关闭/3 根、OI 上限关闭/15%、多空比上限关闭/1.5，共 32 个组合。

`reports/spike-v1-1-anomaly-sweep` 是误用 `exit_policy=confirmed` 生成的历史报告，未经过 `candidate-v1` 退出路径，不得用作 v1.1 最终收益对比；重跑时必须核对报告 `parameters.exit_policy` 为 `candidate-v1`。

可直接使用 [spike_v1_1_anomaly_sweep.toml](/data/projects/quant/trading_platform/docs/spike_v1_1_anomaly_sweep.toml) 运行。universe 先读取 15m 异常上影线报告，再与 PostgreSQL 有效交易对、禁用列表和归档完整性取交集。

以下参数可用于 v1 或 v2，不构成独立策略版本；必须在报告名称和参数中明确记录。

| 参数 | 含义 | 当前状态 |
|---|---|---|
| `max_consecutive_up_minutes` | 信号前已完成的 1m K 线连续收阳根数上限；0 表示关闭 | v2 专项 sweep 已完成；不应直接迁移为 v1 结论 |
| `max_oi_change_pct` | 信号时刻 OI 相较上一 5m 快照的涨幅上限；0 表示关闭 | 15% 未通过时间切分有效性检查，不作为候选 |
| `max_ls_ratio` | 信号时刻全市场多空比上限；0 表示关闭 | 1.5 仅保留为离线研究候选，见上节门禁 |
| `entry_tier_mode` | `three-tier` 或 `tier3-only` | v1 / v2 的核心差异之一 |
| `prior_high_lookback_hours` | 入场前高窗口 | v1 参数扫描已覆盖 0/4/6/8/12/24h |
| `rise_low_lookback_hours` 与 `min_rise_duration_hours` | 近期低点窗口及距信号最短时间 | v2 固定为 168h / 24h |
| `profit_unlock_percent` | 浮盈达到阈值后解除首 90 秒风险保护 | v2 为 1.5，v2.1 已测试 3 |

## 命名和复核约定

- 基线报告名称应包含版本，例如 `spike-v1-*`、`spike-v2-*`。
- 添加或修改任一可选实验参数时，应使用独立输出目录，不能覆盖基线报告。
- 比较两份报告前，先核对 `parameters`、交易对集合、时间范围和数据归档版本。
- “信号子集”只比较 `(symbol, signal_time)`；收益比较还必须同时核对挂单、成交和退出参数。
