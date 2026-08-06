# 回测引擎

确定性回测引擎，用于验证策略性能。

## 特性

- ✅ **确定性**：相同输入产生完全相同的输出
- ✅ **无前向偏差**：严格使用 `available_time` 避免未来信息泄露
- ✅ **虚拟时钟**：由数据驱动，不依赖系统时间
- ✅ **策略代码共用**：策略核心逻辑与实盘完全一致
- ✅ **简化触价模型**：保守的成交判断（严格穿透）
- ✅ **同步策略接口**：策略返回 `OrderIntent` 列表

## 快速开始

### 1. 准备数据

数据目录结构：

```
data/market/
├── aggtrades/
│   ├── BTCUSDT.parquet
│   └── ETHUSDT.parquet
└── klines/
    ├── BTCUSDT_1m.parquet
    ├── BTCUSDT_5m.parquet
    └── BTCUSDT_15m.parquet
```

**必需字段**：

- `aggtrades`: `symbol`, `price`, `qty`, `side`, `trade_time`, `trade_id`
- `klines`: `symbol`, `interval`, `open_time`, `close_time`, `open`, `high`, `low`, `close`, `volume`, `is_final`

### 2. 实现策略

策略需要实现三个方法：

```python
from trading_platform.shared.events import Bar1s, Kline, OrderIntent, Fill

class MyStrategy:
    def on_bar1s(self, bar: Bar1s) -> list[OrderIntent] | None:
        """处理 1s Bar，返回下单意图列表"""
        # 策略逻辑
        if should_trade:
            return [
                OrderIntent(
                    symbol=bar.symbol,
                    side='SELL',
                    price=predicted_price,
                    quantity=quantity,
                    client_order_id='my_order_1',
                    ttl_ms=60000,  # 60秒有效期
                    strategy_id='my_strategy',
                    trigger_reason='spike_detected'
                )
            ]
        return None

    def on_kline(self, kline: Kline) -> list[OrderIntent] | None:
        """处理 K 线"""
        return None

    def on_fill(self, fill: Fill) -> None:
        """成交通知（可选）"""
        pass
```

### 3. 运行回测

#### 使用命令行

```bash
# 回测单币种
docker compose -f compose.test.yaml run --rm test \
  uv run python -m trading_platform.backtest.runner \
    --strategy minimal \
    --symbols BTCUSDT \
    --start 2026-06-01 \
    --end 2026-06-02 \
    --data-dir data/market \
    --output reports/test_run

# 回测多币种
docker compose -f compose.test.yaml run --rm test \
  uv run python -m trading_platform.backtest.runner \
    --strategy demo \
    --symbols BTCUSDT ETHUSDT SOLUSDT \
    --start 2026-06-01 \
    --end 2026-07-01 \
    --output reports/multi_symbol_run
```

#### 使用 Python API

```python
from trading_platform.backtest.loader import BacktestDataLoader
from trading_platform.backtest.engine import BacktestEngine
from trading_platform.backtest.result import ResultAnalyzer
from trading_platform.shared.config import BacktestConfig

# 1. 加载数据
loader = BacktestDataLoader(
    data_dir='data/market',
    symbols=['BTCUSDT'],
    start_ms=1717200000000,
    end_ms=1719791999999
)
events = loader.load_all()

# 2. 创建策略
strategy = MyStrategy(account_id='backtest')

# 3. 运行回测
config = BacktestConfig()
engine = BacktestEngine(strategy, events, config)
result = engine.run()

# 4. 分析结果
analyzer = ResultAnalyzer(result)
summary = analyzer.analyze()
print(summary)

# 保存结果
analyzer.save_results('reports', 'my_run_001')
```

## 输出文件

每次回测生成以下文件：

```
reports/backtest_<run_id>/
├── run_meta.json          # 运行元数据
├── orders.parquet         # 所有订单记录
├── fills.parquet          # 所有成交记录
├── positions.parquet      # 所有持仓记录
└── summary.json           # 汇总指标
```

### summary.json 示例

```json
{
  "orders": {
    "total": 381,
    "filled": 198,
    "cancelled": 145,
    "expired": 38,
    "fill_rate": 0.52
  },
  "positions": {
    "total": 66,
    "profitable": 41,
    "loss": 25,
    "win_rate": 0.62
  },
  "pnl": {
    "total_realized": 1523.45,
    "total_commission": -89.23,
    "net_pnl": 1434.22,
    "profit_factor": 2.13,
    "max_drawdown": -245.67,
    "sharpe_ratio": 1.87
  }
}
```

## 成交模型

### 简化触价模型

V1 采用简化触价模型（保守）：

1. **只有 1s Bar 触发成交判断**（Kline 不触发）
2. **TTL 检查优先于价格检查**（已过期订单不会成交）
3. **做空限价单**：`bar.high > order.price`（严格穿透）
4. **做多限价单**：`bar.low < order.price`（严格穿透）
5. **成交价 = 挂单价**（不使用 bar 内其他价格）
6. **全部成交**（V1 不模拟部分成交）
7. **Maker 费率**（0.02%）

**注意**：该模型仍然简化，实际中 bar 仅触及挂单价不能保证排队中的订单成交。V2 将引入更复杂的盘口深度模型。

## 时间语义

### 避免未来信息泄露

- **1s Bar**：`timestamp` 为该秒开始时间，但 Bar 内包含整秒所有成交  
  → `available_time = timestamp + 1000ms`（秒结束后可用）

- **Kline**：`open_time` 为开盘时间，`close_time` 为收盘时间  
  → `available_time = close_time + 1ms`（K 线完成后可用）

### 虚拟时钟

引擎使用 `event.available_time` 作为虚拟时钟，确保：

- 策略只能看到该时刻之前的数据
- TTL 判断使用虚拟时钟
- 订单时间戳使用虚拟时钟

## 确定性验证

### 验证流程

```bash
# 运行两次相同回测
docker compose -f compose.test.yaml run --rm test \
  uv run python -m trading_platform.backtest.runner \
    --strategy demo \
    --symbols BTCUSDT \
    --start 2026-06-01 \
    --end 2026-06-02 \
    --output reports/det_test_1

docker compose -f compose.test.yaml run --rm test \
  uv run python -m trading_platform.backtest.runner \
    --strategy demo \
    --symbols BTCUSDT \
    --start 2026-06-01 \
    --end 2026-06-02 \
    --output reports/det_test_2

# 比较结果（排除 run_meta.json）
diff reports/det_test_1/orders.parquet reports/det_test_2/orders.parquet
diff reports/det_test_1/fills.parquet reports/det_test_2/fills.parquet
diff reports/det_test_1/positions.parquet reports/det_test_2/positions.parquet
diff reports/det_test_1/summary.json reports/det_test_2/summary.json
```

### 确定性保证

✅ **参与验证**：
- `orders.parquet` - 字节级完全一致
- `fills.parquet` - 字节级完全一致
- `positions.parquet` - 字节级完全一致
- `summary.json` - 所有数值完全一致

❌ **不参与验证**：
- `run_meta.json` - 含墙上时钟（`start_time`/`end_time`），两次运行必然不同

### 禁止的非确定性来源

```python
# ❌ 禁止使用系统时间
import time; time.time()

# ✅ 使用虚拟时钟
now = engine.virtual_time_ms

# ❌ 禁止使用随机数（无固定种子）
import random; random.random()

# ✅ 使用固定种子
rng = np.random.RandomState(seed=42)

# ❌ 禁止读取外部动态数据
requests.get("https://api.example.com/price")

# ✅ 所有数据从预加载的 events 获取
```

## 运行测试

```bash
# 运行单元测试
docker compose -f compose.test.yaml run --rm test uv run pytest tests/test_backtest.py

# 运行演示策略（需要数据）
docker compose -f compose.test.yaml run --rm test \
  uv run python -m trading_platform.backtest.runner \
    --strategy minimal \
    --symbols BTCUSDT \
    --start 2026-06-01 \
    --end 2026-06-02 \
    --data-dir data/market
```

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                     历史数据加载器                            │
│  Parquet → 按时间排序 → 内存事件流                            │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                     回测引擎核心                              │
│  - 虚拟时钟管理                                               │
│  - 事件推送（Bar1s, Kline）                                   │
│  - 订单簿维护                                                 │
│  - 成交判断（简化触价模型）                                    │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                     策略接口层                                │
│  on_bar1s(bar) / on_kline(kline) / on_fill(fill)            │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                  策略核心（与实盘共用）                        │
│  触发判断 / 价格预测 / 挂单计划 / 止损止盈                     │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                     回测执行层                                │
│  模拟下单 / 撤单 / 持仓管理                                    │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                     结果收集器                                │
│  订单 / 成交 / 持仓 / 盈亏 → Parquet + JSON                   │
└─────────────────────────────────────────────────────────────┘
```

## V1 限制

1. **无部分成交**：订单要么全部成交，要么不成交
2. **无滑点**：限价单按挂单价成交
3. **无资金费率**：不扣除持仓期间的资金费
4. **无强平**：不模拟保证金不足导致的强制平仓
5. **单币种独立**：不考虑多币种同时持仓的保证金占用
6. **不模拟 Post-Only 拒绝**：不模拟因立即成交而被拒绝的情况

## V2 规划

1. **部分成交模型**：根据盘口深度判断能成交多少
2. **滑点模拟**：市价单和大额订单增加滑点
3. **资金费率**：每8小时扣除/收取资金费
4. **保证金管理**：全局保证金占用，超限拒单
5. **延迟模拟**：下单到成交之间加入随机延迟

## 相关文档

- [回测引擎设计](../../docs/BACKTEST_ENGINE.md) - 完整设计文档
- [执行协议](../../docs/EXECUTION_PROTOCOL.md) - 订单执行规范
- [事件定义](../shared/events.py) - 标准化事件类型

## 常见问题

### Q: 如何加载自定义策略？

修改 `runner.py` 中的 `load_strategy()` 函数：

```python
def load_strategy(strategy_name: str, account_id: str):
    if strategy_name == 'my_strategy':
        from trading_platform.strategies.my_strategy import MyStrategy
        return MyStrategy(account_id=account_id)
    # ...
```

### Q: 为什么成交率很低？

简化触价模型要求**严格穿透**（`>` 和 `<`），如果 bar 仅触及挂单价（`>=` 或 `<=`）不会成交。这是保守假设，避免过度乐观。

### Q: 如何验证确定性？

运行两次相同参数的回测，比较 `orders.parquet`、`fills.parquet`、`positions.parquet`、`summary.json` 是否完全一致（字节级）。

### Q: 回测结果与实盘差异很大？

可能原因：
1. 简化触价模型过于保守
2. 未模拟滑点和部分成交
3. 未考虑资金费率
4. 数据质量问题（缺失、延迟）

建议先进行**纸盘测试**（实时行情 + 回测执行层），验证触发逻辑一致性。
