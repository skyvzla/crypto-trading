# 订单执行与对账协议

> 文档版本：v1.0
> 创建日期：2026-08-06
> 状态：V1单策略场景，多策略共享账户留V2

---

## 一、适用范围

本协议适用于 `trading_platform/` 下所有策略进程与交易所的订单交互，定义：

- 订单生命周期状态机
- 下单与数据库写入的一致性保证
- User Data Stream 断线恢复
- 进程重启后的状态对账
- 幂等性与去重规则

**V1约束**：每个策略进程独占一组交易对，同一交易对不会被多个策略同时交易，避免持仓归属问题。

---

## 二、订单唯一标识

### clientOrderId 命名规则

每个订单必须携带全局唯一的 `clientOrderId`，格式：

```
{abbrev}_{symbol}_{evhex}_{tier}
```

**示例**：
```
spk_BTCUSDT_668FD3C2_t1
```

**字段说明**：

| 字段 | 说明 | 示例 | 最大长度 |
|---|---|---|---|
| abbrev | 策略缩写（3-4字符） | `spk`, `brk` | 4 |
| symbol | 交易对 | `BTCUSDT` | 10 |
| evhex | 事件开始时间的Unix秒转16进制（8字符） | `668FD3C2` | 8 |
| tier | 档位 | `t1`, `t2`, `t3` | 2 |

**约束**：
- 总长度 ≤ 36 字符（含分隔符 `_`，最长 = 4+1+10+1+8+1+2 = **27 字符**）
- 只能包含字母、数字、下划线
- 相同事件的不同档位因 tier 不同而唯一

```python
def generate_client_order_id(abbrev: str, symbol: str, event_ts_s: int, tier: int) -> str:
    """生成 clientOrderId，总长度 ≤ 36 字符"""
    hex_ts = format(event_ts_s, '08X')  # 8位大写十六进制
    cid = f"{abbrev}_{symbol}_{hex_ts}_t{tier}"
    assert len(cid) <= 36, f"clientOrderId too long: {len(cid)}"
    return cid
```

---

## 三、订单状态机

### 本地状态定义

| 状态 | 说明 | 可转移到 |
|---|---|---|
| `PENDING_NEW` | 已写数据库，REST 调用尚未发出 | `NEW`, `SUBMIT_UNKNOWN`, `FAILED` |
| `SUBMIT_UNKNOWN` | REST 超时/网络异常，无法确认是否已被交易所接受 | `NEW`, `FAILED`（查询后确定） |
| `NEW` | 交易所已接受，等待成交 | `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `EXPIRED` |
| `PARTIALLY_FILLED` | 部分成交 | `FILLED`, `CANCELLED` |
| `FILLED` | 完全成交 | （终态） |
| `CANCELLED` | 已撤销 | （终态） |
| `EXPIRED` | 超时失效 | （终态） |
| `FAILED` | 下单**明确**失败（风控拒绝、交易所拒单，错误码明确） | （终态） |
| `UNKNOWN` | 对账后发现交易所状态无法确认 | （需人工介入） |

### 正常路径

```
PENDING_NEW → NEW → PARTIALLY_FILLED → FILLED
                  → FILLED
                  → CANCELLED
                  → EXPIRED
```

### 异常路径

```
PENDING_NEW → SUBMIT_UNKNOWN → NEW（查询确认交易所已接单）
                             → FAILED（查询确认交易所未接单）

PENDING_NEW → FAILED（交易所明确返回拒单错误码，非超时）

NEW → UNKNOWN（对账后发现交易所无此订单，且不在历史中）
```

---

## 四、下单流程（Write-Ahead Log）

采用**数据库先行**原则，确保本地状态可恢复。

### 4.1 新建订单流程

```python
async def place_order(order_intent: OrderIntent) -> Order:
    """
    下单完整流程
    """
    client_order_id = generate_client_order_id(
        abbrev=order_intent.strategy_abbrev,
        symbol=order_intent.symbol,
        event_ts_s=order_intent.event_ts_s,
        tier=order_intent.tier
    )
    
    # 2. 数据库先行：写入 PENDING_NEW
    order = Order(
        client_order_id=client_order_id,
        account_id=order_intent.account_id,    # 账户维度（V1 固定一个账户）
        symbol=order_intent.symbol,
        side=order_intent.side,
        price=order_intent.price,
        quantity=order_intent.quantity,
        status='PENDING_NEW',
        strategy=order_intent.strategy,
        event_id=order_intent.event_id,
        created_at=now()
    )
    await db.insert_order(order)
    
    # 3. 调用交易所 REST API
    try:
        response = await asyncio.wait_for(
            binance_client.post_order(
                symbol=order.symbol,
                side=order.side,
                type='LIMIT',
                timeInForce='GTX',
                price=order.price,
                quantity=order.quantity,
                newClientOrderId=client_order_id
            ),
            timeout=5.0
        )
        
        # 4. 交易所明确接受 → NEW
        order.status = 'NEW'
        order.binance_order_id = response['orderId']
        order.update_time = response['transactTime']
        await db.update_order(order)
        return order
        
    except asyncio.TimeoutError:
        # 5. 超时：交易所状态不明，不能标为 FAILED
        order.status = 'SUBMIT_UNKNOWN'
        await db.update_order(order)
        # 立即异步查询，查询期间阻塞该 symbol 新增风险
        asyncio.create_task(_resolve_submit_unknown(order))
        raise SubmitUnknownError(order)
        
    except BinanceAPIException as e:
        # 6. 交易所明确拒单（风控/参数错误/余额不足）→ FAILED
        order.status = 'FAILED'
        order.reject_reason = f"code={e.code} msg={e.message}"
        await db.update_order(order)
        raise OrderPlacementFailed(order, e)


async def _resolve_submit_unknown(order: Order) -> None:
    """
    REST 超时后立即查询，确定订单实际状态。
    只有明确解析为终态后才解除 symbol 阻塞；
    持续未知时保持阻塞，等待人工处理或冷启动对账。
    """
    risk_guard.block_symbol(order.account_id, order.symbol, reason='submit_unknown')
    resolved = False
    
    try:
        for attempt in range(3):
            await asyncio.sleep(attempt)      # 0s, 1s, 2s 退避
            try:
                result = await binance_client.query_order(
                    symbol=order.symbol,
                    origClientOrderId=order.client_order_id
                )
                if result:
                    order.status = map_binance_status(result['status'])
                    order.binance_order_id = result['orderId']
                    order.filled_quantity = Decimal(result['executedQty'])
                    await db.update_order(order)
                    logger.info(f"SUBMIT_UNKNOWN resolved to {order.status}: {order.client_order_id}")
                    resolved = True
                    return
            except Exception as exc:
                logger.warning(f"query_order attempt {attempt+1} failed: {exc}")
        
        # 三次查询均失败，保持 SUBMIT_UNKNOWN，发告警，保持阻塞
        await alert.send_critical(
            f"Cannot resolve SUBMIT_UNKNOWN after 3 attempts: {order.client_order_id}. "
            f"Symbol {order.symbol} blocked until manual resolution or next startup reconciliation."
        )
    finally:
        # 只有明确解析后才解除阻塞；状态仍为 SUBMIT_UNKNOWN 时保持阻塞
        if resolved:
            risk_guard.unblock_symbol(order.account_id, order.symbol)
```

**关键点**：
- 数据库写入在 REST 调用之前
- **超时** → `SUBMIT_UNKNOWN`（不是终态），立即启动后台查询，同时阻塞该账户+symbol 的新开仓
- **明确拒单** → `FAILED`（终态）
- 重启对账也会扫描 `SUBMIT_UNKNOWN` 状态的订单

### 4.2 撤单流程

```python
async def cancel_order(client_order_id: str) -> None:
    """
    撤单流程
    """
    # 1. 查询当前状态
    order = await db.get_order(client_order_id)
    if order.status not in ('NEW', 'PARTIALLY_FILLED'):
        return  # 已终态，无需撤单
    
    # 2. 调用交易所撤单API
    try:
        response = await binance_client.cancel_order(
            symbol=order.symbol,
            origClientOrderId=client_order_id
        )
        
        # 3. 交易所确认撤销
        order.status = 'CANCELLED'
        order.update_time = response['transactTime']
        await db.update_order(order)
        
    except BinanceAPIException as e:
        if e.code == -2011:  # Unknown order
            # 可能已成交或已撤销，等待User Data Stream确认
            pass
        else:
            raise
```

**注意**：撤单成功不代表未成交，可能存在竞态：
- 撤单请求到达前已成交 → User Data Stream会推送成交
- 部分成交后撤销 → 已成交部分仍然生效

---

## 五、User Data Stream 处理

### 5.1 executionReport 事件处理

```python
async def on_execution_report(event: dict) -> None:
    """
    处理 User Data Stream 的 executionReport 事件
    修复P1问题6：原子性+状态转换验证+首次通知
    """
    client_order_id = event['c']
    execution_type = event['x']
    new_status = map_binance_status(event['X'])
    
    # 1. 查询本地订单
    order = await db.get_order(client_order_id)
    if not order:
        logger.warning(f"Received executionReport for unknown order: {client_order_id}")
        return
    
    # 2. 状态转换验证（防止倒退）
    old_status = order.status
    if not _is_valid_transition(old_status, new_status):
        logger.error(f"Invalid state transition {old_status} → {new_status} for {client_order_id}")
        return
    
    # 3. 开启事务：插入成交 + 更新订单状态
    async with db.transaction():
        should_notify = False
        
        if execution_type == 'TRADE':
            trade = Trade(
                trade_id=event['t'],
                account_id=order.account_id,
                order_id=order.id,
                client_order_id=client_order_id,
                symbol=event['s'],
                side=event['S'],
                price=Decimal(event['L']),
                quantity=Decimal(event['l']),
                commission=Decimal(event['n']),
                commission_asset=event['N'],
                trade_time=event['T'],
                is_maker=event['m']
            )
            
            # 幂等插入：只有首次插入成功才需要通知策略
            inserted = await db.insert_trade_if_not_exists(trade)
            if inserted:
                order.filled_quantity += trade.quantity
                should_notify = True
        
        # 更新订单状态（状态变化才通知）
        if order.status != new_status:
            order.status = new_status
            order.binance_order_id = event['i']
            order.update_time = event['T']
            should_notify = True
        
        await db.update_order(order)
    
    # 4. 事务提交后才通知策略（避免重复副作用）
    if should_notify:
        await strategy.on_order_update(order)


def _is_valid_transition(old: str, new: str) -> bool:
    """验证订单状态转换是否合法（防止倒退）"""
    transitions = {
        'PENDING_NEW': ['SUBMIT_UNKNOWN', 'NEW', 'FAILED'],
        'SUBMIT_UNKNOWN': ['NEW', 'FAILED'],
        'NEW': ['PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'EXPIRED'],
        'PARTIALLY_FILLED': ['FILLED', 'CANCELLED'],
        'FILLED': [],
        'CANCELLED': [],
        'EXPIRED': [],
        'FAILED': [],
        'UNKNOWN': []
    }
    return new in transitions.get(old, [])
```

**去重规则**：
- Trade 唯一键：`(account_id, symbol, trade_id)`
- 只有首次成功插入 trade 或首次状态转换时才通知策略
- 事务保证 insert + update 原子性

### 5.2 断线重连后补录

```python
async def on_user_stream_reconnect() -> None:
    """
    User Data Stream 重连后的补录流程
    复用冷启动对账逻辑，确保按账户隔离且包含 SUBMIT_UNKNOWN
    """
    logger.warning("User Data Stream reconnected, running reconciliation")
    # 直接复用冷启动对账函数（修复P1问题5 - 账户隔离+SUBMIT_UNKNOWN）
    await startup_reconciliation()
```

---

## 六、进程启动恢复

### 6.1 冷启动对账流程

```python
async def startup_reconciliation() -> None:
    """
    进程启动时的状态对账
    """
    logger.info("Starting order reconciliation...")
    
    # 1. 查询本地非终态订单（按账户过滤）
    local_orders = await db.get_orders(
        account_id=binance_client.account_id,  # 只对账当前账户的订单
        status=['PENDING_NEW', 'SUBMIT_UNKNOWN', 'NEW', 'PARTIALLY_FILLED']
    )
    
    if not local_orders:
        logger.info("No pending orders, reconciliation complete.")
        return
    
    # 2. 查询交易所当前活跃订单
    exchange_open_orders = await binance_client.get_open_orders()
    exchange_map = {o['clientOrderId']: o for o in exchange_open_orders}
    
    # 3. 逐个对账
    for order in local_orders:
        if order.status in ('PENDING_NEW', 'SUBMIT_UNKNOWN'):
            # 本地显示未确认状态
            if order.client_order_id in exchange_map:
                # 交易所有，补偿更新为 NEW
                exch_order = exchange_map[order.client_order_id]
                order.status = map_binance_status(exch_order['status'])
                order.binance_order_id = exch_order['orderId']
                await db.update_order(order)
                logger.info(f"Reconciled {order.client_order_id}: {order.status}")
            else:
                # 不在 open orders，可能已成交/撤销/过期，或真的未成功发送
                hist_order = await binance_client.query_order(
                    symbol=order.symbol,
                    origClientOrderId=order.client_order_id
                )
                
                if hist_order:
                    # 找到历史记录（已成交/撤销/过期）
                    order.status = map_binance_status(hist_order['status'])
                    order.binance_order_id = hist_order['orderId']
                    order.filled_quantity = Decimal(hist_order['executedQty'])
                    await db.update_order(order)
                    
                    # 补录成交记录
                    await backfill_trades(order, hist_order['orderId'])
                    logger.info(f"Reconciled {order.client_order_id}: {order.status} (from history)")
                else:
                    # 完全找不到，确认为未成功发送
                    order.status = 'FAILED'
                    order.reject_reason = 'Not found on exchange after startup'
                    await db.update_order(order)
                    logger.info(f"Marked {order.client_order_id} as FAILED: not found")
        
        elif order.status in ('NEW', 'PARTIALLY_FILLED'):
            # 本地显示活跃，检查交易所状态
            if order.client_order_id in exchange_map:
                # 交易所仍活跃，更新状态
                exch_order = exchange_map[order.client_order_id]
                order.status = map_binance_status(exch_order['status'])
                order.filled_quantity = Decimal(exch_order['executedQty'])
                await db.update_order(order)
            else:
                # 交易所无，查询历史
                hist_order = await binance_client.query_order(
                    symbol=order.symbol,
                    origClientOrderId=order.client_order_id
                )
                
                if hist_order:
                    # 已终态
                    order.status = map_binance_status(hist_order['status'])
                    order.filled_quantity = Decimal(hist_order['executedQty'])
                    await db.update_order(order)
                    
                    # 补录成交
                    await backfill_trades(order, hist_order['orderId'])
                else:
                    # 找不到，异常
                    order.status = 'UNKNOWN'
                    await db.update_order(order)
                    await alert.send(f"Order {order.client_order_id} UNKNOWN on startup")
    
    logger.info("Order reconciliation complete.")
```

### 6.2 持仓对账

```python
async def startup_position_check() -> None:
    """
    对账本地持仓与交易所实际持仓
    """
    account_id = binance_client.account_id

    # 1. 查询本地未平仓（按账户过滤）
    local_positions = await db.get_open_positions(account_id=account_id)
    local_map = {p.symbol: p for p in local_positions}

    # 2. 查询交易所当前持仓（过滤掉 positionAmt == 0）
    exchange_positions = await binance_client.get_position_risk()
    exchange_map = {
        p['symbol']: Decimal(p['positionAmt'])
        for p in exchange_positions
        if Decimal(p['positionAmt']) != 0
    }

    # 3. 交集：双方都有，但数量不一致
    for symbol in set(local_map) | set(exchange_map):
        local_qty = local_map[symbol].signed_quantity if symbol in local_map else Decimal(0)
        exchange_qty = exchange_map.get(symbol, Decimal(0))

        if local_qty != exchange_qty:
            logger.error(
                f"Position mismatch [{account_id}] {symbol}: "
                f"local={local_qty}, exchange={exchange_qty}"
            )
            await alert.send_critical(
                f"Position mismatch [{account_id}] {symbol}: "
                f"local={local_qty} exchange={exchange_qty}. Manual intervention required."
            )
            await risk_guard.block_symbol(account_id, symbol, reason='position_mismatch')

        # 4. 孤儿持仓：交易所有、本地没有
        if symbol in exchange_map and symbol not in local_map:
            logger.error(
                f"Orphan position [{account_id}] {symbol}: "
                f"exchange={exchange_qty}, local=none"
            )
            await alert.send_critical(
                f"Orphan position [{account_id}] {symbol}={exchange_qty}. "
                "Position exists on exchange but not in local DB. "
                "Manual intervention required."
            )
            await risk_guard.block_symbol(account_id, symbol, reason='orphan_position')
```

**V1 约束**：任何持仓不一致或孤儿持仓，禁止该账户+symbol 新开仓，发告警，等待人工介入。

---

## 七、幂等性保证

### 7.1 订单下单幂等

**问题**：REST调用超时，但交易所实际已接单。

**方案**：使用`clientOrderId`保证幂等。

```python
# Binance行为：相同clientOrderId重复下单 → 返回错误码 -2010
# 策略：超时后不重试，在启动对账时发现并更新本地状态
```

### 7.2 成交记录去重

**问题**：User Data Stream可能推送重复的`executionReport`。

**方案**：以 `(account_id, symbol, trade_id)` 三元组为唯一键。

插入时：
```python
await db.execute(
    """
    INSERT INTO trades (...)
    VALUES (...)
    ON CONFLICT (account_id, symbol, trade_id) DO NOTHING
    """
)
```

**说明**：Binance 的 `trade_id` 在单个账户+单个交易对内唯一，跨账户或跨交易对可能重复，因此必须使用三元组。

---

## 八、异常场景处理

### 场景1：下单成功但User Data Stream断线

**现象**：下单后立即断线，未收到成交回报。

**处理**：
1. 订单状态保持`NEW`
2. 重连后触发`on_user_stream_reconnect()`
3. 查询交易所订单状态，补录成交

### 场景2：撤单成功但成交回报延迟

**现象**：撤单API返回成功，但之前已有部分成交，成交回报延迟到达。

**处理**：
1. 撤单后订单状态更新为`CANCELLED`
2. 后续收到`executionReport`（type=TRADE）
3. 检查订单状态，如果已`CANCELLED`但有新成交，更新为`PARTIALLY_FILLED`并记录成交

```python
if execution_type == 'TRADE':
    if order.status == 'CANCELLED' and order.filled_quantity == 0:
        # 撤单后才到的成交，更新状态
        order.status = 'PARTIALLY_FILLED'
    
    await db.insert_trade_if_not_exists(trade)
    order.filled_quantity += trade.quantity
    await db.update_order(order)
```

### 场景3：进程重启期间有成交

**现象**：进程停止期间，挂单部分成交。

**处理**：
1. 启动时执行`startup_reconciliation()`
2. 查询交易所历史订单，发现已部分成交
3. 补录成交记录，更新订单状态
4. 通知策略层（如触发止损/止盈检查）

### 场景4：本地有订单，交易所完全找不到

**现象**：对账时发现订单在交易所不存在，且不在历史订单中。

**可能原因**：
- clientOrderId生成重复（代码bug）
- 数据库被误修改
- 交易所API异常

**处理**：
1. 标记订单状态为`UNKNOWN`
2. 发送高优先级告警
3. 禁止该币种新开仓（进入保护模式）
4. 需人工介入，核查原因

---

## 九、数据库Schema

### orders表

```sql
CREATE TABLE orders (
    id BIGSERIAL PRIMARY KEY,
    client_order_id VARCHAR(64) NOT NULL,
    binance_order_id BIGINT,
    account_id VARCHAR(32) NOT NULL,        -- 账户维度（防止跨账户对账）
    strategy VARCHAR(32) NOT NULL,
    event_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    side VARCHAR(4) NOT NULL,               -- BUY, SELL
    type VARCHAR(10) NOT NULL,              -- LIMIT, MARKET
    price DECIMAL(18, 8),
    quantity DECIMAL(18, 8) NOT NULL,
    filled_quantity DECIMAL(18, 8) DEFAULT 0,
    status VARCHAR(20) NOT NULL,            -- PENDING_NEW, SUBMIT_UNKNOWN, NEW, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED, FAILED, UNKNOWN
    reject_reason TEXT,
    error_note TEXT,
    created_at TIMESTAMP NOT NULL,
    update_time BIGINT,
    CONSTRAINT uq_orders_client_order_id UNIQUE (client_order_id)
);

-- 索引定义（PostgreSQL语法）
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_account_symbol ON orders(account_id, symbol);
CREATE INDEX idx_orders_strategy_event ON orders(strategy, event_id);
```

### trades表

```sql
CREATE TABLE trades (
    id BIGSERIAL PRIMARY KEY,
    trade_id BIGINT NOT NULL,               -- Binance trade ID
    account_id VARCHAR(32) NOT NULL,        -- 账户维度
    order_id BIGINT NOT NULL,
    client_order_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    side VARCHAR(4) NOT NULL,
    price DECIMAL(18, 8) NOT NULL,
    quantity DECIMAL(18, 8) NOT NULL,
    commission DECIMAL(18, 8) NOT NULL,
    commission_asset VARCHAR(10) NOT NULL,
    trade_time BIGINT NOT NULL,
    is_maker BOOLEAN NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT uq_trades_account_symbol_trade UNIQUE (account_id, symbol, trade_id)
);

-- 索引定义
CREATE INDEX idx_trades_order_id ON trades(order_id);
CREATE INDEX idx_trades_client_order_id ON trades(client_order_id);
CREATE INDEX idx_trades_account_symbol ON trades(account_id, symbol);
```

**成交唯一键说明**：
- `(account_id, symbol, trade_id)` 三元组保证唯一
- Binance 的 `trade_id` 在单个账户+单个交易对内唯一，跨账户或跨交易对可能重复
- 插入时：`ON CONFLICT (account_id, symbol, trade_id) DO NOTHING`

### positions表

```sql
CREATE TABLE positions (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(32) NOT NULL,        -- 账户维度
    strategy VARCHAR(32) NOT NULL,
    event_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    side VARCHAR(4) NOT NULL,               -- LONG, SHORT
    entry_price DECIMAL(18, 8) NOT NULL,
    quantity DECIMAL(18, 8) NOT NULL,
    remaining_quantity DECIMAL(18, 8) NOT NULL,
    unrealized_pnl DECIMAL(18, 8),
    realized_pnl DECIMAL(18, 8) DEFAULT 0,
    status VARCHAR(20) NOT NULL,            -- OPEN, CLOSED
    opened_at TIMESTAMP NOT NULL,
    closed_at TIMESTAMP,
    CONSTRAINT uq_positions_account_strategy_event UNIQUE (account_id, strategy, event_id)
);

-- 索引定义
CREATE INDEX idx_positions_status ON positions(status);
CREATE INDEX idx_positions_account_symbol ON positions(account_id, symbol);
```

---

## 十、监控指标

| 指标 | 类型 | 告警阈值 |
|---|---|---|
| `orders_pending_new_duration_seconds` | Histogram | p99 > 5s |
| `orders_unknown_total` | Counter | > 0 |
| `position_mismatch_total` | Counter | > 0 |
| `user_stream_reconnect_total` | Counter | > 3 /小时 |
| `trade_backfill_total` | Counter | - |
| `order_reconciliation_duration_seconds` | Histogram | > 30s |

---

## 十一、V2扩展方向

当前协议适用于V1单策略场景，V2需要扩展：

1. **虚拟子账本**：多策略共享账户时，每个策略维护虚拟持仓
2. **成交分配规则**：同symbol多策略交易时，成交如何归属
3. **净额结算**：策略A做多、策略B做空同symbol，交易所只有净持仓
4. **跨策略风控**：总持仓限制、保证金共享
5. **配置化成交模型**：不同策略使用不同的taker/maker假设

---

*文档状态：V1完整，已覆盖单策略场景的所有核心流程。*
