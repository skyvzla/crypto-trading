"""从只读 DuckDB 快照加载 Binance 账户资金费事实。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb


@dataclass(frozen=True, slots=True)
class FundingIncomeEvent:
    """一笔已确定符号的 USDT 资金费收入；支出为负、收入为正。"""

    transaction_id: int
    symbol: str
    event_time: int
    amount: Decimal


class FundingIncomeDataLoader:
    """读取离线 income 快照，并证明请求窗口没有覆盖缺口。

    快照复用线上 ``account_income_events`` 列结构，并额外要求
    ``account_income_coverage`` 记录已完整导出的半开时间窗。回测不会创建、
    更新或联网补充这两个表。
    """

    _EVENT_COLUMNS = {
        "account_id",
        "transaction_id",
        "income_type",
        "symbol",
        "asset",
        "amount",
        "event_time",
    }
    _COVERAGE_COLUMNS = {
        "account_id",
        "income_type",
        "symbol",
        "start_time",
        "end_time",
    }

    def __init__(
        self,
        duckdb_path: str | Path,
        *,
        account_id: str,
        symbols: list[str],
        start_ms: int,
        end_ms: int,
    ) -> None:
        self.duckdb_path = Path(duckdb_path)
        self.account_id = account_id.strip()
        self.symbols = list(
            dict.fromkeys(
                symbol.strip().upper() for symbol in symbols if symbol.strip()
            )
        )
        self.start_ms = int(start_ms)
        self.end_ms = int(end_ms)
        if not self.account_id:
            raise ValueError("funding account_id must not be blank")
        if not self.symbols:
            raise ValueError("funding symbols must not be empty")
        if self.start_ms >= self.end_ms:
            raise ValueError("funding start_ms must be earlier than end_ms")
        if not self.duckdb_path.is_file():
            raise FileNotFoundError(
                f"funding DuckDB snapshot not found: {self.duckdb_path}"
            )

    def load(self) -> list[FundingIncomeEvent]:
        """返回请求窗口内事件；完整覆盖但没有事件时返回空列表。"""

        try:
            connection = duckdb.connect(str(self.duckdb_path), read_only=True)
        except duckdb.Error as error:
            raise ValueError(
                f"invalid funding DuckDB snapshot: {error}"
            ) from error
        try:
            self._require_columns(
                connection, "account_income_events", self._EVENT_COLUMNS
            )
            self._require_columns(
                connection, "account_income_coverage", self._COVERAGE_COLUMNS
            )
            self._validate_coverage(connection)
            return self._load_events(connection)
        except duckdb.Error as error:
            raise ValueError(
                f"invalid funding DuckDB snapshot: {error}"
            ) from error
        finally:
            connection.close()

    @staticmethod
    def _require_columns(
        connection: duckdb.DuckDBPyConnection,
        table: str,
        required: set[str],
    ) -> None:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ?",
            [table],
        ).fetchall()
        if not rows:
            raise ValueError(f"funding DuckDB snapshot is missing main.{table}")
        columns = {str(row[0]) for row in rows}
        missing = sorted(required - columns)
        if missing:
            raise ValueError(
                f"funding DuckDB {table} missing columns: {', '.join(missing)}"
            )

    def _validate_coverage(self, connection: duckdb.DuckDBPyConnection) -> None:
        placeholders = ", ".join("?" for _ in self.symbols)
        rows = connection.execute(
            "SELECT upper(symbol), epoch_ms(start_time), epoch_ms(end_time) "
            "FROM main.account_income_coverage "
            "WHERE account_id = ? AND income_type = 'FUNDING_FEE' "
            f"AND upper(symbol) IN ({placeholders}) "
            "AND epoch_ms(end_time) > ? AND epoch_ms(start_time) < ? "
            "ORDER BY upper(symbol), start_time, end_time",
            [self.account_id, *self.symbols, self.start_ms, self.end_ms],
        ).fetchall()
        ranges_by_symbol: dict[str, list[tuple[int, int]]] = {
            symbol: [] for symbol in self.symbols
        }
        for raw_symbol, raw_start, raw_end in rows:
            if raw_symbol is None or raw_start is None or raw_end is None:
                raise ValueError("funding coverage contains null values")
            symbol = str(raw_symbol).upper()
            start_ms, end_ms = int(raw_start), int(raw_end)
            if start_ms >= end_ms:
                raise ValueError(
                    f"funding coverage has an invalid range for {symbol}"
                )
            ranges_by_symbol[symbol].append((start_ms, end_ms))

        for symbol, ranges in ranges_by_symbol.items():
            covered_until = self.start_ms
            for range_start, range_end in ranges:
                if range_end <= covered_until:
                    continue
                if range_start > covered_until:
                    break
                covered_until = range_end
                if covered_until >= self.end_ms:
                    break
            if covered_until < self.end_ms:
                raise ValueError(
                    "funding coverage is incomplete for "
                    f"{self.account_id}/{symbol}: missing at {covered_until}ms"
                )

    def _load_events(
        self, connection: duckdb.DuckDBPyConnection
    ) -> list[FundingIncomeEvent]:
        placeholders = ", ".join("?" for _ in self.symbols)
        rows = connection.execute(
            "SELECT transaction_id, upper(symbol), epoch_ms(event_time), "
            "CAST(amount AS VARCHAR), upper(asset) "
            "FROM main.account_income_events "
            "WHERE account_id = ? AND income_type = 'FUNDING_FEE' "
            f"AND upper(symbol) IN ({placeholders}) "
            "AND epoch_ms(event_time) >= ? AND epoch_ms(event_time) < ? "
            "ORDER BY event_time, transaction_id",
            [self.account_id, *self.symbols, self.start_ms, self.end_ms],
        ).fetchall()

        by_transaction: dict[int, FundingIncomeEvent] = {}
        for raw_id, raw_symbol, raw_time, raw_amount, raw_asset in rows:
            try:
                transaction_id = int(raw_id)
                event_time = int(raw_time)
                amount = Decimal(str(raw_amount))
            except (TypeError, ValueError, InvalidOperation) as error:
                raise ValueError("funding snapshot contains an invalid event") from error
            symbol = str(raw_symbol).upper() if raw_symbol is not None else ""
            asset = str(raw_asset).upper() if raw_asset is not None else ""
            if (
                transaction_id < 0
                or event_time < 0
                or not symbol
                or not amount.is_finite()
            ):
                raise ValueError("funding snapshot contains an invalid event")
            if asset != "USDT":
                raise ValueError(
                    f"funding event {transaction_id} is not USDT-denominated"
                )
            event = FundingIncomeEvent(
                transaction_id=transaction_id,
                symbol=symbol,
                event_time=event_time,
                amount=amount,
            )
            previous = by_transaction.get(transaction_id)
            if previous is not None and previous != event:
                raise ValueError(
                    f"funding transaction {transaction_id} has conflicting facts"
                )
            by_transaction[transaction_id] = event
        return sorted(
            by_transaction.values(),
            key=lambda event: (event.event_time, event.transaction_id),
        )
