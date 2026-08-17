# Spike 信号统计指标主表

> 用途：对每笔 spike 做空信号计算统一的指标矩阵，用于集中分析"信号触发过早 / 箱体突破有效性 / 追顶风险"等维度。
> 粒度：1m 原始数据聚合出 5m / 15m / 1h。短仓信号以 5m 为主，15m 辅助，1m 用于触发时刻微观结构。
> 目标：所有币种、任意时间范围的信号都可套用本表计算，输出信号 × 指标矩阵。

---

## 一、系统已研究的指标（源码已有审计字段）

来源：`src/trading_platform/strategies/spike/short.py` 中 `SpikeSignal`、`EntryContextFeatures` 审计字段，以及回测 trades.csv 输出列。

### 1.1 触发信号原始特征（策略内置）

| 指标 | 字段 | 计算方法 |
|---|---|---|
| 触发价 | `trigger_price` | 信号触发时价格 |
| 5 秒涨幅 | `rise_5s` | (当前价 − 5s 前价) / 5s 前价 |
| 5 秒量能倍数 | `volume_multiple_5s` | 最近 5s 成交量 / 过去 1s 中位成交量 |
| 5 分钟量能倍数 | `volume_multiple_5m` | 当前 5m 成交量 / 参考期均量 |
| spike 高点 | `spike_high` | 触发前 spike 序列最高价（回溯 30 分钟） |
| spike 高点时间 | `spike_high_time` | spike_high 对应时间 |
| 起涨低点 | `rise_low` | 起涨段最低价 |
| 起涨低点时间 | `rise_low_time` | rise_low 对应时间 |
| 起涨低点年龄 | `rise_low_age_minutes` | 从 rise_low 到信号经过的分钟数 |
| ATR | `atr` | 5m ATR(14) |
| 12h 低点 | `low_12h` | 过去 12 小时最低价 |
| 12h 低点涨幅 | `rise_from_12h_low` | 触发价相对 12h 低点涨幅 |
| 前高 | `prior_high` | 4h 前高过滤窗口中的前高 |
| TD Sell 5m | `td_sell_setup_5m` | 5m TD Sell Setup 计数（0~9） |
| TD Sell 15m | `td_sell_setup_15m` | 15m TD Sell Setup 计数 |
| 上影线比 5m | `upper_wick_ratio_5m` | (high − max(open,close)) / (high − low) |
| 上影线比 15m | `upper_wick_ratio_15m` | 同 5m，15m 粒度 |

### 1.2 箱体 / 通道突破审计（本系列研究新增）

| 指标 | 字段 | 计算方法 |
|---|---|---|
| 3d 箱体上沿 | `box_upper_3d` | 信号前 3d（含上涨段）log(high) 线性回归 +1.5σ（通道态）或 Winsorized P90（横盘态） |
| 7d 箱体上沿 | `box_upper_7d` | 同上，7d 窗口 |
| 3d 箱体下沿 | `box_lower_3d` | 信号前 3d 回归 −1.5σ / Winsorized P10 |
| 7d 箱体下沿 | `box_lower_7d` | 同上，7d 窗口 |
| 突破线 | `box_breakthrough` | 3d/7d 上沿的均值（当前实现） |
| 突破下沿 | `box_break_lower` | 3d/7d 下沿均值 |
| 突破起点 | `box_break_first_time` | 最近一次收盘跌破突破线后重新站上的时间 |
| 突破分钟数 | `box_break_minutes` | 从突破起点到信号的分钟数 |
| 突破小时数 | `box_break_hours` | box_break_minutes / 60 |

### 1.3 过早触发过滤审计（本系列研究新增）

| 指标 | 字段 | 计算方法 |
|---|---|---|
| 前 30m 均价偏离 | `spike_avg_deviation_pct` | (触发价 / 信号前 30 分钟均价 − 1) × 100 |
| 前 60m 价格极差 | `spike_range_pct` | (信号前 60m max(high) − min(low)) / min(low) × 100 |

### 1.4 退出 / 结果（回测 trades.csv）

| 指标 | 字段 |
|---|---|
| 盈亏 | `net_pnl` |
| 退出原因 | `exit_reason`（time_risk / momentum / trend / profit_drawdown 等） |
| 最大不利回撤 | `max_adverse_return` |
| 盈亏标签 | `winner`（net_pnl > 0） |

---

## 二、箱体上沿多方法对比（研究专用，含上涨 / 剔涨双口径）

> 上沿判定横盘/通道：|slope| ≥ 30 bps/bar 视为通道，否则横盘。
> 横盘用分位数，通道用回归 ±1.5σ；两种口径都算，便于横向对比。

| 指标 | 计算方法 |
|---|---|
| `up_p90` | 窗口 high 的 90 分位 |
| `up_p95` | 窗口 high 的 95 分位 |
| `up_p99` | 窗口 high 的 99 分位 |
| `up_win` | 窗口 high 的 Winsorized(5%) 均值 |
| `up_reg15` | log(high) 线性回归终值 +1.5σ |
| `dn_p10` | 窗口 low 的 10 分位 |
| `dn_win` | 窗口 low 的 Winsorized(5%) 均值 |
| `dn_reg15` | log(low) 线性回归终值 −1.5σ |
| `slope_bps` | log(high) 对时间线性回归斜率 × 10⁴（bps/bar） |
| `r2` | 上述回归拟合优度 R² |
| `tail_share` | 窗口后 1/4 段涨幅 / 全程涨幅（>0 持续新高，<0 后段回落） |
| `chan_type` | 上升 / 下跌 / 震荡 分类（按 slope 阈值） |

窗口口径：
- **含上涨**：`[信号前 N 天, 信号时刻)`，N ∈ {3d, 7d}
- **剔涨（震荡窗口）**：先用含上涨 3d 粗算 P90 找突破起点 `brk_start`（最后收盘 < 粗算上沿的 bar），再用 `[brk_start − 3d, brk_start)` 重算

| 突破特征 | 计算方法 |
|---|---|
| `orig_p90_3d` | 含上涨 3d 粗算 P90（第一步） |
| `brk_start_utc` | 突破起点时刻 |
| `dur_h` | (信号时刻 − brk_start) / 3600s |
| `trig_over_up` | 触发价 / 震荡窗口上沿（站上幅度倍数） |

---

## 三、主流量化指标（1m 聚合，5m 主 / 15m 辅 / 1h 参考）

> 所有指标在信号时刻取值；价格序列取 close，周期用 5m（部分 15m/1h 标注）。

### 3.1 动量 / 趋势

| 指标 | 周期 | 计算方法 |
|---|---|---|
| `rsi` | 14, 5m / 15m / 1h | RSI = 100 − 100/(1 + RS)，RS = 平均涨幅 / 平均跌幅（Wilder 平滑） |
| `macd_hist` | 12/26/9 | MACD = EMA12 − EMA26，hist = MACD − DEA(EMA9 of MACD) |
| `roc` | 5m / 15m | ROC = (close / close[n]前 − 1) × 100，n = 周期数 |
| `adx` | 14, 15m | DX 的 Wilder 平滑均值，DX = \|+DI − −DI\| / (+DI + −DI) × 100 |
| `cci` | 20, 5m | CCI = (TP − SMA(TP)) / (0.015 × 平均绝对偏差)，TP = (H+L+C)/3 |
| `sto_k` / `sto_d` | 14/3, 5m | %K = (C − LL14)/(HH14 − LL14)×100，%D = SMA3(%K) |
| `ema_ratio` | 20, 5m / 15m | close / EMA20（>1 多头排列） |
| `wma_slope` | 20, 5m | WMA20 的一阶差分斜率 |

### 3.2 波动率

| 指标 | 周期 | 计算方法 |
|---|---|---|
| `atr_ratio` | 14, 5m | ATR(14) / close（单位波动） |
| `real_vol` | 30m / 60m | 对数收益标准差 × √252（年化，可标注） |
| `bb_width` | 20, 5m | (上轨 − 下轨) / 中轨 = 4σ / SMA20 |
| `parkinson_vol` | 1h | √(1/(4ln2) × mean(ln(High/Low)²)) × √252 |
| `vol_std_30` / `vol_std_60` | 30m / 60m | 分钟收益标准差（未年化，供相对比较） |

### 3.3 成交量 / 资金流

| 指标 | 周期 | 计算方法 |
|---|---|---|
| `obv_slope` | 5m | OBV = Σ(上涨日 +量 / 下跌日 −量)，取最近 20 根回归斜率 |
| `vwap_dev` | 当日 | (触发价 − VWAP) / VWAP × 100，VWAP = Σ(价×量)/Σ量 |
| `vol_cv` | 1h | 1m 成交量的变异系数（σ/μ） |
| `up_down_vol_ratio` | 5m | Σ上涨bar量 / Σ下跌bar量 |
| `vol_zscore` | 5m | (当前 5m 量 − 1h 均量) / 1h 量标准差 |
| `turnover_1h` | 1h | Σ(close × volume)（成交额） |

### 3.4 蜡烛形态

| 指标 | 周期 | 计算方法 |
|---|---|---|
| `upper_wick_ratio` | 5m / 15m | (high − max(open,close)) / (high − low)（已有） |
| `body_ratio` | 5m | \|close − open\| / (high − low)（实体占比） |
| `consecutive_green` | 5m | 触发前连续阳线根数 |
| `green_share` | 1h | 1h 内阳线根数占比 |
| `marubozu` | 5m | 实体占比 ≥ 0.8 且上下影线均极小（光头光脚） |
| `engulf_pull` | 5m | 当前 K 线实体吞没前一阳线实体 |

### 3.5 拉升形态（策略特有方向）

| 指标 | 周期 | 计算方法 |
|---|---|---|
| `accel_5m` | 5m | 最近 5m 涨幅 − 前 5m 涨幅（加速 / 减速） |
| `pulse_1m` | 1m | 触发前最大单分钟涨幅 |
| `spike_age` | - | (信号时刻 − spike_high_time) / 60s |
| `retrace_from_spike` | - | (触发价 / spike_high − 1) × 100（负为已回撤） |

---

## 四、目标币种筛选（另一方法，本文档仅记录占位）

> 说明：本文档聚焦"信号触发后的指标计算"。**如何筛选目标币种**（流动性、波动率分层、上市时长等）是独立的方法，不在此展开。
> 占位字段（可后续补充）：
> - 1h 平均成交额分层
> - 价格量级（0.001 / 1 / 10 档）
> - 1m 流动性 / 买卖价差
> - 上市时长、市值分层

---

## 五、实现与落地

- 数据源：`data/market/candles/candles.duckdb`（1m 全量，按 `archive_index.parquet` 索引分区查询）
- 聚合：1m → 5m / 15m / 1h（open/high/low/close/volume 标准聚合）
- 输出：信号 × 指标矩阵 CSV（`reports/spike-v2.2-signal-analysis/`）
- 复用：`src/trading_platform/strategies/spike/short.py` 已实现 ATR、TD、箱体、偏离/极差；主流指标（RSI/MACD/BOLL 等）待落地

## 六、待确认清单

- [ ] 主流指标是否全部落地计算（约 40+），还是先选 10 个高区分度子集
- [ ] 指标在信号时刻的"回看窗口"统一为多少（5m/15m/1h 是否全算）
- [ ] 是否做成可复用脚本（任意 symbol + 时间范围 → 指标矩阵），供全市场统计