# Spike V2 指标过滤研究候选

状态：仅离线研究候选；不得修改线上 Spike 默认策略或启用 live 交易。

## 候选参数

在以下固定 v2 基线上，仅保留：

```text
strategy = trading_platform.strategies.spike.v2_1:V21
max_oi_change_pct = 0
max_ls_ratio = 1.5
entry_tier_mode = tier3-only
exit_policy = candidate-v1
total_notional = 1000
warmup_hours = 168
prior_high_lookback_hours = 6
rise_low_lookback_hours = 168
min_rise_duration_hours = 24
profit_unlock_percent = 1.5
chunk_hours = 4320
```

`profit_unlock_percent=1.5` 是本研究基线的一部分，不能误用 `V21` 声明的默认值 `3`。

## 已有证据

全周期参数矩阵 `reports/spike-v2-metrics-sweep-available-time` 覆盖
2025-08-01 至 2026-08-01、92 个交易对、552 个任务、13 workers。未过滤基线为
93 笔、胜率 72.04%、净收益 3349.768U、PF 3.083；`LS=1.5, OI=0` 为
73 笔、胜率 75.34%、净收益 3888.235U、PF 11.058。

时间切分 `reports/spike-v2-metrics-time-robustness-2026h1` 覆盖
2026-02-01 至 2026-08-01、92 个交易对、368 个任务、13 workers：

| OI 上限 | LS 上限 | 交易 | 胜率 | 净收益 U | PF | 总亏损 U |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 0 | 59 | 71.19% | 2265.342 | 4.418 | 662.852 |
| 15 | 0 | 59 | 71.19% | 2265.342 | 4.418 | 662.852 |
| 0 | 1.5 | 47 | 72.34% | 2298.361 | 8.467 | 307.800 |
| 15 | 1.5 | 47 | 72.34% | 2298.361 | 8.467 | 307.800 |

在该切分的 89 个基线信号中，17 个最近可见的 5m 指标快照满足 `LS > 1.5`，与
LS 组缺失的信号集合一致；所有信号的 OI 5m 变化均不超过 9.21456%，所以 OI=15
没有产生过滤。故 `max_oi_change_pct=15` 不纳入候选。

## 结论边界

- LS=1.5 的 H1 改善由移除 12 笔已成交交易形成，净移除 -33.018U；其中
  `1000000BOBUSDT` 的 -297.854U 占避免亏损的 83.9%。剔除该单后，LS 相对基线少赚
  264.835U。因此它是降低单一极端亏损暴露的研究候选，不是已证实的稳健收益或爆仓过滤。
- H1 区间已参与过参数探索，不是独立盲测；没有 LS 阈值敏感性曲线，也没有组合资金竞争或
  内部 K 线缺口的完整验证。
- 当时的两个历史报告运行在拒绝信号审计修复之前，`all_signals.csv` 只含
  `signal_triggered`，并未持久化 OI/LS 拒绝原因。上述 17 条结论由归档指标按同一
  `available_time` 规则复算，不能把旧 CSV 误读为逐条拒绝审计。

## 后续门禁

后续回测会在所有核心入场条件通过后记录 `signal_rejected`，保留连阳、OI、LS 和
涨幅/成交量上限的实际阈值与原始指标。升级该研究候选前，必须完成：

1. 在未参与选参的时间段重放，并核对 `all_signals.csv` 中的逐条拒绝审计。
2. 对指标和 K 线内部缺口做完整性检查，并给出组合级资金竞争与风险统计。
3. 处理 live runtime 的 `metrics_5m` 数据供给能力并完成独立执行验证。

当前 live runtime 会拒绝需要 `metrics_5m` 的策略，且 live 模式本身仍受退出策略校准
门禁保护；本文件不构成任何上线授权。
