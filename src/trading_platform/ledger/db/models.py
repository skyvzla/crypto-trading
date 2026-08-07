"""账本 PostgreSQL 数据访问模型。"""

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, AsyncIterator, Optional, Sequence

from psycopg.rows import class_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from trading_platform.shared.events import StrategyAuditEvent


@dataclass
class Order:
    id: Optional[int] = None
    account_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    order_id: str = ""
    client_order_id: str = ""
    campaign_id: Optional[str] = None
    side: str = ""
    order_type: str = ""
    position_side: Optional[str] = None
    quantity: Decimal = Decimal("0")
    price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    status: str = "NEW"
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
    id: Optional[int] = None
    account_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    trade_id: str = ""
    order_id: str = ""
    client_order_id: str = ""
    campaign_id: Optional[str] = None
    side: str = ""
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
    id: Optional[int] = None
    account_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    position_side: str = ""
    quantity: Decimal = Decimal("0")
    entry_price: Decimal = Decimal("0")
    mark_price: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    liquidation_price: Optional[Decimal] = None
    leverage: Optional[int] = None
    margin_type: Optional[str] = None
    isolated_margin: Optional[Decimal] = None
    exchange_time: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class SubcategoryAdmission:
    subcategory: str
    enabled: bool
    version: int
    updated_at: datetime
    updated_by: str
    reason: Optional[str] = None


@dataclass
class SubcategoryAdmissionAudit:
    id: int
    subcategory: str
    previous_enabled: Optional[bool]
    enabled: bool
    version: int
    changed_at: datetime
    changed_by: str
    reason: Optional[str] = None


@dataclass
class StrategyAuditRecord:
    id: Optional[int] = None
    event_key: str = ""
    account_id: str = ""
    event_time: int = 0
    event_type: str = ""
    symbol: str = ""
    strategy_id: str = ""
    campaign_id: Optional[str] = None
    details: dict[str, Any] | None = None
    created_at: Optional[datetime] = None


@dataclass
class StrategyRuntimeStatus:
    account_id: str
    strategy_id: str
    instance_id: str
    mode: str
    status: str
    entry_enabled: bool
    halted: bool
    halt_reason: Optional[str]
    gate_conditions: dict[str, Any]
    started_at: datetime
    heartbeat_at: datetime
    stopped_at: Optional[datetime]


@dataclass
class CampaignPnLSummary:
    account_id: str
    strategy_id: str
    symbol: str
    campaign_id: str
    trade_count: int
    sell_quantity: Decimal
    sell_avg_price: Optional[Decimal]
    buy_quantity: Decimal
    buy_avg_price: Optional[Decimal]
    total_commission: Decimal
    commission_asset: Optional[str]
    gross_realized_pnl: Decimal
    net_realized_pnl: Decimal
    remaining_quantity: Decimal
    has_open_quantity: bool
    acquired_at: Optional[datetime]
    first_fill_at: datetime
    last_fill_at: datetime
    closed_at: Optional[datetime]
    released_at: Optional[datetime]
    lifecycle_duration_ms: Optional[int]
    pnl_facts_complete: bool


class VersionConflictError(Exception):
    """乐观并发版本冲突。"""


class CampaignPnLFactsError(RuntimeError):
    """Campaign 成交事实不完整或互相冲突，无法可靠计算盈亏。"""


class LedgerDB:
    _STRATEGY_RUNTIME_STATUS_UPSERT = """
        INSERT INTO strategy_runtime_status (
            account_id, strategy_id, instance_id, mode, status,
            entry_enabled, halted, halt_reason, gate_conditions,
            started_at, heartbeat_at, stopped_at
        ) VALUES (
            %(account_id)s, %(strategy_id)s, %(instance_id)s, %(mode)s,
            %(status)s, %(entry_enabled)s, %(halted)s, %(halt_reason)s,
            %(gate_conditions)s, %(started_at)s, %(heartbeat_at)s,
            %(stopped_at)s
        )
        ON CONFLICT (account_id, strategy_id) DO UPDATE
        SET instance_id = EXCLUDED.instance_id,
            mode = EXCLUDED.mode,
            status = EXCLUDED.status,
            entry_enabled = EXCLUDED.entry_enabled,
            halted = EXCLUDED.halted,
            halt_reason = EXCLUDED.halt_reason,
            gate_conditions = EXCLUDED.gate_conditions,
            started_at = CASE
                WHEN strategy_runtime_status.instance_id = EXCLUDED.instance_id
                    THEN strategy_runtime_status.started_at
                ELSE EXCLUDED.started_at
            END,
            heartbeat_at = EXCLUDED.heartbeat_at,
            stopped_at = EXCLUDED.stopped_at
        WHERE (
                strategy_runtime_status.instance_id = EXCLUDED.instance_id
                AND strategy_runtime_status.heartbeat_at <= EXCLUDED.heartbeat_at
              )
           OR (
                strategy_runtime_status.instance_id <> EXCLUDED.instance_id
                AND strategy_runtime_status.started_at < EXCLUDED.started_at
              )
        RETURNING account_id
    """
    _ORDER_UPSERT = """
        INSERT INTO orders (
            account_id, strategy_id, symbol, order_id, client_order_id, campaign_id,
            side, order_type, position_side, quantity, price, stop_price,
            status, filled_quantity, avg_fill_price, commission,
            commission_asset, exchange_created_at
        ) VALUES (
            %(account_id)s, %(strategy_id)s, %(symbol)s, %(order_id)s,
            %(client_order_id)s, %(campaign_id)s, %(side)s, %(order_type)s,
            %(position_side)s,
            %(quantity)s, %(price)s, %(stop_price)s, %(status)s,
            %(filled_quantity)s, %(avg_fill_price)s, %(commission)s,
            %(commission_asset)s, %(exchange_created_at)s
        )
        ON CONFLICT (account_id, symbol, order_id) DO UPDATE
        SET status = EXCLUDED.status,
            campaign_id = COALESCE(orders.campaign_id, EXCLUDED.campaign_id),
            filled_quantity = EXCLUDED.filled_quantity,
            avg_fill_price = EXCLUDED.avg_fill_price,
            commission = EXCLUDED.commission,
            commission_asset = EXCLUDED.commission_asset,
            updated_at = NOW(),
            filled_at = CASE
                WHEN EXCLUDED.status = 'FILLED' THEN NOW()
                ELSE orders.filled_at
            END
        WHERE orders.campaign_id IS NULL
           OR EXCLUDED.campaign_id IS NULL
           OR orders.campaign_id = EXCLUDED.campaign_id
        RETURNING id
    """
    _TRADE_INSERT = """
        INSERT INTO trades (
            account_id, strategy_id, symbol, trade_id, order_id, client_order_id,
            campaign_id,
            side, position_side, quantity, price, quote_quantity, commission,
            commission_asset, realized_pnl, is_maker, exchange_time
        ) VALUES (
            %(account_id)s, %(strategy_id)s, %(symbol)s, %(trade_id)s,
            %(order_id)s, %(client_order_id)s, %(campaign_id)s, %(side)s,
            %(position_side)s,
            %(quantity)s, %(price)s, %(quote_quantity)s, %(commission)s,
            %(commission_asset)s, %(realized_pnl)s, %(is_maker)s, %(exchange_time)s
        )
        ON CONFLICT (account_id, symbol, trade_id) DO NOTHING
        RETURNING id
    """
    _ACCOUNT_POSITION_UPSERT = """
        INSERT INTO positions (
            account_id, strategy_id, symbol, position_side, quantity, entry_price,
            unrealized_pnl, margin_type, isolated_margin, exchange_time
        ) VALUES (
            %(account_id)s, %(strategy_id)s, %(symbol)s, %(position_side)s,
            %(quantity)s, %(entry_price)s, %(unrealized_pnl)s, %(margin_type)s,
            %(isolated_margin)s, %(exchange_time)s
        )
        ON CONFLICT (account_id, strategy_id, symbol, position_side) DO UPDATE
        SET quantity = EXCLUDED.quantity,
            entry_price = EXCLUDED.entry_price,
            unrealized_pnl = EXCLUDED.unrealized_pnl,
            margin_type = EXCLUDED.margin_type,
            isolated_margin = EXCLUDED.isolated_margin,
            exchange_time = EXCLUDED.exchange_time,
            updated_at = NOW()
        WHERE positions.exchange_time IS NULL
           OR positions.exchange_time <= EXCLUDED.exchange_time
        RETURNING id
    """
    _STRATEGY_AUDIT_INSERT = """
        INSERT INTO strategy_audit_events (
            event_key, account_id, event_time, event_type, symbol,
            strategy_id, campaign_id, details
        ) VALUES (
            %(event_key)s, %(account_id)s, %(event_time)s, %(event_type)s,
            %(symbol)s, %(strategy_id)s, %(campaign_id)s, %(details)s
        )
        ON CONFLICT (event_key) DO NOTHING
        RETURNING id
    """

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[object]:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                yield conn

    async def is_healthy(self) -> bool:
        async with self.pool.connection() as conn:
            await conn.execute("SELECT 1")
        return True

    @staticmethod
    def _strategy_audit_record(
        account_id: str, event: StrategyAuditEvent
    ) -> StrategyAuditRecord:
        canonical_details = json.dumps(
            event.details,
            default=str,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        details = json.loads(canonical_details)
        identity = json.dumps(
            [
                account_id,
                event.event_time,
                event.event_type,
                event.symbol,
                event.strategy_id,
                event.campaign_id,
                canonical_details,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return StrategyAuditRecord(
            event_key=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            account_id=account_id,
            event_time=event.event_time,
            event_type=event.event_type,
            symbol=event.symbol,
            strategy_id=event.strategy_id,
            campaign_id=event.campaign_id,
            details=details,
        )

    async def insert_strategy_audit_events(
        self,
        events: Sequence[StrategyAuditEvent],
        *,
        account_id: str,
    ) -> int:
        """原子、幂等写入一批策略审计事件。"""
        records = [self._strategy_audit_record(account_id, event) for event in events]
        if not records:
            return 0
        inserted = 0
        async with self.transaction() as conn:
            for record in records:
                params = record.__dict__.copy()
                params["details"] = Jsonb(record.details)
                row = await (
                    await conn.execute(self._STRATEGY_AUDIT_INSERT, params)
                ).fetchone()
                inserted += int(row is not None)
        return inserted

    async def list_strategy_audit_events(
        self,
        *,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        event_type: Optional[str] = None,
        campaign_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[StrategyAuditRecord], int]:
        """按执行归属查询策略审计事件。"""
        parts: list[str] = []
        params: dict[str, object] = {"limit": limit, "offset": offset}
        for key, value in (
            ("account_id", account_id),
            ("strategy_id", strategy_id),
            ("symbol", symbol),
            ("event_type", event_type),
            ("campaign_id", campaign_id),
        ):
            if value is not None:
                parts.append(f"{key} = %({key})s")
                params[key] = value
        where = " WHERE " + " AND ".join(parts) if parts else ""
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(StrategyAuditRecord))
            await cursor.execute(
                "SELECT * FROM strategy_audit_events"
                f"{where} ORDER BY event_time DESC, id DESC "
                "LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            items = await cursor.fetchall()
            total = await (
                await conn.execute(
                    f"SELECT COUNT(*) FROM strategy_audit_events{where}", params
                )
            ).fetchone()
        return items, int(total[0])

    async def upsert_strategy_runtime_status(
        self, runtime_status: StrategyRuntimeStatus
    ) -> bool:
        """写入运行状态；拒绝旧实例、同启动时间竞争实例和乱序心跳。"""
        params = runtime_status.__dict__.copy()
        params["gate_conditions"] = Jsonb(runtime_status.gate_conditions)
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(self._STRATEGY_RUNTIME_STATUS_UPSERT, params)
            ).fetchone()
        return row is not None

    async def get_strategy_runtime_status(
        self, *, account_id: str, strategy_id: str
    ) -> StrategyRuntimeStatus | None:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(StrategyRuntimeStatus))
            await cursor.execute(
                "SELECT * FROM strategy_runtime_status "
                "WHERE account_id = %s AND strategy_id = %s",
                (account_id, strategy_id),
            )
            return await cursor.fetchone()

    async def list_strategy_runtime_statuses(
        self,
        *,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[StrategyRuntimeStatus], int]:
        parts: list[str] = []
        params: dict[str, object] = {"limit": limit, "offset": offset}
        for key, value in (
            ("account_id", account_id),
            ("strategy_id", strategy_id),
        ):
            if value is not None:
                parts.append(f"{key} = %({key})s")
                params[key] = value
        where = " WHERE " + " AND ".join(parts) if parts else ""
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(StrategyRuntimeStatus))
            await cursor.execute(
                "SELECT * FROM strategy_runtime_status"
                f"{where} ORDER BY account_id, strategy_id "
                "LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            items = await cursor.fetchall()
            total = await (
                await conn.execute(
                    f"SELECT COUNT(*) FROM strategy_runtime_status{where}", params
                )
            ).fetchone()
        return items, int(total[0])

    async def insert_order(self, order: Order) -> int:
        async with self.pool.connection() as conn:
            return await self._insert_order(conn, order)

    async def _insert_order(self, conn: object, order: Order) -> int:
        result = await conn.execute(self._ORDER_UPSERT, order.__dict__)
        row = await result.fetchone()
        if row:
            return row[0]
        existing = await (
            await conn.execute(
                "SELECT campaign_id FROM orders "
                "WHERE account_id = %s AND symbol = %s AND order_id = %s",
                (order.account_id, order.symbol, order.order_id),
            )
        ).fetchone()
        if existing is not None and existing[0] != order.campaign_id:
            raise ValueError("order Campaign attribution is immutable")
        return 0

    async def update_order_status(
        self,
        account_id: str,
        symbol: str,
        order_id: str,
        status: str,
        filled_quantity: Optional[Decimal] = None,
        avg_fill_price: Optional[Decimal] = None,
    ) -> bool:
        query = """
            UPDATE orders
            SET status = %(status)s,
                filled_quantity = COALESCE(%(filled_quantity)s, filled_quantity),
                avg_fill_price = COALESCE(%(avg_fill_price)s, avg_fill_price),
                updated_at = NOW(),
                filled_at = CASE WHEN %(status)s = 'FILLED' THEN NOW() ELSE filled_at END
            WHERE account_id = %(account_id)s
              AND symbol = %(symbol)s
              AND order_id = %(order_id)s
        """
        params = {
            "account_id": account_id,
            "symbol": symbol,
            "order_id": order_id,
            "status": status,
            "filled_quantity": filled_quantity,
            "avg_fill_price": avg_fill_price,
        }
        async with self.pool.connection() as conn:
            result = await conn.execute(query, params)
        return result.rowcount > 0

    @staticmethod
    def _filters(
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        nonzero: bool = False,
    ) -> tuple[str, dict[str, object]]:
        parts = ["quantity <> 0"] if nonzero else []
        params: dict[str, object] = {}
        for key, value in (
            ("account_id", account_id),
            ("strategy_id", strategy_id),
            ("symbol", symbol),
            ("status", status),
        ):
            if value is not None:
                parts.append(f"{key} = %({key})s")
                params[key] = value
        return (" WHERE " + " AND ".join(parts) if parts else ""), params

    async def get_orders(
        self,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Order]:
        where, params = self._filters(account_id, strategy_id, symbol, status)
        params.update(limit=limit, offset=offset)
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(Order))
            await cursor.execute(
                f"SELECT * FROM orders{where} "
                "ORDER BY created_at DESC LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            return await cursor.fetchall()

    async def count_orders(self, **filters: object) -> int:
        return await self._count("orders", **filters)

    async def insert_trade(self, trade: Trade) -> int:
        async with self.pool.connection() as conn:
            return await self._insert_trade(conn, trade)

    async def _insert_trade(self, conn: object, trade: Trade) -> int:
        result = await conn.execute(self._TRADE_INSERT, trade.__dict__)
        row = await result.fetchone()
        if row:
            return row[0]
        existing = await (
            await conn.execute(
                "SELECT strategy_id, order_id, client_order_id, campaign_id, side, "
                "position_side, quantity, price, quote_quantity, commission, "
                "commission_asset, realized_pnl, is_maker, exchange_time "
                "FROM trades "
                "WHERE account_id = %s AND symbol = %s AND trade_id = %s",
                (trade.account_id, trade.symbol, trade.trade_id),
            )
        ).fetchone()
        expected = (
            trade.strategy_id,
            trade.order_id,
            trade.client_order_id,
            trade.campaign_id,
            trade.side,
            trade.position_side,
            trade.quantity,
            trade.price,
            trade.quote_quantity,
            trade.commission,
            trade.commission_asset,
            trade.realized_pnl,
            trade.is_maker,
            trade.exchange_time,
        )
        if existing is not None and tuple(existing) != expected:
            raise ValueError("trade facts are immutable")
        return 0

    async def apply_execution_report(
        self,
        order: Order,
        trade: Trade | None,
    ) -> tuple[int, int | None]:
        """在单个事务中写入订单事实和可选成交事实。"""
        async with self.transaction() as conn:
            order_id = await self._insert_order(conn, order)
            trade_id = await self._insert_trade(conn, trade) if trade else None
        return order_id, trade_id

    async def get_trades(
        self,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Trade]:
        where, params = self._filters(account_id, strategy_id, symbol)
        params.update(limit=limit, offset=offset)
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(Trade))
            await cursor.execute(
                f"SELECT * FROM trades{where} "
                "ORDER BY created_at DESC LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            return await cursor.fetchall()

    async def get_trades_by_client_order_ids(
        self,
        *,
        account_id: str,
        strategy_id: str,
        symbol: str,
        campaign_id: str,
        client_order_ids: Sequence[str],
    ) -> list[Trade]:
        """按执行 WAL 的订单身份读取某个 Campaign 的成交事实。"""
        if not client_order_ids:
            return []
        params = {
            "account_id": account_id,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "campaign_id": campaign_id,
            "client_order_ids": list(client_order_ids),
        }
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(Trade))
            await cursor.execute(
                "SELECT * FROM trades "
                "WHERE account_id = %(account_id)s "
                "AND strategy_id = %(strategy_id)s "
                "AND symbol = %(symbol)s "
                "AND campaign_id = %(campaign_id)s "
                "AND client_order_id = ANY(%(client_order_ids)s) "
                "ORDER BY exchange_time ASC, id ASC",
                params,
            )
            return await cursor.fetchall()

    async def count_trades(self, **filters: object) -> int:
        return await self._count("trades", **filters)

    async def get_campaign_pnl(
        self,
        *,
        account_id: str,
        strategy_id: str,
        campaign_id: str,
    ) -> CampaignPnLSummary | None:
        """Aggregate only trades carrying an explicit Campaign identity."""
        params = {
            "account_id": account_id,
            "strategy_id": strategy_id,
            "campaign_id": campaign_id,
        }
        query = """
            WITH trade_totals AS (
                SELECT account_id, strategy_id, symbol, campaign_id,
                    COUNT(*)::BIGINT AS trade_count,
                    COALESCE(SUM(quantity) FILTER (WHERE side = 'SELL'), 0)
                        AS sell_quantity,
                    COALESCE(SUM(quote_quantity) FILTER (WHERE side = 'SELL'), 0)
                        AS sell_quote_quantity,
                    COALESCE(SUM(quantity) FILTER (WHERE side = 'BUY'), 0)
                        AS buy_quantity,
                    COALESCE(SUM(quote_quantity) FILTER (WHERE side = 'BUY'), 0)
                        AS buy_quote_quantity,
                    COALESCE(SUM(commission), 0) AS total_commission,
                    CASE WHEN COUNT(DISTINCT commission_asset) = 1
                        THEN MIN(commission_asset) END AS commission_asset,
                    BOOL_AND(realized_pnl IS NOT NULL) AS realized_pnl_complete,
                    COALESCE(SUM(realized_pnl), 0) AS gross_realized_pnl,
                    MIN(exchange_time) AS first_fill_at,
                    MAX(exchange_time) AS last_fill_at,
                    MAX(exchange_time) FILTER (WHERE side = 'BUY') AS last_buy_at
                FROM trades
                WHERE account_id = %(account_id)s
                  AND strategy_id = %(strategy_id)s
                  AND campaign_id = %(campaign_id)s
                GROUP BY account_id, strategy_id, symbol, campaign_id
            ), lifecycle AS (
                SELECT
                    MIN(event_time) FILTER (
                        WHERE event_type = 'campaign_acquired'
                    ) AS acquired_ms,
                    MAX(event_time) FILTER (
                        WHERE event_type = 'campaign_released'
                    ) AS released_ms
                FROM strategy_audit_events
                WHERE account_id = %(account_id)s
                  AND strategy_id = %(strategy_id)s
                  AND campaign_id = %(campaign_id)s
            )
            SELECT totals.account_id, totals.strategy_id, totals.symbol,
                totals.campaign_id, totals.trade_count, totals.sell_quantity,
                totals.sell_quote_quantity / NULLIF(totals.sell_quantity, 0)
                    AS sell_avg_price,
                totals.buy_quantity,
                totals.buy_quote_quantity / NULLIF(totals.buy_quantity, 0)
                    AS buy_avg_price,
                totals.total_commission, totals.commission_asset,
                totals.gross_realized_pnl,
                totals.gross_realized_pnl - totals.total_commission
                    AS net_realized_pnl,
                GREATEST(totals.sell_quantity - totals.buy_quantity, 0)
                    AS remaining_quantity,
                totals.sell_quantity > totals.buy_quantity AS has_open_quantity,
                TO_TIMESTAMP(lifecycle.acquired_ms / 1000.0) AS acquired_at,
                totals.first_fill_at, totals.last_fill_at,
                CASE WHEN totals.sell_quantity > 0
                          AND totals.buy_quantity >= totals.sell_quantity
                    THEN totals.last_buy_at END AS closed_at,
                TO_TIMESTAMP(lifecycle.released_ms / 1000.0) AS released_at,
                CASE WHEN lifecycle.acquired_ms IS NOT NULL
                          AND lifecycle.released_ms IS NOT NULL
                    THEN lifecycle.released_ms - lifecycle.acquired_ms END
                    AS lifecycle_duration_ms,
                totals.realized_pnl_complete
                    AND totals.commission_asset = 'USDT'
                    AND totals.sell_quantity >= totals.buy_quantity
                    AS pnl_facts_complete
            FROM trade_totals totals CROSS JOIN lifecycle
        """
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(CampaignPnLSummary))
            await cursor.execute(query, params)
            summaries = await cursor.fetchmany(2)
        if len(summaries) > 1:
            raise CampaignPnLFactsError(
                "Campaign trades span multiple symbols"
            )
        summary = summaries[0] if summaries else None
        if summary is not None and not summary.pnl_facts_complete:
            raise CampaignPnLFactsError(
                "Campaign PnL requires realized PnL, USDT commission, "
                "and nonnegative short quantity"
            )
        return summary

    async def upsert_position(self, position: Position) -> int:
        query = """
            INSERT INTO positions (
                account_id, strategy_id, symbol, position_side, quantity, entry_price,
                mark_price, unrealized_pnl, liquidation_price, leverage, margin_type,
                isolated_margin, exchange_time
            ) VALUES (
                %(account_id)s, %(strategy_id)s, %(symbol)s, %(position_side)s,
                %(quantity)s, %(entry_price)s, %(mark_price)s, %(unrealized_pnl)s,
                %(liquidation_price)s, %(leverage)s, %(margin_type)s,
                %(isolated_margin)s, %(exchange_time)s
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
                exchange_time = COALESCE(EXCLUDED.exchange_time, positions.exchange_time),
                updated_at = NOW()
            RETURNING id
        """
        async with self.pool.connection() as conn:
            result = await conn.execute(query, position.__dict__)
            row = await result.fetchone()
        return row[0] if row else 0

    async def apply_account_update(self, positions: Sequence[Position]) -> list[int]:
        """原子写入一条 ACCOUNT_UPDATE 中携带的仓位快照。"""
        ids: list[int] = []
        async with self.transaction() as conn:
            for position in positions:
                result = await conn.execute(
                    self._ACCOUNT_POSITION_UPSERT,
                    position.__dict__,
                )
                row = await result.fetchone()
                ids.append(row[0] if row else 0)
        return ids

    async def get_positions(
        self,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Position]:
        where, params = self._filters(
            account_id, strategy_id, symbol, nonzero=True
        )
        params.update(limit=limit, offset=offset)
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(Position))
            await cursor.execute(
                f"SELECT * FROM positions{where} "
                "ORDER BY updated_at DESC LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            return await cursor.fetchall()

    async def count_positions(self, **filters: object) -> int:
        return await self._count("positions", nonzero=True, **filters)

    async def get_pnl_summary(
        self,
        account_id: str,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> dict[str, Decimal | int]:
        where, params = self._filters(account_id, strategy_id, symbol)
        async with self.pool.connection() as conn:
            trades = await (
                await conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(commission), 0), "
                    "COALESCE(SUM(realized_pnl), 0), "
                    "COUNT(*) FILTER (WHERE realized_pnl > 0), "
                    "COUNT(*) FILTER (WHERE realized_pnl < 0), "
                    "COALESCE(AVG(realized_pnl) FILTER (WHERE realized_pnl > 0), 0), "
                    "COALESCE(AVG(ABS(realized_pnl)) FILTER (WHERE realized_pnl < 0), 0) "
                    f"FROM trades{where}",
                    params,
                )
            ).fetchone()
            position_where, position_params = self._filters(
                account_id, strategy_id, symbol, nonzero=True
            )
            unrealized = await (
                await conn.execute(
                    f"SELECT COALESCE(SUM(unrealized_pnl), 0) "
                    f"FROM positions{position_where}",
                    position_params,
                )
            ).fetchone()

        return {
            "total_trades": trades[0],
            "total_commission": trades[1],
            "total_realized_pnl": trades[2],
            "total_unrealized_pnl": unrealized[0],
            "win_count": trades[3],
            "loss_count": trades[4],
            "avg_win": trades[5],
            "avg_loss": trades[6],
        }

    async def _count(
        self,
        table: str,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        nonzero: bool = False,
    ) -> int:
        where, params = self._filters(
            account_id, strategy_id, symbol, status, nonzero
        )
        async with self.pool.connection() as conn:
            result = await conn.execute(f"SELECT COUNT(*) FROM {table}{where}", params)
            row = await result.fetchone()
        return int(row[0])

    async def get_subcategory_admission(
        self, subcategory: str
    ) -> Optional[SubcategoryAdmission]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(SubcategoryAdmission))
            await cursor.execute(
                "SELECT * FROM subcategory_admission WHERE subcategory = %s",
                (subcategory,),
            )
            return await cursor.fetchone()

    async def is_subcategory_enabled(self, subcategory: str) -> bool:
        """读取策略准入状态；未配置项按关闭处理（fail closed）。

        该方法只提供事实读取，不负责缓存、轮询或撤单。实时执行协调器可以在
        发起新风险前调用它，避免把 Web 控制状态复制成另一套规则。
        """
        admission = await self.get_subcategory_admission(subcategory)
        return admission is not None and admission.enabled

    async def list_subcategory_admissions(
        self, limit: int = 100, offset: int = 0
    ) -> tuple[list[SubcategoryAdmission], int]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(SubcategoryAdmission))
            await cursor.execute(
                "SELECT * FROM subcategory_admission "
                "ORDER BY subcategory LIMIT %s OFFSET %s",
                (limit, offset),
            )
            items = await cursor.fetchall()
            total = await (await conn.execute(
                "SELECT COUNT(*) FROM subcategory_admission"
            )).fetchone()
        return items, int(total[0])

    async def set_subcategory_admission(
        self,
        subcategory: str,
        enabled: bool,
        expected_version: int,
        updated_by: str,
        reason: Optional[str] = None,
    ) -> SubcategoryAdmission:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (subcategory,),
                )
                current = await (
                    await conn.execute(
                        "SELECT enabled, version FROM subcategory_admission "
                        "WHERE subcategory = %s FOR UPDATE",
                        (subcategory,),
                    )
                ).fetchone()
                if current is None:
                    if expected_version != 0:
                        raise VersionConflictError
                    previous_enabled, version = None, 1
                    await conn.execute(
                        "INSERT INTO subcategory_admission "
                        "(subcategory, enabled, version, updated_by, reason) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (subcategory, enabled, version, updated_by, reason),
                    )
                else:
                    previous_enabled, current_version = current
                    if current_version != expected_version:
                        raise VersionConflictError
                    version = current_version + 1
                    await conn.execute(
                        "UPDATE subcategory_admission SET enabled = %s, version = %s, "
                        "updated_at = NOW(), updated_by = %s, reason = %s "
                        "WHERE subcategory = %s",
                        (enabled, version, updated_by, reason, subcategory),
                    )
                await conn.execute(
                    "INSERT INTO subcategory_admission_audit "
                    "(subcategory, previous_enabled, enabled, version, changed_by, reason) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (subcategory, previous_enabled, enabled, version, updated_by, reason),
                )
        admission = await self.get_subcategory_admission(subcategory)
        assert admission is not None
        return admission

    async def list_subcategory_audit(
        self,
        subcategory: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[SubcategoryAdmissionAudit], int]:
        where = " WHERE subcategory = %s" if subcategory else ""
        page_params = (subcategory, limit, offset) if subcategory else (limit, offset)
        count_params = (subcategory,) if subcategory else ()
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(SubcategoryAdmissionAudit))
            await cursor.execute(
                "SELECT * FROM subcategory_admission_audit"
                f"{where} ORDER BY changed_at DESC, id DESC LIMIT %s OFFSET %s",
                page_params,
            )
            items = await cursor.fetchall()
            total = await (
                await conn.execute(
                    f"SELECT COUNT(*) FROM subcategory_admission_audit{where}",
                    count_params,
                )
            ).fetchone()
        return items, int(total[0])


async def create_connection_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 10,
) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        timeout=30,
        open=False,
    )
    await pool.open()
    return pool
