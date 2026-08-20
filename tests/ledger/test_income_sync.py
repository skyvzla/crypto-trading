from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from trading_platform.ledger.income_sync import (
    FundingIncomeSync,
    IncomeHistorySyncError,
)


def _income(tran_id: int, *, symbol: str = "BTCUSDT") -> dict[str, object]:
    return {
        "symbol": symbol,
        "incomeType": "FUNDING_FEE",
        "income": "-0.01",
        "asset": "USDT",
        "time": 1_700_000_000_000,
        "tranId": tran_id,
    }


@pytest.mark.asyncio
async def test_sync_reads_until_short_page_before_returning_stored_total():
    first_page = [_income(1), _income(2)]
    second_page = [_income(3)]
    client = SimpleNamespace(
        get_income_history=AsyncMock(side_effect=[first_page, second_page])
    )
    store = SimpleNamespace(
        upsert_income_history=AsyncMock(side_effect=[2, 1]),
        funding_fee_total=AsyncMock(return_value=Decimal("-0.03")),
    )
    start_at = datetime(2026, 8, 20, 8, tzinfo=UTC)
    end_at = start_at + timedelta(hours=9)
    sync = FundingIncomeSync(client, store, page_size=2, max_pages=10)

    total = await sync.sync_funding_fee_total(
        account_id="spike-testnet",
        symbol="BTCUSDT",
        start_at=start_at,
        end_at=end_at,
    )

    start_ms = int(start_at.timestamp() * 1000)
    end_ms = int(end_at.timestamp() * 1000) - 1
    assert total == Decimal("-0.03")
    assert client.get_income_history.await_args_list == [
        call(
            symbol="BTCUSDT",
            income_type="FUNDING_FEE",
            start_time=start_ms,
            end_time=end_ms,
            page=1,
            limit=2,
        ),
        call(
            symbol="BTCUSDT",
            income_type="FUNDING_FEE",
            start_time=start_ms,
            end_time=end_ms,
            page=2,
            limit=2,
        ),
    ]
    assert store.upsert_income_history.await_args_list == [
        call(account_id="spike-testnet", rows=first_page),
        call(account_id="spike-testnet", rows=second_page),
    ]
    store.funding_fee_total.assert_awaited_once_with(
        account_id="spike-testnet",
        symbol="BTCUSDT",
        start_at=start_at,
        end_at=end_at,
    )


@pytest.mark.asyncio
async def test_sync_persists_only_campaign_symbol_funding_rows():
    matching = _income(1)
    other_symbol = _income(2, symbol="ETHUSDT")
    other_type = {**_income(3), "incomeType": "COMMISSION"}
    client = SimpleNamespace(
        get_income_history=AsyncMock(
            return_value=[matching, other_symbol, other_type]
        )
    )
    store = SimpleNamespace(
        upsert_income_history=AsyncMock(return_value=1),
        funding_fee_total=AsyncMock(return_value=Decimal("-0.01")),
    )
    start_at = datetime(2026, 8, 20, 8, tzinfo=UTC)
    end_at = start_at + timedelta(hours=1)

    total = await FundingIncomeSync(
        client, store, page_size=10
    ).sync_funding_fee_total(
        account_id="spike-testnet",
        symbol="BTCUSDT",
        start_at=start_at,
        end_at=end_at,
    )

    assert total == Decimal("-0.01")
    store.upsert_income_history.assert_awaited_once_with(
        account_id="spike-testnet", rows=[matching]
    )


@pytest.mark.asyncio
async def test_sync_fails_closed_when_binance_repeats_a_full_page():
    repeated = [_income(1), _income(2)]
    client = SimpleNamespace(
        get_income_history=AsyncMock(
            side_effect=[repeated, list(reversed(repeated))]
        )
    )
    store = SimpleNamespace(
        upsert_income_history=AsyncMock(return_value=2),
        funding_fee_total=AsyncMock(),
    )
    start_at = datetime(2026, 8, 20, 8, tzinfo=UTC)

    with pytest.raises(IncomeHistorySyncError, match="repeated page"):
        await FundingIncomeSync(
            client, store, page_size=2, max_pages=10
        ).sync_funding_fee_total(
            account_id="spike-testnet",
            symbol="BTCUSDT",
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
        )

    store.funding_fee_total.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_fails_closed_at_page_limit_without_aggregation():
    client = SimpleNamespace(
        get_income_history=AsyncMock(
            side_effect=[[_income(1), _income(2)], [_income(3), _income(4)]]
        )
    )
    store = SimpleNamespace(
        upsert_income_history=AsyncMock(return_value=2),
        funding_fee_total=AsyncMock(),
    )
    start_at = datetime(2026, 8, 20, 8, tzinfo=UTC)

    with pytest.raises(IncomeHistorySyncError, match="exceeded page limit"):
        await FundingIncomeSync(
            client, store, page_size=2, max_pages=2
        ).sync_funding_fee_total(
            account_id="spike-testnet",
            symbol="BTCUSDT",
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
        )

    store.funding_fee_total.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_does_not_aggregate_after_income_row_parse_failure():
    client = SimpleNamespace(get_income_history=AsyncMock(return_value=[_income(1)]))
    store = SimpleNamespace(
        upsert_income_history=AsyncMock(
            side_effect=ValueError("invalid Binance income row")
        ),
        funding_fee_total=AsyncMock(),
    )
    start_at = datetime(2026, 8, 20, 8, tzinfo=UTC)

    with pytest.raises(ValueError, match="invalid Binance income row"):
        await FundingIncomeSync(client, store).sync_funding_fee_total(
            account_id="spike-testnet",
            symbol="BTCUSDT",
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
        )

    store.funding_fee_total.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_rejects_ambiguous_or_empty_window_before_rest_call():
    client = SimpleNamespace(get_income_history=AsyncMock())
    store = SimpleNamespace()
    sync = FundingIncomeSync(client, store)
    naive = datetime(2026, 8, 20, 8)

    with pytest.raises(ValueError, match="timezone-aware"):
        await sync.sync_funding_fee_total(
            account_id="spike-testnet",
            symbol="BTCUSDT",
            start_at=naive,
            end_at=naive + timedelta(hours=1),
        )

    start_at = datetime(2026, 8, 20, 8, tzinfo=UTC)
    with pytest.raises(ValueError, match="start_at must be before end_at"):
        await sync.sync_funding_fee_total(
            account_id="spike-testnet",
            symbol="BTCUSDT",
            start_at=start_at,
            end_at=start_at,
        )

    client.get_income_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_accepts_empty_first_page_as_complete_window():
    client = SimpleNamespace(get_income_history=AsyncMock(return_value=[]))
    store = SimpleNamespace(
        upsert_income_history=AsyncMock(return_value=0),
        funding_fee_total=AsyncMock(return_value=Decimal("0")),
    )
    start_at = datetime(2026, 8, 20, 8, tzinfo=UTC)

    assert await FundingIncomeSync(client, store).sync_funding_fee_total(
        account_id="spike-testnet",
        symbol="BTCUSDT",
        start_at=start_at,
        end_at=start_at + timedelta(hours=1),
    ) == Decimal("0")

    assert client.get_income_history.await_count == 1
    store.upsert_income_history.assert_awaited_once_with(
        account_id="spike-testnet", rows=[]
    )


@pytest.mark.asyncio
async def test_sync_rejects_non_object_income_page_before_persistence():
    client = SimpleNamespace(get_income_history=AsyncMock(return_value=["bad-row"]))
    store = SimpleNamespace(
        upsert_income_history=AsyncMock(),
        funding_fee_total=AsyncMock(),
    )
    start_at = datetime(2026, 8, 20, 8, tzinfo=UTC)

    with pytest.raises(IncomeHistorySyncError, match="invalid Binance income page"):
        await FundingIncomeSync(client, store).sync_funding_fee_total(
            account_id="spike-testnet",
            symbol="BTCUSDT",
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
        )

    store.upsert_income_history.assert_not_awaited()
    store.funding_fee_total.assert_not_awaited()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"page_size": 0}, "page_size must be between 1 and 1000"),
        ({"page_size": 1001}, "page_size must be between 1 and 1000"),
        ({"max_pages": 0}, "max_pages must be positive"),
    ],
)
def test_sync_rejects_invalid_pagination_bounds(kwargs, message):
    with pytest.raises(ValueError, match=message):
        FundingIncomeSync(SimpleNamespace(), SimpleNamespace(), **kwargs)
