"""Complete Binance funding-income pagination before Campaign settlement."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading_platform.ledger.income_store import IncomeStore
from trading_platform.shared.binance import BinanceRestClient


class IncomeHistorySyncError(RuntimeError):
    """The requested Binance income window could not be proven complete."""


class FundingIncomeSync:
    """Fetch a bounded income window completely, persist it, then aggregate it."""

    def __init__(
        self,
        client: BinanceRestClient,
        store: IncomeStore,
        *,
        page_size: int = 1000,
        max_pages: int = 100,
    ) -> None:
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self.client = client
        self.store = store
        self.page_size = page_size
        self.max_pages = max_pages

    async def sync_funding_fee_total(
        self,
        *,
        account_id: str,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> Decimal:
        """Return the stored total only after Binance returns a short page."""
        if not account_id.strip():
            raise ValueError("account_id must not be blank")
        if not symbol.strip():
            raise ValueError("symbol must not be blank")
        if (
            start_at.tzinfo is None
            or start_at.utcoffset() is None
            or end_at.tzinfo is None
            or end_at.utcoffset() is None
        ):
            raise ValueError("income window must use timezone-aware datetimes")
        if start_at >= end_at:
            raise ValueError("start_at must be before end_at")
        start_us = _epoch_microseconds(start_at)
        end_us = _epoch_microseconds(end_at)
        start_ms = start_us // 1000
        inclusive_end_ms = (end_us - 1) // 1000
        seen_pages: set[tuple[tuple[str, str], ...]] = set()
        for page in range(1, self.max_pages + 1):
            raw_page = await self.client.get_income_history(
                symbol=symbol,
                income_type="FUNDING_FEE",
                start_time=start_ms,
                end_time=inclusive_end_ms,
                page=page,
                limit=self.page_size,
            )
            if any(not isinstance(row, Mapping) for row in raw_page):
                raise IncomeHistorySyncError("invalid Binance income page")
            fingerprint = tuple(
                sorted(
                    (str(row.get("incomeType")), str(row.get("tranId")))
                    for row in raw_page
                )
            )
            if fingerprint in seen_pages:
                raise IncomeHistorySyncError("Binance income history repeated page")
            seen_pages.add(fingerprint)
            rows: list[Mapping[str, Any]] = [
                row
                for row in raw_page
                if row.get("symbol") == symbol
                and row.get("incomeType") == "FUNDING_FEE"
            ]
            await self.store.upsert_income_history(
                account_id=account_id,
                rows=rows,
            )
            if len(raw_page) < self.page_size:
                return await self.store.funding_fee_total(
                    account_id=account_id,
                    symbol=symbol,
                    start_at=start_at,
                    end_at=end_at,
                )
        raise IncomeHistorySyncError("Binance income history exceeded page limit")


def _epoch_microseconds(value: datetime) -> int:
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
