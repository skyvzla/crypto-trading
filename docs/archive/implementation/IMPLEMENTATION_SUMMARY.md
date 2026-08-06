# Trading Platform - 执行层和策略基础实现

## 实现总结

本次实现了 trading_platform 的执行层和策略基础，严格遵循 PLATFORM_ARCHITECTURE.md 和 EXECUTION_PROTOCOL.md 规范。

## 已完成模块

### 1. Binance 执行层 (`trading_platform/shared/binance/`)

#### ✅ `rest_client.py` - REST 客户端
- POST /fapi/v1/order - 下单（支持 LIMIT/MARKET，自定义 clientOrderId）
- DELETE /fapi/v1/order - 撤单
- GET /fapi/v1/order - 查单
- GET /fapi/v1/openOrders - 查询活跃订单
- GET /fapi/v2/account - 查询账户
- GET /fapi/v2/positionRisk - 查询持仓
- POST/PUT/DELETE /fapi/v1/listenKey - listenKey 管理
- HMAC-SHA256 签名
- httpx AsyncClient
- 完整异常处理（BinanceAPIException）
- 类型注解

#### ✅ `user_stream.py` - User Data Stream 管理
- listenKey 创建和 keepalive（30分钟循环）
- WebSocket 连接（websocket-client WebSocketApp）
- executionReport 事件解析和回调
- 断线自动重连（指数退避：1s → 2s → 4s → 最大60s）
- 重连后触发对账回调（on_reconnect）
- 完整异常处理

#### ✅ `rate_limiter.py` - 限速器
- 滑动窗口算法
- 权重计算（支持不同接口不同权重）
- Binance Futures 默认规则（1200 请求权重/分钟）
- 接口权重映射表（ENDPOINT_WEIGHTS）
- 自动等待和配额获取

### 2. 事件类型 (`trading_platform/shared/events.py`)

#### ✅ 扩展序列化方法
为 `Bar1s` 和 `Kline` 添加：
- `to_dict()` / `from_dict()` - 字典序列化
- `to_json()` / `from_json()` - JSON 序列化

所有 Decimal 字段正确转换为字符串，确保精度不丢失。

### 3. 策略基类

#### ✅ `strategies/tick/base.py` - 1s事件策略基类

**核心功能**：
- Redis Pub/Sub 订阅 `bar1s:{symbol}` 通道
- 自动创建 BinanceRestClient 和 UserDataStream
- 向行情层注册订阅（PUT /subscriptions/{consumer_id}）
- 健康检查循环（30秒轮询 GET /health）
- 检测行情层重启（instance_epoch 变化）并自动恢复订阅
- 抽象方法 `on_bar1s(bar)` 供子类实现
- 完整生命周期管理（start/stop）
- 异步消息处理循环

**订阅恢复机制**（PLATFORM_ARCHITECTURE.md 第3.1节）：
```python
# 每30秒检查行情层健康状态
current_epoch = health["instance_epoch"]
if self.last_known_epoch and current_epoch != self.last_known_epoch:
    # 行情层重启，重新注册订阅
    await self._register_subscriptions()
```

#### ✅ `strategies/kline/base.py` - K线策略基类

**核心功能**：
- asyncio 定时器驱动（每个 interval 独立定时器）
- 从 Redis Hash 读取 `kline:{symbol}:{interval}` 的 `latest` 字段
- 去重机制（维护 `last_processed[(symbol, interval)]` 水位）
- 向行情层注册订阅（PUT /subscriptions/{consumer_id}，types 为 `kline:1m`, `kline:5m` 等）
- 健康检查循环（检测行情层重启并恢复订阅）
- 抽象方法 `on_timer(interval)` 和 `on_kline(kline)` 供子类实现
- 完整生命周期管理

**去重逻辑**（PLATFORM_ARCHITECTURE.md 第3.2节）：
```python
# 使用 (symbol, interval) 元组作为键
key = (symbol, interval)
if key in self.last_processed and kline.close_time <= self.last_processed[key]:
    return  # 已处理，跳过

await self.on_kline(kline)
self.last_processed[key] = kline.close_time  # 成功处理后才更新水位
```

### 4. 示例策略

#### ✅ `strategies/tick/example.py`
简单的 1s 事件策略示例：
- 监控价格变化
- 突破阈值时打印警告
- 展示如何生成 OrderIntent

#### ✅ `strategies/kline/example.py`
简单的 K 线策略示例：
- 计算简单移动平均线（SMA）
- 价格突破均线时打印警告
- 展示如何维护历史数据窗口

### 5. 测试 (`tests/test_execution_layer.py`)

基础单元测试：
- ✅ 限速器测试（单请求、超限等待、权重计算）
- ✅ REST 客户端签名测试
- ✅ 客户端创建和关闭测试

通过 Docker Compose 运行测试：
```bash
docker compose -f compose.test.yaml run --rm test uv run pytest tests/test_execution_layer.py -v
```

## 架构符合度检查

### ✅ EXECUTION_PROTOCOL.md 符合度

| 要求 | 实现 | 位置 |
|-----|------|------|
| REST 下单/撤单/查单 | ✅ | `rest_client.py` |
| clientOrderId 支持 | ✅ | `post_order(new_client_order_id=...)` |
| 签名和限速 | ✅ | `_sign()` + `rate_limiter.py` |
| User Data Stream | ✅ | `user_stream.py` |
| executionReport 解析 | ✅ | `on_message()` 回调 |
| 断线重连 | ✅ | `_reconnect()` 指数退避 |
| 重连后对账 | ✅ | `on_reconnect` 回调 |
| SUBMIT_UNKNOWN 处理 | ⚠️ | REST 客户端抛出 TimeoutException，调用方需处理 |

**注意**：SUBMIT_UNKNOWN 状态机逻辑在 EXECUTION_PROTOCOL.md 第四节定义，需要在策略层实现完整的 Write-Ahead Log 和查询确认逻辑。本次实现提供了基础能力（REST 超时异常、查单接口），具体状态管理由策略层完成。

### ✅ PLATFORM_ARCHITECTURE.md 符合度

| 要求 | 实现 | 位置 |
|-----|------|------|
| 类型 = 进程 = 账户 | ✅ | 每个策略独立实例化客户端 |
| 订阅声明式接口 | ✅ | `PUT /subscriptions/{consumer_id}` |
| 健康检查循环 | ✅ | `_health_check_loop()` 30秒轮询 |
| 行情层重启恢复 | ✅ | 检测 `instance_epoch` 变化 |
| 执行层是库不是服务 | ✅ | 每个进程独立持有 REST/WS 客户端 |
| 1s 事件策略：Pub/Sub | ✅ | `TickStrategyBase` 订阅 `bar1s:{symbol}` |
| K 线策略：定时器+去重 | ✅ | `KlineStrategyBase` 定时器 + `last_processed` |

## 使用示例

### 基础 REST 调用

```python
from trading_platform.shared.binance import BinanceRestClient
from decimal import Decimal

client = BinanceRestClient(
    api_key="YOUR_API_KEY",
    api_secret="YOUR_API_SECRET",
)

# 下单
order = await client.post_order(
    symbol="BTCUSDT",
    side="BUY",
    order_type="LIMIT",
    quantity=Decimal("0.001"),
    price=Decimal("50000.00"),
    time_in_force="GTX",
    new_client_order_id="spk_BTCUSDT_668FD3C2_t1",
)

# 查单
result = await client.query_order(
    symbol="BTCUSDT",
    orig_client_order_id="spk_BTCUSDT_668FD3C2_t1",
)

await client.close()
```

### 实现 1s 事件策略

```python
from trading_platform.strategies.tick import TickStrategyBase
from trading_platform.shared.events import Bar1s

class MyStrategy(TickStrategyBase):
    async def on_bar1s(self, bar: Bar1s) -> None:
        print(f"Received: {bar.symbol} {bar.close}")
        # 生成订单意图...

# 启动
strategy = MyStrategy(
    strategy_name="my_tick",
    consumer_id="tick_strategy_my_001",
    symbols=["BTCUSDT"],
    account_id="account_b",
    binance_config=BinanceConfig(),
    redis_config=RedisConfig(),
    strategy_config=StrategyConfig(account_id="account_b"),
)
await strategy.start()
```

### 实现 K 线策略

```python
from trading_platform.strategies.kline import KlineStrategyBase
from trading_platform.shared.events import Kline

class MyKlineStrategy(KlineStrategyBase):
    async def on_timer(self, interval: str) -> None:
        pass  # 可选
    
    async def on_kline(self, kline: Kline) -> None:
        print(f"Received: {kline.symbol} {kline.interval} {kline.close}")

# 启动
strategy = MyKlineStrategy(
    strategy_name="my_kline",
    consumer_id="kline_strategy_my_001",
    symbols=["BTCUSDT"],
    intervals=["5m", "15m"],
    account_id="account_a",
    binance_config=BinanceConfig(),
    redis_config=RedisConfig(),
    strategy_config=StrategyConfig(account_id="account_a"),
)
await strategy.start()
```

## 依赖说明

所有依赖已在 `pyproject.toml` 中声明，无需新增：
- ✅ httpx >= 0.27.0 - REST 客户端
- ✅ redis >= 5.2 - Redis 连接
- ✅ websocket-client >= 1.8.0 - WebSocket
- ✅ pydantic (via fastapi) - 配置管理

## 文件清单

```
trading_platform/
├── shared/
│   ├── __init__.py              # ✅ 更新导出
│   ├── config.py                # 已有
│   ├── events.py                # ✅ 添加序列化方法
│   └── binance/
│       ├── __init__.py          # ✅ 新建
│       ├── rate_limiter.py      # ✅ 新建 (142 行)
│       ├── rest_client.py       # ✅ 新建 (281 行)
│       └── user_stream.py       # ✅ 新建 (220 行)
├── strategies/
│   ├── tick/
│   │   ├── __init__.py          # ✅ 更新导出
│   │   ├── base.py              # ✅ 新建 (288 行)
│   │   └── example.py           # ✅ 新建 (88 行)
│   └── kline/
│       ├── __init__.py          # ✅ 更新导出
│       ├── base.py              # ✅ 新建 (361 行)
│       └── example.py           # ✅ 新建 (113 行)
└── tests/
    └── test_execution_layer.py  # ✅ 新建 (75 行)
```

**总计新增代码**：~1,568 行（含注释和文档字符串）

## 注意事项

1. **运行前提**：策略基类假设行情层已启动在 `http://localhost:8000`。运行示例策略前需要先实现并启动行情层。

2. **环境变量**：配置类支持环境变量前缀：
   ```bash
   export BINANCE_API_KEY="your_key"
   export BINANCE_API_SECRET="your_secret"
   export REDIS_HOST="localhost"
   export REDIS_PORT="6379"
   ```

3. **WebSocket 实现**：当前使用 `websocket-client` 库的 `WebSocketApp`，在独立线程运行。如需纯 async 实现（如 `websockets` 库），需修改 `user_stream.py`。

4. **限速器共享**：默认使用全局 `DEFAULT_RATE_LIMITER`。多个策略进程在同一机器需独立实例化限速器。

5. **SUBMIT_UNKNOWN 完整处理**：需在策略层实现 Write-Ahead Log、数据库状态管理、查询确认逻辑（见 EXECUTION_PROTOCOL.md 第四节）。

## 下一步工作

根据 PLATFORM_ARCHITECTURE.md，还需实现：

### 行情层 (`market/`)
- [ ] Binance WebSocket 接入
- [ ] 1s Bar 聚合器
- [ ] Redis Pub/Sub 发布
- [ ] K 线存储（Redis Hash）
- [ ] Parquet 写入
- [ ] FastAPI 订阅管理接口

### 账本层 (`ledger/`)
- [ ] PostgreSQL schema
- [ ] 数据模型（psycopg3）
- [ ] FastAPI 查询接口
- [ ] 紧急控制接口

### 策略层完整实现
- [ ] Write-Ahead Log（PENDING_NEW → REST → NEW）
- [ ] 启动对账（startup_reconciliation）
- [ ] 持仓管理
- [ ] 风控检查

### 回测引擎 (`backtest/`)
- [ ] 虚拟时钟引擎
- [ ] Parquet 数据加载器
- [ ] 内存订单簿
- [ ] 策略运行器

## 总结

本次实现完成了 trading_platform 的核心执行层和策略基础框架，严格遵循架构文档规范：

✅ **Binance REST 客户端**（下单、撤单、查单、签名、限速）  
✅ **User Data Stream 管理**（listenKey、WebSocket、重连、对账回调）  
✅ **滑动窗口限速器**（1200 请求权重/分钟）  
✅ **1s 事件策略基类**（Pub/Sub 驱动、健康检查、订阅恢复）  
✅ **K 线策略基类**（定时器驱动、去重机制、订阅恢复）  
✅ **事件序列化**（Bar1s/Kline to_json/from_json）  
✅ **示例策略**（展示如何继承基类）  
✅ **单元测试**（限速器、签名、客户端）  

代码质量：
- 完整类型注解
- 异常处理
- 文档字符串
- 符合架构规范

为后续实现行情层、账本层、完整订单执行逻辑打下了坚实基础。
