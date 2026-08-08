"""可交易池扫描节拍与准入刷新编排。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any


UNIVERSE_SCAN_INTERVAL_SECONDS = 5 * 60
EXCHANGE_SYMBOL_SYNC_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_DELISTING_FREEZE_DAYS = 15
MILLISECONDS_PER_DAY = 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class ExchangeSymbolSnapshot:
    allowed_symbols: frozenset[str]
    blocked_symbols: frozenset[str]
    blocked_reasons: dict[str, str]


def classify_exchange_symbols(
    exchange_info: object,
    managed_symbols: Iterable[str],
    *,
    now_ms: int | None = None,
    freeze_days: int = DEFAULT_DELISTING_FREEZE_DAYS,
) -> ExchangeSymbolSnapshot:
    """Apply non-bypassable USD-M perpetual entry rules to a snapshot."""

    if not isinstance(exchange_info, dict) or not isinstance(
        exchange_info.get("symbols"), list
    ):
        raise ValueError("Binance exchangeInfo has incompatible symbol metadata")
    if freeze_days < 0:
        raise ValueError("freeze_days must be non-negative")
    requested = {
        symbol.strip().upper() for symbol in managed_symbols if symbol.strip()
    }
    metadata = {
        str(item.get("symbol", "")).strip().upper(): item
        for item in exchange_info["symbols"]
        if isinstance(item, dict)
    }
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    delivery_cutoff = current_ms + freeze_days * MILLISECONDS_PER_DAY
    allowed: set[str] = set()
    reasons: dict[str, str] = {}
    for symbol in sorted(requested):
        item = metadata.get(symbol)
        if item is None:
            reasons[symbol] = "missing_exchange_info"
            continue
        contract_type = str(item.get("contractType", "")).strip().upper()
        status = str(item.get("status", "")).strip().upper()
        if contract_type != "PERPETUAL":
            reasons[symbol] = f"contract_type:{contract_type or 'MISSING'}"
            continue
        if status != "TRADING":
            reasons[symbol] = f"status:{status or 'MISSING'}"
            continue
        try:
            onboard_ms = int(item["onboardDate"])
            delivery_ms = int(item["deliveryDate"])
        except (KeyError, TypeError, ValueError):
            reasons[symbol] = "invalid_lifecycle_date"
            continue
        if onboard_ms > current_ms:
            reasons[symbol] = "not_onboarded"
            continue
        if delivery_ms <= delivery_cutoff:
            reasons[symbol] = "delivery_within_freeze_window"
            continue
        allowed.add(symbol)
    return ExchangeSymbolSnapshot(
        allowed_symbols=frozenset(allowed),
        blocked_symbols=frozenset(requested - allowed),
        blocked_reasons=reasons,
    )


async def fetch_exchange_symbol_snapshot(
    fetch: Callable[[], Awaitable[dict[str, Any]]],
    managed_symbols: Iterable[str],
    *,
    now_ms: int | None = None,
    freeze_days: int = DEFAULT_DELISTING_FREEZE_DAYS,
    attempts: int = 3,
    retry_base_seconds: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, int, Exception], None] | None = None,
) -> ExchangeSymbolSnapshot:
    """Fetch exchangeInfo with bounded retries, then classify managed symbols."""

    total_attempts = max(1, attempts)
    last_error: Exception | None = None
    for attempt in range(total_attempts):
        try:
            return classify_exchange_symbols(
                await fetch(),
                managed_symbols,
                now_ms=now_ms,
                freeze_days=freeze_days,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            last_error = error
        if attempt + 1 < total_attempts:
            if on_retry is not None:
                assert last_error is not None
                on_retry(attempt + 2, total_attempts, last_error)
            await sleep(max(0.0, retry_base_seconds) * (2**attempt))
    assert last_error is not None
    raise last_error


class UniverseScanLoop:
    """每五分钟执行一次扫描，并在同一节拍刷新 subcategory 准入。"""

    def __init__(
        self,
        scan_once: Callable[[], Awaitable[object]],
        admission_services: Iterable[object] = (),
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.scan_once = scan_once
        self.admission_services = tuple(admission_services)
        self._sleep = sleep

    async def run_once(self) -> object:
        result = await self.scan_once()
        for service in self.admission_services:
            await service.on_universe_scan()
        return result

    async def run(self) -> None:
        while True:
            await self.run_once()
            await self._sleep(UNIVERSE_SCAN_INTERVAL_SECONDS)
