# P1 阶段详细实施计划：衍生品数据接入 + Factor Lab 建设

## 1. P1目标

P0解决实时基础数据问题（1s成交聚合、buy/sell volume、CVD）。

P1的目标不是立即上线模型，而是建立一个可以回答以下问题的研究系统：

1. OI、Funding、多空比是否真的提升 spike 做空预测能力？
2. 哪些因子是独立信息，哪些只是重复指标？
3. 当前人工评分中的权重是否合理？
4. 哪些市场状态下插针反转概率最高？

最终产物：

```
market data
    ↓
feature dataset
    ↓
factor analysis
    ↓
research report
    ↓
strategy configuration
```

## 1.1 P1 数据约束：不再扩 1s 原始归档

P0 已经提供完整度足够的 1s 聚合订单流数据。由于当前磁盘容量不支持继续扩大
长期秒级数据，P1 明确采用“原始聚合冻结、派生因子按需计算”的方案。

现有 1s 数据可直接用于 P1：

```text
OHLCV + quote_volume
trade_count + raw_trade_count
taker buy/sell base volume
taker buy/sell quote volume
taker buy/sell raw trade count
taker buy/sell aggTrade count
方向最大 aggTrade quantity
trade / aggTrade ID 边界
```

其中：

- base volume 表示基础资产数量；
- quote volume 表示计价资产金额，USDT 合约中可理解为成交的 USDT 名义金额；
- CVD、quote CVD、buy ratio、imbalance、rolling CVD、Z-score 等全部在 Factor Lab
  运行时派生，不增加归档字段。

P1 也不默认落盘全量 factor frame。默认只生成事件级 Dataset 和小型统计报告；
是否保存事件集由研究命令显式决定。

---

# 2. 当前项目结构分析

当前已有：

```
market/
    行情输入

backtest/
    回放和验证

research/
    指标研究

strategies/spike/
    策略实现
```

当前 spike 已经具备：

- `entry_features.py`
  - upper_wick_ratio
  - volume_multiple
  - TD setup

- `shared_features.py`
  - 1s bar共享窗口
  - 5s上涨检测
  - 成交量窗口

- `scoring.py`
  - 倒U评分模型
  - 权重配置

当前缺失：

- 衍生品特征层
- 因子数据集
- 因子统计分析
- 因子相关性管理

---

# 3. 数据结构调整

## 3.1 增加 derivatives feature layer

建议新增：

```
src/trading_platform/research/derivatives/
```

职责：

只负责将原始衍生品数据转换为研究特征。

不要直接放入策略代码。

例如：

```
Open Interest
        ↓
oi_change_5m
oi_zscore
price_oi_divergence
```

---

# 4. 衍生品数据接入设计

## 4.1 Open Interest

数据源：Binance Futures。

当前周期：5m。

不需要强行变成1s。

保存：

```
timestamp
symbol
open_interest
```

派生：

## OI变化

```
oi_change_5m
```

## OI变化率

```
(oi_now - oi_prev) / oi_prev
```

## OI异常

```
current_change
/
historical_std
```

## Price-OI Divergence

重点因子：

```
price ↑ + OI ↑
```

代表上涨过程中新增杠杆。

---

## 4.2 Funding Rate

用途：慢周期环境因子。

不要用于秒级交易触发。

保存：

```
funding_rate
funding_percentile
```

计算历史分位：

```
当前funding在过去30天的位置
```

---

## 4.3 Long Short Ratio

用途：市场拥挤度。

保存：

```
long_short_ratio
ratio_percentile
```

注意：

不要直接：

```
多头高 => 做空
```

只能作为概率增强因子。

---

# 5. Factor Lab设计

## 5.1 新目录

新增：

```
src/trading_platform/research/factor_lab/
```

结构：

```
factor_lab/

dataset.py
event.py
labels.py
analysis.py
correlation.py
report.py

factors/
    price.py
    volume.py
    orderflow.py
    derivatives.py
    structure.py
```

---

# 6. 与现有 spike 模块关系

不要修改 spike 策略直接计算所有因子。

推荐：

```
research/factor_lab
        |
        |
        ↓
factor dataset
        |
        |
        ↓
strategies/spike
```

原因：

- 研究代码和交易代码分离
- 防止回测偷看未来
- 方便替换模型

---

# 7. Event Dataset扩展

P1需要建立正式事件表。

建议：

```
spike_events
```

字段：

```
event_id
symbol
event_time

price_before
price_peak
rise_percent

factor_snapshot

future_return_5m
future_return_15m
future_return_30m

mfe
mae
success
```

---

# 8. 因子分析流程

每个因子自动生成报告。

例如：

```
factor:
oi_change_5m

IC:
0.08

ICIR:
0.45

top quantile return:
2.1%

correlation:
volume_zscore 0.35
```

---

# 9. 替换当前 scoring 的规划

当前：

```
factor
 ↓
人工权重
 ↓
score
```

P1不删除。

新增：

```
factor
 ↓
statistical evaluation
 ↓
weight suggestion
 ↓
score config
```

最终仍可以保持可解释评分。

---

# 10. P1开发顺序

## Step 1

冻结并复用当前数据层，不新增 1s 原始字段。

已有 metrics 归档则直接复用其中 5m OI / Long Short Ratio；Funding 在没有现成
历史归档前不作为 P1 主流程的硬依赖，避免为了一个待验证因子先扩大数据存储。

---

## Step 2

实现 derivatives factors。

完成：

- oi_change
- oi_zscore
- price_oi_divergence
- funding_percentile

---

## Step 3

建立 event dataset。

复用已有：

- spike回测结果
- reports/research_record

---

## Step 4

建立 factor report。

输出：

- IC
- ICIR
- correlation
- 分位收益

---

# 11. 不建议P1做的事情

暂时不要：

- 引入深度学习
- 删除现有评分系统

---

# 12. 当前 P1 实现状态（2026-08-21）

已落地代码：

```text
src/trading_platform/research/factor_lab/
├── dataset.py        # 只读加载现有 1s 归档、构建事件 Dataset
├── event.py          # spike 事件检测与 cooldown 聚类
├── labels.py         # 做空视角 future return / MFE / MAE / success 标签
├── derivatives.py    # 复用现有 5m metrics，严格按 available_time 拼接
├── analysis.py       # IC / Spearman IC / 月度 ICIR / 分位收益
├── correlation.py    # 因子相关矩阵与高相关因子对
├── horizon.py        # 5m/15m/30m/1h Signal Horizon 与离散 Half-life
├── catalog.py        # 第一批因子分组与默认研究目录
├── report.py         # 轻量 Markdown 报告
├── workflow.py       # 事件级研究闭环
└── cli.py            # 分 symbol、分时间块执行研究
```

第一批 Factor Group：

```text
price
volume
structure
orderflow
derivatives
```

原始 CVD / quote CVD 属于 scale-sensitive 因子，默认不进入跨 symbol 的自动比较；
优先使用 `taker_buy_ratio`、`volume_imbalance`、`orderflow_exhaustion` 等归一化
因子。需要单 symbol 研究原始 CVD 时再显式开启。

## 12.1 默认标签

每个事件生成：

```text
short_return_5m / 15m / 30m / 1h
short_mfe_5m / 15m / 30m / 1h
short_mae_5m / 15m / 30m / 1h
success
```

所有收益均按做空仓位相对入场价计算，例如：

```text
short MFE = (entry - future_min) / entry
short MAE = (future_max - entry) / entry
```

Factor 只能读取事件时点及以前的数据，未来窗口只允许由 label 阶段读取。

## 12.2 Signal Horizon

Factor Lab 会额外比较同一因子对：

```text
short_return_5m
short_return_15m
short_return_30m
short_return_1h
```

的 Spearman IC，并记录：

```text
peak_horizon_seconds
signal_half_life_seconds
```

Half-life 是当前离散研究周期上的近似值，不应当解释成精确到秒的物理半衰期。
它用于决定某个因子更适合执行层、短 Alpha 还是较慢环境层。

## 12.3 大数据执行方式

禁止一次把“全市场 × 数月 1s Factor Frame”全部加载到内存。

CLI 默认：

```text
一个 symbol
    ↓
24 小时时间块
    ↓
只读 1s 原始归档
    ↓
临时 Factor Frame
    ↓
事件级 Dataset
    ↓
释放秒级数据
```

最终只合并事件级 Dataset 做全市场统计，因此不会生成新的全量 1s factor archive。

## 12.4 使用方式

默认只向 stdout 输出报告，不保存 Dataset：

```bash
python -m trading_platform.research.factor_lab.cli \
  data/market/candles/candles.duckdb \
  --symbols BTCUSDT,ETHUSDT \
  --start 2026-07-01T00:00:00+00:00 \
  --end 2026-08-01T00:00:00+00:00
```

如果已有 metrics archive，可增加：

```text
--metrics-catalog data/market/metrics/metrics.duckdb
```

只有明确需要保留事件级研究样本时才使用：

```text
--dataset-out reports/factor_events.parquet
```

这不是全量秒级 Factor Frame，体积应远小于原始 1s 数据。

## 12.5 当前 P1 不覆盖

- 不新增 Funding 历史归档；有稳定现成数据源后再进入增量价值验证。
- 不训练 Logistic / XGBoost；先完成统计筛因子和去冗余。
- 不自动修改 `strategies/spike/scoring.py` 权重。
- 不把研究因子写入实时策略路径，避免研究实现影响线上执行。
- 加入大量技术指标
- 保存全部trade tick历史

先建立统计闭环。

---

# 12. 完成标准

P1完成后应该能够回答：

1. OI增加是否提高顶部预测？
2. CVD背离是否比MACD有效？
3. 哪些因子互相重复？
4. 当前评分权重是否合理？
5. 哪些市场环境最容易出现成功short？

达到以上结果后，再进入P2模型阶段。
