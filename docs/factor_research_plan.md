# 插针逼空策略因子研究体系改造计划

## 1. 目标

将当前基于 K 线指标和规则评分的 spike 策略，升级为事件驱动因子研究体系。

目标不是预测下一根 K 线，而是预测：

- 极端上涨后未来 15m/30m/60m 是否产生有效回撤
- 当前上涨是否属于不可持续的逼空事件

核心模型：

```
市场数据
  ↓
事件检测
  ↓
因子计算
  ↓
因子评价(IC/ICIR)
  ↓
概率模型
  ↓
执行策略
```

---

## 2. 当前项目基础

已有：

- spike 策略模块
- backtest 框架
- research 指标库
- 1s 数据引擎
- 5m/15m K线特征

已有特征：

- upper wick ratio
- volume multiple
- TD setup
- 基础技术指标

---

## 3. 第一阶段：完善 1s Feature Engine

当前 1s 数据需要增加成交方向统计。

新增字段：

### 成交统计

- trade_count
- total_volume
- buy_volume
- sell_volume
- buy_ratio
- sell_ratio
- avg_trade_size

### CVD

新增：

```
CVD = cumulative(buy_volume - sell_volume)
```

派生：

- cvd_change_1m
- cvd_change_5m
- price_cvd_divergence

用途：检测价格创新高但主动买入衰竭。

---

## 4. 衍生品数据接入

增加：

### Open Interest

保存：

- open_interest
- oi_change_5m
- oi_change_percent
- oi_zscore

重点分析：

```
价格上涨 + OI增加
```

是否代表杠杆追涨。

### Funding

保存：

- funding_rate
- funding_percentile

### 多空比

保存：

- long_short_ratio
- percentile_rank

作为慢因子，不作为秒级触发。

---

## 5. 建立 Factor Lab

新增研究模块：

```
research/factor_lab/

dataset.py
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

## 6. 建立事件数据集

不要保存全部行情训练。

建立 spike event dataset。

事件条件示例：

- 1m 涨幅超过阈值
- 5m 涨幅超过阈值
- ATR 异常突破

保存窗口：

事件前：

-30m
-15m
-5m

事件后：

5m
15m
30m
60m

标签：

- future return
- maximum favorable excursion
- reversal success/fail

---

## 7. 因子分类

### 极端上涨

- return_1m
- return_5m
- return_15m
- velocity
- acceleration
- ATR multiple

### 成交异常

- volume zscore
- volume multiple
- trade count change

### 订单流衰竭

- CVD
- CVD divergence
- buy volume decay
- aggressive buy ratio

### 杠杆压力

- OI change
- price/OI divergence
- funding percentile
- long short ratio

### 价格结构

- upper wick ratio
- failed breakout
- new high distance

---

## 8. 因子评价流程

每个因子需要统计：

- IC
- ICIR
- 分位收益
- 因子相关矩阵

删除：

- 预测能力弱的因子
- 与其他因子高度重复的因子

---

## 9. 模型演进

阶段1：规则模型

验证因子方向。

阶段2：Logistic Regression

输出：

```
P(reversal)
```

阶段3：XGBoost

捕捉非线性组合：

```
OI异常
+
Volume异常
+
CVD衰竭
+
上影线
```

---

## 10. 执行策略调整

增加状态机：

```
NORMAL
 ↓
WARNING
 ↓
ARMED
 ↓
TRIGGER
 ↓
EXIT
```

提前挂单保留，但增加：

- 多价格梯度挂单
- 动态撤单
- 趋势继续确认保护

---

## 11. 开发优先级

### P0

- 1s trade 聚合
- buy/sell volume
- CVD
- event dataset

### P1

- OI/Funding/多空比
- factor_lab
- IC/ICIR报告

### P2

- 概率模型
- 自动因子筛选

### P3

- 实盘状态机
- 动态挂单优化
