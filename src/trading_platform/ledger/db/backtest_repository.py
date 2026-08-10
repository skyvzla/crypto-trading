"""Queries and bulk persistence for isolated backtest research data."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        descriptor = await self._fetchone(
            "SELECT report_type AS type, title, category, description, columns, "
            "row_count "
            "FROM backtest_reports WHERE research_id = %s AND report_type = %s",
            (research_id, report_type),
        )
        if descriptor is None:
            return None, []
        rows = await self._fetchall(
            "SELECT data FROM backtest_report_rows "
            "WHERE research_id = %s AND report_type = %s "
            "ORDER BY row_index LIMIT %s OFFSET %s",
            (research_id, report_type, limit, offset),
        )
        return descriptor, [row["data"] for row in rows]

    async def list_symbols(
        self, research_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        rows = await self._fetchall(
            """
            SELECT symbol,
                   COUNT(*)::BIGINT AS trade_count,
                   COUNT(*) FILTER (WHERE winner)::BIGINT AS win_count,
                   COALESCE(AVG(CASE WHEN winner THEN net_pnl END), 0) AS average_win,
                   COALESCE(AVG(CASE WHEN NOT winner THEN net_pnl END), 0) AS average_loss,
                   COALESCE(MAX(net_pnl), 0) AS max_profit,
                   COALESCE(MIN(net_pnl), 0) AS max_loss,
                   COALESCE(SUM(net_pnl), 0) AS net_pnl,
                   COALESCE(AVG(exit_time - entry_time) / 1000.0, 0)
                       AS average_holding_seconds,
                   COUNT(*) FILTER (WHERE
                       COALESCE(NULLIF(strategy_data->>'tier1_fill_count', '')::INTEGER, 0) > 0
                       AND COALESCE(NULLIF(strategy_data->>'tier2_fill_count', '')::INTEGER, 0) > 0
                       AND COALESCE(NULLIF(strategy_data->>'tier3_fill_count', '')::INTEGER, 0) > 0
                   )::BIGINT
                       AS three_tier_count,
                   COUNT(DISTINCT run_id)::BIGINT AS run_count
            FROM backtest_trades
            WHERE research_id = %s
            GROUP BY symbol
            ORDER BY net_pnl DESC, symbol
            LIMIT %s OFFSET %s
            """,
            (research_id, limit, offset),
        )
        total = await self._fetchone(
            "SELECT COUNT(DISTINCT symbol) AS count FROM backtest_trades "
            "WHERE research_id = %s",
            (research_id,),
        )
        for row in rows:
            trades = int(row["trade_count"])
            row["win_rate"] = int(row["win_count"]) / trades if trades else 0.0
            row["full_tier_fill_rate"] = (
                int(row["three_tier_count"]) / trades if trades else 0.0
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
        fields = (
            "id, run_id, trade_id, campaign_id, symbol, side, signal_time, "
            "entry_time, exit_time, entry_price, exit_price, "
            "entry_fill_count AS filled_tier_count, "
            "(exit_time - entry_time) / 1000.0 AS holding_seconds, "
            "net_pnl, net_return, winner, status, exit_reason, parameters, "
            "strategy_data AS metrics"
        )
        rows = await self._fetchall(
            f"SELECT {fields} FROM backtest_trades WHERE {where} "
            "ORDER BY entry_time DESC, run_id LIMIT %s OFFSET %s",
            (*values, limit, offset),
        )
        count = await self._fetchone(
            f"SELECT COUNT(*) AS count FROM backtest_trades WHERE {where}",
            tuple(values),
        )
        return rows, int(count["count"] if count else 0)

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
        orders = await self._fetchall(
            "SELECT order_id, campaign_id, symbol, side, price, quantity, status, "
            "created_at, fill_time, payload FROM backtest_orders "
            "WHERE research_id = %s AND run_id = %s "
            "AND ((%s::TEXT IS NULL AND campaign_id IS NULL) "
            "OR campaign_id = %s::TEXT) "
            "ORDER BY created_at, order_id",
            (research_id, run_id, campaign_id, campaign_id),
        )
        fills = await self._fetchall(
            "SELECT f.fill_id, f.order_id, f.symbol, f.side, f.price, f.quantity, "
            "f.commission, f.fill_time, f.payload FROM backtest_fills f "
            "JOIN backtest_orders o ON o.research_id = f.research_id "
            "AND o.run_id = f.run_id AND o.order_id = f.order_id "
            "WHERE f.research_id = %s AND f.run_id = %s "
            "AND ((%s::TEXT IS NULL AND o.campaign_id IS NULL) "
            "OR o.campaign_id = %s::TEXT) "
            "ORDER BY f.fill_time, f.fill_id",
            (research_id, run_id, campaign_id, campaign_id),
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
        trade["filled_tier_count"] = trade.get("entry_fill_count")
        trade["holding_seconds"] = (
            (trade["exit_time"] - trade["entry_time"]) / 1000
            if trade.get("entry_time") is not None and trade.get("exit_time") is not None
            else None
        )
        trade["metrics"] = strategy_data
        trade["orders"] = [
            {
                **row,
                "id": row.pop("order_id"),
                "created_time": row.pop("created_at"),
                "price": _float_or_none(row.get("price")),
                "quantity": _float_or_none(row.get("quantity")),
            }
            for row in orders
        ]
        trade["fills"] = [
            {
                **row,
                "id": row.pop("fill_id"),
                "time": row.pop("fill_time"),
                "price": _float_or_none(row.get("price")),
                "quantity": _float_or_none(row.get("quantity")),
            }
            for row in fills
        ]
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
