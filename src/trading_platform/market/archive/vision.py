from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import io
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Lock, Semaphore, local
from typing import BinaryIO

import duckdb
import httpx
import pyarrow as pa

from trading_platform.market.feed.aggregator import Bar1sAggregator

from .models import Candle, Candle1s


VISION_PUBLIC_ROOT = "https://data.binance.vision"
VISION_S3_ROOT = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
VISION_ROOT = f"{VISION_S3_ROOT}/data/futures/um"
EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
NATIVE_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
TRANSFER_PROGRESS_INTERVAL_SECONDS = 0.25
_WORKER_CONTEXT = local()


def current_archive_worker_id() -> int:
    """Return the current download worker's process-local sequence number."""

    return int(getattr(_WORKER_CONTEXT, "worker_id", 0))


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
    total_bytes: int = 0
    elapsed_seconds: float = 0.0
    rows: int = 0
    error: str = ""
    worker_id: int = 0
    download_seconds: float = 0.0
    processing_seconds: float = 0.0


@dataclass(frozen=True)
class _TransferProgressContext:
    callback: Callable[[DownloadProgress], None] | None
    current: int
    total: int
    symbol: str
    timeframe: str
    period: str
    worker_id: int


@contextmanager
def track_archive_transfer(
    callback: Callable[[DownloadProgress], None] | None,
    current: int,
    total: int,
    symbol: str,
    timeframe: str,
    period: str,
    *,
    worker_id: int | None = None,
) -> Iterator[None]:
    previous = getattr(_WORKER_CONTEXT, "transfer_progress", None)
    _WORKER_CONTEXT.transfer_progress = _TransferProgressContext(
        callback,
        current,
        total,
        symbol,
        timeframe,
        period,
        current_archive_worker_id() if worker_id is None else worker_id,
    )
    try:
        yield
    finally:
        if previous is None:
            delattr(_WORKER_CONTEXT, "transfer_progress")
        else:
            _WORKER_CONTEXT.transfer_progress = previous


def _notify_transfer(
    phase: str,
    *,
    downloaded_bytes: int = 0,
    total_bytes: int = 0,
    elapsed_seconds: float = 0.0,
) -> None:
    context: _TransferProgressContext | None = getattr(
        _WORKER_CONTEXT, "transfer_progress", None
    )
    if context is None or context.callback is None:
        return
    context.callback(
        DownloadProgress(
            phase=phase,
            current=context.current,
            total=context.total,
            symbol=context.symbol,
            timeframe=context.timeframe,
            period=context.period,
            downloaded_bytes=downloaded_bytes,
            total_bytes=total_bytes,
            elapsed_seconds=elapsed_seconds,
            worker_id=context.worker_id,
        )
    )


class BinanceVisionHTTPFetcher:
    """Bounded downloader that verifies every Binance Vision SHA-256 file."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        attempts: int = 3,
        retry_base_seconds: float = 1.0,
        on_retry: Callable[[str, int, int, Exception], None] | None = None,
        temporary_slots: Semaphore | None = None,
    ) -> None:
        self._client = client
        self._attempts = max(1, attempts)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._on_retry = on_retry
        self._temporary_slots = temporary_slots

    def __call__(self, url: str) -> bytes:
        with self.open_archive(url) as source:
            return source.read()

    @contextmanager
    def open_archive(self, url: str) -> Iterator[BinaryIO]:
        """Stream a verified archive to disk and keep it seekable for ZIP."""

        _notify_transfer("waiting")
        if self._temporary_slots is not None:
            self._temporary_slots.acquire()
        try:
            with tempfile.TemporaryFile(mode="w+b") as source:
                actual = self._download_to(url, source)
                _notify_transfer(
                    "verifying",
                    downloaded_bytes=source.tell(),
                )
                checksum_text = self._get(url + ".CHECKSUM").text
                self._verify_checksum(url, checksum_text, actual)
                source.seek(0)
                yield source
        finally:
            if self._temporary_slots is not None:
                self._temporary_slots.release()

    @staticmethod
    def _verify_checksum(url: str, checksum_text: str, actual: str) -> None:
        parts = checksum_text.strip().split()
        if not parts or not SHA256_PATTERN.fullmatch(parts[0]):
            raise ValueError(f"invalid Binance checksum for {url}")
        if actual != parts[0].lower():
            raise ValueError(f"Binance checksum mismatch for {url}")

    def _download_to(self, url: str, target: BinaryIO) -> str:
        last_error: Exception | None = None
        candidates = _download_urls(url)
        for attempt in range(self._attempts):
            not_found_count = 0
            for candidate in candidates:
                target.seek(0)
                target.truncate()
                digest = hashlib.sha256()
                started = time.monotonic()
                downloaded_bytes = 0
                total_bytes = 0
                last_progress_at = started
                _notify_transfer("connecting")
                try:
                    with self._client.stream(
                        "GET",
                        candidate,
                        headers={
                            "User-Agent": "spike-trading-platform-history/1.0"
                        },
                    ) as response:
                        response.raise_for_status()
                        try:
                            total_bytes = int(
                                response.headers.get("Content-Length", "0")
                            )
                        except ValueError:
                            total_bytes = 0
                        for chunk in response.iter_bytes():
                            target.write(chunk)
                            digest.update(chunk)
                            downloaded_bytes += len(chunk)
                            now = time.monotonic()
                            if (
                                now - last_progress_at
                                >= TRANSFER_PROGRESS_INTERVAL_SECONDS
                            ):
                                _notify_transfer(
                                    "downloading",
                                    downloaded_bytes=downloaded_bytes,
                                    total_bytes=total_bytes,
                                    elapsed_seconds=now - started,
                                )
                                last_progress_at = now
                    _notify_transfer(
                        "downloading",
                        downloaded_bytes=downloaded_bytes,
                        total_bytes=total_bytes,
                        elapsed_seconds=time.monotonic() - started,
                    )
                    target.flush()
                    return digest.hexdigest()
                except httpx.HTTPStatusError as error:
                    if error.response.status_code == 404:
                        not_found_count += 1
                    else:
                        last_error = error
                except httpx.HTTPError as error:
                    last_error = error
            if not_found_count == len(candidates):
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
        candidates = _download_urls(url)
        for attempt in range(self._attempts):
            not_found_count = 0
            for candidate in candidates:
                try:
                    response = self._client.get(
                        candidate,
                        headers={"User-Agent": "spike-trading-platform-history/1.0"},
                    )
                    response.raise_for_status()
                    return response
                except httpx.HTTPStatusError as error:
                    if error.response.status_code == 404:
                        not_found_count += 1
                    else:
                        last_error = error
                except httpx.HTTPError as error:
                    last_error = error
            if not_found_count == len(candidates):
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
    """Retry archives across free proxies, then fall back to direct access."""

    def __init__(
        self,
        fetchers: Sequence[Callable[[str], bytes]],
        *,
        direct_fetcher: Callable[[str], bytes],
        attempts: int = 5,
        labels: Sequence[str] | None = None,
        retry_base_seconds: float = 1.0,
        on_retry: Callable[..., None] | None = None,
        on_route: Callable[..., None] | None = None,
    ) -> None:
        if not fetchers:
            raise ValueError("at least one proxy fetcher is required")
        self._fetchers = tuple(fetchers)
        self._direct_fetcher = direct_fetcher
        self._attempts = max(1, attempts)
        self._labels = tuple(labels or ())
        if self._labels and len(self._labels) != len(self._fetchers):
            raise ValueError("proxy labels must match proxy fetchers")
        if not self._labels:
            self._labels = tuple(
                f"proxy-{index + 1}" for index in range(len(self._fetchers))
            )
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._on_retry = on_retry
        self._on_route = on_route
        self._proxy_lock = Lock()
        self._busy = [False] * len(self._fetchers)
        self._next = 0

    def __call__(self, url: str) -> bytes:
        excluded: set[int] = set()
        started = time.monotonic()
        last_error: Exception | None = None
        last_label = "direct"
        previous_label: str | None = None
        for attempt in range(1, self._attempts + 1):
            force_direct = attempt == self._attempts
            index, fetcher, label = self._acquire(
                excluded, force_direct=force_direct
            )
            try:
                self._notify_route(
                    url,
                    attempt,
                    previous_label,
                    label,
                    force_direct=force_direct,
                )
            except Exception:
                self._release(index)
                raise
            last_label = label
            try:
                return fetcher(url)
            except ArchiveNotFoundError:
                raise
            except Exception as error:
                last_error = error
                previous_label = label
                if index is not None:
                    excluded.add(index)
            finally:
                self._release(index)
            if attempt < self._attempts:
                self._notify_retry(url, attempt + 1, last_error, label, started)
                time.sleep(self._retry_base_seconds * (2 ** (attempt - 1)))
        assert last_error is not None
        raise RuntimeError(
            f"archive fetch failed source={last_label} after "
            f"{self._attempts} attempts: {last_error}"
        ) from last_error

    @contextmanager
    def open_archive(self, url: str) -> Iterator[bytes | BinaryIO]:
        excluded: set[int] = set()
        started = time.monotonic()
        last_error: Exception | None = None
        last_label = "direct"
        previous_label: str | None = None
        for attempt in range(1, self._attempts + 1):
            force_direct = attempt == self._attempts
            index, fetcher, label = self._acquire(
                excluded, force_direct=force_direct
            )
            try:
                self._notify_route(
                    url,
                    attempt,
                    previous_label,
                    label,
                    force_direct=force_direct,
                )
            except Exception:
                self._release(index)
                raise
            last_label = label
            source_context = open_fetched_archive(fetcher, url)
            try:
                source = source_context.__enter__()
            except ArchiveNotFoundError:
                self._release(index)
                raise
            except Exception as error:
                last_error = error
                previous_label = label
                if index is not None:
                    excluded.add(index)
                self._release(index)
                if attempt < self._attempts:
                    self._notify_retry(
                        url, attempt + 1, error, label, started
                    )
                    time.sleep(
                        self._retry_base_seconds * (2 ** (attempt - 1))
                    )
                continue
            self._release(index)
            try:
                yield source
            finally:
                source_context.__exit__(*sys.exc_info())
            return
        assert last_error is not None
        raise RuntimeError(
            f"archive fetch failed source={last_label} after "
            f"{self._attempts} attempts: {last_error}"
        ) from last_error

    def _acquire(
        self, excluded: set[int], *, force_direct: bool = False
    ) -> tuple[int | None, Callable[[str], bytes], str]:
        worker_id = current_archive_worker_id()
        if force_direct or worker_id > len(self._fetchers):
            return None, self._direct_fetcher, "direct"
        while True:
            with self._proxy_lock:
                for offset in range(len(self._fetchers)):
                    index = (self._next + offset) % len(self._fetchers)
                    if index in excluded or self._busy[index]:
                        continue
                    self._busy[index] = True
                    self._next = (index + 1) % len(self._fetchers)
                    return index, self._fetchers[index], self._labels[index]
                return None, self._direct_fetcher, "direct"

    def _release(self, index: int | None) -> None:
        if index is None:
            return
        with self._proxy_lock:
            self._busy[index] = False

    def _notify_retry(
        self,
        url: str,
        next_attempt: int,
        error: Exception,
        label: str,
        started: float,
    ) -> None:
        if self._on_retry is not None:
            self._on_retry(
                url,
                next_attempt,
                self._attempts,
                error,
                proxy=label,
                elapsed_seconds=time.monotonic() - started,
                worker_id=current_archive_worker_id(),
            )

    def _notify_route(
        self,
        url: str,
        attempt: int,
        previous_label: str | None,
        label: str,
        *,
        force_direct: bool,
    ) -> None:
        if self._on_route is None:
            return
        if label == "direct":
            if previous_label == label:
                return
            mode = "fallback"
            reason = "final-attempt" if force_direct else "no-available-proxy"
        elif previous_label is None:
            mode = "proxy"
            reason = None
        elif previous_label != label:
            mode = "switch"
            reason = None
        else:
            return
        self._on_route(
            url,
            attempt,
            self._attempts,
            mode,
            label,
            previous_source=previous_label,
            reason=reason,
            worker_id=current_archive_worker_id(),
        )


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
    """Parse one Binance Vision daily aggTrades ZIP using the live aggregator."""

    normalized_symbol = symbol.strip().upper()
    member = f"{normalized_symbol}-aggTrades-{day}.csv"
    aggregator = Bar1sAggregator(
        window_tolerance_ms=24 * 60 * 60 * 1000,
        auto_finalize=False,
    )
    source = io.BytesIO(content) if isinstance(content, bytes) else content
    with zipfile.ZipFile(source) as archive:
        with archive.open(member) as raw:
            rows = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))
            required = {
                "agg_trade_id",
                "price",
                "quantity",
                "first_trade_id",
                "last_trade_id",
                "transact_time",
                "is_buyer_maker",
            }
            if rows.fieldnames is None or not required.issubset(rows.fieldnames):
                raise ValueError(f"{member} has incompatible columns")
            for row in rows:
                aggregator.add_trade(
                    normalized_symbol,
                    Decimal(row["price"]),
                    Decimal(row["quantity"]),
                    _epoch_millis(int(row["transact_time"])),
                    int(row["agg_trade_id"]),
                    first_trade_id=int(row["first_trade_id"]),
                    last_trade_id=int(row["last_trade_id"]),
                    is_buyer_maker=_parse_bool(row["is_buyer_maker"]),
                )
    return [_candle_from_bar1s(bar) for bar in aggregator.flush_symbol(normalized_symbol)]


def _parse_bool(value: object) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _epoch_millis(value: int) -> int:
    return value // 1_000 if abs(value) >= 100_000_000_000_000 else value


def _candle_from_bar1s(bar) -> Candle1s:
    return Candle1s(
        symbol=bar.symbol,
        timeframe="1s",
        open_time=datetime.fromtimestamp(bar.timestamp / 1000, tz=UTC),
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
        volume=float(bar.volume),
        close_time=datetime.fromtimestamp((bar.timestamp + 1000) / 1000, tz=UTC),
        vwap=float(bar.vwap),
        quote_volume=_optional_float(bar.quote_volume),
        trade_count=bar.trade_count,
        raw_trade_count=bar.raw_trade_count,
        taker_buy_volume=_optional_float(bar.taker_buy_volume),
        taker_sell_volume=_optional_float(bar.taker_sell_volume),
        taker_buy_quote_volume=_optional_float(bar.taker_buy_quote_volume),
        taker_sell_quote_volume=_optional_float(bar.taker_sell_quote_volume),
        taker_buy_trade_count=bar.taker_buy_trade_count,
        taker_sell_trade_count=bar.taker_sell_trade_count,
        taker_buy_agg_trade_count=bar.taker_buy_agg_trade_count,
        taker_sell_agg_trade_count=bar.taker_sell_agg_trade_count,
        max_agg_trade_quantity=_optional_float(bar.max_agg_trade_quantity),
        max_taker_buy_agg_trade_quantity=_optional_float(
            bar.max_taker_buy_agg_trade_quantity
        ),
        max_taker_sell_agg_trade_quantity=_optional_float(
            bar.max_taker_sell_agg_trade_quantity
        ),
        first_aggregate_trade_id=bar.first_aggregate_trade_id,
        last_aggregate_trade_id=bar.last_aggregate_trade_id,
        first_trade_id=bar.first_trade_id,
        last_trade_id=bar.last_trade_id,
    )


def _optional_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def parse_monthly_aggtrade_archive(
    content: bytes | BinaryIO,
    symbol: str,
    month: str,
) -> Iterator[tuple[date, list[Candle]]]:
    """Aggregate one monthly aggTrades ZIP into daily 1s partitions."""

    for partition_day, table in _aggregate_monthly_aggtrade_archive(
        content, symbol, month
    ):
        yield partition_day, _candles_from_arrow(table)


def _aggregate_monthly_aggtrade_archive(
    content: bytes | BinaryIO,
    symbol: str,
    month: str,
) -> Iterator[tuple[date, pa.Table]]:
    normalized_symbol = symbol.strip().upper()
    member = f"{normalized_symbol}-aggTrades-{month}.csv"
    source = io.BytesIO(content) if isinstance(content, bytes) else content

    with tempfile.TemporaryDirectory(
        prefix=f"aggtrades-{os.getpid()}-"
    ) as temporary:
        temporary_path = Path(temporary)
        csv_path = temporary_path / member
        with zipfile.ZipFile(source) as archive:
            with archive.open(member) as raw, csv_path.open("wb") as extracted:
                shutil.copyfileobj(raw, extracted, length=1024 * 1024)

        with csv_path.open(encoding="utf-8", newline="") as extracted:
            fieldnames = next(csv.reader(extracted), None)
        required = {
            "agg_trade_id",
            "price",
            "quantity",
            "first_trade_id",
            "last_trade_id",
            "transact_time",
            "is_buyer_maker",
        }
        if fieldnames is None or not required.issubset(fieldnames):
            raise ValueError(f"{member} has incompatible columns")

        connection = duckdb.connect(
            config={
                "threads": "1",
                "memory_limit": "768MB",
                "preserve_insertion_order": "false",
                "temp_directory": str(temporary_path / "spill"),
            }
        )
        try:
            connection.execute("SET TimeZone = 'UTC'")
            connection.execute("SET enable_progress_bar = false")
            connection.execute(
                """
                CREATE TEMP TABLE monthly_candles AS
                WITH source AS (
                    SELECT
                        agg_trade_id,
                        price,
                        quantity,
                        first_trade_id,
                        last_trade_id,
                        is_buyer_maker,
                        CASE
                            WHEN abs(transact_time) >= 100000000000000
                                THEN transact_time
                            ELSE transact_time * 1000
                        END AS event_micros
                    FROM read_csv(
                        ?,
                        header = true,
                        columns = {
                            'agg_trade_id': 'BIGINT',
                            'price': 'DOUBLE',
                            'quantity': 'DOUBLE',
                            'first_trade_id': 'BIGINT',
                            'last_trade_id': 'BIGINT',
                            'transact_time': 'BIGINT',
                            'is_buyer_maker': 'BOOLEAN'
                        }
                    )
                ), trades AS (
                    SELECT
                        *,
                        event_micros // 1000000 AS second_epoch,
                        struct_pack(
                            event_micros := event_micros,
                            trade_id := agg_trade_id
                        ) AS event_key
                    FROM source
                )
                SELECT
                    second_epoch,
                    arg_min(price, event_key) AS open,
                    max(price) AS high,
                    min(price) AS low,
                    arg_max(price, event_key) AS close,
                    sum(quantity) AS volume,
                    sum(price * quantity) AS quote_volume,
                    sum(price * quantity) / nullif(sum(quantity), 0) AS vwap,
                    count(*)::BIGINT AS trade_count,
                    sum(greatest(last_trade_id - first_trade_id + 1, 0))::BIGINT
                        AS raw_trade_count,
                    sum(CASE WHEN NOT is_buyer_maker THEN quantity ELSE 0 END)
                        AS taker_buy_volume,
                    sum(CASE WHEN is_buyer_maker THEN quantity ELSE 0 END)
                        AS taker_sell_volume,
                    sum(CASE WHEN NOT is_buyer_maker THEN price * quantity ELSE 0 END)
                        AS taker_buy_quote_volume,
                    sum(CASE WHEN is_buyer_maker THEN price * quantity ELSE 0 END)
                        AS taker_sell_quote_volume,
                    sum(CASE WHEN NOT is_buyer_maker
                        THEN greatest(last_trade_id - first_trade_id + 1, 0)
                        ELSE 0 END)::BIGINT AS taker_buy_trade_count,
                    sum(CASE WHEN is_buyer_maker
                        THEN greatest(last_trade_id - first_trade_id + 1, 0)
                        ELSE 0 END)::BIGINT AS taker_sell_trade_count,
                    count(*) FILTER (WHERE NOT is_buyer_maker)::BIGINT
                        AS taker_buy_agg_trade_count,
                    count(*) FILTER (WHERE is_buyer_maker)::BIGINT
                        AS taker_sell_agg_trade_count,
                    max(quantity) AS max_agg_trade_quantity,
                    coalesce(max(quantity) FILTER (WHERE NOT is_buyer_maker), 0)
                        AS max_taker_buy_agg_trade_quantity,
                    coalesce(max(quantity) FILTER (WHERE is_buyer_maker), 0)
                        AS max_taker_sell_agg_trade_quantity,
                    min(agg_trade_id)::BIGINT AS first_aggregate_trade_id,
                    max(agg_trade_id)::BIGINT AS last_aggregate_trade_id,
                    min(first_trade_id)::BIGINT AS first_trade_id,
                    max(last_trade_id)::BIGINT AS last_trade_id
                FROM trades
                GROUP BY second_epoch
                """,
                [str(csv_path)],
            )
            days = connection.execute(
                """
                SELECT DISTINCT CAST(to_timestamp(second_epoch) AS DATE) AS day
                FROM monthly_candles
                ORDER BY day
                """
            ).fetchall()
            for (partition_day,) in days:
                table = connection.execute(
                    """
                    SELECT
                        ?::VARCHAR AS symbol,
                        '1s'::VARCHAR AS timeframe,
                        to_timestamp(second_epoch) AS open_time,
                        open,
                        high,
                        low,
                        close,
                        volume,
                        to_timestamp(second_epoch + 1) AS close_time,
                        vwap,
                        quote_volume,
                        trade_count,
                        raw_trade_count,
                        taker_buy_volume,
                        taker_sell_volume,
                        taker_buy_quote_volume,
                        taker_sell_quote_volume,
                        taker_buy_trade_count,
                        taker_sell_trade_count,
                        taker_buy_agg_trade_count,
                        taker_sell_agg_trade_count,
                        max_agg_trade_quantity,
                        max_taker_buy_agg_trade_quantity,
                        max_taker_sell_agg_trade_quantity,
                        first_aggregate_trade_id,
                        last_aggregate_trade_id,
                        first_trade_id,
                        last_trade_id
                    FROM monthly_candles
                    WHERE CAST(to_timestamp(second_epoch) AS DATE) = ?
                    ORDER BY second_epoch
                    """,
                    [normalized_symbol, partition_day],
                ).arrow().read_all()
                yield partition_day, table
        finally:
            connection.close()


def _candles_from_arrow(table: pa.Table) -> list[Candle]:
    candles: list[Candle] = []
    for row in table.to_pylist():
        common = {
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "open_time": row["open_time"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "close_time": row["close_time"],
        }
        if row["timeframe"] != "1s":
            candles.append(Candle(**common))
            continue
        candles.append(
            Candle1s(
                **common,
                vwap=row.get("vwap"),
                quote_volume=row.get("quote_volume"),
                trade_count=row.get("trade_count"),
                raw_trade_count=row.get("raw_trade_count"),
                taker_buy_volume=row.get("taker_buy_volume"),
                taker_sell_volume=row.get("taker_sell_volume"),
                taker_buy_quote_volume=row.get("taker_buy_quote_volume"),
                taker_sell_quote_volume=row.get("taker_sell_quote_volume"),
                taker_buy_trade_count=row.get("taker_buy_trade_count"),
                taker_sell_trade_count=row.get("taker_sell_trade_count"),
                taker_buy_agg_trade_count=row.get("taker_buy_agg_trade_count"),
                taker_sell_agg_trade_count=row.get("taker_sell_agg_trade_count"),
                max_agg_trade_quantity=row.get("max_agg_trade_quantity"),
                max_taker_buy_agg_trade_quantity=row.get(
                    "max_taker_buy_agg_trade_quantity"
                ),
                max_taker_sell_agg_trade_quantity=row.get(
                    "max_taker_sell_agg_trade_quantity"
                ),
                first_aggregate_trade_id=row.get("first_aggregate_trade_id"),
                last_aggregate_trade_id=row.get("last_aggregate_trade_id"),
                first_trade_id=row.get("first_trade_id"),
                last_trade_id=row.get("last_trade_id"),
            )
        )
    return candles


def parse_kline_archive(
    content: bytes | BinaryIO,
    symbol: str,
    timeframe: str,
    month: str,
) -> list[Candle]:
    """Parse one native Binance Vision daily or monthly kline ZIP."""

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
    on_worker_exit: Callable[[int], None] | None = None,
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
                periods = _kline_periods(start_utc, end_utc)
            for period in periods:
                bounds = (symbol_availability or {}).get(symbol)
                period_start, period_end = _period_bounds(period)
                if bounds is not None and not bounds.intersects(
                    max(period_start, start_utc), min(period_end, end_utc)
                ):
                    continue
                jobs.append((symbol, timeframe, period))

    total = len(jobs)

    def run_process(
        job: tuple[int, tuple[str, str, date | tuple[int, int]]],
        task_started: float,
    ) -> DownloadResult:
        current, (symbol, timeframe, period) = job
        daily_period = isinstance(period, date)
        monthly_seconds = timeframe == "1s" and not daily_period
        if isinstance(period, date):
            label = period.isoformat()
            if timeframe == "1s":
                url = aggtrade_archive_url(symbol, label)
            else:
                url = daily_kline_archive_url(symbol, timeframe, label)
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
                            elapsed_seconds=time.monotonic() - task_started,
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
                            elapsed_seconds=time.monotonic() - task_started,
                            rows=existing_rows,
                        )
                        return DownloadResult(
                            symbol, timeframe, label, existing_rows, skipped=True
                        )
        if storage_check is not None:
            storage_check()
        _notify(on_progress, "downloading", current, total, symbol, timeframe, label)
        started = time.monotonic()
        download_seconds = 0.0
        processing_seconds = 0.0
        try:
            with track_archive_transfer(
                on_progress,
                current,
                total,
                symbol,
                timeframe,
                label,
            ), open_fetched_archive(fetch, url) as content:
                download_seconds = time.monotonic() - started
                _notify(
                    on_progress,
                    "downloaded",
                    current,
                    total,
                    symbol,
                    timeframe,
                    label,
                    downloaded_bytes=_archive_size(content),
                    elapsed_seconds=download_seconds,
                )
                processing_started = time.monotonic()
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
                    upsert_table = getattr(archive, "upsert_table", None)
                    for day, table in _aggregate_monthly_aggtrade_archive(
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
                        if callable(upsert_table):
                            rows += upsert_table(
                                table,
                                symbol=symbol,
                                timeframe=timeframe,
                                year=day.year,
                                month=day.month,
                                day=day.day,
                            )
                        else:
                            rows += archive.upsert(_candles_from_arrow(table))
                else:
                    if isinstance(period, date) and timeframe == "1s":
                        candles = parse_aggtrade_archive(content, symbol, label)
                    else:
                        candles = parse_kline_archive(
                            content, symbol, timeframe, label
                        )
                    # Vision files are immutable day/month partitions. Store the
                    # complete source partition rather than a requested slice.
                    if storage_check is not None:
                        storage_check()
                    if daily_period and timeframe != "1s":
                        rows = archive.upsert(candles, partition_day=period.day)
                    else:
                        rows = archive.upsert(candles)
                processing_seconds = time.monotonic() - processing_started
        except ArchiveNotFoundError:
            if monthly_seconds:
                rows = 0
                found_daily = False
                fallback_started = time.monotonic()
                relevant_days = _relevant_days(
                    period,
                    start_utc,
                    end_utc,
                    (symbol_availability or {}).get(symbol),
                )
                for day in relevant_days:
                    existing_rows = existing_daily_rows.get(day)
                    if existing_rows is not None:
                        rows += existing_rows
                        found_daily = True
                        continue
                    day_label = day.isoformat()
                    day_url = aggtrade_archive_url(symbol, day_label)
                    _notify(
                        on_progress,
                        "downloading",
                        current,
                        total,
                        symbol,
                        timeframe,
                        day_label,
                    )
                    daily_started = time.monotonic()
                    try:
                        with track_archive_transfer(
                            on_progress,
                            current,
                            total,
                            symbol,
                            timeframe,
                            day_label,
                        ), open_fetched_archive(fetch, day_url) as content:
                            download_seconds += time.monotonic() - daily_started
                            _notify(
                                on_progress,
                                "processing",
                                current,
                                total,
                                symbol,
                                timeframe,
                                day_label,
                            )
                            candles = parse_aggtrade_archive(
                                content, symbol, day_label
                            )
                    except ArchiveNotFoundError:
                        continue
                    if storage_check is not None:
                        storage_check()
                    rows += archive.upsert(candles)
                    found_daily = True
                processing_seconds += time.monotonic() - fallback_started
                if found_daily:
                    _notify(
                        on_progress,
                        "stored",
                        current,
                        total,
                        symbol,
                        timeframe,
                        label,
                        elapsed_seconds=time.monotonic() - task_started,
                        download_seconds=download_seconds,
                        processing_seconds=processing_seconds,
                        rows=rows,
                    )
                    return DownloadResult(symbol, timeframe, label, rows)
            _notify(
                on_progress,
                "unavailable",
                current,
                total,
                symbol,
                timeframe,
                label,
                elapsed_seconds=time.monotonic() - task_started,
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
            elapsed_seconds=time.monotonic() - task_started,
            download_seconds=download_seconds,
            processing_seconds=processing_seconds,
            rows=rows,
        )
        return DownloadResult(symbol, timeframe, label, rows)

    def process(
        job: tuple[int, tuple[str, str, date | tuple[int, int]]],
    ) -> DownloadResult:
        task_started = time.monotonic()
        current, (symbol, timeframe, period) = job
        label = (
            period.isoformat()
            if isinstance(period, date)
            else f"{period[0]:04d}-{period[1]:02d}"
        )
        try:
            return run_process(job, task_started)
        except Exception as error:
            _notify(
                on_progress,
                "failed",
                current,
                total,
                symbol,
                timeframe,
                label,
                elapsed_seconds=time.monotonic() - task_started,
                error=f"{type(error).__name__}: {error}",
            )
            raise

    indexed_jobs = tuple(enumerate(jobs, start=1))
    if max_workers == 1:
        previous_worker_id = current_archive_worker_id()
        _WORKER_CONTEXT.worker_id = 1
        try:
            return [process(job) for job in indexed_jobs]
        finally:
            if indexed_jobs and on_worker_exit is not None:
                on_worker_exit(1)
            _WORKER_CONTEXT.worker_id = previous_worker_id

    worker_ids = iter(range(1, max_workers + 1))
    worker_id_lock = Lock()
    started_worker_ids: set[int] = set()

    def initialize_worker() -> None:
        with worker_id_lock:
            worker_id = next(worker_ids)
            started_worker_ids.add(worker_id)
            _WORKER_CONTEXT.worker_id = worker_id

    try:
        with ThreadPoolExecutor(
            max_workers=max_workers,
            initializer=initialize_worker,
            thread_name_prefix="archive-worker",
        ) as executor:
            results = list(executor.map(process, indexed_jobs))
    finally:
        if on_worker_exit is not None:
            for worker_id in sorted(started_worker_ids):
                on_worker_exit(worker_id)
    return results


@contextmanager
def open_fetched_archive(
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
    total_bytes: int = 0,
    elapsed_seconds: float = 0.0,
    rows: int = 0,
    error: str = "",
    download_seconds: float = 0.0,
    processing_seconds: float = 0.0,
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
                worker_id=current_archive_worker_id(),
                downloaded_bytes=downloaded_bytes,
                total_bytes=total_bytes,
                elapsed_seconds=elapsed_seconds,
                rows=rows,
                error=error,
                download_seconds=download_seconds,
                processing_seconds=processing_seconds,
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


def daily_kline_archive_url(symbol: str, timeframe: str, day: str) -> str:
    normalized = symbol.strip().upper()
    filename = f"{normalized}-{timeframe}-{day}.zip"
    return f"{VISION_ROOT}/daily/klines/{normalized}/{timeframe}/{filename}"


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


def _kline_periods(
    start: datetime, end: datetime
) -> tuple[date | tuple[int, int], ...]:
    periods: list[date | tuple[int, int]] = []
    now = _utc_now()
    for month in _months(start, end):
        month_start, month_end = _period_bounds(month)
        if month_end <= now:
            periods.append(month)
            continue
        periods.extend(_days(max(start, month_start), min(end, month_end)))
    return tuple(periods)


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
