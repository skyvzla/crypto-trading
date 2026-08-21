# Factor Lift 第一轮研究（W1）：事件级逼空因子对照分析

> 状态：初步结论（in-sample）。样本仅覆盖 20 个高波动币 × 2026-02~07，
> 未做时间外推验证，不可直接用于实盘参数。

## 一、试验设置

- 数据：`data/market/candles` 1s 归档，20 个插针高发币（PIPPIN/FHE/BULLA/SIREN/RAVE/H/COAI/AGT/SKYAI/BLESS/PLAY/EVAA/ESPORTS/TA/AKE/UAI/TRADOOR/BEAT/BAS/LAB），2026-02-01 ~ 2026-07-31
- 执行方式：symbol × 7 天分块读取，事件级 dataset 常驻、秒级 frame 即用即弃；全程峰值内存 < 500MB，总耗时 284s
- 事件定义：`rise_5s ≥ 3% & volume_multiple_5s ≥ 3 & continuous_61s`，cooldown 60s 聚类 → **2235 个事件**
- 标签：做空视角 `short_mfe_30m / short_mae_30m`（entry = 事件秒 close）
- 方法：base rate 对照 + 分位 lift + 地形图 + 两因子规则组合 + 阈值敏感性 + 扣费期望（评审报告 docs/factor_research_review.md 五步法的第 1~3、5 步）

## 二、Base Rate（一切 lift 的对照基准）

| 指标 | 值 |
|---|---|
| 样本数 | 2235 |
| short_mfe_30m 中位 | **6.86%** |
| short_mfe_30m 均值 | 10.09% |
| success rate（MFE>0.2%） | 86.6% |

高波动小币在暴涨秒点做空，30m 内中位就有 ~7% 的有利回撤——**这是无条件 base rate，不是因子功劳**。评价任何因子必须看它相对这个基准的提升。

## 三、地形图（rise × volume_multiple → MFE 均值）

| rise\vol_mult | (5,10] | (10,20] | (20,50] | (50,+inf] |
|---|---:|---:|---:|---:|
| (0.05,0.08] | 13.0% | 11.4% | 8.0% | 7.7% |
| (0.08,0.12] | 24.5% | 8.0% | 2.9% | 7.3% |
| (0.12,0.2] | 21.9% | - | - | 14.0% |

**核心规律：温和爆量 + 急涨 → 回落大；极端爆量 → 几乎不回落。**
`volume_multiple_5s` 单因子 top lift 仅 0.56（bottom 1.49）——爆量最猛的事件做空 MFE 反而最低。与"爆量=顶部"直觉相反：极端放量更可能是逼空加速期，做空危险。

## 四、单因子 lift 排名（top 分位 vs base）

| 因子 | top lift | 方向 | 敏感性(0.6→0.9分位) | 结论 |
|---|---:|---|---|---|
| return_300s | 1.20 | + | 1.04→1.32 单调走强 | 5min 已大涨的事件回落更深 |
| return_30s | 1.20 | + | 1.09→1.31 单调走强 | 同上，短窗口速度 |
| price_acceleration_5s | 1.19 | + | 1.10→1.18 平稳 | 加速度稳健但弹性略低 |
| return_5s / velocity | 1.15 | + | monotonic=True | 触发强度本身 |
| taker_buy_ratio_60s | 0.66 | − | - | 买占比高 → 继续涨 |
| orderflow_exhaustion_5s_vs_60s | 0.76 | − | - | 衰竭不明显 → 继续涨 |
| volume_multiple_5s | 0.56 | − | bottom 1.49 | 极端爆量 → 不回落 |

方向解读：**利于做空的是"涨得急"，不利于做空的是"买压强/爆量猛"**。订单流衰竭因子在本样本里是反向指标（衰竭程度低 = 买方仍主导 = 继续涨），做反转信号需取其反面。

## 五、两因子规则组合（min_samples=15）

Top 组合全部由 `volume_multiple_5s Q1（温和量）` 驱动：

| 规则 | n | hit_rate | MFE均值 | lift |
|---|---:|---:|---:|---:|
| vol_mult Q1 & quote_vol_zscore Q3 | 42 | 100% | 17.1% | 1.56 |
| upper_wick Q3 & vol_mult Q1 | 205 | 99.5% | 16.6% | 1.51 |
| vol_mult Q1 & orderflow_exhaustion Q3 | 115 | 100% | 16.6% | 1.51 |

最优规则扣费期望（近似口径，fee+slip 0.1%）：expectancy ≈ **+16.7%**（p_win 按 MFE>成本计 100%，avg_loss(MAE 中位) 6.8%）。

## 六、必须泼的冷水（caveat）

1. **hit_rate≈100% ≠ 可盈利**：它只说明 30m 内出现过有利 tick；MAE 中位 6.8% 意味着止损带宽必须 >7%，否则先被打掉
2. **未计资金费率与深度滑点**：小币插针时段盘口极薄，实际 slippage 远高于 0.05%
3. **纯 in-sample**：无 train/test 时间切分，阈值敏感性只说明扰动稳健，不说明样本外成立
4. **样本偏置**：只选了插针高发币，代表"已知高波动 universe"内的条件分布
5. base rate 本身极高（中位 6.9%），lift 1.5 的绝对增量需要扣费后路径级回测确认

## 七、下一步（对应评审报告路线图）

- [ ] 时间外推：前 70% 时间选参，后 30% 只验证一次（embargo ≥ 1h）
- [ ] pre-event 数据集：波动率压缩/OI 积累等预警期因子（当前只有 TRIGGER 后）
- [ ] MAE 分布细化 → 止损带宽与仓位参数（MFE/MAE 联合分布，而非独立中位数）
- [ ] OI/funding 慢因子单独验证"选 universe"价值
- [ ] 路径级回测：挂单梯度 + 动态撤单，用真实成交假设替代 MFE 近似

## 复现

```bash
# 事件构建 + IC 报告 + lift 报告（CLI 已集成 --lift-report）
.venv/bin/python -m trading_platform.research.factor_lab.cli \
  data/market/candles/candles.duckdb --symbols <SYMBOLS> \
  --start 2026-02-01T00:00:00+00:00 --end 2026-07-31T00:00:00+00:00 \
  --rise-threshold 0.03 --volume-multiple-threshold 3.0 --lift-report

# 本轮脚本（分块直读 parquet，内存安全）
# /tmp/opencode/run_lift_study.py → reports/factor_lab_lift_test_top20.md
```
