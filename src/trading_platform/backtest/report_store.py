"""Transactional PostgreSQL import for parsed backtest report directories."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from trading_platform.backtest.report_import import ReportDirectoryParser
from trading_platform.backtest.strategy_schemas import schema_for


REPORT_TITLES = {
    "comparison": "交易对参数结果",
    "parameter_summary": "参数组合汇总",
    "holding_bucket_summary": "持仓时间分档",
    "pnl_bucket_summary": "盈亏金额分档",
    "tier_fill_summary": "实际成交档位",
    "tier3_only_projection_summary": "仅挂第三档推算",
    "breakout_window_summary": "上涨窗口验证",
    "box_position_summary": "箱体位置分档",
    "box_proximity_summary": "箱体底部距离",
    "collisions": "同时交易竞争",
    "signal_collisions": "同时信号竞争",
    "all_signals": "全部信号审计",
    "memory_estimate": "内存估算",
    "universe": "回测交易对范围",
    "loss_over_100_ad_hoc": "大额亏损复核",
    "rise_duration_validation_ad_hoc": "上涨持续时间验证",
}


def _source_identity(root: Path) -> tuple[str, UUID]:
    digest = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        filename = path.relative_to(root).as_posix()
        digest.update(filename.encode("utf-8"))
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    source_key = digest.hexdigest()
    return source_key, uuid5(NAMESPACE_URL, f"trading-platform:backtest:{source_key}")


def _trade_uuid(research_id: UUID, run_id: str, trade_id: str) -> UUID:
    return uuid5(research_id, f"{run_id}:{trade_id}")


def _payload(data: dict[str, Any], excluded: set[str]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key not in excluded}


def _best_parameter_summary(parser: ReportDirectoryParser) -> dict[str, Any]:
    for report in parser.iter_reports():
        if report.name != "parameter_summary" or not report.rows:
            continue
        return max(
            report.rows,
            key=lambda row: float(row.get("net_pnl") or float("-inf")),
        )
    return {}


class BacktestReportStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def import_directory(self, root: str | Path) -> UUID:
        parser = ReportDirectoryParser(root)
        source_key, research_id = _source_identity(parser.root)
        metadata = parser.metadata
        best = _best_parameter_summary(parser)
        source_metadata = {
            **metadata.extra,
            "source_path": metadata.source_path,
            "archive_index_path": metadata.config.get("archive_index_path"),
            "duckdb_path": metadata.config.get("duckdb_path"),
            "summary_mode": "best_parameter" if best else "unavailable",
            "summary_parameters": best.get("parameters"),
        }
        start = metadata.config.get("start")
        end = metadata.config.get("end")
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM backtest_researches "
                    "WHERE source_key = %s OR report_path = %s",
                    (source_key, metadata.source_path),
                )
                await connection.execute(
                    """
                    INSERT INTO backtest_researches (
                        id, source_key, name, strategy_id, strategy_version, status,
                        started_at, ended_at, config, source_metadata, report_path,
                        symbol_count, run_count, trade_count, win_count, net_pnl,
                        win_rate
                    ) VALUES (%s, %s, %s, %s, %s, 'completed', %s, %s, %s, %s,
                              %s, %s, %s, 0, 0, %s, %s)
                    """,
                    (
                        research_id,
                        source_key,
                        metadata.name,
                        metadata.strategy_id,
                        metadata.config.get("strategy_version"),
                        start,
                        end,
                        Jsonb(metadata.config),
                        Jsonb(source_metadata),
                        metadata.source_path,
                        len(metadata.symbols),
                        metadata.run_count,
                        best.get("net_pnl") or 0,
                        best.get("win_rate") or 0,
                    ),
                )
                await self._insert_runs(connection, parser, research_id)
                trade_count, win_count = await self._insert_trades(
                    connection, parser, research_id
                )
                await self._insert_execution_records(connection, parser, research_id)
                await self._insert_reports(connection, parser, research_id)
                await self._insert_strategy_schema(
                    connection, metadata.strategy_id
                )
                await connection.execute(
                    "UPDATE backtest_researches SET trade_count = %s, win_count = %s "
                    "WHERE id = %s",
                    (trade_count, win_count, research_id),
                )
        return research_id

    @staticmethod
    async def _insert_runs(connection: object, parser: ReportDirectoryParser, research_id: UUID) -> None:
        async with connection.cursor() as cursor:
            for batch in parser.iter_run_batches():
                rows = [
                    {
                        "research_id": research_id,
                        "run_id": record.run_id,
                        "symbol": record.symbol or "UNKNOWN",
                        "status": record.status or "unknown",
                        "parameters": Jsonb(record.parameters or {}),
                        "summary": Jsonb(record.metrics),
                    }
                    for record in batch.records
                ]
                if rows:
                    await cursor.executemany(
                        "INSERT INTO backtest_runs "
                        "(research_id, run_id, symbol, status, parameters, summary) "
                        "VALUES (%(research_id)s, %(run_id)s, %(symbol)s, %(status)s, "
                        "%(parameters)s, %(summary)s)",
                        rows,
                    )

    @staticmethod
    async def _insert_trades(
        connection: object, parser: ReportDirectoryParser, research_id: UUID
    ) -> tuple[int, int]:
        trade_count = 0
        win_count = 0
        async with connection.cursor() as cursor:
            for batch in parser.iter_trade_batches():
                rows = []
                for record in batch.records:
                    values = asdict(record)
                    values.pop("strategy_id", None)
                    values["id"] = _trade_uuid(
                        research_id, str(record.run_id), str(record.trade_id)
                    )
                    values["research_id"] = research_id
                    values["parameters"] = Jsonb(record.parameters or {})
                    values["strategy_data"] = Jsonb(record.strategy_data)
                    rows.append(values)
                    trade_count += 1
                    win_count += int(record.winner is True)
                if rows:
                    await cursor.executemany(
                        """
                        INSERT INTO backtest_trades (
                            id, research_id, run_id, trade_id, campaign_id, symbol,
                            side, signal_time, entry_time, exit_time, entry_price,
                            exit_price, entry_quantity, entry_notional,
                            entry_fill_count, exit_fill_count, gross_pnl, commission,
                            net_pnl, net_return, winner, status, exit_reason,
                            parameters, strategy_data
                        ) VALUES (
                            %(id)s, %(research_id)s, %(run_id)s, %(trade_id)s,
                            %(campaign_id)s, %(symbol)s, %(side)s, %(signal_time)s,
                            %(entry_time)s, %(exit_time)s, %(entry_price)s,
                            %(exit_price)s, %(entry_quantity)s, %(entry_notional)s,
                            %(entry_fill_count)s, %(exit_fill_count)s, %(gross_pnl)s,
                            %(commission)s, %(net_pnl)s, %(net_return)s, %(winner)s,
                            %(status)s, %(exit_reason)s, %(parameters)s,
                            %(strategy_data)s
                        )
                        """,
                        rows,
                    )
        return trade_count, win_count

    @staticmethod
    async def _insert_execution_records(
        connection: object, parser: ReportDirectoryParser, research_id: UUID
    ) -> None:
        definitions = (
            (
                parser.iter_order_batches(),
                {"order_id", "campaign_id", "symbol", "side", "price", "quantity", "status", "created_at", "fill_time"},
                "INSERT INTO backtest_orders "
                "(research_id, run_id, order_id, campaign_id, symbol, side, price, "
                "quantity, status, created_at, fill_time, payload) VALUES "
                "(%(research_id)s, %(run_id)s, %(order_id)s, %(campaign_id)s, "
                "%(symbol)s, %(side)s, %(price)s, %(quantity)s, %(status)s, "
                "%(created_at)s, %(fill_time)s, %(payload)s)",
            ),
            (
                parser.iter_fill_batches(),
                {"fill_id", "order_id", "symbol", "side", "price", "quantity", "commission", "fill_time"},
                "INSERT INTO backtest_fills "
                "(research_id, run_id, fill_id, order_id, symbol, side, price, "
                "quantity, commission, fill_time, payload) VALUES "
                "(%(research_id)s, %(run_id)s, %(fill_id)s, %(order_id)s, "
                "%(symbol)s, %(side)s, %(price)s, %(quantity)s, %(commission)s, "
                "%(fill_time)s, %(payload)s)",
            ),
            (
                parser.iter_event_batches(),
                {"campaign_id", "symbol", "event_time", "event_type", "details"},
                "INSERT INTO backtest_events "
                "(research_id, run_id, campaign_id, symbol, event_time, event_type, "
                "payload) VALUES (%(research_id)s, %(run_id)s, %(campaign_id)s, "
                "%(symbol)s, %(event_time)s, %(event_type)s, %(payload)s)",
            ),
        )
        async with connection.cursor() as cursor:
            for batches, core_fields, statement in definitions:
                for batch in batches:
                    rows = []
                    for record in batch.records:
                        data = dict(record.data)
                        details = data.pop("details", None)
                        row = {
                            key: data.get(key)
                            for key in core_fields
                            if key != "details"
                        }
                        row.update(
                            research_id=research_id,
                            run_id=record.run_id,
                            payload=Jsonb(
                                details if isinstance(details, dict) else _payload(data, core_fields)
                            ),
                        )
                        rows.append(row)
                    if rows:
                        await cursor.executemany(statement, rows)

    @staticmethod
    async def _insert_reports(
        connection: object, parser: ReportDirectoryParser, research_id: UUID
    ) -> None:
        next_index: dict[str, int] = defaultdict(int)
        async with connection.cursor() as cursor:
            for report in parser.iter_reports():
                if report.name not in next_index:
                    await cursor.execute(
                        "INSERT INTO backtest_reports "
                        "(research_id, report_type, title, category, columns, row_count) "
                        "VALUES (%s, %s, %s, %s, %s, 0)",
                        (
                            research_id,
                            report.name,
                            REPORT_TITLES.get(report.name, report.name.replace("_", " ")),
                            _report_category(report.name),
                            Jsonb([asdict(column) for column in report.columns]),
                        ),
                    )
                rows = [
                    {
                        "research_id": research_id,
                        "report_type": report.name,
                        "row_index": next_index[report.name] + index,
                        "data": Jsonb(row),
                    }
                    for index, row in enumerate(report.rows)
                ]
                if rows:
                    await cursor.executemany(
                        "INSERT INTO backtest_report_rows "
                        "(research_id, report_type, row_index, data) VALUES "
                        "(%(research_id)s, %(report_type)s, %(row_index)s, %(data)s)",
                        rows,
                    )
                next_index[report.name] += len(rows)
            for report_type, count in next_index.items():
                await cursor.execute(
                    "UPDATE backtest_reports SET row_count = %s "
                    "WHERE research_id = %s AND report_type = %s",
                    (count, research_id, report_type),
                )

    @staticmethod
    async def _insert_strategy_schema(connection: object, strategy_id: str) -> None:
        schema = schema_for(strategy_id)
        if schema is None:
            return
        await connection.execute(
            "INSERT INTO backtest_strategy_schemas "
            "(strategy_id, schema_version, descriptor) VALUES (%s, %s, %s) "
            "ON CONFLICT (strategy_id, schema_version) DO UPDATE "
            "SET descriptor = EXCLUDED.descriptor, updated_at = NOW()",
            (
                schema["strategy_id"],
                schema["schema_version"],
                Jsonb(schema["descriptor"]),
            ),
        )


def _report_category(name: str) -> str:
    if "collision" in name or "signal" in name:
        return "competition"
    if name.startswith(("holding", "pnl", "tier")):
        return "execution"
    if name.startswith(("breakout", "box", "rise")):
        return "market_context"
    if name in {"memory_estimate", "universe"}:
        return "data_quality"
    return "parameters" if name in {"comparison", "parameter_summary"} else "analysis"
