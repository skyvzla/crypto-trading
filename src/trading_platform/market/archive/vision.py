from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import io
import re
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from threading import Lock, get_ident
from typing import BinaryIO

import httpx

from .models import Candle


VISION_PUBLIC_ROOT = "https://data.binance.vision"
VISION_S3_ROOT = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
VISION_ROOT = f"{VISION_S3_ROOT}/data/futures/um"
EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
NATIVE_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class ArchiveNotFoundError(Exception):
    """Binance does not publish the requested archive partition."""


@dataclass(frozen=True)
class SymbolAvailability:
    onboard_time: datetime | None
    delivery_time: datetime | None

    def intersects(self, start: datetime, end: datetime) -> bool:
        return (
            (self.onboard_time is None or end > self.onboard_time)
            and (self.delivery_time is None or start < self.delivery_time)
        )


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
        with self.open_archive(url) as source:
            return source.read()

    @contextmanager
    def open_archive(self, url: str) -> Iterator[BinaryIO]:
        """Stream a verified archive to disk and keep it seekable for ZIP."""

        with tempfile.TemporaryFile(mode="w+b") as source:
            actual = self._download_to(url, source)
            checksum_text = self._get(url + ".CHECKSUM").text
            self._verify_checksum(url, checksum_text, actual)
            source.seek(0)
            yield source

    @staticmethod
    def _verify_checksum(url: str, checksum_text: str, actual: str) -> None:
        parts = checksum_text.strip().split()
        if not parts or not SHA256_PATTERN.fullmatch(parts[0]):
            raise ValueError(f"invalid Binance checksum for {url}")
        if actual != parts[0].lower():
            raise ValueError(f"Binance checksum mismatch for {url}")

    def _download_to(self, url: str, target: BinaryIO) -> str:
        last_error: Exception | None = None
        for attempt in range(self._attempts):
            not_found = False
            for candidate in _download_urls(url):
                target.seek(0)
                target.truncate()
                digest = hashlib.sha256()
                try:
                    with self._client.stream(
                        "GET",
                        candidate,
                        headers={
                            "User-Agent": "spike-trading-platform-history/1.0"
                        },
                    ) as response:
                        response.raise_for_status()
                        for chunk in response.iter_bytes():
                            target.write(chunk)
                            digest.update(chunk)
                    target.flush()
                    return digest.hexdigest()
                except httpx.HTTPStatusError as error:
                    if error.response.status_code == 404:
                        not_found = True
                    else:
                        last_error = error
                except httpx.HTTPError as error:
                    last_error = error
            if not_found:
                raise ArchiveNotFoundError(f"Binance archive not found: {url}")
            if attempt + 1 < self._attempts:
                if self._on_retry is not None:
                    assert last_error is not None
                    self._on_retry(url, attempt + 2, self._attempts, last_error)
                time.sleep(self._retry_base_seconds * (2**attempt))
        assert last_error is not None
        raise last_error

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


class BinanceVisionWorkerPoolFetcher:
    """Bind every archive worker thread to one fetcher for its lifetime."""

    def __init__(self, fetchers: Sequence[Callable[[str], bytes]]) -> None:
        if not fetchers:
            raise ValueError("at least one proxy fetcher is required")
        self._fetchers = tuple(fetchers)
        self._lock = Lock()
        self._assignments: dict[int, Callable[[str], bytes]] = {}
        self._next = 0

    def __call__(self, url: str) -> bytes:
        return self._fetcher()(url)

    @contextmanager
    def open_archive(self, url: str) -> Iterator[bytes | BinaryIO]:
        with _open_fetched_archive(self._fetcher(), url) as source:
            yield source

    def _fetcher(self) -> Callable[[str], bytes]:
        worker = get_ident()
        with self._lock:
            fetcher = self._assignments.get(worker)
            if fetcher is not None:
                return fetcher
            if len(self._assignments) == len(self._fetchers):
                raise RuntimeError("more download workers than configured proxies")
            fetcher = self._fetchers[self._next]
            self._next = (self._next + 1) % len(self._fetchers)
            self._assignments[worker] = fetcher
            return fetcher


class BinanceFuturesMetadataFetcher:
    """Load exact symbol lifecycle bounds from USD-M exchangeInfo."""

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

    def __call__(
        self, symbols: Sequence[str]
    ) -> dict[str, SymbolAvailability]:
        last_error: Exception | None = None
        for attempt in range(self._attempts):
            try:
                response = self._client.get(EXCHANGE_INFO_URL)
                response.raise_for_status()
                return parse_symbol_availability(response.json(), symbols)
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
            if attempt + 1 < self._attempts:
                if self._on_retry is not None:
                    self._on_retry(
                        EXCHANGE_INFO_URL,
                        attempt + 2,
                        self._attempts,
                        last_error,
                    )
                time.sleep(self._retry_base_seconds * (2**attempt))
        assert last_error is not None
        raise last_error


def parse_symbol_availability(
    payload: object,
    symbols: Sequence[str],
) -> dict[str, SymbolAvailability]:
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise ValueError("Binance exchangeInfo has incompatible symbol metadata")
    requested = {item.strip().upper() for item in symbols if item.strip()}
    availability: dict[str, SymbolAvailability] = {}
    for item in payload["symbols"]:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        if symbol not in requested:
            continue
        availability[symbol] = SymbolAvailability(
            onboard_time=_optional_epoch_datetime(item.get("onboardDate")),
            delivery_time=_optional_epoch_datetime(item.get("deliveryDate")),
        )
    return availability


def _optional_epoch_datetime(value: object) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return _epoch_datetime(int(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid Binance lifecycle timestamp: {value!r}") from error


def _download_urls(url: str) -> tuple[str, ...]:
    if url.startswith(VISION_S3_ROOT + "/"):
        public = VISION_PUBLIC_ROOT + url.removeprefix(VISION_S3_ROOT)
        return url, public
    if url.startswith(VISION_PUBLIC_ROOT + "/"):
        origin = VISION_S3_ROOT + url.removeprefix(VISION_PUBLIC_ROOT)
        return url, origin
    return (url,)


def parse_aggtrade_archive(
    content: bytes | BinaryIO,
    symbol: str,
    day: str,
) -> list[Candle]:
    """Parse one Binance Vision daily aggTrades ZIP into trade-active 1s bars."""

    normalized_symbol = symbol.strip().upper()
    member = f"{normalized_symbol}-aggTrades-{day}.csv"
    # Keep only aggregate state per second. Vision rows are normally ordered,
    # but tracking the timestamp/id keys preserves correct open/close prices
    # for unordered input without retaining the full trade file in memory.
    grouped: dict[
        datetime,
        tuple[
            tuple[datetime, int, float],
            tuple[datetime, int, float],
            float,
            float,
            float,
        ],
    ] = {}
    source = io.BytesIO(content) if isinstance(content, bytes) else content
    with zipfile.ZipFile(source) as archive:
        with archive.open(member) as raw:
            rows = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))
            required = {"agg_trade_id", "price", "quantity", "transact_time"}
            if rows.fieldnames is None or not required.issubset(rows.fieldnames):
                raise ValueError(f"{member} has incompatible columns")
            for row in rows:
                timestamp = int(row["transact_time"])
                occurred = _epoch_datetime(timestamp)
                trade_id = int(row["agg_trade_id"])
                price = float(row["price"])
                quantity = float(row["quantity"])
                second = occurred.replace(microsecond=0)
                key = (occurred, trade_id, price)
                current = grouped.get(second)
                if current is None:
                    grouped[second] = (key, key, price, price, quantity)
                    continue
                first, last, high, low, volume = current
                grouped[second] = (
                    min(first, key),
                    max(last, key),
                    max(high, price),
                    min(low, price),
                    volume + quantity,
                )

    candles: list[Candle] = []
    for second, (first, last, high, low, volume) in sorted(grouped.items()):
        candles.append(
            Candle(
                symbol=normalized_symbol,
                timeframe="1s",
                open_time=second,
                open=first[2],
                high=high,
                low=low,
                close=last[2],
                volume=volume,
                close_time=second + timedelta(seconds=1),
            )
        )
    return candles


def parse_monthly_aggtrade_archive(
    content: bytes | BinaryIO,
    symbol: str,
    month: str,
) -> Iterator[tuple[date, list[Candle]]]:
    """Stream one monthly aggTrades ZIP as bounded daily 1s partitions."""

    normalized_symbol = symbol.strip().upper()
    member = f"{normalized_symbol}-aggTrades-{month}.csv"
    source: bytes | BinaryIO
    source = io.BytesIO(content) if isinstance(content, bytes) else content
    with zipfile.ZipFile(source) as archive:
        with archive.open(member) as raw:
            rows = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))
            required = {"agg_trade_id", "price", "quantity", "transact_time"}
            if rows.fieldnames is None or not required.issubset(rows.fieldnames):
                raise ValueError(f"{member} has incompatible columns")

            partition_day: date | None = None
            partition: list[Candle] = []
            second: datetime | None = None
            first: tuple[datetime, int, float] | None = None
            last: tuple[datetime, int, float] | None = None
            high = low = volume = 0.0
            previous_key: tuple[datetime, int] | None = None

            def flush_second() -> None:
                nonlocal second, first, last, high, low, volume
                if second is None or first is None or last is None:
                    return
                partition.append(
                    Candle(
                        symbol=normalized_symbol,
                        timeframe="1s",
                        open_time=second,
                        open=first[2],
                        high=high,
                        low=low,
                        close=last[2],
                        volume=volume,
                        close_time=second + timedelta(seconds=1),
                    )
                )
                second = None
                first = None
                last = None

            for row in rows:
                occurred = _epoch_datetime(int(row["transact_time"]))
                trade_id = int(row["agg_trade_id"])
                price = float(row["price"])
                quantity = float(row["quantity"])
                key = (occurred, trade_id)
                if previous_key is not None and key < previous_key:
                    raise ValueError(f"{member} is not ordered by trade time and id")
                previous_key = key

                row_second = occurred.replace(microsecond=0)
                row_day = row_second.date()
                if partition_day is None:
                    partition_day = row_day
                if row_day != partition_day:
                    flush_second()
                    yield partition_day, partition
                    partition_day = row_day
                    partition = []

                row_key = (occurred, trade_id, price)
                if second != row_second:
                    flush_second()
                    second = row_second
                    first = last = row_key
                    high = low = price
                    volume = quantity
                else:
                    assert first is not None
                    last = row_key
                    high = max(high, price)
                    low = min(low, price)
                    volume += quantity

            flush_second()
            if partition_day is not None:
                yield partition_day, partition


def parse_kline_archive(
    content: bytes | BinaryIO,
    symbol: str,
    timeframe: str,
    month: str,
) -> list[Candle]:
    """Parse one native Binance Vision monthly kline ZIP using epoch values."""

    normalized_symbol = symbol.strip().upper()
    member = f"{normalized_symbol}-{timeframe}-{month}.csv"
    candles: list[Candle] = []
    source = io.BytesIO(content) if isinstance(content, bytes) else content
    with zipfile.ZipFile(source) as archive:
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
    symbol_availability: Mapping[str, SymbolAvailability] | None = None,
    storage_check: Callable[[], None] | None = None,
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
                periods: Iterable[date | tuple[int, int]] = _aggtrade_periods(
                    start_utc, end_utc
                )
            else:
                periods = _months(start_utc, end_utc)
            for period in periods:
                bounds = (symbol_availability or {}).get(symbol)
                period_start, period_end = _period_bounds(period)
                if bounds is not None and not bounds.intersects(
                    max(period_start, start_utc), min(period_end, end_utc)
                ):
                    continue
                jobs.append((symbol, timeframe, period))

    total = len(jobs)

    def process(
        job: tuple[int, tuple[str, str, date | tuple[int, int]]],
    ) -> DownloadResult:
        current, (symbol, timeframe, period) = job
        monthly_seconds = timeframe == "1s" and not isinstance(period, date)
        if isinstance(period, date):
            label = period.isoformat()
            url = aggtrade_archive_url(symbol, label)
        else:
            label = f"{period[0]:04d}-{period[1]:02d}"
            if monthly_seconds:
                url = monthly_aggtrade_archive_url(symbol, label)
            else:
                url = kline_archive_url(symbol, timeframe, label)
        partition_rows = getattr(archive, "partition_rows", None)
        existing_daily_rows: dict[date, int | None] = {}
        if not overwrite:
            if partition_rows is not None:
                if monthly_seconds:
                    relevant_days = _relevant_days(
                        period,
                        start_utc,
                        end_utc,
                        (symbol_availability or {}).get(symbol),
                    )
                    existing_daily_rows = {
                        day: partition_rows(
                            symbol, timeframe, day.year, day.month, day.day
                        )
                        for day in relevant_days
                    }
                    if existing_daily_rows and all(
                        rows is not None for rows in existing_daily_rows.values()
                    ):
                        existing_rows = sum(
                            rows or 0 for rows in existing_daily_rows.values()
                        )
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
                            symbol,
                            timeframe,
                            label,
                            existing_rows,
                            skipped=True,
                        )
                elif isinstance(period, date):
                    year, month, day = period.year, period.month, period.day
                else:
                    year, month, day = period[0], period[1], 0
                if not monthly_seconds:
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
        if storage_check is not None:
            storage_check()
        _notify(on_progress, "downloading", current, total, symbol, timeframe, label)
        started = time.monotonic()
        try:
            with _open_fetched_archive(fetch, url) as content:
                elapsed = time.monotonic() - started
                _notify(
                    on_progress,
                    "downloaded",
                    current,
                    total,
                    symbol,
                    timeframe,
                    label,
                    downloaded_bytes=_archive_size(content),
                    elapsed_seconds=elapsed,
                )
                _notify(
                    on_progress,
                    "processing",
                    current,
                    total,
                    symbol,
                    timeframe,
                    label,
                )
                if monthly_seconds:
                    relevant_days = set(
                        _relevant_days(
                            period,
                            start_utc,
                            end_utc,
                            (symbol_availability or {}).get(symbol),
                        )
                    )
                    rows = 0
                    for day, candles in parse_monthly_aggtrade_archive(
                        content, symbol, label
                    ):
                        if day not in relevant_days:
                            continue
                        existing_rows = existing_daily_rows.get(day)
                        if existing_rows is not None:
                            rows += existing_rows
                            continue
                        if storage_check is not None:
                            storage_check()
                        rows += archive.upsert(candles)
                else:
                    if isinstance(period, date):
                        candles = parse_aggtrade_archive(content, symbol, label)
                    else:
                        candles = parse_kline_archive(
                            content, symbol, timeframe, label
                        )
                    # Vision files are immutable day/month partitions. Store the
                    # complete source partition rather than a requested slice.
                    if storage_check is not None:
                        storage_check()
                    rows = archive.upsert(candles)
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


@contextmanager
def _open_fetched_archive(
    fetch: Callable[[str], bytes], url: str
) -> Iterator[bytes | BinaryIO]:
    streaming_fetch = getattr(type(fetch), "open_archive", None)
    if callable(streaming_fetch):
        with streaming_fetch(fetch, url) as source:
            yield source
        return
    yield fetch(url)


def _archive_size(content: bytes | BinaryIO) -> int:
    if isinstance(content, bytes):
        return len(content)
    position = content.tell()
    content.seek(0, io.SEEK_END)
    size = content.tell()
    content.seek(position)
    return size


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


def monthly_aggtrade_archive_url(symbol: str, month: str) -> str:
    normalized = symbol.strip().upper()
    filename = f"{normalized}-aggTrades-{month}.zip"
    return f"{VISION_ROOT}/monthly/aggTrades/{normalized}/{filename}"


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


def _aggtrade_periods(
    start: datetime, end: datetime
) -> tuple[date | tuple[int, int], ...]:
    periods: list[date | tuple[int, int]] = []
    for month in _months(start, end):
        month_start, month_end = _period_bounds(month)
        slice_start = max(start, month_start)
        slice_end = min(end, month_end)
        if slice_start == month_start and slice_end == month_end:
            periods.append(month)
        else:
            periods.extend(_days(slice_start, slice_end))
    return tuple(periods)


def _relevant_days(
    period: tuple[int, int],
    start: datetime,
    end: datetime,
    availability: SymbolAvailability | None,
) -> tuple[date, ...]:
    period_start, period_end = _period_bounds(period)
    lower = max(period_start, start)
    upper = min(period_end, end)
    if availability is not None:
        if availability.onboard_time is not None:
            lower = max(lower, availability.onboard_time)
        if availability.delivery_time is not None:
            upper = min(upper, availability.delivery_time)
    if upper <= lower:
        return ()
    return _days(lower, upper)


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


def _period_bounds(
    period: date | tuple[int, int],
) -> tuple[datetime, datetime]:
    if isinstance(period, date):
        start = datetime(period.year, period.month, period.day, tzinfo=UTC)
        return start, start + timedelta(days=1)
    year, month = period
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        return start, datetime(year + 1, 1, 1, tzinfo=UTC)
    return start, datetime(year, month + 1, 1, tzinfo=UTC)
