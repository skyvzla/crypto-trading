# Spike V2 退出候选合集（深回撤保护 + 静态强弱分桶）

状态：两个方向均已全量回测验证通过，登记为 D-027 replay/testnet 候选；
**不改写 `V21` 默认值或线上配置**，启用时经 `run_spike_short`/sweep 参数显式传入。

## 候选一：深回撤保护（profit_drawdown_peak_ratio + profit_drawdown_ratio）

基线之上新增两个退出候选参数（`exit_policy=candidate-v1`）：

```text
profit_drawdown_peak_ratio = 0.20   # 峰值浮盈达到 20% 后粘滞 arm 回撤保护
profit_drawdown_ratio      = 0.10   # 从持仓峰值（1m close）反弹 10% 即止盈
```

其余固定基线：

```text
strategy = trading_platform.strategies.spike.v2_1:V21
total_notional = 1000
exit_policy = candidate-v1
warmup_hours = 168
entry_tier_mode = tier3-only
prior_high_lookback_hours = 6
rise_low_lookback_hours = 168
min_rise_duration_hours = 24
profit_unlock_percent = 1.5
max_oi_change_pct = 0
max_ls_ratio = 1.5
max_consecutive_up_minutes = 4
group_rise_12h_threshold = 1.0
strong_tier_atr_shift = 0.2
exit_strict_age_ms = 900000
```

## 机制（实现要点）

- **粘滞 arm**：持仓期峰值浮盈（1m close 最低价相对 entry）达到 20% 后，
  `_candidate_drawdown_armed` 永久置位，价格回撤不解除
- **1m close 粒度**：峰值与回撤均基于持仓内已完成的 1m K 线收盘价
  （`_candidate_peak_1m_price`），不能用 1s mark 价——1s 盘中波动会让
  保护在 0~2min 内过早触发（首版 smoke 全线跑输的根因）
- **与弱化时间解耦**：只触发 `profit_drawdown` 止盈（reason=
  `candidate_profit_drawdown_exit`），不改变 momentum 分档 required
  （维持 3/2/1），不受 `profit_unlock_ratio` 弱化时间副作用影响
- TOML 0 哨兵 = 关闭（run 层 0 → None → 默认关闭）
- 代码：`exit_policy.py`（`CandidateV1Config.profit_drawdown_peak_ratio`）、
  `short.py`（`_candidate_peak_1m_price`/`_candidate_drawdown_armed`）、
  `v2_1.py`、`run_spike_short.py`（`--profit-drawdown-peak-ratio`）、
  `sweep.py`
- 单测：`tests/strategies/spike/test_spike_candidate_strategy.py`（arm 后
  触发/需先 arm 再回撤/不弱化动量分档）

## 已有证据（全量 92 币，828 runs）

`experiments/spike-v2-grouped-exit-deep-drawdown-full.toml` →
`reports/spike-v2-grouped-exit-deep-drawdown-full`，13 workers：

| 组合 | 净收益 U | 笔数 | 胜率 | Δ净 |
| ---: | ---: | ---: | ---: | ---: |
| 基线 | 4146.20 | 70 | 77.1% | 0 |
| **peak20/dd10** | **4673.50** | **70** | **77.1%** | **+527.30** |
| peak20/dd12 | 4647.20 | 70 | 77.1% | +501.00 |
| 无前置 dd10 | 3027.60 | 70 | 72.9% | -1118.60 |

逐笔确认仅 2 笔退出行为改变（其余 68 笔完全不动）：
- AKEUSDT：15.0min/89.0U → 5.2min/303.1U（Δ+214.1，基线反弹 48% 才走）
- BANKUSDT：15.0min/95.1U → 6.3min/408.4U（Δ+313.2，基线反弹 70% 才走）

## 阈值边界（已验证范围）

- **peak 上限 ≈ 38%**：AKE 峰值浮盈 38.3%，peak>38% 会丢 AKE（-214U）；
  峰值 ≥20% 的 8 笔中仅 AKE/BANK 反弹 ≥10%，其余（XNY 63%、BSB 45%、
  COAI 37%、FHE 29%、MMT 24%、ARC 23%）峰值后反弹 <6%，不会误伤
- **dd 下限 = 10%**：dd=6% 多砍 XNY(-70U)/FHE(-20U)，dd=8% 多砍
  BSB(-221U)/ARC(-10U)，均明显跑输 dd10（离线模拟）；dd10 时 BSB 恰好
  无损（Δ-0.1）
- 离线模拟（`scripts/simulate_spike_deep_drawdown_exit.py`）与真实回测
  高度一致：预测 Δ+528.1 vs 实测 +527.3

## 限制与门禁

- 仅覆盖 2025-08-01 至 2026-08-01 全周期 70 笔，命中仅 2 笔——样本很小，
  replay/testnet 验证时若触发笔收益与回测不符，需重新评估
- 保护只在"峰值浮盈 ≥20% 且 1m close 反弹 ≥10%"时介入；若 testnet 出现
  反弹 50%+ 才退出的极端单但峰值 <20%，本规则不覆盖
- 候选依赖完整 `profit_unlock_percent=1.5` 研究基线，不能改写 `V21` 默认值
## 候选二：静态强弱分桶（strong_bucket_strict_age_ms + weak_bucket_strict_age_ms）

入场按信号快照 `rise_from_12h_low >= group_rise_12h_threshold(1.0)` 定强弱桶
（持仓期不变），静态分档退出时间；与动态 decay 分档（strong/weak_strict_age_ms）
解耦——动态方案每秒重评 `decay_agreement`，44% 笔存在 2+ 次强弱来回切换
（平均 1.02 次/10min），退出时机不稳定；静态桶无此问题。

```text
group_rise_12h_threshold    = 1.0
strong_bucket_strict_age_ms = 1500000   # 强桶 25min
weak_bucket_strict_age_ms   = 600000    # 弱桶 10min
```

### 机制（实现要点）

- 信号层 `_entry_bucket(rise_from_12h_low)` 定桶，on_fill 时写入
  `_candidate_entry_bucket`；live 恢复经 Redis lease `entry_bucket` 字段
- `candidate_v1_risks(entry_bucket=...)` 静态分档优先于动态 decay 分档，
  同一 `strict_age` 同时作用于 time_risk 与 momentum required
- TOML 0 哨兵 = 关闭（0 → None → 默认 strict_age_ms）
- 代码：`exit_policy.py`、`short.py`、`v2_1.py`、`campaign_store.py`、
  `live.py`、`run_spike_short.py`、`sweep.py`
- 单测：`tests/strategies/spike/test_spike_exit_policy.py`（静态桶
  strong/weak/覆盖动态/关闭）、`test_spike_candidate_strategy.py`（强桶
  延后/弱桶提前）

### 已有证据（全量 92 币，368 runs）

`experiments/spike-v2-grouped-exit-bucket-full.toml` →
`reports/spike-v2-grouped-exit-bucket-full`：

| 强桶 | 弱桶 | 净收益 U | 胜率 | Δ净 |
| ---: | ---: | ---: | ---: | ---: |
| 15min(基线) | 15min(基线) | 4146.20 | 77.1% | 0 |
| 25min | 15min | 4347.25 | 77.1% | +201.05 |
| 15min | 10min | 4238.52 | 74.3% | +92.32 |
| **25min** | **10min** | **4439.57** | 74.3% | **+293.37** |

逐笔 25 笔变化与第 4 节合成值完全吻合：强桶 5 笔 +200.9（BULLA +138.8、
BSB +64.0、ESPORTS +9.3；MYX -11.2）；弱桶 20 笔 +92.5（BANK +170.2、
FF +47.1、AKE 三笔 +35.7；LAB -135.8、ALCH -35.4、PRL -29.0 等）。
超过全桶静态 25min（+214.54），为当前最优退出候选。

### 限制与门禁

- 弱桶 10min 把 LABUSDT 在 600s 深亏 -136.9 时砍仓，基线 1062s 回本到
  -1.1（-135.8 最大单项损失）；replay/testnet 验证时需关注同类深亏单
- 弱桶 10min 降低胜率（77.1% → 74.3%），净收益提升来自少数大单
  （BANK/FF/AKE），样本敏感，需 testnet 持续观察
- 与候选一（深回撤保护）可叠加：peak20/dd10 已证实只动 AKE/BANK 两笔，
  两者机制独立（回撤保护看浮盈，分桶看时间），叠加需重新全量验证
