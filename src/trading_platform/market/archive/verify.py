from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

import duckdb
import httpx


@dataclass(frozen=True)
class OfficialKline:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float


OfficialKlineFetcher = Callable[[str, int], OfficialKline]


class BinanceUSDMKlineFetcher:
    """Fetch a single official USD-M one-minute kline by UTC epoch."""

    endpoint = "https://fapi.binance.com/fapi/v1/klines"

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __call__(self, symbol: str, open_time_ms: int) -> OfficialKline:
        response = self._client.get(
            self.endpoint,
            params={
                "symbol": symbol.strip().upper(),
                "interval": "1m",
                "startTime": open_time_ms,
                "endTime": open_time_ms + 59_999,
                "limit": 1,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload or len(payload[0]) < 5:
            raise ValueError(
                f"Binance returned no 1m kline for {symbol} at {open_time_ms}"
            )
        row = payload[0]
        return OfficialKline(
            open_time_ms=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
        )


@dataclass(frozen=True)
class SymbolVerification:
    symbol: str
    matched_minutes: int
    archive_max_relative_error: float
    official_samples: int
    official_max_relative_error: float | None
    sampled_open_times: tuple[datetime, ...]


@dataclass(frozen=True)
class VerificationReport:
    symbols: tuple[SymbolVerification, ...]
    tolerance: float

    @property
    def passed(self) -> bool:
        return bool(self.symbols) and all(
            item.matched_minutes > 0
            and item.archive_max_relative_error <= self.tolerance
            and (
                item.official_max_relative_error is None
                or item.official_max_relative_error <= self.tolerance
            )
            for item in self.symbols
        )


def verify_history(
    archive_path: str | Path,
    *,
    tolerance: float = 1e-9,
    official_fetcher: OfficialKlineFetcher | None = None,
    official_sample_count: int = 0,
    symbols_filter: Sequence[str] = (),
    start: datetime | None = None,
    end: datetime | None = None,
) -> VerificationReport:
    normalized_symbols = tuple(
        dict.fromkeys(item.strip().upper() for item in symbols_filter if item.strip())
    )
    filter_sql, filter_params = _filter_sql(normalized_symbols, start, end)
    connection = duckdb.connect(str(archive_path), read_only=True)
    try:
        connection.execute("SET TimeZone = 'UTC'")
        summaries = connection.execute(
            f"""
            WITH seconds AS (
                SELECT
                    symbol,
                    (epoch_ms(open_time) // 60000) * 60000 AS minute_ms,
                    arg_min(open, epoch_ms(open_time)) AS open,
                    max(high) AS high,
                    min(low) AS low,
                    arg_max(close, epoch_ms(open_time)) AS close
                FROM candles
                WHERE timeframe = '1s' {filter_sql}
                GROUP BY symbol, minute_ms
            ), minutes AS (
                SELECT
                    symbol,
                    epoch_ms(open_time) AS minute_ms,
                    open,
                    high,
                    low,
                    close
                FROM candles
                WHERE timeframe = '1m' {filter_sql}
            ), comparisons AS (
                SELECT
                    seconds.symbol,
                    seconds.minute_ms,
                    greatest(
                        abs(seconds.open - minutes.open)
                            / greatest(abs(minutes.open), 1e-300),
                        abs(seconds.high - minutes.high)
                            / greatest(abs(minutes.high), 1e-300),
                        abs(seconds.low - minutes.low)
                            / greatest(abs(minutes.low), 1e-300),
                        abs(seconds.close - minutes.close)
                            / greatest(abs(minutes.close), 1e-300)
                    ) AS relative_error
                FROM seconds
                JOIN minutes USING (symbol, minute_ms)
            ), symbols AS (
                SELECT DISTINCT symbol
                FROM candles
                WHERE timeframe = '1s' {filter_sql}
            )
            SELECT
                symbols.symbol,
                count(comparisons.minute_ms) AS matched_minutes,
                coalesce(max(comparisons.relative_error), 'Infinity'::DOUBLE)
                    AS max_relative_error
            FROM symbols
            LEFT JOIN comparisons USING (symbol)
            GROUP BY symbols.symbol
            ORDER BY symbols.symbol
            """,
            [*filter_params, *filter_params, *filter_params],
        ).fetchall()
        samples = _load_official_samples(
            connection, official_sample_count, filter_sql, filter_params
        )
    finally:
        connection.close()

    samples_by_symbol: dict[str, list[tuple[int, float, float, float, float]]] = {}
    for symbol, minute_ms, open_, high, low, close in samples:
        samples_by_symbol.setdefault(symbol, []).append(
            (minute_ms, open_, high, low, close)
        )

    symbols: list[SymbolVerification] = []
    for symbol, matched_minutes, archive_error in summaries:
        selected = samples_by_symbol.get(symbol, []) if official_fetcher else []
        official_errors: list[float] = []
        for minute_ms, open_, high, low, close in selected:
            official = official_fetcher(symbol, minute_ms)
            if official.open_time_ms != minute_ms:
                official_errors.append(float("inf"))
                continue
            official_errors.append(
                _max_relative_error(
                    (open_, high, low, close),
                    (official.open, official.high, official.low, official.close),
                )
            )
        symbols.append(
            SymbolVerification(
                symbol=symbol,
                matched_minutes=matched_minutes,
                archive_max_relative_error=archive_error,
                official_samples=len(official_errors),
                official_max_relative_error=(
                    max(official_errors) if official_errors else None
                ),
                sampled_open_times=tuple(
                    datetime.fromtimestamp(item[0] / 1000, tz=UTC) for item in selected
                ),
            )
        )
    return VerificationReport(symbols=tuple(symbols), tolerance=tolerance)


def _load_official_samples(
    connection: duckdb.DuckDBPyConnection,
    count: int,
    filter_sql: str,
    filter_params: Sequence[object],
) -> list[tuple[str, int, float, float, float, float]]:
    if count <= 0:
        return []
    ranked = f"""
        WITH ranked AS (
            SELECT
                symbol,
                epoch_ms(open_time) AS minute_ms,
                open,
                high,
                low,
                close,
                row_number() OVER (PARTITION BY symbol ORDER BY open_time) AS row_number,
                count(*) OVER (PARTITION BY symbol) AS total_rows
            FROM candles
            WHERE timeframe = '1m' {filter_sql}
        )
    """
    if count == 1:
        return connection.execute(
            ranked
            + """
                SELECT symbol, minute_ms, open, high, low, close
                FROM ranked
                WHERE row_number = ((total_rows + 1) // 2)
                ORDER BY symbol, minute_ms
            """,
            filter_params,
        ).fetchall()
    return connection.execute(
        ranked
        + """
            SELECT DISTINCT symbol, minute_ms, open, high, low, close
            FROM ranked
            CROSS JOIN range(?) AS samples(sample_index)
            WHERE row_number = 1 + round(
                sample_index * (total_rows - 1) / (? - 1)
            )
            ORDER BY symbol, minute_ms
        """,
        [*filter_params, count, count],
    ).fetchall()


def _filter_sql(
    symbols: Sequence[str], start: datetime | None, end: datetime | None
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if symbols:
        clauses.append("AND symbol IN (" + ", ".join("?" for _ in symbols) + ")")
        params.extend(symbols)
    for operator, value in ((">=", start), ("<", end)):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("verification times must include a timezone")
            clauses.append(f"AND epoch_ms(open_time) {operator} ?")
            params.append(int(value.astimezone(UTC).timestamp() * 1000))
    return " ".join(clauses), params


def _max_relative_error(
    actual: Sequence[float], expected: Sequence[float]
) -> float:
    return max(
        abs(actual_value - expected_value) / max(abs(expected_value), 1e-300)
        for actual_value, expected_value in zip(actual, expected, strict=True)
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify archived 1s candles against 1m and Binance USD-M data."
    )
    parser.add_argument("archive", type=Path, help="DuckDB archive path")
    parser.add_argument("--symbols", nargs="*", default=())
    parser.add_argument("--start", type=_parse_datetime, default=None)
    parser.add_argument("--end", type=_parse_datetime, default=None)
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1e-9,
        help="maximum allowed relative OHLC error (default: 1e-9)",
    )
    parser.add_argument(
        "--official-samples",
        type=int,
        default=5,
        help="evenly spaced Binance 1m samples per symbol; 0 disables network checks",
    )
    args = parser.parse_args(argv)
    if args.tolerance < 0:
        parser.error("--tolerance must be non-negative")
    if args.official_samples < 0:
        parser.error("--official-samples must be non-negative")

    verification_options = {
        "tolerance": args.tolerance,
        "official_sample_count": args.official_samples,
        "symbols_filter": args.symbols,
        "start": args.start,
        "end": args.end,
    }
    if args.official_samples:
        with httpx.Client(timeout=15.0) as client:
            report = verify_history(
                args.archive,
                official_fetcher=BinanceUSDMKlineFetcher(client),
                **verification_options,
            )
    else:
        report = verify_history(args.archive, **verification_options)

    for item in report.symbols:
        official_error = (
            "not_checked"
            if item.official_max_relative_error is None
            else f"{item.official_max_relative_error:.12g}"
        )
        print(
            f"{item.symbol}: matched={item.matched_minutes} "
            f"archive_max_relative_error={item.archive_max_relative_error:.12g} "
            f"official_samples={item.official_samples} "
            f"official_max_relative_error={official_error}"
        )
    if not report.symbols:
        print("No matching 1s/1m candles found.")
    return 0 if report.passed else 1


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("time must include a timezone")
    return parsed.astimezone(UTC)
