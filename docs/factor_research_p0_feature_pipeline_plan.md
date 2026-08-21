# P0 Feature Pipeline 改造计划

> 状态：已完成。后续以实际归档 schema 为准，本节保留最初设计背景。

## P0 完成后的数据边界（2026-08-21）

当前 1s 归档已经包含足够的不可恢复订单流原始聚合，包括：

```text
OHLCV
quote_volume
trade_count / raw_trade_count
taker_buy_volume / taker_sell_volume
taker_buy_quote_volume / taker_sell_quote_volume
taker_buy_trade_count / taker_sell_trade_count
taker_buy_agg_trade_count / taker_sell_agg_trade_count
max_agg_trade_quantity
max_taker_buy_agg_trade_quantity / max_taker_sell_agg_trade_quantity
aggTrade / raw trade ID 边界
```

字段语义：

- `taker_buy_volume` / `taker_sell_volume` 是基础资产数量，例如 BTC、0G。
- `taker_buy_quote_volume` / `taker_sell_quote_volume` 是对应成交的计价资产金额，
  USDT 合约中即成交的 USDT 名义金额，更适合跨 symbol 做资金强度比较。
- `trade_count` 表示 aggTrade 数，`raw_trade_count` 表示底层原始撮合笔数；研究时
  不应混用两种口径。
- CVD、买卖比、imbalance、滚动均值、Z-score、velocity 等都能由上述字段无损或
  因果地重新计算，因此不作为新的长期归档列保存。

### 存储冻结原则

当前磁盘空间不足以继续扩大 1s 长期归档宽度。P1 起冻结 1s 原始 schema：

1. 不因为新增因子继续增加可派生字段；
2. CVD 使用 `taker_buy_volume - taker_sell_volume` 在研究/回测时按窗口计算；
3. Factor Dataset 默认按需生成，不默认永久保存全量秒级派生数据；
4. 只有“无法从现有归档恢复、且经研究证明有显著增量价值”的数据，才进入未来
   的存储扩展评审。

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
