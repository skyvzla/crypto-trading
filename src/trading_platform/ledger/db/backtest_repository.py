"""Queries and bulk persistence for isolated backtest research data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool_value(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _trade_order_side(value: Any) -> str | None:
    """Convert a trade's position side to the exchange order direction."""
    if value is None:
        return None
    side = str(value).strip().upper()
    if side in {"SHORT", "SELL"}:
        return "SELL"
    if side in {"LONG", "BUY"}:
        return "BUY"
    return side or None


def _normalise_order(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = _payload(row.get("payload"))
    order_id = str(row.get("order_id") or "")
    created_at = row.get("created_at")
    fill_time = row.get("fill_time")
    cancel_time = payload.get("cancel_time")
    status = row.get("status")
    completed_time = cancel_time
    if completed_time is None and str(status or "").upper() == "FILLED":
        completed_time = fill_time
    return {
        "id": order_id,
        "order_id": order_id,
        "client_order_id": payload.get("client_order_id"),
        "account_id": payload.get("account_id"),
        "strategy_id": payload.get("strategy_id"),
        "campaign_id": (
            str(row["campaign_id"])
            if row.get("campaign_id") is not None
            else None
        ),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "order_type": payload.get("type"),
        "type": payload.get("type"),
        "price": _float_or_none(row.get("price")),
        "quantity": _float_or_none(row.get("quantity")),
        "status": status,
        "created_at": created_at,
        "created_time": created_at,
        "completed_time": completed_time,
        "fill_time": fill_time,
        "cancel_time": cancel_time,
        "ttl_ms": payload.get("ttl_ms"),
        "reduce_only": _bool_value(payload.get("reduce_only")),
        "filled_quantity": _float_or_none(payload.get("filled_quantity")),
        "avg_fill_price": _float_or_none(payload.get("avg_fill_price")),
        "commission": _float_or_none(payload.get("commission")),
        "commission_asset": payload.get("commission_asset"),
        "is_maker": _bool_value(payload.get("is_maker")),
        "trigger_reason": payload.get("trigger_reason"),
        "tier": payload.get("tier"),
        "payload": payload,
    }


def _normalise_fill(
    row: Mapping[str, Any],
    order: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _payload(row.get("payload"))
    fill_id = str(row.get("fill_id") or "")
    order_id = str(row.get("order_id") or order.get("order_id") or "")
    fill_time = row.get("fill_time")
    order_type = payload.get("type") or order.get("order_type")
    return {
        "id": fill_id,
        "fill_id": fill_id,
        "order_id": order_id,
        "symbol": row.get("symbol") or order.get("symbol"),
        "side": row.get("side") or order.get("side"),
        "order_type": order_type,
        "type": order_type,
        "time": fill_time,
        "fill_time": fill_time,
        "price": _float_or_none(row.get("price")),
        "quantity": _float_or_none(row.get("quantity")),
        "commission": _float_or_none(
            row.get("commission")
            if row.get("commission") is not None else payload.get("commission")
        ),
        "commission_asset": payload.get("commission_asset") or order.get("commission_asset"),
        "is_maker": _bool_value(
            payload.get("is_maker")
            if payload.get("is_maker") is not None else order.get("is_maker")
        ),
        "client_order_id": payload.get("client_order_id") or order.get("client_order_id"),
        "reduce_only": _bool_value(
            payload.get("reduce_only")
            if payload.get("reduce_only") is not None else order.get("reduce_only")
        ),
        "trigger_reason": payload.get("trigger_reason") or order.get("trigger_reason"),
        "tier": (
            payload.get("tier")
            if payload.get("tier") is not None
            else order.get("tier")
        ),
        "payload": payload,
    }


class BacktestRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def _fetchall(
        self, query: str, parameters: tuple[object, ...] = ()
    ) -> list[dict[str, Any]]:
        async with self.pool.connection() as connection:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(query, parameters)
                return list(await cursor.fetchall())

    async def _fetchone(
        self, query: str, parameters: tuple[object, ...] = ()
    ) -> dict[str, Any] | None:
        rows = await self._fetchall(query, parameters)
        return rows[0] if rows else None

    async def list_researches(
        self, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        rows = await self._fetchall(
            "SELECT id, name, strategy_id, strategy_version, status, "
            "started_at AS start_time, ended_at AS end_time, symbol_count, "
            "run_count, trade_count, win_count, net_pnl, win_rate, "
            "imported_at AS created_at, source_metadata->>'summary_mode' "
            "AS summary_mode "
            "FROM backtest_researches ORDER BY imported_at DESC "
            "LIMIT %s OFFSET %s",
            (limit, offset),
        )
        total = await self._fetchone("SELECT COUNT(*) AS count FROM backtest_researches")
        return rows, int(total["count"] if total else 0)

    async def get_research(self, research_id: UUID) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT *, started_at AS start_time, ended_at AS end_time, "
            "imported_at AS created_at FROM backtest_researches WHERE id = %s",
            (research_id,),
        )

    async def has_symbol(self, research_id: UUID, symbol: str) -> bool:
        row = await self._fetchone(
            "SELECT 1 AS present FROM backtest_trades "
            "WHERE research_id = %s AND symbol = %s LIMIT 1",
            (research_id, symbol),
        )
        return row is not None

    async def list_reports(self, research_id: UUID) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT report_type AS type, title, category, description, columns, "
            "row_count "
            "FROM backtest_reports WHERE research_id = %s "
            "ORDER BY category, title",
            (research_id,),
        )

    async def get_report(
        self,
        research_id: UUID,
        report_type: str,
        *,
        limit: int,
        offset: int,
        sort_by: str | None = None,
        sort_order: str = "desc",
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        descriptor = await self._fetchone(
            "SELECT report_type AS type, title, category, description, columns, "
            "row_count "
            "FROM backtest_reports WHERE research_id = %s AND report_type = %s",
            (research_id, report_type),
        )
        if descriptor is None:
            return None, []
        columns = descriptor.get("columns") or []
        allowed = {
            item if isinstance(item, str) else item.get("key")
            for item in columns
        }
        if sort_by not in allowed:
            sort_by = None
        direction = "ASC" if sort_order.lower() == "asc" else "DESC"
        if sort_by is None:
            order_sql = "row_index"
            order_values: tuple[object, ...] = ()
        else:
            column = next(
                (item for item in columns if isinstance(item, dict) and item.get("key") == sort_by),
                {},
            )
            expression = "(data->>%s)::NUMERIC" if column.get("type") == "number" else "data->>%s"
            order_sql = f"{expression} {direction} NULLS LAST, row_index"
            order_values = (sort_by,)
        rows = await self._fetchall(
            "SELECT data FROM backtest_report_rows "
            "WHERE research_id = %s AND report_type = %s "
            f"ORDER BY {order_sql} LIMIT %s OFFSET %s",
            (research_id, report_type, *order_values, limit, offset),
        )
        return descriptor, [row["data"] for row in rows]

    async def list_symbols(
        self,
        research_id: UUID,
        *,
        limit: int,
        offset: int,
        symbol_filter: str | None = None,
        sort_by: str = "net_pnl",
        sort_order: str = "desc",
    ) -> tuple[list[dict[str, Any]], int]:
        sort_expressions = {
            "symbol": "symbol",
            "trade_count": "trade_count",
            "win_rate": "win_rate",
            "net_pnl": "net_pnl",
            "average_win": "average_win",
            "average_loss": "average_loss",
            "average_holding_seconds": "average_holding_seconds",
            "limit_order_fill_rate": "limit_order_fill_rate",
        }
        sort_sql = sort_expressions.get(sort_by, "net_pnl")
        direction = "ASC" if sort_order.lower() == "asc" else "DESC"
        pattern = f"%{symbol_filter.strip().upper()}%" if symbol_filter else "%"
        rows = await self._fetchall(
            f"""
            WITH unique_trades AS (
                SELECT DISTINCT ON (symbol, trade_id) *
                FROM backtest_trades
                WHERE research_id = %s
                ORDER BY symbol, trade_id, run_id
            )
            , summary AS (SELECT symbol,
                   COUNT(*)::BIGINT AS trade_count,
                   COUNT(*) FILTER (WHERE winner)::BIGINT AS win_count,
                   COALESCE(AVG(CASE WHEN winner THEN net_pnl END), 0) AS average_win,
                   COALESCE(AVG(CASE WHEN NOT winner THEN net_pnl END), 0) AS average_loss,
                   COALESCE(MAX(net_pnl), 0) AS max_profit,
                   COALESCE(MIN(net_pnl), 0) AS max_loss,
                   COALESCE(SUM(net_pnl), 0) AS net_pnl,
                   COALESCE(AVG(exit_time - entry_time) / 1000.0, 0)
                       AS average_holding_seconds,
                   COUNT(DISTINCT run_id)::BIGINT AS run_count
            FROM unique_trades
            GROUP BY symbol)
            , limit_entry_orders AS (
                SELECT DISTINCT t.symbol, o.research_id, o.run_id, o.order_id,
                       EXISTS (
                           SELECT 1
                           FROM backtest_fills f
                           WHERE f.research_id = o.research_id
                             AND f.run_id = o.run_id
                             AND f.order_id = o.order_id
                       ) AS has_fill
                FROM unique_trades t
                JOIN backtest_orders o
                  ON o.research_id = t.research_id
                 AND o.run_id = t.run_id
                 AND o.symbol = t.symbol
                 AND (
                     o.campaign_id = t.campaign_id
                     OR (
                         o.campaign_id IS NULL
                         AND t.campaign_id IS NULL
                         AND COALESCE(o.fill_time, o.created_at) >= t.entry_time
                         AND (t.exit_time IS NULL OR o.created_at <= t.exit_time)
                     )
                 )
               WHERE UPPER(o.payload->>'type') = 'LIMIT'
                  AND UPPER(o.side) = CASE UPPER(t.side)
                      WHEN 'SHORT' THEN 'SELL'
                      WHEN 'LONG' THEN 'BUY'
                      ELSE UPPER(t.side)
                  END
                  AND COALESCE((o.payload->>'reduce_only')::BOOLEAN, FALSE) = FALSE
            )
            , limit_order_stats AS (
                SELECT symbol,
                       COUNT(*)::BIGINT AS limit_order_count,
                       COUNT(*) FILTER (WHERE has_fill)::BIGINT
                           AS filled_limit_order_count
                FROM limit_entry_orders
                GROUP BY symbol
            )
            SELECT summary.*,
                   win_count::NUMERIC / NULLIF(trade_count, 0) AS win_rate,
                   CASE
                       WHEN limit_order_count > 0
                       THEN filled_limit_order_count::NUMERIC / limit_order_count
                   END AS limit_order_fill_rate
            FROM summary
            LEFT JOIN limit_order_stats USING (symbol)
            WHERE symbol ILIKE %s
            ORDER BY {sort_sql} {direction} NULLS LAST, symbol
            LIMIT %s OFFSET %s
            """,
            (research_id, pattern, limit, offset),
        )
        total = await self._fetchone(
            "SELECT COUNT(DISTINCT symbol) AS count FROM backtest_trades "
            "WHERE research_id = %s AND symbol ILIKE %s",
            (research_id, pattern),
        )
        for row in rows:
            trades = int(row["trade_count"])
            row["win_rate"] = int(row["win_count"]) / trades if trades else 0.0
            fill_rate = row.get("limit_order_fill_rate")
            row["limit_order_fill_rate"] = (
                float(fill_rate) if fill_rate is not None else None
            )
        return rows, int(total["count"] if total else 0)

    async def list_trades(
        self,
        research_id: UUID,
        symbol: str,
        *,
        limit: int,
        offset: int,
        winner: bool | None = None,
        exit_reason: str | None = None,
        min_pnl: float | None = None,
        max_pnl: float | None = None,
        sort_by: str = "entry_time",
        sort_order: str = "desc",
    ) -> tuple[list[dict[str, Any]], int]:
        clauses = ["research_id = %s", "symbol = %s"]
        values: list[object] = [research_id, symbol]
        for clause, value in (
            ("winner = %s", winner),
            ("exit_reason = %s", exit_reason),
            ("net_pnl >= %s", min_pnl),
            ("net_pnl <= %s", max_pnl),
        ):
            if value is not None:
                clauses.append(clause)
                values.append(value)
        where = " AND ".join(clauses)
        sort_expressions = {
            "entry_time": "entry_time",
            "entry_price": "entry_price",
            "exit_time": "exit_time",
            "exit_price": "exit_price",
            "entry_fill_count": "entry_fill_count",
            "holding_seconds": "exit_time - entry_time",
            "net_pnl": "net_pnl",
            "net_return": "net_return",
            "winner": "winner",
            "exit_reason": "exit_reason",
        }
        sort_sql = sort_expressions.get(sort_by, "entry_time")
        direction = "ASC" if sort_order.lower() == "asc" else "DESC"
        fields = (
            "id, run_id, trade_id, campaign_id, symbol, side, signal_time, "
            "entry_time, exit_time, entry_price, exit_price, "
            "entry_fill_count, "
            "(exit_time - entry_time) / 1000.0 AS holding_seconds, "
            "net_pnl, net_return, winner, status, exit_reason, parameters, "
            "strategy_data AS metrics"
        )
        rows = await self._fetchall(
            f"WITH unique_trades AS ("
            f"SELECT DISTINCT ON (symbol, trade_id) * FROM backtest_trades "
            f"WHERE research_id = %s ORDER BY symbol, trade_id, run_id) "
            f"SELECT {fields} FROM unique_trades WHERE {where} "
            f"ORDER BY {sort_sql} {direction} NULLS LAST, entry_time DESC, run_id "
            "LIMIT %s OFFSET %s",
            (research_id, *values, limit, offset),
        )
        count = await self._fetchone(
            f"WITH unique_trades AS ("
            f"SELECT DISTINCT ON (symbol, trade_id) * FROM backtest_trades "
            f"WHERE research_id = %s ORDER BY symbol, trade_id, run_id) "
            f"SELECT COUNT(*) AS count FROM unique_trades WHERE {where}",
            (research_id, *values),
        )
        return rows, int(count["count"] if count else 0)

    async def list_replay_parameter_sets(
        self, research_id: UUID
    ) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT r.parameters, COUNT(t.id)::BIGINT AS trade_count, "
            "COALESCE(SUM(t.net_pnl), 0) AS net_pnl "
            "FROM backtest_runs r LEFT JOIN backtest_trades t "
            "ON t.research_id = r.research_id AND t.run_id = r.run_id "
            "WHERE r.research_id = %s GROUP BY r.parameters "
            "ORDER BY net_pnl DESC, r.parameters::TEXT",
            (research_id,),
        )

    async def list_replay_trades(
        self, research_id: UUID, parameters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT t.id, t.run_id, t.trade_id, t.symbol, t.side, "
            "t.signal_time, t.entry_time, t.exit_time, t.entry_price, "
            "t.exit_price, t.entry_notional, t.gross_pnl, t.commission, "
            "t.net_pnl, t.net_return, "
            "CASE WHEN t.entry_notional <> 0 "
            "THEN t.gross_pnl / t.entry_notional END AS gross_return, "
            "t.winner, t.status, t.exit_reason, t.parameters "
            "FROM backtest_trades t JOIN backtest_runs r "
            "ON r.research_id = t.research_id AND r.run_id = t.run_id "
            "WHERE t.research_id = %s AND r.parameters = %s::JSONB "
            "ORDER BY COALESCE(t.signal_time, t.entry_time) ASC NULLS LAST, "
            "t.entry_time ASC NULLS LAST, t.symbol, t.trade_id, t.run_id",
            (research_id, Jsonb(parameters)),
        )

    async def get_trade(
        self, research_id: UUID, trade_id: UUID
    ) -> dict[str, Any] | None:
        trade = await self._fetchone(
            "SELECT t.*, r.strategy_id, r.strategy_version, r.name AS research_name "
            "FROM backtest_trades t JOIN backtest_researches r ON r.id = t.research_id "
            "WHERE t.research_id = %s AND t.id = %s",
            (research_id, trade_id),
        )
        if trade is None:
            return None
        campaign_id = trade.get("campaign_id")
        run_id = trade["run_id"]
        order_side = _trade_order_side(trade.get("side"))
        orders = await self._fetchall(
            "SELECT order_id, campaign_id, symbol, side, price, quantity, status, "
            "created_at, fill_time, payload FROM backtest_orders "
            "WHERE research_id = %s AND run_id = %s "
            "AND symbol = %s "
            "AND (campaign_id = %s::TEXT OR (campaign_id IS NULL "
            "AND (COALESCE((payload->>'reduce_only')::BOOLEAN, FALSE) "
            "OR (%s::TEXT IS NULL AND UPPER(side) = UPPER(%s::TEXT))) "
            "AND COALESCE(fill_time, created_at) >= COALESCE(%s::BIGINT, %s::BIGINT) "
            "AND (%s::BIGINT IS NULL OR created_at <= %s::BIGINT))) "
            "ORDER BY created_at, order_id",
            (
                research_id,
                run_id,
                trade["symbol"],
                campaign_id,
                campaign_id,
                order_side,
                trade.get("signal_time"),
                trade.get("entry_time"),
                trade.get("exit_time"),
                trade.get("exit_time"),
            ),
        )
        order_ids = [row["order_id"] for row in orders]
        fills = []
        if order_ids:
            fills = await self._fetchall(
                "SELECT f.fill_id, f.order_id, f.symbol, f.side, f.price, "
                "f.quantity, f.commission, f.fill_time, f.payload "
                "FROM backtest_fills f "
                "JOIN backtest_orders o ON o.research_id = f.research_id "
                "AND o.run_id = f.run_id AND o.order_id = f.order_id "
                "WHERE f.research_id = %s AND f.run_id = %s "
                "AND o.symbol = %s AND o.order_id = ANY(%s) "
                "ORDER BY f.fill_time, f.fill_id",
                (research_id, run_id, trade["symbol"], order_ids),
            )
        strategy_data = trade.get("strategy_data") or {}
        trade["entry_price"] = _float_or_none(trade.get("entry_price"))
        trade["exit_price"] = _float_or_none(trade.get("exit_price"))
        trade["net_pnl"] = _float_or_none(trade.get("net_pnl"))
        trade["net_return"] = _float_or_none(trade.get("net_return"))
        trade["average_entry_price"] = trade.get("entry_price")
        trade["signal_price"] = _float_or_none(strategy_data.get("trigger_price"))
        trade["invalid_price"] = _float_or_none(strategy_data.get("invalid_price"))
        trade["tier_prices"] = [
            _float_or_none(value)
            for value in (strategy_data.get("tier_prices") or [
                value
            for value in (
                strategy_data.get("tier1_price"),
                strategy_data.get("tier2_price"),
                strategy_data.get("tier3_price"),
            )
            if value is not None
            ])
        ]
        trade["tier_prices"] = [
            value for value in trade["tier_prices"] if value is not None
        ]
        trade["holding_seconds"] = (
            (trade["exit_time"] - trade["entry_time"]) / 1000
            if trade.get("entry_time") is not None and trade.get("exit_time") is not None
            else None
        )
        trade["metrics"] = strategy_data
        trade["orders"] = [_normalise_order(row) for row in orders]
        orders_by_id = {order["order_id"]: order for order in trade["orders"]}
        trade["fills"] = []
        fills_by_order: dict[str, list[dict[str, Any]]] = {}
        for fill_row in fills:
            order_id = str(fill_row.get("order_id") or "")
            fill = _normalise_fill(
                fill_row,
                orders_by_id.get(order_id, {}),
            )
            trade["fills"].append(fill)
            fills_by_order.setdefault(fill["order_id"], []).append(fill)
        for order in trade["orders"]:
            order_fills = fills_by_order.get(order["order_id"], [])
            if not order_fills:
                continue
            total_quantity = sum(
                (fill["quantity"] or 0.0) for fill in order_fills
            )
            if order["filled_quantity"] is None:
                order["filled_quantity"] = total_quantity
            priced_fills = [
                fill
                for fill in order_fills
                if fill["price"] is not None
                and fill["quantity"] is not None
                and fill["quantity"] > 0
            ]
            priced_quantity = sum(fill["quantity"] for fill in priced_fills)
            if order["avg_fill_price"] is None and priced_quantity > 0:
                order["avg_fill_price"] = sum(
                    fill["price"] * fill["quantity"] for fill in priced_fills
                ) / priced_quantity
            commissions = [
                fill["commission"]
                for fill in order_fills
                if fill["commission"] is not None
            ]
            if order["commission"] is None and commissions:
                order["commission"] = sum(commissions)
            commission_assets = {
                fill["commission_asset"]
                for fill in order_fills
                if fill["commission_asset"]
            }
            if order["commission_asset"] is None and len(commission_assets) == 1:
                order["commission_asset"] = commission_assets.pop()
        return trade

    async def list_events(
        self, research_id: UUID, trade_id: UUID
    ) -> list[dict[str, Any]] | None:
        trade = await self._fetchone(
            "SELECT run_id, campaign_id FROM backtest_trades "
            "WHERE research_id = %s AND id = %s",
            (research_id, trade_id),
        )
        if trade is None:
            return None
        rows = await self._fetchall(
            "SELECT id, event_time, event_type, symbol, payload FROM backtest_events "
            "WHERE research_id = %s AND run_id = %s "
            "AND ((%s::TEXT IS NULL AND campaign_id IS NULL) "
            "OR campaign_id = %s::TEXT) "
            "ORDER BY event_time, id",
            (
                research_id,
                trade["run_id"],
                trade["campaign_id"],
                trade["campaign_id"],
            ),
        )
        events = []
        for row in rows:
            payload = row["payload"] or {}
            price = next(
                (
                    _float_or_none(payload.get(key))
                    for key in ("price", "trigger_price", "fill_price")
                    if payload.get(key) is not None
                ),
                None,
            )
            events.append(
                {
                    "time": row["event_time"],
                    "type": row["event_type"],
                    "title": row["event_type"],
                    "description": payload.get("description")
                    or payload.get("reason")
                    or payload.get("message"),
                    "price": price,
                    "data": payload,
                    "symbol": row["symbol"],
                    "id": row["id"],
                }
            )
        return events

    async def get_strategy_schema(self, strategy_id: str) -> dict[str, Any] | None:
        row = await self._fetchone(
            "SELECT strategy_id, schema_version, descriptor "
            "FROM backtest_strategy_schemas WHERE strategy_id = %s "
            "ORDER BY schema_version DESC LIMIT 1",
            (strategy_id,),
        )
        return row
