# 回测引擎设计

> 文档版本：v1.0
> 创建日期：2026-08-06
> 状态：完全重新设计，不依赖Redis replay

---

## 一、设计原则

### 核心约束

1. **确定性**：相同输入必须产生完全相同的输出
2. **无前向偏差**：任何决策只能使用该时刻之前的数据
3. **虚拟时钟**：由数据驱动，不依赖墙上时钟
4. **策略代码共用**：策略核心逻辑与实盘完全一致，只替换数据源和执行层

### 与实盘的差异

| 方面 | 实盘 | 回测 |
|---|---|---|
| 数据来源 | Binance WebSocket + Redis | Parquet文件预加载 |
| 时钟 | 系统时间 | 虚拟时间（由数据驱动） |
| 订单执行 | Binance REST API | 保守成交模型 |
| 成交判断 | User Data Stream推送 | 逐事件匹配 |
| 状态持久化 | PostgreSQL | 内存 + 结果文件 |

---

## 二、整体架构

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
│  - 成交判断（保守模型）                                        │
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

---

## 三、数据加载与预处理

### 3.1 数据源要求

回测数据必须包含：

| 数据类型 | 存储格式 | 必需字段 |
|---|---|---|
| aggTrade | Parquet | `symbol, price, qty, side, trade_time, trade_id` |
| Kline | Parquet | `symbol, interval, open_time, close_time, open, high, low, close, volume, is_final` |

**数据质量要求**：
- aggTrade 按 `trade_time` 严格升序
- Kline 按 `close_time` 严格升序（**注意**：不是 open_time）
- 无重复 `trade_id` / `(symbol, interval, open_time)`
- 无空值
- 时间戳为毫秒级 Unix 时间

**关键时间语义**：
- **1s Bar**：`timestamp` 为该秒开始时间（如 `16:30:25.000`），但 Bar 内包含整秒所有成交，**可用时间 = timestamp + 1000ms**
- **Kline**：`open_time` 为开盘时间，`close_time` 为收盘时间，但完成 K 线只能在 `close_time` 后可用，**可用时间 = close_time + 1ms**

这避免了"在 K 线开盘时就能看到整根 K 线所有数据"的未来信息泄露。

### 3.2 数据加载流程

```python
class BacktestDataLoader:
    """
    回测数据加载器
    """
    def __init__(self, data_dir: str, symbols: list[str], start: int, end: int):
        self.data_dir = data_dir
        self.symbols = symbols
        self.start_ms = start
        self.end_ms = end
        
    def load_all(self) -> list[Event]:
        """
        加载所有数据并合并排序
        """
        events = []
        
        # 1. 加载 aggTrade，聚合为 1s Bar
        for symbol in self.symbols:
            agg_trades = self._load_agg_trades(symbol)
            bars = self._aggregate_to_1s_bars(agg_trades)
            events.extend(bars)
        
        # 2. 加载 K 线（只保留 is_final=true 的）
        for symbol in self.symbols:
            for interval in ['1m', '5m', '15m']:
                klines = self._load_klines(symbol, interval)
                events.extend(klines)
        
        # 3. 按稳定排序键排序（关键！）
        events.sort(key=lambda e: (e.available_time, e.type_priority, e.symbol, e.sequence))
        
        return events
    
    def _aggregate_to_1s_bars(self, agg_trades: pd.DataFrame) -> list[Bar1s]:
        """
        将 aggTrade 聚合为 1s Bar
        """
        bars = []
        
        # 按秒分组
        agg_trades['second'] = agg_trades['trade_time'] // 1000
        
        for idx, (second, group) in enumerate(agg_trades.groupby('second')):
            bar = Bar1s(
                symbol=group.iloc[0]['symbol'],
                timestamp=second * 1000,        # 事件时间（秒开始）
                available_time=second * 1000 + 1000,  # 可用时间（秒结束后）
                type_priority=1,                # Bar1s 优先级
                sequence=idx,
                open=group.iloc[0]['price'],
                high=group['price'].max(),
                low=group['price'].min(),
                close=group.iloc[-1]['price'],
                volume=group['qty'].sum(),
                trade_count=len(group),
                vwap=np.average(group['price'], weights=group['qty'])
            )
            bars.append(bar)
        
        return bars
    
    def _load_klines(self, symbol: str, interval: str) -> list[Kline]:
        """
        加载 K 线，设置可用时间为 close_time + 1ms
        """
        df = pd.read_parquet(
            f"{self.data_dir}/klines/{symbol}_{interval}.parquet",
            filters=[
                ('close_time', '>=', self.start_ms),
                ('close_time', '<', self.end_ms),
                ('is_final', '==', True)
            ]
        )
        
        klines = []
        for idx, row in df.iterrows():
            kline = Kline(
                symbol=row['symbol'],
                interval=interval,
                open_time=row['open_time'],
                close_time=row['close_time'],
                available_time=row['close_time'] + 1,  # K 线完成后 1ms 可用
                type_priority=2,                       # Kline 优先级低于 Bar
                sequence=idx,
                open=row['open'],
                high=row['high'],
                low=row['low'],
                close=row['close'],
                volume=row['volume']
            )
            klines.append(kline)
        
        return klines
```

**关键点**：
- aggTrade 在加载时就聚合为 1s Bar，避免事件推送时实时聚合
- **所有事件按 `(available_time, type_priority, symbol, sequence)` 稳定排序**
- 预加载全部数据到内存（单月单币种 ~几百MB，可接受）

**排序键说明**：
1. `available_time`：事件何时对策略可见（避免未来信息）
2. `type_priority`：同一时刻 Bar1s(1) 先于 Kline(2)
3. `symbol`：同类型按 symbol 字母序（稳定）
4. `sequence`：同 symbol 同类型按加载顺序（稳定）

---

## 四、回测引擎核心

### 4.1 引擎主循环

```python
class BacktestEngine:
    """
    回测引擎
    """
    def __init__(self, strategy: Strategy, events: list[Event], config: BacktestConfig):
        self.strategy = strategy
        self.events = events
        self.config = config
        
        # 虚拟时钟（修复P0问题4 - 使用 available_time）
        self.virtual_time_ms = events[0].available_time if events else 0
        
        # 订单管理
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        
        # 持仓管理
        self.positions: dict[str, Position] = {}
        
        # 结果收集
        self.order_records = []
        self.fill_records = []
        self.position_records = []
        
    def run(self) -> BacktestResult:
        """
        主循环：逐事件推送
        """
        print(f"Backtest starting: {len(self.events)} events")
        
        for i, event in enumerate(self.events):
            # 1. 更新虚拟时钟（使用 available_time，避免未来信息）
            self.virtual_time_ms = event.available_time
            
            # 2. 先检查订单成交（重要！成交判断在事件推送之前）
            self._check_fills(event)
            
            # 3. 再推送事件给策略（V1：同步调用，策略返回 OrderIntent 列表）
            if isinstance(event, Bar1s):
                order_intents = self.strategy.on_bar1s(event)
            elif isinstance(event, Kline):
                order_intents = self.strategy.on_kline(event)
            else:
                order_intents = []
            
            # 4. 执行策略返回的下单意图
            for intent in (order_intents or []):
                await self.executor.place_order(intent)
            
            # 5. 进度打印（可选）
            if i % 10000 == 0:
                print(f"Progress: {i}/{len(self.events)}")
        
        # 6. 生成结果报告
        return self._generate_result()
```

**策略接口说明（修复P1问题8 - 同步接口）**：

V1 采用**同步策略核心**模式，策略方法返回 `OrderIntent` 列表而非直接下单：

```python
@dataclass
class OrderIntent:
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    client_order_id: str

class SpikeStrategy:
    def on_bar1s(self, bar: Bar1s) -> list[OrderIntent]:
        """同步方法，返回下单意图"""
        if self.should_trigger(bar):
            return [
                OrderIntent(symbol=bar.symbol, side='SELL', price=predicted_price, ...),
                # ...
            ]
        return []
```

好处：策略核心完全确定性，无异步副作用；回测和实盘共用同一套策略代码。

### 4.2 成交判断（简化触价模型）

```python
def _check_fills(self, event: Event) -> None:
    """
    检查当前事件是否触发挂单成交
    """
    if not isinstance(event, Bar1s):
        return  # 只有1s Bar才能判断成交
    
    symbol = event.symbol
    
    # 遍历该币种的所有活跃订单
    for order_id, order in list(self.orders.items()):
        if order.symbol != symbol or order.status != 'NEW':
            continue
        
        # 1. 先检查 TTL 是否过期（在价格检查之前）
        if order.ttl_ms and self.virtual_time_ms >= order.created_at + order.ttl_ms:
            self._expire_order(order)
            continue
        
        # 2. 再检查价格是否触发成交（修复P1问题9 - 简化触价模型）
        # 做空限价单成交条件：bar.high > order.price（严格穿透）
        if order.side == 'SELL' and event.high > order.price:
            fill = self._execute_fill(order, event)
            self.fills.append(fill)
            self.strategy.on_fill(fill)
        
        # 做多限价单成交条件：bar.low < order.price（严格穿透）
        elif order.side == 'BUY' and event.low < order.price:
            fill = self._execute_fill(order, event)
            self.fills.append(fill)
            self.strategy.on_fill(fill)
```

**简化触价模型约束**（重命名，之前叫"保守模型"实际偏乐观）：
1. **TTL 检查在价格检查之前**，已过期订单不会成交
2. 使用**严格穿透**判断：`>` 和 `<`，而非 `>=` / `<=`
3. 成交价=挂单价（不使用 bar 内其他价格）
4. 全部成交，V1 不模拟部分成交
5. 使用 Maker 费率（0.02%），因为限价单 Post-Only
6. 不模拟滑点（限价单无滑点）
7. **不模拟 Post-Only 因立即成交而被拒绝**（V2 扩展）

**注意**：该模型仍然简化，实际中：
- Bar 仅触及挂单价不能保证排队中的订单成交
- Post-Only 订单可能因立即成交被拒绝（GTX）
- V1 作为起点可接受，V2 可引入更复杂的盘口深度模型
            self.strategy.on_fill(fill)

def _execute_fill(self, order: Order, event: Bar1s) -> Fill:
    """
    执行成交，采用保守假设
    """
    # 保守假设：按挂单价成交（而非触发价）
    fill_price = order.price
    fill_qty = order.quantity
    
    # 计算手续费（Maker费率）
    commission = fill_qty * fill_price * self.config.maker_fee_rate
    
    fill = Fill(
        fill_id=f"fill_{order.order_id}_{self.virtual_time_ms}",
        order_id=order.order_id,
        symbol=order.symbol,
        side=order.side,
        price=fill_price,
        quantity=fill_qty,
        commission=commission,
        commission_asset='USDT',
        fill_time=self.virtual_time_ms,
        is_maker=True
    )
    
    # 更新订单状态
    order.status = 'FILLED'
    order.filled_quantity = fill_qty
    order.fill_time = self.virtual_time_ms
    
    # 更新持仓（支持多档累加）
    self._update_position(fill)
    
    return fill

def _expire_order(self, order: Order) -> None:
    """订单超时失效"""
    order.status = 'EXPIRED'
    order.cancel_time = self.virtual_time_ms
    logger.debug(f"Order {order.order_id} expired at {self.virtual_time_ms}")
```

**保守模型约束**：
1. **TTL 检查在价格检查之前**，已过期订单不会成交
2. 只在 bar 触及挂单价时成交，不使用更激进的假设
3. 成交价=挂单价，不使用 bar 内的其他价格
4. 全部成交，V1 不模拟部分成交
5. 使用 Maker 费率（0.02%），因为限价单 Post-Only
6. 不模拟滑点（限价单无滑点）

---

## 五、回测执行层

### 5.1 下单接口

```python
class BacktestExecutor:
    """
    回测执行层，模拟交易所订单接口
    """
    def __init__(self, engine: BacktestEngine):
        self.engine = engine
        
    async def place_order(self, order_intent: OrderIntent) -> Order:
        """
        下单（立即返回，不调用真实API）
        """
        order = Order(
            order_id=f"order_{len(self.engine.orders)}_{self.engine.virtual_time_ms}",
            client_order_id=order_intent.client_order_id,
            symbol=order_intent.symbol,
            side=order_intent.side,
            type='LIMIT',
            price=order_intent.price,
            quantity=order_intent.quantity,
            status='NEW',
            created_at=self.engine.virtual_time_ms,
            ttl_ms=order_intent.ttl_ms
        )
        
        # 加入引擎订单簿
        self.engine.orders[order.order_id] = order
        self.engine.order_records.append(order)
        
        return order
    
    async def cancel_order(self, order_id: str) -> None:
        """
        撤单（立即生效）
        """
        order = self.engine.orders.get(order_id)
        if not order or order.status != 'NEW':
            return
        
        order.status = 'CANCELLED'
        order.cancel_time = self.engine.virtual_time_ms
```

**关键差异**：
- 无需等待网络响应，立即返回结果
- 无需数据库持久化，全部在内存
- 订单状态机简化：只有NEW → FILLED/CANCELLED/EXPIRED

### 5.2 持仓管理（支持多档挂单）

```python
def _update_position(self, fill: Fill) -> None:
    """
    根据成交更新持仓，支持多档同方向累加
    """
    symbol = fill.symbol
    
    if symbol not in self.positions:
        # 新开仓
        self.positions[symbol] = Position(
            symbol=symbol,
            side='SHORT' if fill.side == 'SELL' else 'LONG',
            entry_price=fill.price,
            quantity=fill.quantity,
            total_commission=fill.commission,
            unrealized_pnl=0,
            realized_pnl=0,
            opened_at=self.virtual_time_ms
        )
    else:
        pos = self.positions[symbol]
        
        if (pos.side == 'SHORT' and fill.side == 'BUY') or \
           (pos.side == 'LONG' and fill.side == 'SELL'):
            # 平仓
            close_qty = min(fill.quantity, pos.quantity)
            
            # 计算已实现盈亏
            if pos.side == 'SHORT':
                pnl = (pos.entry_price - fill.price) * close_qty
            else:  # LONG
                pnl = (fill.price - pos.entry_price) * close_qty
            
            pnl -= fill.commission  # 扣除手续费
            pos.realized_pnl += pnl
            pos.quantity -= close_qty
            pos.total_commission += fill.commission
            
            if pos.quantity <= 0:
                # 完全平仓
                pos.status = 'CLOSED'
                pos.closed_at = self.virtual_time_ms
                self.position_records.append(pos)
                del self.positions[symbol]
            
            # V1 不支持反向开仓（平仓后立即反向）
            if fill.quantity > close_qty:
                raise ValueError(
                    f"V1 does not support reverse opening: "
                    f"fill_qty={fill.quantity} > pos_qty={pos.quantity}"
                )
        else:
            # 同方向加仓（多档挂单场景）
            old_qty = pos.quantity
            old_price = pos.entry_price
            add_qty = fill.quantity
            add_price = fill.price
            
            # 加权平均开仓价
            pos.entry_price = (old_qty * old_price + add_qty * add_price) / (old_qty + add_qty)
            pos.quantity += add_qty
            pos.total_commission += fill.commission
            
            logger.debug(
                f"Added to position {symbol}: "
                f"qty {old_qty}→{pos.quantity}, "
                f"entry {old_price:.2f}→{pos.entry_price:.2f}"
            )
```

---

## 六、结果输出

### 6.1 输出文件结构

每次回测生成以下文件：

```
reports/backtest_{run_id}/
├── run_meta.json          # 运行元数据（包含墙上时间戳，不参与确定性验证）
├── orders.parquet         # 所有订单记录
├── fills.parquet          # 所有成交记录
├── positions.parquet      # 所有持仓记录
└── summary.json           # 汇总指标
```

### 6.2 run_meta.json

```json
{
  "run_id": "backtest_20260806_143025",
  "strategy": "spike",
  "strategy_version": "git:a3f5b2c",
  "config_version": "v1",
  "config_hash": "sha256:3a4f...",
  "data_version": {
    "symbols": ["BTCUSDT", "ETHUSDT"],
    "start": "2026-06-01T00:00:00Z",
    "end": "2026-07-01T00:00:00Z",
    "data_hash": "sha256:7b2e..."
  },
  "execution": {
    "start_time": "2026-08-06T14:30:25Z",    # 墙上时钟，两次运行必然不同
    "end_time": "2026-08-06T14:35:18Z",
    "duration_seconds": 293,
    "total_events": 2592000,
    "virtual_time_start": 1717200000000,    # 虚拟时钟起点，确定性
    "virtual_time_end": 1719791999999       # 虚拟时钟终点，确定性
  }
}
```

**说明**：`run_meta.json` 中的 `execution.start_time/end_time` 是墙上时钟，两次运行必然不同，**不参与确定性验证**。其他字段（配置哈希、数据哈希、虚拟时钟范围）必须一致。

### 6.3 summary.json

```json
{
  "triggers": {
    "total": 127,
    "by_symbol": {"BTCUSDT": 83, "ETHUSDT": 44}
  },
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
  },
  "stop_loss_analysis": {
    "stopped_by_time_decay": 12,
    "stopped_by_momentum_not_decay": 8,
    "stopped_by_sideways": 5,
    "stopped_by_extreme": 0,
    "swept_rate": 0.24
  }
}
```

---

## 七、确定性验证

### 7.1 验证范围

| 文件 | 是否参与确定性验证 | 说明 |
|---|---|---|
| `orders.parquet` | ✅ 是 | 字节级完全一致 |
| `fills.parquet` | ✅ 是 | 字节级完全一致 |
| `positions.parquet` | ✅ 是 | 字节级完全一致 |
| `summary.json` | ✅ 是 | 所有数值完全一致（包括浮点数） |
| `run_meta.json` | ❌ 否 | 含墙上时钟（`start_time`/`end_time`/`duration_seconds`），两次运行必然不同 |

### 7.2 验证流程

```bash
# 运行两次相同回测
docker compose -f compose.test.yaml run --rm test \
  uv run python -m trading_platform.backtest.runner \
    --config config/spike_v1.yaml \
    --symbols BTCUSDT \
    --start 2026-06-01 \
    --end 2026-06-02 \
    --output reports/det_test_1

docker compose -f compose.test.yaml run --rm test \
  uv run python -m trading_platform.backtest.runner \
    --config config/spike_v1.yaml \
    --symbols BTCUSDT \
    --start 2026-06-01 \
    --end 2026-06-02 \
    --output reports/det_test_2

# 比较确定性输出（排除 run_meta.json）
diff reports/det_test_1/orders.parquet   reports/det_test_2/orders.parquet
diff reports/det_test_1/fills.parquet    reports/det_test_2/fills.parquet
diff reports/det_test_1/positions.parquet reports/det_test_2/positions.parquet
diff reports/det_test_1/summary.json     reports/det_test_2/summary.json
```

### 7.3 禁止的非确定性来源

```python
# ❌ 禁止使用系统时间（run_meta.json 的 execution 字段除外）
import time; time.time()           # 错误！

# ✅ 策略逻辑使用虚拟时钟
now = self.engine.virtual_time_ms

# ❌ 禁止使用随机数（无固定种子）
import random; random.random()     # 错误！

# ✅ 如需随机，使用固定种子
rng = np.random.RandomState(seed=42)

# ❌ 禁止读取外部动态数据
requests.get("https://api.example.com/price")  # 错误！

# ✅ 所有行情数据从预加载的 events 获取
```

---

## 八、与实盘对比验证

### 8.1 纸盘测试

```python
class PaperTradingEngine:
    """
    纸盘引擎：实时行情 + 回测执行层
    """
    def __init__(self, strategy: Strategy, executor: BacktestExecutor):
        self.strategy = strategy
        self.executor = executor
        
        # 记录所有触发事件
        self.trigger_log = []
        
    async def on_bar1s(self, bar: Bar1s):
        """
        接收实时1s Bar
        """
        # 记录到日志
        self.trigger_log.append({
            'timestamp': bar.timestamp,
            'symbol': bar.symbol,
            'price': bar.close
        })
        
        # 推送给策略
        self.strategy.on_bar1s(bar)
```

### 8.2 对比流程

```
1. 纸盘运行3天，记录所有触发事件和订单 → paper_run_20260806.json

2. 采集相同时间段的历史数据 → data/20260806_20260809.parquet

3. 回测相同时间段 → backtest_20260806.json

4. 比较两份日志：
   - 触发时间差 < 2秒
   - 触发价格差 < 0.1%
   - 挂单价格差 < 0.1%
   - 预测极值差 < 3%
```

**通过标准**：
- 触发次数相同
- 每次触发的时间、价格、挂单计划在合理误差内一致

---

## 九、已知限制与V2规划

### V1限制

1. **无部分成交**：订单要么全部成交，要么不成交
2. **无滑点**：限价单按挂单价成交
3. **无资金费率**：不扣除持仓期间的资金费
4. **无强平**：不模拟保证金不足导致的强制平仓
5. **单币种独立**：不考虑多币种同时持仓的保证金占用

### V2扩展

1. **部分成交模型**：根据盘口深度判断能成交多少
2. **滑点模拟**：市价单和大额订单增加滑点
3. **资金费率**：每8小时扣除/收取资金费
4. **保证金管理**：全局保证金占用，超限拒单
5. **延迟模拟**：下单到成交之间加入随机延迟

---

*文档状态：V1完整，可直接实现。回测引擎与实盘完全解耦，不依赖Redis。*
