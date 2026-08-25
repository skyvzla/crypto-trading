"""从只读 DuckDB 历史归档加载回测事件流。"""

from collections.abc import Collection, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Union

import duckdb
import pandas as pd

from trading_platform.market.archive.index import (
    ARCHIVE_INDEX_FILENAME,
    load_archive_index,
    verify_archive_index_files,
)
from trading_platform.market.archive.metrics import load_metrics_index
from trading_platform.market.archive.parquet import (
    CANDLE_BASE_COLUMNS,
    CANDLE_FEATURE_COLUMNS,
    archive_root_from_catalog,
)
from trading_platform.shared.events import Bar1s, Kline

Event = Union[Bar1s, Kline]
# 默认按 180 天（4320 小时）切分 DuckDB 流式回放窗口。
DEFAULT_CHUNK_HOURS = 4320.0


class MetricsDataLoader:
    """按 metrics sidecar index 读取指定窗口内策略可见的指标快照。"""

    def __init__(self, root: str | Path, *, symbol: str, start_ms: int | None = None,
                 end_ms: int | None = None, period: str = "5m") -> None:
        self.root = Path(root).resolve()
        self.symbol = symbol.strip().upper()
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.period = period
        if not self.symbol:
            raise ValueError("symbol must not be empty")

    def load(self) -> list[tuple[int, float, float]]:
        """返回 ``(available_ms, open_interest, long_short_ratio)`` 序列。"""
        table = load_metrics_index(self.root, verify_files=True)
        frame = table.to_pandas()
        selected = frame[
            (frame["symbol"] == self.symbol) & (frame["period"] == self.period)
        ]
        if self.end_ms is not None:
            selected = selected[selected["first_snapshot_ms"] < self.end_ms]
        paths = [str(self.root / path) for path in selected["relative_path"].drop_duplicates()]
        if not paths:
            return []
        connection = duckdb.connect()
        try:
            rows = connection.execute(
                """
                SELECT extract(epoch from available_time AT TIME ZONE 'UTC') * 1000,
                       sum_open_interest, count_long_short_ratio
                FROM read_parquet(?)
                WHERE symbol = ? AND period = ?
                  AND available_time IS NOT NULL
                  AND (? IS NULL OR available_time >= to_timestamp(? / 1000.0))
                  AND (? IS NULL OR available_time < to_timestamp(? / 1000.0))
                ORDER BY available_time, snapshot_time
                """,
                [paths, self.symbol, self.period, self.start_ms, self.start_ms,
                 self.end_ms, self.end_ms],
            ).fetchall()
        finally:
            connection.close()
        return [
            (int(ms), float(oi), float(ls))
            for ms, oi, ls in rows
            if oi is not None and ls is not None
        ]


def _decimal_value(value: object) -> Decimal:
    """将归档数值转为 Decimal，避免对已格式化字符串重复调用 str。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return Decimal(value)
    return Decimal(str(value))


def _optional_decimal_value(value: object) -> Decimal | None:
    return None if value is None else _decimal_value(value)


def _optional_int_value(value: object) -> int | None:
    return None if value is None else int(value)


_DECIMAL_BAR1S_FEATURES = {
    "vwap",
    "quote_volume",
    "taker_buy_volume",
    "taker_sell_volume",
    "taker_buy_quote_volume",
    "taker_sell_quote_volume",
    "max_agg_trade_quantity",
    "max_taker_buy_agg_trade_quantity",
    "max_taker_sell_agg_trade_quantity",
}


class BacktestDataLoader:
    """按固定窗口从 DuckDB candles 表流式读取已排序事件。"""

    def __init__(
        self,
        duckdb_path: str,
        symbols: list[str],
        start_ms: int,
        end_ms: int,
        require_aggtrades: bool = False,
        required_kline_intervals: list[str] | None = None,
        archive_index_path: str | None = None,
        bar1s_time_shift_ms: int = 0,
        bar1s_feature_columns: Collection[str] | None = None,
    ):
        if start_ms >= end_ms:
            raise ValueError("start_ms must be earlier than end_ms")
        if not symbols:
            raise ValueError("symbols must not be empty")

        self.symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.require_aggtrades = require_aggtrades
        self.required_kline_intervals = set(required_kline_intervals or [])
        if "1s" in self.required_kline_intervals:
            raise ValueError("use require_aggtrades for 1s market data")
        if bar1s_feature_columns is None:
            self.bar1s_feature_columns = None
            self._selected_bar1s_feature_columns = CANDLE_FEATURE_COLUMNS
        else:
            requested_features = frozenset(bar1s_feature_columns)
            unknown_features = requested_features.difference(CANDLE_FEATURE_COLUMNS)
            if unknown_features:
                names = ", ".join(sorted(unknown_features))
                raise ValueError(
                    "unknown projected 1s feature columns: "
                    f"{names}"
                )
            self.bar1s_feature_columns = requested_features
            self._selected_bar1s_feature_columns = tuple(
                name for name in CANDLE_FEATURE_COLUMNS if name in requested_features
            )
        self.duckdb_path = Path(duckdb_path)
        self.archive_index_path = (
            Path(archive_index_path) if archive_index_path else None
        )
        if self.archive_index_path is None:
            try:
                self.archive_index_path = (
                    archive_root_from_catalog(self.duckdb_path)
                    / ARCHIVE_INDEX_FILENAME
                )
            except RuntimeError:
                # 独立 DuckDB 表可直接查询；生产归档 catalog 必须带 sidecar。
                pass
        self.bar1s_time_shift_ms = int(bar1s_time_shift_ms)
        self._duckdb_connection: duckdb.DuckDBPyConnection | None = None
        self._source_index: pd.DataFrame | None = None

        if not self.duckdb_path.is_file():
            raise FileNotFoundError(f"DuckDB archive not found: {self.duckdb_path}")

    def iter_all(
        self,
        *,
        chunk_hours: float = DEFAULT_CHUNK_HOURS,
        fetch_batch_size: int = 10_000,
        duckdb_memory_limit: str | None = None,
        duckdb_threads: int = 1,
    ) -> Iterator[Event]:
        """按固定时间窗口流式读取 DuckDB 事件。"""
        if chunk_hours <= 0:
            raise ValueError("chunk_hours must be positive")
        if fetch_batch_size <= 0:
            raise ValueError("fetch_batch_size must be positive")
        if duckdb_threads <= 0:
            raise ValueError("duckdb_threads must be positive")

        chunk_ms = max(1, int(chunk_hours * 3_600_000))
        connection = duckdb.connect(str(self.duckdb_path), read_only=True)
        self._duckdb_connection = connection
        try:
            connection.execute("SET enable_progress_bar = false")
            connection.execute(f"SET threads = {int(duckdb_threads)}")
            if duckdb_memory_limit:
                connection.execute("SET memory_limit = ?", [duckdb_memory_limit])
            self._validate_duckdb_source()
            self._validate_stream_datasets()

            sequence_by_stream: dict[tuple[str, str], int] = {}
            stream_end_ms = self.end_ms + 1_001
            chunk_start_ms = self.start_ms
            while chunk_start_ms < stream_end_ms:
                chunk_end_ms = min(chunk_start_ms + chunk_ms, stream_end_ms)
                cursor = self._execute_stream_query(
                    chunk_start_ms=chunk_start_ms,
                    chunk_end_ms=chunk_end_ms,
                )
                if cursor is None:
                    chunk_start_ms = chunk_end_ms
                    continue
                while rows := cursor.fetchmany(fetch_batch_size):
                    for row in rows:
                        symbol = str(row[0])
                        timeframe = str(row[1])
                        stream_key = (symbol, timeframe)
                        sequence = sequence_by_stream.get(stream_key, 0)
                        sequence_by_stream[stream_key] = sequence + 1
                        yield self._stream_row_to_event(row, sequence)
                chunk_start_ms = chunk_end_ms
        finally:
            self._duckdb_connection = None
            connection.close()

    def _validate_stream_datasets(self) -> None:
        """在开始 yield 前完整校验必需数据集，避免半程失败。"""
        if self.archive_index_path is not None:
            self._validate_stream_datasets_from_index()
            return
        connection = self._require_duckdb_connection()
        placeholders = ", ".join("?" for _ in self.symbols)
        required_timeframes = sorted(
            ({"1s"} if self.require_aggtrades else set())
            | self.required_kline_intervals
        )
        if not required_timeframes:
            return
        timeframe_placeholders = ", ".join("?" for _ in required_timeframes)
        rows = connection.execute(
            "SELECT symbol, timeframe, count(*) "
            "FROM main.candles "
            f"WHERE symbol IN ({placeholders}) "
            f"AND timeframe IN ({timeframe_placeholders}) "
            "AND ((timeframe = '1s' AND epoch_ms(open_time) >= ? "
            "AND epoch_ms(open_time) < ?) "
            "OR (timeframe <> '1s' AND epoch_ms(close_time) >= ? "
            "AND epoch_ms(close_time) < ?)) "
            "GROUP BY symbol, timeframe",
            [
                *self.symbols,
                *required_timeframes,
                self.start_ms - self.bar1s_time_shift_ms,
                self.end_ms - self.bar1s_time_shift_ms,
                self.start_ms,
                self.end_ms,
            ],
        ).fetchall()
        available = {(str(symbol), str(timeframe)) for symbol, timeframe, _ in rows}
        for symbol in self.symbols:
            if self.require_aggtrades and (symbol, "1s") not in available:
                raise ValueError(f"Missing required 1s market data for {symbol}")
            for interval in self.required_kline_intervals:
                if (symbol, interval) not in available:
                    raise ValueError(
                        f"Missing required {interval} Kline data for {symbol}"
                    )

    def _validate_stream_datasets_from_index(self) -> None:
        frame = load_archive_index(self.archive_index_path)
        raw_start = self.start_ms - self.bar1s_time_shift_ms
        raw_end = self.end_ms - self.bar1s_time_shift_ms
        selected_parts = []
        required = (
            ({"1s"} if self.require_aggtrades else set())
            | self.required_kline_intervals
        )
        for symbol in self.symbols:
            for timeframe in required:
                start_ms = raw_start if timeframe == "1s" else self.start_ms
                end_ms = raw_end if timeframe == "1s" else self.end_ms
                matches = frame[
                    (frame["symbol"] == symbol)
                    & (frame["timeframe"] == timeframe)
                    & (frame["first_open_ms"] < end_ms)
                    & (frame["last_close_ms"] >= start_ms)
                ]
                if matches.empty:
                    label = "1s market data" if timeframe == "1s" else f"{timeframe} Kline"
                    raise ValueError(f"Missing required {label} for {symbol}")
                selected_parts.append(matches)
        if selected_parts:
            selected = pd.concat(selected_parts, ignore_index=True).drop_duplicates(
                "relative_path"
            )
            verify_archive_index_files(selected, self.archive_index_path.parent)
            connection = self._require_duckdb_connection()
            for row in selected.itertuples(index=False):
                relative_path = str(row.relative_path)
                physical_path = str(
                    self.archive_index_path.parent / relative_path
                )
                physical_columns = {
                    str(column[0])
                    for column in connection.execute(
                        "DESCRIBE SELECT * FROM read_parquet(?)",
                        [[physical_path]],
                    ).fetchall()
                }
                required_columns = set(CANDLE_BASE_COLUMNS)
                if str(row.timeframe) == "1s":
                    required_columns.update(self.bar1s_feature_columns or ())
                missing_columns = sorted(required_columns - physical_columns)
                if missing_columns:
                    missing_features = sorted(
                        set(missing_columns).intersection(
                            self.bar1s_feature_columns or ()
                        )
                    )
                    if missing_features:
                        raise ValueError(
                            "projected/requested 1s feature columns missing from "
                            f"physical source schema {relative_path}: "
                            f"{', '.join(missing_features)}"
                        )
                    raise ValueError(
                        "base columns missing from physical source schema "
                        f"{relative_path}: {', '.join(missing_columns)}"
                    )
            self._source_index = selected

    def _execute_stream_query(
        self, *, chunk_start_ms: int, chunk_end_ms: int
    ) -> duckdb.DuckDBPyConnection | None:
        connection = self._require_duckdb_connection()
        placeholders = ", ".join("?" for _ in self.symbols)
        required_timeframes = sorted(
            ({"1s"} if self.require_aggtrades else set())
            | self.required_kline_intervals
        )
        if not required_timeframes:
            return None
        timeframe_placeholders = ", ".join("?" for _ in required_timeframes)
        source_sql = "main.candles"
        source_parameters: list[object] = []
        if self._source_index is not None:
            source_files = self._source_files_for_chunk(
                chunk_start_ms=chunk_start_ms,
                chunk_end_ms=chunk_end_ms,
            )
            if not source_files:
                return None
            source_sql = "read_parquet(?, union_by_name=true)"
            source_parameters.append(source_files)
        source_columns = {
            str(row[0])
            for row in connection.execute(
                f"DESCRIBE SELECT * FROM {source_sql}", source_parameters
            ).fetchall()
        }
        feature_select = []
        for name in self._selected_bar1s_feature_columns:
            if name not in source_columns:
                if self.bar1s_feature_columns is not None:
                    raise ValueError(
                        "projected/requested 1s feature column is missing from "
                        f"the source schema: {name}"
                    )
                feature_select.append(f"NULL AS {name}")
            elif name in _DECIMAL_BAR1S_FEATURES:
                feature_select.append(f"CAST({name} AS VARCHAR) AS {name}")
            else:
                feature_select.append(name)
        available_time_sql = (
            "CASE WHEN timeframe = '1s' "
            "THEN epoch_ms(open_time) + 1000 + ? "
            "ELSE epoch_ms(close_time) + 1 END"
        )
        select_columns = [
            "symbol", "timeframe", "epoch_ms(open_time)",
            "epoch_ms(close_time)", "CAST(open AS VARCHAR)",
            "CAST(high AS VARCHAR)", "CAST(low AS VARCHAR)",
            "CAST(close AS VARCHAR)", "CAST(volume AS VARCHAR)",
            f"{available_time_sql} AS available_time",
            *feature_select,
        ]
        query = (
            "SELECT " + ", ".join(select_columns)
            + " "
            + f"FROM {source_sql} "
            f"WHERE symbol IN ({placeholders}) "
            f"AND timeframe IN ({timeframe_placeholders}) "
            "AND ((timeframe = '1s' AND epoch_ms(open_time) >= ? "
            "AND epoch_ms(open_time) < ?) "
            "OR (timeframe <> '1s' AND epoch_ms(close_time) >= ? "
            "AND epoch_ms(close_time) < ?)) "
            f"AND {available_time_sql} >= ? "
            f"AND {available_time_sql} < ? "
            "ORDER BY available_time, "
            "CASE WHEN timeframe = '1s' THEN 1 ELSE 2 END, "
            "symbol, open_time, close_time"
        )
        shift = self.bar1s_time_shift_ms
        return connection.execute(
            query,
            [
                shift,
                *source_parameters,
                *self.symbols,
                *required_timeframes,
                self.start_ms - shift,
                self.end_ms - shift,
                self.start_ms,
                self.end_ms,
                shift,
                chunk_start_ms,
                shift,
                chunk_end_ms,
            ],
        )

    def _source_files_for_chunk(
        self, *, chunk_start_ms: int, chunk_end_ms: int
    ) -> list[str] | None:
        if self._source_index is None:
            return None
        shift = self.bar1s_time_shift_ms
        raw_chunk_start = chunk_start_ms - shift - 1_001
        raw_chunk_end = chunk_end_ms - shift
        selected = self._source_index[
            (self._source_index["first_open_ms"] < raw_chunk_end)
            & (self._source_index["last_close_ms"] >= raw_chunk_start)
        ]
        return [
            str(self.archive_index_path.parent / relative_path)
            for relative_path in selected["relative_path"].drop_duplicates()
        ]

    def _stream_row_to_event(self, row: tuple, sequence: int) -> Event:
        symbol, timeframe = str(row[0]), str(row[1])
        open_time, close_time = int(row[2]), int(row[3])
        open_, high, low, close, volume = row[4:9]
        available_time = int(row[9])
        if timeframe == "1s":
            shifted_open = open_time + self.bar1s_time_shift_ms
            shifted_close = close_time + self.bar1s_time_shift_ms
            if shifted_close - shifted_open not in {999, 1_000}:
                raise ValueError(
                    f"invalid 1s candle duration for {symbol}: "
                    f"{shifted_open}..{shifted_close}"
                )
            close_decimal = _decimal_value(close)
            feature_values = dict.fromkeys(CANDLE_FEATURE_COLUMNS)
            feature_values.update(
                zip(
                    self._selected_bar1s_feature_columns,
                    row[10:],
                    strict=True,
                )
            )
            vwap = _optional_decimal_value(feature_values["vwap"]) or close_decimal
            return Bar1s(
                symbol=symbol, timestamp=shifted_open,
                available_time=available_time, type_priority=1, sequence=sequence,
                open=_decimal_value(open_), high=_decimal_value(high),
                low=_decimal_value(low), close=close_decimal,
                volume=_decimal_value(volume),
                trade_count=int(feature_values["trade_count"] or 0),
                vwap=vwap,
                quote_volume=_optional_decimal_value(feature_values["quote_volume"]),
                raw_trade_count=_optional_int_value(feature_values["raw_trade_count"]),
                taker_buy_volume=_optional_decimal_value(
                    feature_values["taker_buy_volume"]
                ),
                taker_sell_volume=_optional_decimal_value(
                    feature_values["taker_sell_volume"]
                ),
                taker_buy_quote_volume=_optional_decimal_value(
                    feature_values["taker_buy_quote_volume"]
                ),
                taker_sell_quote_volume=_optional_decimal_value(
                    feature_values["taker_sell_quote_volume"]
                ),
                taker_buy_trade_count=_optional_int_value(
                    feature_values["taker_buy_trade_count"]
                ),
                taker_sell_trade_count=_optional_int_value(
                    feature_values["taker_sell_trade_count"]
                ),
                taker_buy_agg_trade_count=_optional_int_value(
                    feature_values["taker_buy_agg_trade_count"]
                ),
                taker_sell_agg_trade_count=_optional_int_value(
                    feature_values["taker_sell_agg_trade_count"]
                ),
                max_agg_trade_quantity=_optional_decimal_value(
                    feature_values["max_agg_trade_quantity"]
                ),
                max_taker_buy_agg_trade_quantity=_optional_decimal_value(
                    feature_values["max_taker_buy_agg_trade_quantity"]
                ),
                max_taker_sell_agg_trade_quantity=_optional_decimal_value(
                    feature_values["max_taker_sell_agg_trade_quantity"]
                ),
                first_aggregate_trade_id=_optional_int_value(
                    feature_values["first_aggregate_trade_id"]
                ),
                last_aggregate_trade_id=_optional_int_value(
                    feature_values["last_aggregate_trade_id"]
                ),
                first_trade_id=_optional_int_value(feature_values["first_trade_id"]),
                last_trade_id=_optional_int_value(feature_values["last_trade_id"]),
            )
        return Kline(
            symbol=symbol, interval=timeframe, open_time=open_time,
            close_time=close_time, available_time=available_time,
            type_priority=2, sequence=sequence, open=_decimal_value(open_),
            high=_decimal_value(high), low=_decimal_value(low),
            close=_decimal_value(close), volume=_decimal_value(volume),
        )

    def _validate_duckdb_source(self) -> None:
        connection = self._require_duckdb_connection()
        table_exists = connection.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = 'candles'"
        ).fetchone()[0]
        if not table_exists:
            raise ValueError(f"{self.duckdb_path} is missing main.candles")

        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = 'candles'"
            ).fetchall()
        }
        required = {
            "symbol", "timeframe", "open_time", "close_time",
            "open", "high", "low", "close", "volume",
        }
        missing = sorted(required - columns)
        if missing:
            raise ValueError(
                f"{self.duckdb_path} candles missing columns: {', '.join(missing)}"
            )
        missing_features = sorted(
            (self.bar1s_feature_columns or frozenset()).difference(columns)
        )
        if missing_features:
            raise ValueError(
                "projected/requested 1s feature columns missing from source schema: "
                f"{', '.join(missing_features)}"
            )

    def _require_duckdb_connection(self) -> duckdb.DuckDBPyConnection:
        if self._duckdb_connection is None:
            raise RuntimeError("DuckDB source is not open")
        return self._duckdb_connection
