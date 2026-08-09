"""
回测数据加载器

从 Parquet 或只读 DuckDB 历史归档加载事件流。
"""
import logging
from collections.abc import Iterator
from pathlib import Path
from decimal import Decimal
from typing import Union

import duckdb
import pandas as pd
from trading_platform.market.archive.index import (
    load_archive_index,
    verify_archive_index_files,
)
from trading_platform.shared.events import Bar1s, Kline

logger = logging.getLogger(__name__)

# 事件类型（用于类型提示）
Event = Union[Bar1s, Kline]


def _decimal_value(value: object) -> Decimal:
    """将归档数值转为 Decimal，避免对已格式化字符串重复调用 str。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return Decimal(value)
    return Decimal(str(value))


class BacktestDataLoader:
    """
    回测数据加载器

    职责：
    1. 从 Parquet 加载 aggTrade 并聚合为 1s Bar
    2. 从 Parquet 加载 Kline
    3. 按稳定排序键排序所有事件

    时间语义：
    - Bar1s: available_time = timestamp + 1000
    - Kline: available_time = close_time + 1
    """

    def __init__(
        self,
        data_dir: str,
        symbols: list[str],
        start_ms: int,
        end_ms: int,
        require_aggtrades: bool = False,
        required_kline_intervals: list[str] | None = None,
        duckdb_path: str | None = None,
        archive_index_path: str | None = None,
        bar1s_time_shift_ms: int = 0,
    ):
        """
        Args:
            data_dir: 数据根目录（包含 aggtrades/ 和 klines/ 子目录）
            symbols: 币种列表
            start_ms: 开始时间（毫秒时间戳）
            end_ms: 结束时间（毫秒时间戳）
            duckdb_path: 可选只读 DuckDB 归档；提供后不读取 Parquet
            bar1s_time_shift_ms: 已由外部数据对账确认时，显式平移 1s 事件时间
        """
        if start_ms >= end_ms:
            raise ValueError("start_ms must be earlier than end_ms")
        if not symbols:
            raise ValueError("symbols must not be empty")

        self.data_dir = Path(data_dir)
        self.symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.require_aggtrades = require_aggtrades
        self.required_kline_intervals = set(required_kline_intervals or [])
        self.duckdb_path = Path(duckdb_path) if duckdb_path else None
        self.archive_index_path = (
            Path(archive_index_path) if archive_index_path else None
        )
        self.bar1s_time_shift_ms = int(bar1s_time_shift_ms)
        self._duckdb_connection: duckdb.DuckDBPyConnection | None = None

        if self.duckdb_path is not None and not self.duckdb_path.is_file():
            raise FileNotFoundError(f"DuckDB archive not found: {self.duckdb_path}")

    def iter_all(
        self,
        *,
        chunk_hours: float = 24.0 * 90,
        fetch_batch_size: int = 10_000,
        duckdb_memory_limit: str | None = None,
        duckdb_threads: int = 1,
    ) -> Iterator[Event]:
        """按固定时间窗口流式读取 DuckDB 事件。

        该接口保持策略状态连续，但不会把 DuckDB 完整回测区间物化为
        Python 对象列表。目录型 Parquet 的 aggTrade 聚合路径仍会在
        内部物化源文件，因此大规模实验必须使用 DuckDB catalog。
        """
        if self.duckdb_path is None:
            # 目录型 Parquet 仍通过既有聚合器提供事件，但对外只暴露
            # 迭代器接口；实验程序默认使用 DuckDB 固定窗口读取。
            yield from self._load_all_from_source()
            return
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
            # 1s Bar 在 open_time 后 1000ms 可用，因此流式范围需要覆盖
            # end 之后的一个边界；原始时间过滤仍确保事件集合不变。
            stream_end_ms = self.end_ms + 1_001
            chunk_start_ms = self.start_ms
            while chunk_start_ms < stream_end_ms:
                chunk_end_ms = min(chunk_start_ms + chunk_ms, stream_end_ms)
                cursor = self._execute_stream_query(
                    chunk_start_ms=chunk_start_ms,
                    chunk_end_ms=chunk_end_ms,
                )
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
        rows = connection.execute(
            "SELECT symbol, timeframe, count(*) "
            "FROM main.candles "
            f"WHERE symbol IN ({placeholders}) "
            "AND timeframe IN ('1s', '1m', '5m', '15m') "
            "AND ((timeframe = '1s' AND epoch_ms(open_time) >= ? "
            "AND epoch_ms(open_time) < ?) "
            "OR (timeframe <> '1s' AND epoch_ms(close_time) >= ? "
            "AND epoch_ms(close_time) < ?)) "
            "GROUP BY symbol, timeframe",
            [
                *self.symbols,
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
        raw_one_second_start = self.start_ms - self.bar1s_time_shift_ms
        raw_one_second_end = self.end_ms - self.bar1s_time_shift_ms
        selected_parts = []
        required = set(self.required_kline_intervals)
        if self.require_aggtrades:
            required.add("1s")
        for symbol in self.symbols:
            for timeframe in required:
                start_ms = (
                    raw_one_second_start if timeframe == "1s" else self.start_ms
                )
                end_ms = raw_one_second_end if timeframe == "1s" else self.end_ms
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

    def _execute_stream_query(
        self, *, chunk_start_ms: int, chunk_end_ms: int
    ) -> duckdb.DuckDBPyConnection:
        connection = self._require_duckdb_connection()
        placeholders = ", ".join("?" for _ in self.symbols)
        available_time_sql = (
            "CASE WHEN timeframe = '1s' "
            "THEN epoch_ms(open_time) + 1000 + ? "
            "ELSE epoch_ms(close_time) + 1 END"
        )
        query = (
            "SELECT symbol, timeframe, epoch_ms(open_time), epoch_ms(close_time), "
            "CAST(open AS VARCHAR), CAST(high AS VARCHAR), "
            "CAST(low AS VARCHAR), CAST(close AS VARCHAR), "
            "CAST(volume AS VARCHAR), "
            f"{available_time_sql} AS available_time "
            "FROM main.candles "
            f"WHERE symbol IN ({placeholders}) "
            "AND timeframe IN ('1s', '1m', '5m', '15m') "
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
                *self.symbols,
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
            return Bar1s(
                symbol=symbol,
                timestamp=shifted_open,
                available_time=available_time,
                type_priority=1,
                sequence=sequence,
                open=_decimal_value(open_),
                high=_decimal_value(high),
                low=_decimal_value(low),
                close=close_decimal,
                volume=_decimal_value(volume),
                trade_count=0,
                vwap=close_decimal,
            )
        return Kline(
            symbol=symbol,
            interval=timeframe,
            open_time=open_time,
            close_time=close_time,
            available_time=available_time,
            type_priority=2,
            sequence=sequence,
            open=_decimal_value(open_),
            high=_decimal_value(high),
            low=_decimal_value(low),
            close=_decimal_value(close),
            volume=_decimal_value(volume),
        )

    def _load_all_from_source(self) -> list[Event]:
        """
        加载所有数据并合并排序

        Returns:
            按 (available_time, type_priority, symbol, sequence) 排序的事件列表
        """
        logger.info(
            f"Loading backtest data: symbols={self.symbols}, "
            f"start={self.start_ms}, end={self.end_ms}"
        )

        events: list[Event] = []

        # 1. 加载 aggTrade 聚合结果或 DuckDB 1s candle
        for symbol in self.symbols:
            try:
                bars = self._load_bars(symbol)
                if self.require_aggtrades and not bars:
                    source_name = (
                        "aggTrade rows"
                        if self.duckdb_path is None
                        else "1s market data"
                    )
                    raise ValueError(
                        f"No {source_name} in requested range for {symbol}"
                    )
                events.extend(bars)
                logger.info(f"Loaded {len(bars)} bars for {symbol}")
            except FileNotFoundError:
                if self.require_aggtrades:
                    source_name = (
                        "aggTrade data"
                        if self.duckdb_path is None
                        else "1s market data"
                    )
                    raise ValueError(f"Missing required {source_name} for {symbol}")
                logger.warning(f"No aggTrade data found for {symbol}")

        # 2. 加载 K 线
        for symbol in self.symbols:
            for interval in ['1m', '5m', '15m']:
                try:
                    klines = self._load_klines(symbol, interval)
                    if interval in self.required_kline_intervals and not klines:
                        raise ValueError(
                            f"No {interval} Kline rows in requested range for {symbol}"
                        )
                    events.extend(klines)
                    logger.info(
                        f"Loaded {len(klines)} {interval} klines for {symbol}"
                    )
                except FileNotFoundError:
                    if interval in self.required_kline_intervals:
                        raise ValueError(
                            f"Missing required {interval} Kline data for {symbol}"
                        )
                    logger.warning(
                        f"No {interval} kline data found for {symbol}"
                    )

        # 3. 按稳定排序键排序（确定性关键）
        events.sort(
            key=lambda e: (
                e.available_time,
                e.type_priority,
                e.symbol,
                e.sequence
            )
        )

        logger.info(f"Total events loaded: {len(events)}")
        return events

    def _load_bars(self, symbol: str) -> list[Bar1s]:
        """
        加载 aggTrade 并聚合为 1s Bar

        Args:
            symbol: 币种符号

        Returns:
            1s Bar 列表
        """
        if self._duckdb_connection is not None:
            return self._load_duckdb_bars(symbol)

        # 尝试多种路径模式
        possible_paths = [
            self.data_dir / 'aggtrades' / f'{symbol}.parquet',
            self.data_dir / 'aggtrades' / symbol / 'data.parquet',
        ]

        agg_trades_path = None
        for path in possible_paths:
            if path.exists():
                agg_trades_path = path
                break

        if not agg_trades_path:
            raise FileNotFoundError(
                f"aggTrade file not found for {symbol} in {self.data_dir}"
            )

        # 读取 Parquet
        df = pd.read_parquet(agg_trades_path)

        self._require_columns(df, {'trade_time', 'price', 'qty'}, agg_trades_path)

        # 过滤时间范围并按事件时间稳定排序，确保 OHLC 不依赖文件行顺序。
        raw_start_ms = self.start_ms - self.bar1s_time_shift_ms
        raw_end_ms = self.end_ms - self.bar1s_time_shift_ms
        df = df[
            (df['trade_time'] >= raw_start_ms) &
            (df['trade_time'] < raw_end_ms)
        ].sort_values('trade_time', kind='stable')

        if df.empty:
            return []
        if self.bar1s_time_shift_ms:
            df = df.copy()
            df['trade_time'] += self.bar1s_time_shift_ms

        # 聚合为 1s Bar
        return self._aggregate_to_1s_bars(df, symbol)

    def _aggregate_to_1s_bars(
        self,
        agg_trades: pd.DataFrame,
        symbol: str
    ) -> list[Bar1s]:
        """
        将 aggTrade 聚合为 1s Bar

        Args:
            agg_trades: aggTrade DataFrame
            symbol: 币种符号

        Returns:
            1s Bar 列表
        """
        bars = []

        # 按秒分组（向下取整到秒）
        agg_trades = agg_trades.copy()
        agg_trades['second'] = agg_trades['trade_time'] // 1000

        for idx, (second, group) in enumerate(agg_trades.groupby('second')):
            # 确保价格和数量是 Decimal
            prices = [Decimal(str(p)) for p in group['price'].values]
            quantities = [Decimal(str(q)) for q in group['qty'].values]

            # 计算 VWAP
            total_value = sum(p * q for p, q in zip(prices, quantities))
            total_qty = sum(quantities)
            vwap = total_value / total_qty if total_qty > 0 else prices[0]

            bar = Bar1s(
                symbol=symbol,
                timestamp=int(second * 1000),  # 秒开始时间
                available_time=int(second * 1000 + 1000),  # 秒结束后可用
                type_priority=1,  # Bar1s 优先级
                sequence=idx,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=total_qty,
                trade_count=len(group),
                vwap=vwap
            )
            bars.append(bar)

        return bars

    def _load_klines(self, symbol: str, interval: str) -> list[Kline]:
        """
        加载 K 线数据

        Args:
            symbol: 币种符号
            interval: K 线周期（如 '1m', '5m'）

        Returns:
            Kline 列表
        """
        if self._duckdb_connection is not None:
            return self._load_duckdb_klines(symbol, interval)

        # 尝试多种路径模式
        possible_paths = [
            self.data_dir / 'klines' / f'{symbol}_{interval}.parquet',
            self.data_dir / 'klines' / symbol / f'{interval}.parquet',
        ]

        kline_path = None
        for path in possible_paths:
            if path.exists():
                kline_path = path
                break

        if not kline_path:
            raise FileNotFoundError(
                f"Kline file not found for {symbol} {interval}"
            )

        # 读取 Parquet（只读取 is_final=True 的）
        df = pd.read_parquet(kline_path)

        self._require_columns(
            df,
            {'open_time', 'close_time', 'open', 'high', 'low', 'close', 'volume'},
            kline_path,
        )

        # 过滤 is_final 和时间范围
        if 'is_final' in df.columns:
            df = df[df['is_final'] == True]

        df = df[
            (df['close_time'] >= self.start_ms) &
            (df['close_time'] < self.end_ms)
        ].sort_values(['close_time', 'open_time'], kind='stable')

        if df.empty:
            return []

        # 转换为 Kline 对象
        klines = []
        for idx, row in df.iterrows():
            kline = Kline(
                symbol=symbol,
                interval=interval,
                open_time=int(row['open_time']),
                close_time=int(row['close_time']),
                available_time=int(row['close_time'] + 1),  # K 线完成后 1ms 可用
                type_priority=2,  # Kline 优先级低于 Bar
                sequence=idx,
                open=Decimal(str(row['open'])),
                high=Decimal(str(row['high'])),
                low=Decimal(str(row['low'])),
                close=Decimal(str(row['close'])),
                volume=Decimal(str(row['volume']))
            )
            klines.append(kline)

        return klines

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
            "symbol",
            "timeframe",
            "open_time",
            "close_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
        }
        missing = sorted(required - columns)
        if missing:
            raise ValueError(
                f"{self.duckdb_path} candles missing columns: {', '.join(missing)}"
            )

    def _load_duckdb_bars(self, symbol: str) -> list[Bar1s]:
        """读取预聚合 1s candle；归档不提供的成交元数据使用显式哨兵。"""
        rows = self._query_duckdb_candles(
            symbol,
            "1s",
            time_column="open_time",
            start_ms=self.start_ms - self.bar1s_time_shift_ms,
            end_ms=self.end_ms - self.bar1s_time_shift_ms,
        )
        bars: list[Bar1s] = []
        for sequence, row in enumerate(rows):
            open_time, close_time, open_, high, low, close, volume = row
            open_time = int(open_time) + self.bar1s_time_shift_ms
            close_time = int(close_time) + self.bar1s_time_shift_ms
            duration_ms = int(close_time) - int(open_time)
            if duration_ms not in {999, 1_000}:
                raise ValueError(
                    f"invalid 1s candle duration for {symbol}: "
                    f"{open_time}..{close_time}"
                )
            close_decimal = Decimal(str(close))
            bars.append(
                Bar1s(
                    symbol=symbol,
                    timestamp=int(open_time),
                    # 归档同时存在“最后毫秒”和“右开边界”两种 close_time；
                    # 统一在下一秒边界可用。
                    available_time=int(open_time) + 1_000,
                    type_priority=1,
                    sequence=sequence,
                    open=Decimal(str(open_)),
                    high=Decimal(str(high)),
                    low=Decimal(str(low)),
                    close=close_decimal,
                    volume=Decimal(str(volume)),
                    # Candle 归档没有逐笔成交数/VWAP；0 明确表示未知。
                    # Spike 不读取这两个字段，vwap 仅以 close 满足事件结构。
                    trade_count=0,
                    vwap=close_decimal,
                )
            )
        return bars

    def _load_duckdb_klines(self, symbol: str, interval: str) -> list[Kline]:
        rows = self._query_duckdb_candles(
            symbol,
            interval,
            time_column="close_time",
        )
        return [
            Kline(
                symbol=symbol,
                interval=interval,
                open_time=int(row[0]),
                close_time=int(row[1]),
                available_time=int(row[1]) + 1,
                type_priority=2,
                sequence=sequence,
                open=Decimal(str(row[2])),
                high=Decimal(str(row[3])),
                low=Decimal(str(row[4])),
                close=Decimal(str(row[5])),
                volume=Decimal(str(row[6])),
            )
            for sequence, row in enumerate(rows)
        ]

    def _query_duckdb_candles(
        self,
        symbol: str,
        timeframe: str,
        *,
        time_column: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[tuple]:
        if time_column not in {"open_time", "close_time"}:
            raise ValueError(f"unsupported candle time column: {time_column}")
        connection = self._require_duckdb_connection()
        return connection.execute(
            "SELECT epoch_ms(open_time), epoch_ms(close_time), "
            "open, high, low, close, volume "
            "FROM main.candles "
            "WHERE symbol = ? AND timeframe = ? "
            f"AND epoch_ms({time_column}) >= ? "
            f"AND epoch_ms({time_column}) < ? "
            "ORDER BY open_time, close_time",
            [
                symbol,
                timeframe,
                self.start_ms if start_ms is None else start_ms,
                self.end_ms if end_ms is None else end_ms,
            ],
        ).fetchall()

    def _require_duckdb_connection(self) -> duckdb.DuckDBPyConnection:
        if self._duckdb_connection is None:
            raise RuntimeError("DuckDB source is not open")
        return self._duckdb_connection

    @staticmethod
    def _require_columns(
        df: pd.DataFrame, required: set[str], source: Path
    ) -> None:
        """在进入聚合前给出可操作的数据格式错误。"""
        missing = sorted(required.difference(df.columns))
        if missing:
            raise ValueError(
                f"{source} missing required columns: {', '.join(missing)}"
            )
