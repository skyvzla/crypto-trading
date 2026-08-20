# P0 Feature Pipeline 改造计划

## 目标

为 spike 插针逼空策略建立统一的实时/回测/研究数据层。

当前已有：

- 1s 行情数据
- SpikeSharedFeatureProvider
- K线回测

缺少：

- trade方向聚合
- buy/sell volume
- CVD
- 历史feature归档

核心目标：

```
原始行情
  ↓
Feature Engine
  ↓
Archive
  ↓
Live / Backtest / Research
```

---

## 一、当前问题

现在如果只保存OHLCV：

```
回测 feature != 实盘 feature
```

无法研究：

- 主动买卖压力
- CVD背离
- 成交衰竭

所以P0首先改造数据归档。

---

## 二、统一Trade聚合层

新增统一模块：

```
TradeEvent
    ↓
TradeAggregator
    ↓
Bar1sFeature
```

实时和历史必须使用同一套聚合逻辑。

禁止：

```
live计算一套
backtest重新实现一套
```

---

## 三、1s Feature扩展

当前1s数据增加：

### 基础行情

```
timestamp
open
high
low
close
volume
```

### 成交统计

```
trade_count
buy_volume
sell_volume
buy_count
sell_count
avg_trade_size
```

### CVD

新增：

```
delta_volume = buy_volume - sell_volume

cvd
```

窗口：

```
cvd_change_5s
cvd_change_1m
cvd_change_5m
```

---

## 四、数据归档改造

新增feature archive。

建议：

```
archive/
 symbol/
   date/
     market_feature_1s.parquet
```

保存内容：

- OHLCV
- trade聚合
- CVD
- 后续衍生feature

不要只归档K线。

---

## 五、代码调整位置

建议新增：

```
data/
 feature/
   trade_aggregator.py
   cvd.py
   market_feature.py

 archive/
   writer.py
   reader.py
```

职责：

### trade_aggregator

输入：

```
TradeEvent
```

输出：

```
1s buy/sell统计
```

### cvd

负责：

```
buy_volume - sell_volume
```

生成订单流指标。

### archive

负责：

- 历史保存
- 压缩
- 回测读取

---

## 六、Spike策略调整

当前：

```
Bar1s
 ↓
rise_5s
volume
```

扩展：

```
SpikeBarFeatures

+ buy_volume
+ sell_volume
+ trade_count
+ cvd
+ cvd_change
```

策略不直接计算底层数据。

---

## 七、回测引擎调整

当前：

```
Kline
 ↓
Strategy
```

升级：

```
FeatureArchive
 ↓
FeatureProvider
 ↓
Strategy
```

保证：

实时和回测使用同一feature。

---

## 八、验证方案

### 数据一致性

比较：

```
实时feature
vs
archive重放feature
```

必须一致。

### CVD验证

检查：

```
价格创新高
CVD不创新高
```

是否能够产生背离信号。

---

## 九、开发顺序

### P0-1 Trade聚合

- TradeEvent统一模型
- buy/sell volume统计
- trade_count

### P0-2 CVD

- CVD计算
- 1s feature扩展

### P0-3 Archive

- feature writer
- feature reader

### P0-4 Backtest接入

- 回测读取feature archive
- SpikeSharedFeatureProvider接入

---

## 完成标准

完成后：

1. 实盘可实时获得buy/sell volume和CVD。
2. 回测可以读取同样feature。
3. factor_lab可以直接使用1s feature。
4. 新增因子无需修改交易核心逻辑。

