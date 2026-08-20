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

新增衍生品数据存储。

完成：

- OI
- Funding
- Long Short Ratio

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
