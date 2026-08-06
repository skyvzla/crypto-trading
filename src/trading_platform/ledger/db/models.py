"""
账本层数据模型
使用 psycopg3 异步接口，提供 CRUD 方法和事务支持
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import class_row
from psycopg_pool import AsyncConnectionPool


@dataclass
class Order:
    """订单模型"""
    id: Optional[int] = None
    account_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    order_id: str = ""
    client_order_id: str = ""
    side: str = ""  # BUY, SELL
    order_type: str = ""  # LIMIT, MARKET, STOP_MARKET, TAKE_PROFIT_MARKET
    position_side: Optional[str] = None  # LONG, SHORT, BOTH
    quantity: Decimal = Decimal("0")
    price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    status: str = "NEW"  # NEW, PARTIALLY_FILLED, FILLED, CANCELED, REJECTED, EXPIRED
    filled_quantity: Decimal = Decimal("0")
    avg_fill_price: Optional[Decimal] = None
    commission: Optional[Decimal] = None
    commission_asset: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    exchange_created_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None


@dataclass
class Trade:
    """成交流水模型"""
    id: Optional[int] = None
    account_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    trade_id: str = ""
    order_id: str = ""
    client_order_id: str = ""
    side: str = ""  # BUY, SELL
    position_side: Optional[str] = None
    quantity: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    quote_quantity: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    commission_asset: str = ""
    realized_pnl: Optional[Decimal] = None
    is_maker: bool = False
    created_at: Optional[datetime] = None
    exchange_time: Optional[datetime] = None


@dataclass
class Position:
    """持仓模型"""
    id: Optional[int] = None
    account_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    position_side: str = ""  # LONG, SHORT, BOTH
    quantity: Decimal = Decimal("0")
    entry_price: Decimal = Decimal("0")
    mark_price: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    liquidation_price: Optional[Decimal] = None
    leverage: Optional[int] = None
    margin_type: Optional[str] = None
    isolated_margin: Optional[Decimal] = None
    updated_at: Optional[datetime] = None


@dataclass
class AccountControlState:
    """账户控制状态"""
    account_id: str
    desired_state: str  # NORMAL, HALT_NEW, CANCEL_ORDERS, CLOSE_ALL
    state_version: int
    updated_at: datetime
    updated_by: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class ControlCommandLog:
    """控制命令审计日志"""
    id: Optional[int] = None
    account_id: str = ""
    command: str = ""
    issued_by: Optional[str] = None
    issued_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    execution_result: Optional[str] = None
    execution_error: Optional[str] = None


@dataclass
class StrategyConfig:
    """策略配置"""
    id: Optional[int] = None
    account_id: str = ""
    strategy_id: str = ""
    config_key: str = ""
    config_value: str = ""
    config_type: str = "string"  # string, int, float, bool, json
    description: Optional[str] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


class LedgerDB:
    """账本数据库异步操作类"""

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    @asynccontextmanager
    async def transaction(self):
        """事务上下文管理器"""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                yield conn

    async def get_connection(self):
        """获取连接（用于非事务操作）"""
        return await self.pool.connection()

    # ============ 订单操作 ============

    async def insert_order(self, order: Order) -> int:
        """插入订单，返回 id"""
        async with self.pool.connection() as conn:
            result = await conn.execute(
                """
                INSERT INTO orders (
                    account_id, strategy_id, symbol, order_id, client_order_id,
                    side, order_type, position_side, quantity, price, stop_price,
                    status, filled_quantity, avg_fill_price, commission, commission_asset,
                    exchange_created_at
                ) VALUES (
                    %(account_id)s, %(strategy_id)s, %(symbol)s, %(order_id)s, %(client_order_id)s,
                    %(side)s, %(order_type)s, %(position_side)s, %(quantity)s, %(price)s, %(stop_price)s,
                    %(status)s, %(filled_quantity)s, %(avg_fill_price)s, %(commission)s, %(commission_asset)s,
                    %(exchange_created_at)s
                )
                ON CONFLICT (account_id, symbol, order_id) DO UPDATE
                SET status = EXCLUDED.status,
                    filled_quantity = EXCLUDED.filled_quantity,
                    avg_fill_price = EXCLUDED.avg_fill_price,
                    commission = EXCLUDED.commission,
                    commission_asset = EXCLUDED.commission_asset,
                    updated_at = NOW(),
                    filled_at = CASE WHEN EXCLUDED.status = 'FILLED' THEN NOW() ELSE orders.filled_at END
                RETURNING id
                """,
                {
                    "account_id": order.account_id,
                    "strategy_id": order.strategy_id,
                    "symbol": order.symbol,
                    "order_id": order.order_id,
                    "client_order_id": order.client_order_id,
                    "side": order.side,
                    "order_type": order.order_type,
                    "position_side": order.position_side,
                    "quantity": order.quantity,
                    "price": order.price,
                    "stop_price": order.stop_price,
                    "status": order.status,
                    "filled_quantity": order.filled_quantity,
                    "avg_fill_price": order.avg_fill_price,
                    "commission": order.commission,
                    "commission_asset": order.commission_asset,
                    "exchange_created_at": order.exchange_created_at,
                }
            )
            row = await result.fetchone()
            return row[0] if row else 0

    async def update_order_status(
        self,
        account_id: str,
        symbol: str,
        order_id: str,
        status: str,
        filled_quantity: Optional[Decimal] = None,
        avg_fill_price: Optional[Decimal] = None
    ) -> bool:
        """更新订单状态"""
        async with self.pool.connection() as conn:
            result = await conn.execute(
                """
                UPDATE orders
                SET status = %(status)s,
                    filled_quantity = COALESCE(%(filled_quantity)s, filled_quantity),
                    avg_fill_price = COALESCE(%(avg_fill_price)s, avg_fill_price),
                    updated_at = NOW(),
                    filled_at = CASE WHEN %(status)s = 'FILLED' THEN NOW() ELSE filled_at END
                WHERE account_id = %(account_id)s
                  AND symbol = %(symbol)s
                  AND order_id = %(order_id)s
                """,
                {
                    "account_id": account_id,
                    "symbol": symbol,
                    "order_id": order_id,
                    "status": status,
                    "filled_quantity": filled_quantity,
                    "avg_fill_price": avg_fill_price,
                }
            )
            return result.rowcount > 0

    async def get_orders(
        self,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Order]:
        """查询订单列表"""
        conditions = []
        params: Dict[str, Any] = {"limit": limit, "offset": offset}

        if account_id:
            conditions.append("account_id = %(account_id)s")
            params["account_id"] = account_id
        if strategy_id:
            conditions.append("strategy_id = %(strategy_id)s")
            params["strategy_id"] = strategy_id
        if symbol:
            conditions.append("symbol = %(symbol)s")
            params["symbol"] = symbol
        if status:
            conditions.append("status = %(status)s")
            params["status"] = status

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=class_row(Order))
            await cur.execute(
                f"""
                SELECT * FROM orders
                {where_clause}
                ORDER BY created_at DESC
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                params
            )
            return await cur.fetchall()

    # ============ 成交操作 ============

    async def insert_trade(self, trade: Trade) -> int:
        """插入成交记录（幂等，防止重复）"""
        async with self.pool.connection() as conn:
            result = await conn.execute(
                """
                INSERT INTO trades (
                    account_id, strategy_id, symbol, trade_id, order_id, client_order_id,
                    side, position_side, quantity, price, quote_quantity,
                    commission, commission_asset, realized_pnl, is_maker, exchange_time
                ) VALUES (
                    %(account_id)s, %(strategy_id)s, %(symbol)s, %(trade_id)s, %(order_id)s, %(client_order_id)s,
                    %(side)s, %(position_side)s, %(quantity)s, %(price)s, %(quote_quantity)s,
                    %(commission)s, %(commission_asset)s, %(realized_pnl)s, %(is_maker)s, %(exchange_time)s
                )
                ON CONFLICT (account_id, symbol, trade_id) DO NOTHING
                RETURNING id
                """,
                {
                    "account_id": trade.account_id,
                    "strategy_id": trade.strategy_id,
                    "symbol": trade.symbol,
                    "trade_id": trade.trade_id,
                    "order_id": trade.order_id,
                    "client_order_id": trade.client_order_id,
                    "side": trade.side,
                    "position_side": trade.position_side,
                    "quantity": trade.quantity,
                    "price": trade.price,
                    "quote_quantity": trade.quote_quantity,
                    "commission": trade.commission,
                    "commission_asset": trade.commission_asset,
                    "realized_pnl": trade.realized_pnl,
                    "is_maker": trade.is_maker,
                    "exchange_time": trade.exchange_time,
                }
            )
            row = await result.fetchone()
            return row[0] if row else 0

    async def get_trades(
        self,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Trade]:
        """查询成交流水"""
        conditions = []
        params: Dict[str, Any] = {"limit": limit, "offset": offset}

        if account_id:
            conditions.append("account_id = %(account_id)s")
            params["account_id"] = account_id
        if strategy_id:
            conditions.append("strategy_id = %(strategy_id)s")
            params["strategy_id"] = strategy_id
        if symbol:
            conditions.append("symbol = %(symbol)s")
            params["symbol"] = symbol

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=class_row(Trade))
            await cur.execute(
                f"""
                SELECT * FROM trades
                {where_clause}
                ORDER BY created_at DESC
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                params
            )
            return await cur.fetchall()

    # ============ 持仓操作 ============

    async def upsert_position(self, position: Position) -> int:
        """插入或更新持仓"""
        async with self.pool.connection() as conn:
            result = await conn.execute(
                """
                INSERT INTO positions (
                    account_id, strategy_id, symbol, position_side, quantity, entry_price,
                    mark_price, unrealized_pnl, liquidation_price, leverage, margin_type, isolated_margin
                ) VALUES (
                    %(account_id)s, %(strategy_id)s, %(symbol)s, %(position_side)s, %(quantity)s, %(entry_price)s,
                    %(mark_price)s, %(unrealized_pnl)s, %(liquidation_price)s, %(leverage)s, %(margin_type)s, %(isolated_margin)s
                )
                ON CONFLICT (account_id, strategy_id, symbol, position_side) DO UPDATE
                SET quantity = EXCLUDED.quantity,
                    entry_price = EXCLUDED.entry_price,
                    mark_price = EXCLUDED.mark_price,
                    unrealized_pnl = EXCLUDED.unrealized_pnl,
                    liquidation_price = EXCLUDED.liquidation_price,
                    leverage = EXCLUDED.leverage,
                    margin_type = EXCLUDED.margin_type,
                    isolated_margin = EXCLUDED.isolated_margin,
                    updated_at = NOW()
                RETURNING id
                """,
                {
                    "account_id": position.account_id,
                    "strategy_id": position.strategy_id,
                    "symbol": position.symbol,
                    "position_side": position.position_side,
                    "quantity": position.quantity,
                    "entry_price": position.entry_price,
                    "mark_price": position.mark_price,
                    "unrealized_pnl": position.unrealized_pnl,
                    "liquidation_price": position.liquidation_price,
                    "leverage": position.leverage,
                    "margin_type": position.margin_type,
                    "isolated_margin": position.isolated_margin,
                }
            )
            row = await result.fetchone()
            return row[0] if row else 0

    async def get_positions(
        self,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None
    ) -> List[Position]:
        """查询持仓"""
        conditions = []
        params: Dict[str, Any] = {}

        if account_id:
            conditions.append("account_id = %(account_id)s")
            params["account_id"] = account_id
        if strategy_id:
            conditions.append("strategy_id = %(strategy_id)s")
            params["strategy_id"] = strategy_id
        if symbol:
            conditions.append("symbol = %(symbol)s")
            params["symbol"] = symbol

        # 只返回有持仓的记录
        conditions.append("quantity > 0")

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=class_row(Position))
            await cur.execute(
                f"""
                SELECT * FROM positions
                {where_clause}
                ORDER BY updated_at DESC
                """,
                params
            )
            return await cur.fetchall()

    # ============ 紧急控制操作 ============

    async def get_account_control_state(self, account_id: str) -> Optional[AccountControlState]:
        """获取账户控制状态"""
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=class_row(AccountControlState))
            await cur.execute(
                "SELECT * FROM account_control_state WHERE account_id = %s",
                (account_id,)
            )
            return await cur.fetchone()

    async def update_account_control_state(
        self,
        account_id: str,
        desired_state: str,
        updated_by: Optional[str] = None,
        reason: Optional[str] = None
    ) -> int:
        """更新账户控制状态，返回新版本号"""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                # 原子递增版本号
                result = await conn.execute(
                    """
                    INSERT INTO account_control_state (account_id, desired_state, state_version, updated_by, reason)
                    VALUES (%(account_id)s, %(desired_state)s, 1, %(updated_by)s, %(reason)s)
                    ON CONFLICT (account_id) DO UPDATE
                    SET desired_state = EXCLUDED.desired_state,
                        state_version = account_control_state.state_version + 1,
                        updated_at = NOW(),
                        updated_by = EXCLUDED.updated_by,
                        reason = EXCLUDED.reason
                    RETURNING state_version
                    """,
                    {
                        "account_id": account_id,
                        "desired_state": desired_state,
                        "updated_by": updated_by,
                        "reason": reason,
                    }
                )
                row = await result.fetchone()
                new_version = row[0] if row else 1

                # 同时写入审计日志
                await conn.execute(
                    """
                    INSERT INTO control_command_log (account_id, command, issued_by)
                    VALUES (%(account_id)s, %(command)s, %(issued_by)s)
                    """,
                    {
                        "account_id": account_id,
                        "command": desired_state,
                        "issued_by": updated_by,
                    }
                )

                return new_version

    async def log_control_execution(
        self,
        account_id: str,
        execution_result: Optional[str] = None,
        execution_error: Optional[str] = None
    ) -> None:
        """记录控制命令执行结果（更新最新一条未执行的日志）"""
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE control_command_log
                SET executed_at = NOW(),
                    execution_result = %(execution_result)s,
                    execution_error = %(execution_error)s
                WHERE id = (
                    SELECT id FROM control_command_log
                    WHERE account_id = %(account_id)s AND executed_at IS NULL
                    ORDER BY issued_at DESC
                    LIMIT 1
                )
                """,
                {
                    "account_id": account_id,
                    "execution_result": execution_result,
                    "execution_error": execution_error,
                }
            )

    # ============ 策略配置操作 ============

    async def get_strategy_config(
        self,
        account_id: str,
        strategy_id: str,
        config_key: Optional[str] = None
    ) -> List[StrategyConfig]:
        """获取策略配置"""
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=class_row(StrategyConfig))
            if config_key:
                await cur.execute(
                    """
                    SELECT * FROM strategy_config
                    WHERE account_id = %s AND strategy_id = %s AND config_key = %s
                    """,
                    (account_id, strategy_id, config_key)
                )
            else:
                await cur.execute(
                    """
                    SELECT * FROM strategy_config
                    WHERE account_id = %s AND strategy_id = %s
                    ORDER BY config_key
                    """,
                    (account_id, strategy_id)
                )
            return await cur.fetchall()

    async def upsert_strategy_config(
        self,
        account_id: str,
        strategy_id: str,
        config_key: str,
        config_value: str,
        config_type: str = "string",
        description: Optional[str] = None,
        updated_by: Optional[str] = None
    ) -> int:
        """插入或更新策略配置"""
        async with self.pool.connection() as conn:
            result = await conn.execute(
                """
                INSERT INTO strategy_config (
                    account_id, strategy_id, config_key, config_value, config_type, description, updated_by
                )
                VALUES (%(account_id)s, %(strategy_id)s, %(config_key)s, %(config_value)s, %(config_type)s, %(description)s, %(updated_by)s)
                ON CONFLICT (account_id, strategy_id, config_key) DO UPDATE
                SET config_value = EXCLUDED.config_value,
                    config_type = EXCLUDED.config_type,
                    description = EXCLUDED.description,
                    updated_at = NOW(),
                    updated_by = EXCLUDED.updated_by
                RETURNING id
                """,
                {
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "config_key": config_key,
                    "config_value": config_value,
                    "config_type": config_type,
                    "description": description,
                    "updated_by": updated_by,
                }
            )
            row = await result.fetchone()
            return row[0] if row else 0


async def create_connection_pool(dsn: str, min_size: int = 2, max_size: int = 10) -> AsyncConnectionPool:
    """创建连接池"""
    pool = AsyncConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        timeout=30,
        open=False
    )
    await pool.open()
    return pool
