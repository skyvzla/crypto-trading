# 量化交易平台整体架构方案

> 文档版本：v1.1（修订中）
> 创建日期：2026-08-05
> 最后更新：2026-08-06
> 状态：核心架构确定，订单执行、回测引擎、订阅接口已补充独立文档

---

## 文档定位

本文描述的是**通用量化交易平台架构**，不是逼空插针策略的专用系统架构。

- 平台层负责行情接入与历史存储、策略运行环境、交易所执行协议、账户级风控、账本、回测和运维能力。
- 数据层按可扩展的市场数据模型设计，结构上能够容纳 K 线、1s 事件及后续数据类型，因此不应被当前逼空策略永久锁死；但 V1 只实现该策略实际需要的数据能力。
- 逼空插针策略是平台当前首个重点落地和验收策略，用于验证秒级行情、分档订单、回放一致性和高风险空头管理等能力。
- 起涨点识别、清算地图、极值预测、多档价格和具体退出规则属于逼空策略域，不是所有平台策略必须遵守的通用规则。
- 平台通用架构与具体策略规格分别演进；策略需求可以推动平台能力完善，但不能反向把平台绑定为单策略系统。

**当前阶段约束**：V1 只开发和验收逼空插针策略。平台接口和数据模型可以保持通用，但不得以支持未来其他策略为理由提前建设额外功能；K 线策略及其他策略类型只保留架构边界，不进入当前实施范围。当前开发顺序始终以“逼空策略可信回测闭环 → 执行闭环 → 小额实盘闭环”为主线。

逼空策略的业务规则以 `SPIKE_STRATEGY_SPEC.md` 和 `docs/spike_trader/` 为准。

---

## 一、核心架构

平台采用**三层架构**，类型 = 进程 = 账户，层间职责严格隔离：

```
┌──────────────────────────────────────────────────────────────┐
│                     行情层（共享进程）                         │
│  Binance WS 接入 → 1s Bar 聚合 → Redis 分发 + Parquet 历史    │
│  HTTP 接口：/subscribe  /health  /klines                      │
└───────────────┬─────────────────────────┬────────────────────┘
   HTTP 订阅注册 │                         │ Redis 数据
                ▼                         ▼
┌───────────────────────┐   ┌─────────────────────────────────┐
│  K 线策略群（账户 A）  │   │  1s 事件策略群（账户 B）         │
│  定时器轮询 Redis       │   │  Redis Pub/Sub 推送             │
│  进程内统一风控         │   │  进程内统一风控                  │
│  shared/ 执行库        │   │  shared/ 执行库                  │
└───────────┬───────────┘   └───────────────┬─────────────────┘
            │ 直接写 PostgreSQL              │ 直接写 PostgreSQL
            └───────────────┬───────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────┐
│                     账本层（共享进程）                         │
│    PostgreSQL + FastAPI 后端 + Vue3 前端                      │
│    统一查看订单/成交/持仓/盈亏 + 配置管理                       │
└──────────────────────────────────────────────────────────────┘
```

**设计原则**：

- **类型 = 进程 = 账户**：同类型策略共用一个进程和一个交易所账户，不同类型完全隔离
- **行情和账本共享**：多个策略共用同一个行情源和账本数据库，避免重复接入和存储
- **执行层是库不是服务**：每个策略进程持有自己的 Binance 客户端实例，不经过中间代理

---

## 二、确定技术栈

所有依赖均已在现有项目中使用，**无需引入新框架**，仅需新增 `pyarrow`。

### 后端

| 用途 | 库 | 版本 |
|---|---|---|
| Web/API 框架 | FastAPI + uvicorn | 已安装 0.139 / 0.51 |
| PostgreSQL 客户端 | psycopg（v3，async） | 已安装 3.3.4 |
| Redis 客户端 | redis（含 asyncio） | pyproject 已声明 5.x |
| HTTP 客户端 | httpx | 已安装 0.28 |
| Binance WebSocket | websocket-client | 已安装 1.9 |
| 数据处理 | pandas 2 | 已安装 2.3 |
| Parquet 写入 | pyarrow | **待新增** |
| 历史查询 | DuckDB | 已安装 1.4 |
| 配置 / 校验 | pydantic 2 + PyYAML | 已安装 |
| 异步框架 | asyncio（标准库） | Python 3.12 |

### 前端（账本层 Web）

| 用途 | 库 | 说明 |
|---|---|---|
| UI 框架 | Vue 3 + TypeScript | 现有 `/web/` 目录 |
| 组件库 | naive-ui | 已安装 |
| 图表 | echarts | 已安装，用于盈亏走势 |
| 路由 | vue-router | 已安装 |
| 构建 | Vite | 已安装 |

账本层前端**直接在现有 `/web/` 目录下扩展**，添加账本相关页面路由即可。

### 基础设施

| 组件 | 版本 | 说明 |
|---|---|---|
| PostgreSQL | 16（`compose.yaml` 已有） | 订单/成交/持仓/配置 |
| Redis | 7.4（`compose.yaml` 已有） | 1s Bar Pub/Sub + K 线存储 |

**新增依赖**（仅一个）：

```bash
uv add pyarrow
```

---

## 三、各层详细设计

### 3.1 行情层

**职责**：Binance WebSocket 接入，在层内将 aggTrade 聚合为 1s Bar，向策略分发，写入历史存储。

**1s Bar 聚合（关键决策）**

策略不直接接收原始 aggTrade，行情层在内存中每秒滚动聚合：

```
原始 aggTrade（每秒可能数百条）
         ↓ 行情层内聚合（1 秒窗口）
1s Bar {open, high, low, close, volume, trade_count, vwap}
         ↓ Redis Pub/Sub
策略进程（每秒收到一条结构化事件）
```

好处：Redis 消息量降低 100 倍以上，策略逻辑统一处理结构化数据，回测按 1s 步进无需处理毫秒乱序。

**K 线数据**：直接订阅 Binance K 线 WS 流，过滤 `isFinal=true` 后写入 Redis Hash，不从 aggTrade 重建。

**对外接口**：

| 接口 | 方法 | 说明 | 状态 |
|---|---|---|---|
| `/subscriptions/{consumer_id}` | PUT | 声明式订阅，幂等覆盖完整期望集合 | V1 必需 |
| `/subscriptions/{consumer_id}` | DELETE | 进程关闭时注销订阅 | V1 必需 |
| `/health` | GET | 返回就绪状态和 instance_epoch | V1 必需 |
| `/klines` | GET | 查询历史 K 线（大周期数据：4h/1d/1w/1M） | V2 可选 |

**订阅管理接口详细说明**：

采用**声明式幂等接口**，每次提交完整期望集合：

```http
# 声明/更新订阅（幂等）
PUT /subscriptions/{consumer_id}
Content-Type: application/json

{
  "symbols": ["BTCUSDT", "ETHUSDT"],
  "types": ["bar1s", "kline:1m", "kline:5m"]
}

Response 200:
{
  "status": "ok",
  "consumer_id": "tick_strategy_spike_12345",
  "subscribed": {
    "BTCUSDT": ["bar1s", "kline:1m", "kline:5m"],
    "ETHUSDT": ["bar1s", "kline:1m", "kline:5m"]
  },
  "active_streams": 6
}

# 注销全部订阅（进程关闭时）
DELETE /subscriptions/{consumer_id}

Response 200:
{
  "status": "ok",
  "consumer_id": "tick_strategy_spike_12345",
  "unsubscribed": "all"
}
```

**consumer_id 格式**：`{strategy_type}_{strategy_name}_{instance_id}`
- 示例：`tick_strategy_spike_001`
- 进程启动时生成（可用 UUID 后8位作为 instance_id），写入进程状态文件
- 重启后读取状态文件，使用相同 consumer_id 重新 PUT，行情层幂等覆盖

**行情层重启自动恢复（解决P0问题1）**：

行情层重启后订阅状态会丢失。恢复机制：

1. 行情层启动时生成新的 `instance_epoch`（UUID）
2. `/health` 响应中包含 `instance_epoch`
3. 策略进程每 30 秒轮询 `/health`，检测 epoch 变化
4. 检测到变化 → 重新 `PUT /subscriptions/{consumer_id}`，恢复订阅

```python
# 策略进程的健康检查循环
class StrategyProcess:
    def __init__(self):
        self.last_known_epoch = None
        
    async def health_check_loop(self):
        while True:
            await asyncio.sleep(30)
            health = await httpx.get("http://market-api:8000/health")
            current_epoch = health.json()["instance_epoch"]
            
            if self.last_known_epoch and current_epoch != self.last_known_epoch:
                logger.warning(f"Market layer restarted, re-registering subscriptions")
                await self.register_subscriptions()
            
            self.last_known_epoch = current_epoch
```

# 健康检查（返回实例标识）
GET /health

Response 200:
{
  "status": "ready",
  "instance_epoch": "a3f5b2c1-4d8e-...",  # 行情层启动时生成，重启后改变
  "uptime_seconds": 3600,
  "subscribed_symbols": 100,
  "active_ws_streams": 300
}
```
# 内部状态
subscriptions = {
    "BTCUSDT": {"bar1s": 2, "kline:1m": 1},  # 2个进程订阅bar1s，1个订阅kline
    "ETHUSDT": {"bar1s": 1}
}

# 收到 DELETE /subscribe?symbols=BTCUSDT（来自进程A）
# → 减少计数
subscriptions["BTCUSDT"]["bar1s"] -= 1  # 从 2 降到 1

# 如果某个 type 的计数降到 0，关闭对应的 Binance WS 流
if subscriptions["BTCUSDT"]["bar1s"] == 0:
    close_binance_stream("BTCUSDT", "aggTrade")
```

这样多个策略进程可以安全地订阅同一交易对，最后一个进程取消时才真正关闭 WS 连接。

**Redis Key 规则**：

| 数据类型 | 分发方式 | Key | 过期策略 |
|---|---|---|---|
| 1s Bar | Pub/Sub 推送 | `bar1s:{symbol}` | 无持久化，策略自维护窗口 |
| 已完成 K 线 | Hash 写入 | `kline:{symbol}:{interval}` | 只保留 `latest` 一条 |
| 标记价格 | Pub/Sub 推送（可选） | `mark:{symbol}` | 无持久化 |

**Redis 数据保留策略**：

采用简化方案，最小化内存占用：

| 数据类型 | 保留策略 | 说明 |
|---|---|---|
| 1s Bar | Pub/Sub，不持久化 | 策略进程自己维护滑动窗口（如最近 300 秒） |
| K 线（1m/5m/15m/1h） | Hash 只保留 `latest` 一条 | 策略读取最新完成的 K 线，历史数据从 DuckDB 查询 |

**内存占用估算（简化方案）**：

| 数据类型 | 单币种 | 100 币种 |
|---|---|---|
| 1s Bar | 0 KB（Pub/Sub） | 0 KB |
| K 线 latest | < 2 KB | < 200 KB |
| **Redis 总计** | **< 2 KB** | **< 200 KB** |

Redis 配置建议 `maxmemory 512mb`，实际占用极小，非常安全。

**策略进程内存管理**：

策略进程自己维护 1s Bar 的滑动窗口：
```python
class MarketDataWindow:
    def __init__(self, window_seconds=300):
        self.window_seconds = window_seconds
        self.bars = deque(maxlen=window_seconds)  # 自动淘汰最老数据
    
    async def on_bar1s(self, bar):
        self.bars.append(bar)
        # 最多保留 300 条，内存占用 < 100 KB
```

每个策略进程维护自己需要的窗口大小，100 个币种 × 300 秒 ≈ 10 MB 内存。

**历史存储**：

- 原始 aggTrade 异步追加写入 **Parquet**（按天、按币种分区），供回测引擎使用
- K 线数据同时写入 **Parquet**（历史，供回测）和 **Redis Hash latest**（热数据，供实盘策略读取）
- DuckDB 作为查询层供 V2 大周期数据按需查询（V1 不实现）
- 写入在独立线程完成，不阻塞事件处理主循环

**订阅管理逻辑**（配合新声明式接口）：

```
收到 PUT /subscriptions/{consumer_id} {symbols: ["BTCUSDT"], types: ["bar1s", "kline:1m"]}
  → 行情层比对当前订阅集合
  → 新增的 symbol+type → 向 Binance 开启对应 WS 流
  → 此 consumer 之前有、新集合没有的 → 计数减一，若降到 0 → 关闭 WS 流
```

---

### 3.2 策略层

**分组原则**：类型 = 进程 = 账户。同类型策略共用一个进程，共用一个交易所账户。

#### K 线策略群进程（账户 A）

- **驱动模式**：内部 asyncio 定时器，每根 K 线周期触发
- **数据获取**：`redis.hget(f"kline:{symbol}:{interval}", "latest")` 读取最新已完成 K 线
- **去重机制（V1 必需）**：策略维护 `last_processed[(symbol, interval)]`，每次读取后检查 K 线的 `close_time`，只处理比上次记录更新的 K 线，避免定时器重复触发
- **执行**：调用 `shared/binance/` 库，REST 下单，User Data Stream 接收回报
- **风控**：进程内维护账户 A 总仓位，所有策略模块共享同一个风控实例

```python
# 示例：K 线去重（修复P1问题7）
class KlineStrategy:
    def __init__(self):
        # 使用 (symbol, interval) 元组作为键
        self.last_processed = {}  # {(symbol, interval): last_close_time}
    
    async def on_timer(self, symbol: str, interval: str):
        kline_json = await redis.hget(f"kline:{symbol}:{interval}", "latest")
        if not kline_json:
            return
        kline = json.loads(kline_json)
        
        # 去重检查（注意使用元组作为键）
        key = (symbol, interval)
        if key not in self.last_processed or kline['close_time'] > self.last_processed[key]:
            # 成功处理后才更新水位（失败会重试）
            try:
                await self.process_kline(kline)
                self.last_processed[key] = kline['close_time']
                # 可选：持久化水位到文件/数据库，重启后恢复
            except Exception as e:
                logger.error(f"Failed to process kline {key}: {e}")
                # 不更新水位，下次定时器会重试
```

**适用策略类型**：趋势突破、均线交叉、反转形态等基于 K 线形态的策略。

#### 1s 事件策略群进程（账户 B）

- **驱动模式**：Redis Pub/Sub，订阅 `bar1s:{symbol}` 通道，事件到达立即处理
- **数据获取**：来自行情层推送的 1s Bar 结构体
- **执行**：调用 `shared/binance/` 库，REST 下单，User Data Stream 接收回报
- **风控**：进程内维护账户 B 总仓位

**适用策略类型**：逼空插针、动量加速、高频反转等需要秒级响应的策略。

**执行库说明**：`shared/binance/` 是代码库，不是共享服务。每个策略进程自己实例化，各自持有独立的 REST 连接池和 User Data Stream，进程间无共享状态。

---

### 3.3 账本层

**职责**：接收策略写入的交易流水，提供统一查看和配置管理 Web 界面。

**数据流**：策略进程用 psycopg3 直接写入 PostgreSQL，账本层后端读取同一数据库，不经过任何 HTTP 中转。

**FastAPI 后端职责**：
- 提供账本查询 API（订单、成交、持仓、盈亏）
- 接收 Web 配置修改写入配置表
- 提供紧急控制入口（写数据库 `account_control_state` 表，策略轮询执行；V1 简化方案）

**紧急控制设计（修复P0问题3 - 解决乱序和重启失效）**：

使用两表设计：

```sql
-- 账户当前期望状态（每账户一行）
CREATE TABLE account_control_state (
    account_id VARCHAR(32) PRIMARY KEY,
    desired_state VARCHAR(32) NOT NULL,  -- NORMAL, HALT_NEW, CANCEL_ORDERS, CLOSE_ALL
    state_version BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_by VARCHAR(64)
);

-- 控制命令审计日志（只追加）
CREATE TABLE control_command_log (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(32) NOT NULL,
    command VARCHAR(32) NOT NULL,
    issued_by VARCHAR(64),
    issued_at TIMESTAMP NOT NULL DEFAULT NOW(),
    executed_at TIMESTAMP,
    execution_result TEXT
);
```

**状态语义**：

| 状态 | 操作 | 说明 |
|---|---|---|
| `NORMAL` | 正常运行 | 默认状态 |
| `HALT_NEW` | 禁止新开仓信号 | 已有持仓继续运行，挂单不撤 |
| `CANCEL_ORDERS` | 撤销所有挂单 + 禁止新开仓 | 不平持仓 |
| `CLOSE_ALL` | 撤单 + 市价平所有持仓 + 禁止新开仓 | 全部清场 |

**策略进程处理流程**：

1. **启动时**：读取 `account_control_state`，进入对应状态（解决重启失效）
2. **运行中**：每秒轮询 `account_control_state`，检查 `state_version` 是否增加
3. **检测到变化**：执行新状态对应的操作，回写执行结果到 `control_command_log`

```python
class StrategyProcess:
    def __init__(self, account_id: str):
        self.account_id = account_id
        self.current_state_version = 0
        
    async def startup(self):
        # 启动时读取当前状态
        state = await db.fetch_one(
            "SELECT desired_state, state_version FROM account_control_state WHERE account_id = ?",
            self.account_id
        )
        if state:
            await self.apply_state(state['desired_state'])
            self.current_state_version = state['state_version']
    
    async def control_loop(self):
        while True:
            await asyncio.sleep(1)
            state = await db.fetch_one(
                "SELECT desired_state, state_version FROM account_control_state WHERE account_id = ?",
                self.account_id
            )
            if state and state['state_version'] > self.current_state_version:
                await self.apply_state(state['desired_state'])
                self.current_state_version = state['state_version']
```

**Vue3 前端（扩展现有 `/web/`）**：
- 订单列表、成交流水、持仓快照（按策略筛选）
- 盈亏走势图（echarts，已有）
- 交易对配置、策略参数配置
- 紧急停止按钮

**配置下发流程（V1）**：

```
Web 修改配置 → 写入 PostgreSQL 配置表
策略进程重启后读取新配置生效（V1 不实现热更新）

V2 扩展：策略轮询配置表→检测变更→热更新参数并重新注册订阅
```

---

## 四、通信协议

| 通信方向 | 协议 | 理由 |
|---|---|---|
| 策略 → 行情层（订阅注册） | HTTP REST（FastAPI） | 低频控制操作，易调试，curl 直接测试 |
| 行情层 → 1s 事件策略（数据） | Redis Pub/Sub | 高频推送，解耦，行情层重启策略不崩 |
| 行情层 → K 线策略（数据） | Redis Hash + 策略定时读 | K 线低频，轮询完全够用 |
| 策略 → 账本层（写流水） | 直接写 PostgreSQL（psycopg3） | 无服务调用，事务保证原子性 |
| 账本层 → 策略（配置下发） | PostgreSQL 轮询（30s 间隔） | 配置变更不频繁 |
| 账本层 → 策略（紧急停止） | PostgreSQL `global_control` 表 | 策略每秒轮询；V1 简化方案，无需知道 PID |

---

## 五、项目目录结构

新平台在现有仓库下新建顶层目录，现有 `src/crypto_trader/` 保持不动：

```
/data/projects/quant/crypto/
│
├── src/crypto_trader/       # 现有系统，保持不变
│
├── trading_platform/                # 新平台（新建）
│   │
│   ├── market/              # 行情层（独立进程）
│   │   ├── feed/
│   │   │   ├── binance_ws.py      # Binance aggTrade + K线 WS 接入
│   │   │   ├── aggregator.py      # 1s Bar 内存聚合
│   │   │   └── normalizer.py      # 事件标准化、去重、质量检查
│   │   ├── store/
│   │   │   ├── redis_pub.py       # Redis Pub/Sub 发布
│   │   │   ├── kline_store.py     # 已完成 K 线写 Redis Hash
│   │   │   └── parquet_writer.py  # aggTrade 异步写 Parquet
│   │   ├── api/
│   │   │   └── routes.py          # FastAPI /subscribe /health /klines
│   │   └── main.py
│   │
│   ├── strategies/
│   │   ├── kline/                 # K 线策略群（账户 A）
│   │   │   ├── base.py            # K 线策略基类（定时器驱动）
│   │   │   ├── breakout.py        # 策略实现示例
│   │   │   ├── risk.py            # 账户 A 统一风控
│   │   │   └── main.py
│   │   └── tick/                  # 1s 事件策略群（账户 B）
│   │       ├── base.py            # 1s 事件策略基类（Pub/Sub 驱动）
│   │       ├── spike_short.py     # 逼空插针策略
│   │       ├── risk.py            # 账户 B 统一风控
│   │       └── main.py
│   │
│   ├── ledger/                    # 账本层后端（独立进程）
│   │   ├── db/
│   │   │   ├── schema.sql         # 订单/成交/仓位/配置表结构
│   │   │   └── models.py          # psycopg3 数据模型
│   │   ├── api/
│   │   │   └── routes.py          # FastAPI CRUD + 配置接口
│   │   └── main.py
│   │
│   ├── shared/                    # 各层公用代码库（非进程）
│   │   ├── binance/
│   │   │   ├── rest_client.py     # REST 下单/撤单/查单（httpx async）
│   │   │   ├── user_stream.py     # User Data Stream 管理
│   │   │   └── rate_limiter.py    # 限速、重试、签名
│   │   ├── events.py              # 标准化事件类型（dataclass）
│   │   └── config.py              # 配置加载（pydantic-settings）
│   │
│   └── backtest/                  # 回测入口
│       ├── engine.py              # 回测引擎（虚拟时钟驱动，不依赖 Redis）
│       ├── loader.py              # 数据加载器（Parquet → 内存事件流）
│       └── runner.py              # 策略回测运行器
│
└── web/                     # 前端（Vue3 + naive-ui，现有，扩展账本页面）
```

---

## 六、与现有代码库的关系

现有 `src/crypto_trader/` 是成熟的 K 线扫描和交易系统，**不改动**，新平台与之并存。

| 现有模块 | 与新平台的关系 |
|---|---|
| `exchange/binance_market.py` | 可参考，REST 调用模式直接借鉴到 `shared/binance/` |
| `market/binance_archive.py` | aggTrade 存储逻辑可参考，但新平台独立实现 |
| `execution/` | 不复用，订单状态机语义不同 |
| `risk/service.py` | 不复用，新平台风控在各策略进程内独立实现 |
| `backtest/engine.py` | 不复用，新回测基于 1s Bar 事件驱动 |
| `web/` 前端 | **直接扩展**，在现有 Vue3 项目里增加账本相关页面路由 |

现有系统继续独立运行，新平台是并行的新项目，不需要迁移现有策略。

---

## 七、部署方式

单机部署，各层通过根目录 `compose.yaml` 启动，依赖 Compose 网络中的 PostgreSQL 和 Redis：

```bash
# 启动基础设施和正式服务
docker compose -f compose.yaml up -d --build

# 前端
cd web && npm run dev                             # 开发模式
```

**启动顺序**：PostgreSQL / Redis → 行情层（等 `/health` 就绪）→ 策略层 → 账本层。

策略进程启动序列：
1. 等待 Redis 连接成功
2. 调用行情层 `GET /health` 确认就绪
3. 发送 `POST /subscribe` 注册所需交易对
4. 收到 200 确认后开始处理数据

---

## 八、回测架构

回测引擎完全独立，不依赖行情层 Redis，采用**虚拟时钟 + 预加载数据**的确定性方案。

**详细设计见**：[docs/BACKTEST_ENGINE.md](docs/BACKTEST_ENGINE.md)

### 核心架构

```
Parquet 预加载（aggTrade → 1s Bar + Kline）
         ↓ 按时间严格排序
虚拟时钟驱动事件循环
         ↓ 先判断挂单成交，再推送事件给策略
策略核心逻辑（与实盘完全共用）
         ↓
回测执行层（内存模拟下单/撤单/成交）
         ↓
结果输出 reports/backtest_{run_id}/
```

**与旧 Redis replay 方案的关键区别**：

| 方面 | 旧方案（废弃） | 新方案 |
|---|---|---|
| 数据流 | Parquet → Redis Pub/Sub → 策略 | Parquet → 内存事件流 → 策略 |
| 时钟 | 墙上时钟（不受控） | 虚拟时钟（数据驱动） |
| K 线策略触发 | asyncio 定时器（可能跳帧/重复） | 确定性逐事件推送 |
| Redis 背压 | 无（快速回放时可能丢事件） | 无（同步调用） |
| 确定性 | ❌ 不保证 | ✅ 相同输入必定相同输出 |

**运行方式**：

```bash
docker compose -f compose.test.yaml run --rm test \
  uv run python -m trading_platform.backtest.runner \
    --strategy spike \
    --config config/spike_v1.yaml \
    --symbols BTCUSDT ETHUSDT \
    --start 2026-06-01 \
    --end 2026-07-01
```

**执行层差异**（策略核心逻辑不变，只替换执行层）：

| 功能 | 实盘执行层 | 回测执行层 |
|---|---|---|
| 下单 | 调用 Binance REST API | 记录订单到内存订单簿 |
| 成交判断 | 监听 User Data Stream | bar.high/low 触价即成交（保守模型） |
| 订单状态 | 实时更新 | 成交检查时同步更新 |
| 仓位 | Binance API 查询 | 内存累计计算 |

**回测结果输出**（每次运行生成）：
- `run_meta.json`：数据版本哈希、策略 git commit、配置 SHA256、运行时间
- `orders.parquet`：逐笔订单记录
- `fills.parquet`：逐笔成交记录
- `positions.parquet`：持仓记录
- `summary.json`：胜率、Profit Factor、最大回撤、止损分析

**确定性验证**：两次相同参数运行，除 `run_meta.json` 中的墙上时钟字段外，所有输出文件（`orders.parquet`/`fills.parquet`/`positions.parquet`/`summary.json`）字节级完全一致。详见 [BACKTEST_ENGINE.md](docs/BACKTEST_ENGINE.md) 第七节。

---

## 九、注意事项

### 9.1 Redis Pub/Sub 无持久化

策略进程重启后会丢失中间的 1s Bar 事件。处理方式：重启后进入**数据预热期**（等滑动窗口填满后才允许触发），预热时长 = 策略最长计算窗口。

### 9.2 行情层是单点

行情层重启时策略短暂无数据。处理方式：策略检测到 Redis 超过阈值无数据时进入**保护模式**（禁止新开仓），行情层快速重启（目标 5 秒内）后自动恢复。

### 9.3 同账户多策略风控必须合并

K 线策略群进程内多个策略共用账户 A 的保证金。进程内风控层维护**账户总仓位**，各策略仓位之和不超过账户设定的总上限。

### 9.4 配置变更（V1：重启生效）

V1 不实现配置热更新。修改配置后，重启对应策略进程即可生效。进程重启时会自动执行启动对账流程（见 [EXECUTION_PROTOCOL.md](docs/EXECUTION_PROTOCOL.md) 第六节），确保账本状态一致。

**V2 扩展**：热更新时需严格按顺序执行：先注册新订阅并等待行情层 HTTP 200 确认 → 再激活新交易对策略逻辑；删除交易对时先停止新信号触发 → 处理完当前挂单/持仓 → 再取消订阅。

### 9.5 Parquet 写入不阻塞实时路径

aggTrade Parquet 写入在独立线程完成，与 1s 聚合和 Redis 发布完全隔离。写入失败只记录告警，不影响实时数据分发。

### 9.6 账本并发写入

多个策略进程同时写 PostgreSQL 时：每个进程持有自己的连接池，关键写入用事务，所有表包含 `strategy_id` 字段区分来源。

---

## 十、大周期数据存储方案（V2 优化项）

**当前状态（V1）**：Redis 只保留热数据（K 线只保留 `latest` 一条，1s Bar 不持久化），内存占用 < 200 KB（100 币种）。

**大周期数据需求**（4h/1d/1w/1M，数月至数年）在 V2 按需实现，有两种方案：

### 方案 A：策略直接读 DuckDB（简单，优先）

策略进程启动时直接查询行情层写入的 DuckDB 文件（只读模式，多进程并发读支持）：

```python
# 策略启动时预加载
conn = duckdb.connect("data/market_archive.duckdb", read_only=True)
daily_data = conn.execute("""
    SELECT * FROM klines 
    WHERE symbol = 'BTCUSDT' AND interval = '1d' 
    ORDER BY open_time DESC LIMIT 365
""").fetchdf()
self.daily_cache = daily_data
```

优点：实现最快，无需新增行情层接口。
缺点：策略进程需要知道 DuckDB 文件路径，多进程并发读有轻微锁竞争。

### 方案 B：行情层提供 HTTP API（解耦，后期优化）

行情层新增 `GET /klines` 接口，策略通过 HTTP 查询。优点是解耦更好，行情层可加缓存优化。缺点是需要额外实现接口。

**V1 决策**：暂不实现大周期数据查询，逼空插针策略（V1 主要目标）只需 1s Bar 和短期 K 线。若后续策略需要日线/周线，优先用方案 A 快速实现。

---

## 十一、已关闭的决策记录

| 编号 | 问题 | 决策 | 理由 |
|---|---|---|---|
| Q1 | 回测模式 | 独立回测引擎（虚拟时钟 + 预加载数据），不经 Redis | 确定性可复现，不依赖 Redis 背压；详见 BACKTEST_ENGINE.md |
| Q2 | K 线来源 | Binance K 线 WS 流，过滤 `isFinal=true` | 简单，不需要从 aggTrade 重建 |
| Q3 | 通信协议 | HTTP（订阅管理）+ Redis（数据分发） | gRPC 对低频控制操作无明显收益 |
| Q4 | 最小数据粒度 | 1s Bar（行情层聚合，不暴露原始 tick） | 策略最快需求是 1s，消息量降低百倍 |
| Q5 | 账本 Web 前端 | 扩展现有 Vue3 + naive-ui（`/web/` 目录） | 技术栈已有，echarts 图表现成可用 |
| Q6 | 大周期数据 | V1 暂不实现，V2 优先方案 A（直接读 DuckDB） | 逼空策略只需 1s Bar，按需扩展 |
| Q7 | Redis 数据保留 | K 线只保留 `latest`，1s Bar 不持久化 | 最小化内存，策略自维护窗口 |
| Q8 | 订阅接口 | 声明式 `PUT /subscriptions/{consumer_id}`，幂等覆盖完整期望集合 | consumer_id 稳定，重启重新 PUT，行情层差量更新 WS 流 |

---

## 十二、已知限制与V2规划

### V1 限制

| 限制项 | 说明 | V2 扩展方向 |
|---|---|---|
| **单策略独占交易对** | 每个策略进程独占一组交易对，避免持仓归属问题 | 多策略共享账户，虚拟子账本，成交分配规则 |
| **配置不支持热更新** | 修改配置需重启策略进程 | 轮询配置表，检测变更后热更新并重新注册订阅 |
| **紧急停止无确认回执** | 命令执行结果只写回 `executed_at`，无 ack 超时重试 | 带 ack 的可靠命令通道，超时告警 |
| **订阅无心跳检测** | 依赖进程正常关闭调用 DELETE；进程崩溃后孤立订阅需手动清理 | 租约/心跳机制，超时自动回收孤立订阅 |
| **1s Bar 事件语义简化** | 按接收时间分桶，允许 5s 迟到，无序列号 | 完整事件协议：窗口时间、序列号、质量标记、backfill 流程 |
| **Parquet 写入无恢复** | 失败只告警，不阻塞实时分发 | 有界队列、积压保护、缺口记录、确定的 backfill 流程 |
| **回测无部分成交/滑点** | 限价单全量按挂单价成交 | 部分成交模型、盘口深度、滑点、资金费率、保证金管理 |

### 补充文档

- **订单执行与对账协议**：[docs/EXECUTION_PROTOCOL.md](docs/EXECUTION_PROTOCOL.md)  
  订单状态机、Write-Ahead Log、User Data Stream 断线恢复、启动对账、幂等性保证
  
- **回测引擎设计**：[docs/BACKTEST_ENGINE.md](docs/BACKTEST_ENGINE.md)  
  虚拟时钟驱动、预加载数据、确定性成交模型、纸盘测试对比流程

- **逼空插针策略规格**：[SPIKE_STRATEGY_SPEC.md](SPIKE_STRATEGY_SPEC.md)  
  时间衰减止损、多档挂单、形态提前退出、参数校准流程

---

*文档状态：v1.1 修订版。核心架构已确定，订单执行和回测已补充独立文档。V1 聚焦单策略场景，复杂扩展留 V2。*
