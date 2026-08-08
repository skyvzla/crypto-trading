from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import re
import time
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx

from .models import Candle


VISION_PUBLIC_ROOT = "https://data.binance.vision"
VISION_S3_ROOT = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
VISION_ROOT = f"{VISION_S3_ROOT}/data/futures/um"
NATIVE_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class ArchiveNotFoundError(Exception):
    """Binance does not publish the requested archive partition."""


@dataclass(frozen=True)
class DownloadResult:
    symbol: str
    timeframe: str
    period: str
    rows: int
    skipped: bool = False
    unavailable: bool = False


@dataclass(frozen=True)
class DownloadProgress:
    phase: str
    current: int
    total: int
    symbol: str
    timeframe: str
    period: str
    downloaded_bytes: int = 0
    elapsed_seconds: float = 0.0
    rows: int = 0


class BinanceVisionHTTPFetcher:
    """Bounded downloader that verifies every Binance Vision SHA-256 file."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        attempts: int = 3,
        retry_base_seconds: float = 1.0,
        on_retry: Callable[[str, int, int, Exception], None] | None = None,
    ) -> None:
        self._client = client
        self._attempts = max(1, attempts)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._on_retry = on_retry

    def __call__(self, url: str) -> bytes:
        content = self._get(url).content
        checksum_text = self._get(url + ".CHECKSUM").text
        parts = checksum_text.strip().split()
        if not parts or not SHA256_PATTERN.fullmatch(parts[0]):
            raise ValueError(f"invalid Binance checksum for {url}")
        actual = hashlib.sha256(content).hexdigest()
        if actual != parts[0].lower():
            raise ValueError(f"Binance checksum mismatch for {url}")
        return content

    def _get(self, url: str) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self._attempts):
            not_found = False
            for candidate in _download_urls(url):
                try:
                    response = self._client.get(
                        candidate,
                        headers={"User-Agent": "spike-trading-platform-history/1.0"},
                    )
                    response.raise_for_status()
                    return response
                except httpx.HTTPStatusError as error:
                    if error.response.status_code == 404:
                        not_found = True
                    else:
                        last_error = error
                except httpx.HTTPError as error:
                    last_error = error
            if not_found:
                raise ArchiveNotFoundError(
                    f"Binance archive not found: {url}"
                )
            if attempt + 1 < self._attempts:
                if self._on_retry is not None:
                    assert last_error is not None
                    self._on_retry(
                        url,
                        attempt + 2,
                        self._attempts,
                        last_error,
                    )
                time.sleep(self._retry_base_seconds * (2**attempt))
        assert last_error is not None
        raise last_error


def _download_urls(url: str) -> tuple[str, ...]:
    if url.startswith(VISION_S3_ROOT + "/"):
        public = VISION_PUBLIC_ROOT + url.removeprefix(VISION_S3_ROOT)
        return url, public
    if url.startswith(VISION_PUBLIC_ROOT + "/"):
        origin = VISION_S3_ROOT + url.removeprefix(VISION_PUBLIC_ROOT)
        return url, origin
    return (url,)


def parse_aggtrade_archive(
    content: bytes,
    symbol: str,
    day: str,
) -> list[Candle]:
    """Parse one Binance Vision daily aggTrades ZIP into trade-active 1s bars."""

    normalized_symbol = symbol.strip().upper()
    member = f"{normalized_symbol}-aggTrades-{day}.csv"
    trades: list[tuple[datetime, int, float, float]] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        with archive.open(member) as raw:
            rows = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))
            required = {"agg_trade_id", "price", "quantity", "transact_time"}
            if rows.fieldnames is None or not required.issubset(rows.fieldnames):
                raise ValueError(f"{member} has incompatible columns")
            for row in rows:
                timestamp = int(row["transact_time"])
                occurred = _epoch_datetime(timestamp)
                trades.append(
                    (
                        occurred,
                        int(row["agg_trade_id"]),
                        float(row["price"]),
                        float(row["quantity"]),
                    )
                )

    grouped: dict[datetime, list[tuple[datetime, int, float, float]]] = defaultdict(list)
    for trade in trades:
        grouped[trade[0].replace(microsecond=0)].append(trade)

    candles: list[Candle] = []
    for second, values in sorted(grouped.items()):
        ordered = sorted(values, key=lambda item: (item[0], item[1]))
        prices = [item[2] for item in ordered]
        candles.append(
            Candle(
                symbol=normalized_symbol,
                timeframe="1s",
                open_time=second,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=sum(item[3] for item in ordered),
                close_time=second + timedelta(seconds=1),
            )
        )
    return candles


def parse_kline_archive(
    content: bytes,
    symbol: str,
    timeframe: str,
    month: str,
) -> list[Candle]:
    """Parse one native Binance Vision monthly kline ZIP using epoch values."""

    normalized_symbol = symbol.strip().upper()
    member = f"{normalized_symbol}-{timeframe}-{month}.csv"
    candles: list[Candle] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        with archive.open(member) as raw:
            rows = csv.reader(io.TextIOWrapper(raw, encoding="utf-8"))
            for row in rows:
                if not row or row[0].strip().lower() in {"open_time", "open time"}:
                    continue
                if len(row) < 7:
                    raise ValueError(f"{member} has an incomplete row")
                candles.append(
                    Candle(
                        symbol=normalized_symbol,
                        timeframe=timeframe,
                        open_time=_epoch_datetime(int(row[0])),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        close_time=_epoch_datetime(int(row[6])),
                    )
                )
    return candles


def _epoch_datetime(value: int) -> datetime:
    divisor = 1_000_000 if abs(value) >= 100_000_000_000_000 else 1_000
    return datetime.fromtimestamp(value / divisor, tz=UTC)


def download_history(
    archive: object,
    *,
    fetch: Callable[[str], bytes],
    symbols: Sequence[str],
    timeframes: Sequence[str],
    start: datetime,
    end: datetime,
    on_progress: Callable[[DownloadProgress], None] | None = None,
    max_workers: int = 1,
    overwrite: bool = False,
) -> list[DownloadResult]:
    """Download a bounded UTC range; network reads are separate from one writer."""

    start_utc = _require_utc(start)
    end_utc = _require_utc(end)
    if end_utc <= start_utc:
        raise ValueError("end must be later than start")
    normalized_symbols = tuple(
        dict.fromkeys(value.strip().upper() for value in symbols if value.strip())
    )
    normalized_timeframes = tuple(
        dict.fromkeys(value.strip().lower() for value in timeframes if value.strip())
    )
    unsupported = set(normalized_timeframes) - NATIVE_TIMEFRAMES - {"1s"}
    if not normalized_symbols:
        raise ValueError("at least one symbol is required")
    if not normalized_timeframes or unsupported:
        raise ValueError(f"unsupported timeframes: {sorted(unsupported)}")
    if max_workers < 1:
        raise ValueError("max_workers must be positive")

    jobs: list[tuple[str, str, date | tuple[int, int]]] = []
    for symbol in normalized_symbols:
        for timeframe in normalized_timeframes:
            if timeframe == "1s":
                periods: Iterable[date | tuple[int, int]] = _days(start_utc, end_utc)
            else:
                periods = _months(start_utc, end_utc)
            for period in periods:
                jobs.append((symbol, timeframe, period))

    total = len(jobs)

    def process(
        job: tuple[int, tuple[str, str, date | tuple[int, int]]],
    ) -> DownloadResult:
        current, (symbol, timeframe, period) = job
        if isinstance(period, date):
            label = period.isoformat()
            url = aggtrade_archive_url(symbol, label)
        else:
            label = f"{period[0]:04d}-{period[1]:02d}"
            url = kline_archive_url(symbol, timeframe, label)
        if not overwrite:
            partition_rows = getattr(archive, "partition_rows", None)
            if partition_rows is not None:
                if isinstance(period, date):
                    year, month, day = period.year, period.month, period.day
                else:
                    year, month, day = period[0], period[1], 0
                existing_rows = partition_rows(
                    symbol, timeframe, year, month, day
                )
                if existing_rows is not None:
                    _notify(
                        on_progress,
                        "skipped",
                        current,
                        total,
                        symbol,
                        timeframe,
                        label,
                        rows=existing_rows,
                    )
                    return DownloadResult(
                        symbol, timeframe, label, existing_rows, skipped=True
                    )
        _notify(on_progress, "downloading", current, total, symbol, timeframe, label)
        started = time.monotonic()
        try:
            content = fetch(url)
        except ArchiveNotFoundError:
            _notify(
                on_progress,
                "unavailable",
                current,
                total,
                symbol,
                timeframe,
                label,
            )
            return DownloadResult(
                symbol, timeframe, label, 0, unavailable=True
            )
        elapsed = time.monotonic() - started
        _notify(
            on_progress,
            "downloaded",
            current,
            total,
            symbol,
            timeframe,
            label,
            downloaded_bytes=len(content),
            elapsed_seconds=elapsed,
        )
        _notify(on_progress, "processing", current, total, symbol, timeframe, label)
        if isinstance(period, date):
            candles = parse_aggtrade_archive(content, symbol, label)
        else:
            candles = parse_kline_archive(content, symbol, timeframe, label)
        # Vision files are immutable day/month partitions. Store the complete
        # source partition so partial requests cannot overwrite it.
        rows = archive.upsert(candles)
        _notify(
            on_progress,
            "stored",
            current,
            total,
            symbol,
            timeframe,
            label,
            rows=rows,
        )
        return DownloadResult(symbol, timeframe, label, rows)

    indexed_jobs = tuple(enumerate(jobs, start=1))
    if max_workers == 1:
        return [process(job) for job in indexed_jobs]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(process, indexed_jobs))


def _notify(
    callback: Callable[[DownloadProgress], None] | None,
    phase: str,
    current: int,
    total: int,
    symbol: str,
    timeframe: str,
    period: str,
    downloaded_bytes: int = 0,
    elapsed_seconds: float = 0.0,
    rows: int = 0,
) -> None:
    if callback is not None:
        callback(
            DownloadProgress(
                phase=phase,
                current=current,
                total=total,
                symbol=symbol,
                timeframe=timeframe,
                period=period,
                downloaded_bytes=downloaded_bytes,
                elapsed_seconds=elapsed_seconds,
                rows=rows,
            )
        )


def aggtrade_archive_url(symbol: str, day: str) -> str:
    normalized = symbol.strip().upper()
    filename = f"{normalized}-aggTrades-{day}.zip"
    return f"{VISION_ROOT}/daily/aggTrades/{normalized}/{filename}"


def kline_archive_url(symbol: str, timeframe: str, month: str) -> str:
    normalized = symbol.strip().upper()
    filename = f"{normalized}-{timeframe}-{month}.zip"
    return (
        f"{VISION_ROOT}/monthly/klines/{normalized}/{timeframe}/{filename}"
    )


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("download range must include a timezone")
    return value.astimezone(UTC)


def _days(start: datetime, end: datetime) -> tuple[date, ...]:
    current = start.date()
    final = (end - timedelta(microseconds=1)).date()
    values: list[date] = []
    while current <= final:
        values.append(current)
        current += timedelta(days=1)
    return tuple(values)


def _months(start: datetime, end: datetime) -> tuple[tuple[int, int], ...]:
    year, month = start.year, start.month
    final = end - timedelta(microseconds=1)
    values: list[tuple[int, int]] = []
    while (year, month) <= (final.year, final.month):
        values.append((year, month))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return tuple(values)
