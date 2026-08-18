# Spike V2 退出时间止损（strict_age）研究演变记录

状态：离线研究汇总。含已证伪方案与未实测候选；**均未接入线上策略**。
基准参数（`max_ls_ratio=1.5`、`max_oi_change_pct=0`、`tier3-only`、
`strong_tier_atr_shift=0.2`、`group_rise_12h_threshold=1.0`）之外的任何退出
时间改动都是候选，不得修改线上 Spike 默认策略。

## 1. 基线（研究起点）

固定 v2 基线（70 笔、全周期 2025-08-01 至 2026-08-01、92 交易对、13 workers）：

```text
strategy = trading_platform.strategies.spike.v2_1:V21
exit_policy = candidate-v1
exit_strict_age_ms = 900000        # 15min，本研究的唯一自变量起点
total_notional = 1000
warmup_hours = 168
max_consecutive_up_minutes = 4
profit_unlock_percent = 1.5
max_oi_change_pct = 0
max_ls_ratio = 1.5
entry_tier_mode = tier3-only
group_rise_12h_threshold = 1.0
strong_tier_atr_shift = 0.2
```

| 指标 | 值 |
| ---: | ---: |
| 交易 | 70 |
| 胜率 | 77.1% |
| 净收益 | 4146.20U |
| 总盈利 | 4511.53U |
| 总亏损 | 365.33U |

`exit_strict_age_ms` 同时控制两件事（关键机制）：time_risk 止损时间，以及
动量分档第三档（agreement≥1 生效）的起始时间——分档时间右移会让动量退出
要求更高、持有更久。

## 2. 研究问题（三问）

对基线 70 笔逐一复盘（1m K 线后续走势）：

1. **短持仓是否垃圾单？** 否。<3min 的 18 笔全是 trend_exit 快进快出
   （+238U），单均收益小但非垃圾；15min+ 的 37 笔贡献 +2405.52U，是动量
   退出主力。
2. **盈利单是否退得太慢？** 部分真实但覆盖率低：48 笔盈利单退出价相对
   持仓最低价的回撤 P50 4.29%，回撤>10% 仅 3 笔（BANK 47.5%、AKE 36.5%、
   BSB 17.5%）。**不为个案套规则。**
3. **time_risk 亏损单退出后是否继续跌？** 9/9 继续下跌，24h 再跌>3% 占
   8/9（FF -36.75%、LAB -32.08%、XPIN -25.19%）。但反转发生在几小时后，
   短窗口（30/45min）救不了——预告了 grace 方案失败。

## 3. 时间网格实验

### 3.1 子集网格（17 币，119 runs）

`configs/spike-v2-grouped-exit-time-grid.toml` → `reports/spike-v2-grouped-exit-time-grid`

| strict_age | Δ净（子集） |
| ---: | ---: |
| 10min | +127.70 |
| 12min | +70.16 |
| 15min | 0（基线） |
| 17min | +148.10 |
| 20min | -31.03 |
| 25min | +340.20 |
| 30min | +132.79 |

### 3.2 全量网格（92 币，368 runs）

`configs/spike-v2-grouped-exit-time-full.toml` → `reports/spike-v2-grouped-exit-time-full`

| strict_age | 净收益 U | Δ净 |
| ---: | ---: | ---: |
| 15min | 4146.20 | 0 |
| 20min | 4115.94 | -30.26 |
| **25min** | **4360.74** | **+214.54** |
| 30min | 3956.60 | -189.60 |

**结论：全桶 25min 最优（+214.54）**；30min 过度持仓亏损扩大。

### 3.3 早期 8 臂与单档对照（已收录，勿混淆）

`reports/spike-v2-grouped-exit-time`（736 runs，含修复 super() 传参 bug 后重跑）：
- 分档 10min（flat=0, strict=600000）= 4200.85U；分档 5min -93.33、3min -841.89
- 单档（flat 恒定 = strict）全线 -2659 ~ -3004U——单档破坏 100% 胜率动量段
- **单档 10min = 1457.63U（与分档 10min 是不同臂，分析勿混淆）**

## 4. 分桶合成分析（静态标签）

按入场时 `rise_from_12h_low >= 1.0`（强桶）分桶：强桶 7 笔、弱桶 63 笔。

15min→25min 逐笔 diff：改善 18 笔（强 3 笔 +212.17：BULLA +138.79、BSB +64.03、
ESPORTS +9.34；弱 15 笔 +375.62：AKE +105.10、FHE +73.25、BANANAS31 +37.56、
VELVET +35.42、FF +34.14、ESP +26.79）；恶化 15 笔（强 1 笔 -11.12：MYX；
弱 14 笔 -362.13：PRL -122.27、BUSDT -34.71、DUSK -31.83、SYN -27.13、
UAI -25.83、ALCH -25.00、LAB -24.92 等）。

合成（逐笔拼接两臂数据，非实测）：

| 方案 | Δ净（合成） |
| ---: | ---: |
| 强桶25min + 弱桶15min | +201.05 |
| **强桶25min + 弱桶10min** | **+293.37**（4439.57U） |

分桶并未简单优于全桶 25min（强25弱15 合成 +201.05 < +214.54），但强桶 25min
（+201.05）+ 弱桶 10min（+92.32）组合超过全桶——分桶价值在于强弱各自发挥。

## 5. grace 方案：实现并证伪

浮亏宽限（`time_risk_grace_ms` + `time_risk_grace_loss_ratio`，1%/2% 阈值）：
time_risk 到点后若浮亏小于阈值则继续持有一段时间。全链路实现
（exit_policy/short/v2_1/sweep/run_spike_short/单测，Decimal(str()) 防浮点噪声）。

17 币子集 136 runs（`reports/spike-v2-grouped-exit-grace-subset`）：

| 臂 | Δ净 |
| ---: | ---: |
| 1% 宽限 | -17.91 |
| 2% 宽限 | -19.74 |

唯一正面 BANANAS31 +37.56；LAB/ALCH/DUSK 变差；FF/XPIN/ESP 浮亏超阈值不受
影响。**方案放弃**（代码保留，默认 0 关闭）——与三问结论一致：time_risk 单
退出后 1h 内仍在跌，反转在几小时后，短宽限窗口救不了。

## 6. 动态强弱分型（按持仓实时动量切换）

### 6.1 设计

用户思路：强弱桶不按入场静态标签，持仓中动态评估作为退出信号。论证：
`rise_from_12h_low` 是 12h 慢变量（持仓期几乎不变，动态重评无意义）→ 改用
**decay_agreement（快变量）**：alive（decay_agreement≥1）→ strong 分档时间；
衰竭（0/None）→ weak 分档时间；**每个评估 tick 重评**，不一次性确定。

实现要点（`exit_policy.py`、`short.py`、`v2_1.py`、`sweep.py`、`run_spike_short.py`）：
- `CandidateV1Config` 新增 `strong_strict_age_ms`/`weak_strict_age_ms`
  （None → 回退 `strict_age_ms`）
- `candidate_v1_risks` 内联动量分档（原 `momentum_agreement_required` 基于
  config.strict_age_ms，无法感知动态值）：
  - alive：5min 内 required=3，直到 strong 时间 required=2，之后 required=1
  - 衰竭：5min 内 required=3，直到 weak 时间 required=2，之后 required=1
- TOML 不支持 null → 0 哨兵（sweep/run 层 0→None→默认 900000）
- 修复：首版只动态化 time_risk、未动态化动量分档 → 13 币 smoke 强参数无差异
  （-30.93/-40.37 仅 weak 生效）→ 内联分档后生效

### 6.2 子集 smoke（13 币，156 runs）

`configs/spike-v2-grouped-exit-dynamic-smoke.toml` → `reports/spike-v2-grouped-exit-dynamic-smoke`

| 组合 | Δ净 |
| ---: | ---: |
| 强25min/弱10min | +153.29 |
| 强25min/弱12min | +143.85 |
| **强25min/弱15min** | **+184.22** |
| 强30min/弱10min | +110.69 |
| 强30min/弱15min | +141.62 |

动态模式下弱 10min 反而差：动态"弱"只作用于动量已衰竭的单，10min 对它们
砍太早；而静态分桶的"弱桶 10min"是整体早砍，机制不同。

### 6.3 全量验证（92 币，552 runs）

`configs/spike-v2-grouped-exit-dynamic-full.toml` → `reports/spike-v2-grouped-exit-dynamic-full`

| 组合 | 净收益 U | 胜率 | Δ净 |
| ---: | ---: | ---: | ---: |
| 基线 15/15 | 4146.20 | 77.1% | 0 |
| 强15/弱10 | 4079.35 | 74.3% | -66.85 |
| **强25/弱15** | **4293.26** | 74.3% | **+147.06** |
| 强25/弱10 | 4198.95 | 71.4% | +52.75 |

动态方案未跑赢全桶静态 25min（+147.06 < +214.54）：动态把"动量已衰竭"的单
从 25min 提前到 15min 砍，全量里这些单 15min 砍反而少赚约 67U（FF/ESP/
BANANAS 等在 25min 时继续改善）。

## 7. 结论与候选排名（全量实测口径）

| 方案 | Δ净 | 状态 |
| ---: | ---: | ---: |
| **静态分桶强25/弱10** | **+293.37** | **已实测（368 runs，逐笔吻合）** |
| 全桶静态 25min | +214.54 | 已实测 |
| 动态强25/弱15 | +147.06 | 已实测 |

### 7.1 静态分桶实测（2026-08，92 币 368 runs）

`configs/spike-v2-grouped-exit-bucket-full.toml` → `reports/spike-v2-grouped-exit-bucket-full`：

| 强桶 | 弱桶 | 净收益 U | 胜率 | Δ净 |
| ---: | ---: | ---: | ---: | ---: |
| 15min(基线) | 15min(基线) | 4146.20 | 77.1% | 0 |
| 25min | 15min | 4347.25 | 77.1% | +201.05 |
| 15min | 10min | 4238.52 | 74.3% | +92.32 |
| **25min** | **10min** | **4439.57** | 74.3% | **+293.37** |

实测与第 4 节合成值逐笔完全吻合（25 笔变化：强桶 5 笔 +200.9、弱桶 20 笔
+92.5）。实现：信号层 `_entry_bucket` 按入场 `rise_from_12h_low >=
group_rise_12h_threshold` 定桶（快照不变），传入 `candidate_v1_risks`
静态分档（`strong_bucket_strict_age_ms`/`weak_bucket_strict_age_ms`），
与动态 decay 分档解耦（无 1s 抖动问题）。

主要变化笔：BULLAUSDT +138.8、BANKUSDT +170.2、BSBUSDT +64.0、FFUSDT +47.1；
最大隐患 LABUSDT -135.8（弱桶 10min 在 600s 深亏 -136.9 时砍仓，基线 1062s
回本到 -1.1）。

## 8. 代码与复现

- 代码：`src/trading_platform/strategies/spike/exit_policy.py`
  （`CandidateV1Config`/`candidate_v1_risks`）、`short.py`、`v2_1.py`、
  `src/trading_platform/backtest/sweep.py`、`run_spike_short.py`
- 单测：`tests/strategies/spike/test_spike_exit_policy.py`（grace + 动态
  strict_age 用例）、`tests/strategies/spike/test_spike_short_strategy.py`、
  `tests/backtest/test_spike_sweep_symbol.py`
- 复现命令（全量）：`rm -rf <output> && nohup env PYTHONPATH=src python3
  -m trading_platform.backtest.sweep --config <toml> --workers 13 > /tmp/log 2>&1 &`
- 配置：`configs/spike-v2-grouped-exit-time-full.toml`（全桶网格）、
  `configs/spike-v2-grouped-exit-dynamic-full.toml`（动态）、
  `configs/spike-v2-grouped-exit-bucket-full.toml`（静态分桶，2026-08）、
  `configs/spike-v2-grouped-exit-time-smoke.toml`/`-dynamic-smoke.toml`/`-bucket-smoke.toml`（子集）、
  `configs/spike-v2-grouped-exit-grace-subset.toml`（已弃）
- 报告：`reports/spike-v2-grouped-exit-time-full/`、`-dynamic-full/`、
  `-bucket-full/`、`-bucket-smoke/`、`-time-grid/`、`-dynamic-smoke/`、
  `-grace-subset/`、`spike-v2-grouped-exit-time/`

## 9. 补充复盘：盈利单"下跌见底后反弹才走"占比（时间限制视角）

**问题**：用户假设盈利单下跌盈利没及时退出、反弹后才走，可能与退出时间
限制（`strict_age_ms` 分档）有关。离线逐笔复盘基线 70 笔（1m K 线、只读
DuckDB），脚本 `scripts/review_spike_bounce_exit.py`，产物
`reports/spike-v2-bounce-exit-review.csv`。

**口径**：对每笔取持仓期 `[entry_time, exit_time)` 1m K 线，定位持仓最低价
（SHORT 最大盈利点）及出现时刻；计算退出价相对最低价的反弹幅度；在最低点
时刻模拟 `candidate_v1_risks` 动量分档条件（<5min required=3、5-15min
required=2、>=15min required=1）。

**结果**：

| 指标 | 值 |
| ---: | ---: |
| 反弹后才走（exit_price > 持仓最低价） | 69/70 (98.6%)，盈利单 53/54 (98.1%) |
| 反弹幅度 P25/P50/P75 | 3.57% / 5.61% / 8.81% |
| 反弹 >2% / >5% | 62 (88.6%) / 39 (55.7%) |
| 最低点出现在 <15min 时间窗内 | 64/69 (92.8%)（<5min 45、5-15min 19） |
| **时间限制直接原因**（最低点在 <15min 且 1<=decay<required） | **25/69 (36.2%)** |
| 低点时刻无衰减信号（decay<1，非时间限制） | 34/69 (49.3%) |
| 低点时刻动量已达标但未退（执行/阻塞类） | 10/69 (14.5%) |

**解读**：
1. **"反弹后才走"是常态而非异常**：98.6% 的退出价都高于持仓最低价，
   因为最低点是事后才知、持仓期唯一的极值点；真正的问题是反弹幅度
   （P50 5.61%，>2% 占 88.6%）。
2. **时间限制可解释约 1/3（36.2%）**：这 25 笔最低点出现在 15min 分档
   窗口内且当时 decay_agreement 已有 1~2（未达 required=2/3），被时间
   门槛拦下，等反弹后才触发出。合计净收益 1329.8U。
3. **约一半（49.3%）不是时间限制**：最低点时刻 decay<1，动量衰减信号
   本身未出现（价格仍在急跌），退出信号是反弹后由指标自然点亮——这类
   属于"信号确认延迟"，时间分档缩短也救不了（与第 3 节三问结论一致）。
4. **与 25min 全桶最优不矛盾**：被时间门槛拦下的等待并不全是坏事——
   全量 25min（required=2 窗口更长）反而 +214.54U，说明过早砍在
   "事后低点"是追跌陷阱；时间分档的真正作用是防止在最低点附近砍仓，
   代价是反弹后才走（~5.6% 中位反弹）。
- 数据口径：只读 DuckDB 历史归档，不联网、不回写、不读 Redis 最新数据；
  回测禁止归档器运行

## 10. 盈利解锁弱化时间（profit_unlock）+ 峰值回撤保护（profit_drawdown）

用户思路：盈利达一定百分比（5%/10%/20%）后弱化时间限制，尽早锁定利润。
离线模拟（`scripts/simulate_spike_profit_drawdown_exit.py`，
`reports/spike-v2-momentum-drawdown-exit-simulation.csv`）：
- R1 纯峰值回撤止盈：低阈值（5%/10%）砍掉后续趋势，全线跑输
- R2 回撤+动能：回撤确认延迟退出，也跑输
- **R3 弱化时间（浮盈达阈值后动量分档直接 required=1）**：5% +201U、
  20% +229U，与全桶 25min (+214.54U) 相当，是唯一稳定正面

实现（`exit_policy.py`/`short.py`/`v2_1.py`/`run_spike_short.py`/`sweep.py`）：
- `CandidateV1Config` 新增 `profit_unlock_ratio`（浮盈达峰值比例后
  required=1 立即生效）与 `profit_drawdown_ratio`（解锁后从持仓峰值回撤
  该比例立即止盈，reason=`profit_drawdown`）
- **粘滞解锁**：`_candidate_profit_unlocked` 由策略层用持仓峰值价格维护，
  曾达到阈值后价格回撤不解除（首版实现用"当前 net_pnl/notional"即时判断，
  回撤后解锁失效，13 币 smoke 全部跑输——修复为粘滞后重跑）
- TOML 0 哨兵关闭；单测覆盖解锁分档跳过/正收益要求/回撤退出优先

### 10.1 13 币 smoke 结果（粘滞版，468 runs 完成）

`configs/spike-v2-grouped-exit-profit-unlock-smoke.toml`：36 组合笛卡尔积
（unlock ∈ {0,0.05,0.10,0.20,0.05,0.20}，dd ∈ {0,0,0,0,0.005,0.01}，0=关闭），
13 币；汇总先按 `(unlock,dd,symbol)` 取均值再按组合求和（直接 sum 会因
重复 run 虚高）。基线（0/0）：**1580.39U / 22 笔 / 胜率 38.6%**。

| unlock | dd | 净收益U | Δ | 备注 |
|---|---|---|---|---|
| 0.05 | 0 | 505.98 | -1074.41 | 胜率升至 62.8%，但砍掉大单 |
| 0.10 | 0 | 760.82 | -819.57 | |
| 0.20 | 0 | 888.49 | -691.90 | 最接近基线，仍大幅跑输 |
| 0 | 0.005 | -88.89 | -1669.28 | 纯回撤止盈不可行 |
| 0 | 0.010 | -123.91 | -1704.30 | |
| 0.05 | 0.005 | 294.52 | -1285.87 | 组合更差 |
| 0.10 | 0.005 | 377.39 | -1203.00 | |
| 0.20 | 0.005 | 505.71 | -1074.68 | |

**所有 11 个实验臂全部跑输基线**。逐笔对比（按 symbol+entry_time 匹配，
跨重复 run 取均值）显示原因一致：基线 >50U 的 9 笔大盈利单（平均 +199.3U）
在解锁后持仓从 13.5min 缩到 2.8~5.8min、收益降到 57~119U，大单损失
-1283/-1062/-722U（unlock 0.05/0.10/0.20），小单改善仅 +30~+242U 无法弥补。
解锁后 required=1 且 decay>=1 即触发 momentum_risk，大趋势单的衰减信号在
下跌中途频繁点亮，导致过早退出。**策略的盈利来源正是大趋势单的长持仓，
弱化时间限制会系统性砍掉这部分利润**——与离线模拟 R3 的正收益相反。

模拟失真原因：离线模拟以 1m close 为退出触发粒度、未建模 90s min_risk_age、
origin 检查与执行时序，且在全量 70 笔上评估（smoke 仅 13 币 22 笔子集）；
真实回测以持仓峰值 mark price 跟踪，解锁触发更早、更频繁。

**结论：profit_unlock 方向证伪，不推进全量回测。** 与第 8 节结论互相印证
（25min 全桶最优 = 更长持仓更好）；时间限制不是大单退出的瓶颈，动量分档
required=1 在 15min 后已让动量单及时退出。

### 10.2 深回撤保护（解耦版，profit_drawdown_peak_ratio）——smoke 跑赢基线

用户反馈"动能衰减也不一定对，但确实有几笔回撤比较厉害"。复盘确认
（第 9 节）：19 笔从持仓最低点反弹 >8% 才退出，合计贡献 1042.6U，
其中 BANKUSDT（反弹 90% 才走）、AKEUSDT（反弹 57%）、BSBUSDT 等最极端。

离线模拟（`scripts/simulate_spike_deep_drawdown_exit.py`，含浮盈前置条件）
在修正汇总口径后：**浮盈峰值≥20% 且从峰值反弹≥10% 止盈 = Δ+528.1U**，
是唯一显著正收益（触发 BANK +313.6、AKE +214.5、BSB -0.1 三笔）；
低浮盈阈值（5%/10%）与低回撤阈值（≤5%）全部跑输。教训：早期模拟
`simulate_spike_profit_drawdown_exit.py` 未设浮盈前置，价格从 entry 上涨
即算"回撤"，导致 R1/R2 虚低；且未触发笔必须沿用实际 pnl，不能当 0。

实现（解耦版，与弱化时间完全独立）：
- `CandidateV1Config` 新增 `profit_drawdown_peak_ratio`：峰值浮盈达到该
  比例后**粘滞 arm** 回撤保护（`_candidate_drawdown_armed`），不改变
  momentum 分档（required 维持原 3/2/1），彻底绕开 unlock 的副作用
- **峰值与回撤必须用 1m close 粒度**（持仓内已完成 1m K 线的收盘价，
  `_candidate_peak_1m_price`），不能用 1s mark——首版用 mark 粒度时
  smoke 全部跑输（盘中 1s 波动让保护过早触发，AKEUSDT 0.0min 就退出）；
  改用 1m close 后与离线模拟口径一致，才复现正收益
- 单测：arm 后回撤触发/需先 arm 再回撤/不弱化动量分档

13 币 smoke（`configs/spike-v2-grouped-exit-deep-drawdown-smoke.toml`，
325 runs，peak=0.20 + dd∈{0.08,0.10,0.12,0.15} 及纯 dd 对照）：

| peak | dd | 净收益U | vs 基线 |
|---|---|---|---|
| 0 | 0 | 1580.4 | 基线 |
| 0 | 0.08 | 860.3 | -720.1 |
| 0 | 0.10 | 749.5 | -830.9 |
| 0 | 0.12 | 704.3 | -876.1 |
| 0 | 0.15 | 774.3 | -806.1 |
| 0.20 | 0.08 | 1602.4 | +22.0 |
| **0.20** | **0.10** | **1794.5** | **+214.1** |
| 0.20 | 0.12 | 1794.5 | +214.1 |
| 0.20 | 0.15 | 1726.9 | +146.5 |

**peak=20% dd=10%/12% 跑赢基线 +214.1U**，与离线模拟方向一致（模拟全量
Δ+528.1 含子集外 BANKUSDT 的 +313.6）。逐笔：仅 AKEUSDT 一笔变化，
15.0min/89.0U（momentum_exit，反弹 57% 才走）→ 5.2min/303.1U
（profit_drawdown_exit），其余 21 笔完全不动。纯 dd（无浮盈前置）仍全输，
证明浮盈前置是必要条件。

### 10.3 全量 92 币验证（828 runs 完成）——确认跑赢基线

`configs/spike-v2-grouped-exit-deep-drawdown-full.toml`（anomaly-report
universe，92 币，3 组合 = 基线 + peak20%/dd10% + peak20%/dd12%）：

| peak | dd | 净收益U | 笔数 | 胜率 | vs 基线 |
|---|---|---|---|---|---|
| 0 | 0 | 4146.2 | 70 | 77.1% | 基线 |
| 0 | 0.10 | 3027.6 | 70 | 72.9% | -1118.6 |
| 0 | 0.12 | 3018.8 | 70 | 71.4% | -1127.4 |
| **0.20** | **0.10** | **4673.5** | **70** | **77.1%** | **+527.3** |
| 0.20 | 0.12 | 4647.2 | 70 | 77.1% | +501.0 |

**peak=20% dd=10% 全量净收益 4673.5U（Δ+527.3U），胜率 77.1% 不变。**
逐笔确认仅 2 笔退出行为改变（其余 68 笔完全不动）：
- AKEUSDT：15.0min/89.0U → 5.2min/303.1U（Δ+214.1，反弹 57% 才走的单）
- BANKUSDT：15.0min/95.1U → 6.3min/408.4U（Δ+313.2，反弹 90% 才走的单）

与离线模拟预测（+528.1U；AKE +214.5、BANK +313.6）几乎完全吻合，
1m close 口径的模拟与真实回测一致性得到验证。纯 dd（无浮盈前置）全量仍
大幅跑输，浮盈前置是必要条件。

**结论：深回撤保护（peak=20% + dd=10%，1m close 粒度）确认有效，
全量 +527.3U（+12.7%），只影响极端深回撤单，不影响其余持仓。**
代价：若峰值浮盈≥20% 后出现 ≥10% 的 1m close 反弹，会提前止盈——这两类
单在样本内都是"反弹 50%+ 才走"的极端案例，规则对它们属于纯改善。
是否采纳为候选参数由用户决定（D-027 候选阈值，非生产默认）。