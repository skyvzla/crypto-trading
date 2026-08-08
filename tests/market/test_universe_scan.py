from unittest.mock import AsyncMock

import pytest

from trading_platform.strategies.universe import (
    DEFAULT_DELISTING_FREEZE_DAYS,
    UNIVERSE_SCAN_INTERVAL_SECONDS,
    UniverseScanLoop,
    classify_exchange_symbols,
    fetch_exchange_symbol_snapshot,
)


@pytest.mark.asyncio
async def test_scan_and_admission_share_five_minute_cadence():
    calls = []
    scan = AsyncMock(side_effect=lambda: calls.append("scan") or {"symbols": []})
    admission = AsyncMock()
    admission.on_universe_scan.side_effect = lambda: calls.append("admission")
    sleep = AsyncMock(side_effect=RuntimeError("stop"))
    loop = UniverseScanLoop(scan, [admission], sleep=sleep)

    with pytest.raises(RuntimeError, match="stop"):
        await loop.run()

    assert calls == ["scan", "admission"]
    sleep.assert_awaited_once_with(300)
    assert UNIVERSE_SCAN_INTERVAL_SECONDS == 300


@pytest.mark.asyncio
async def test_run_once_returns_scanner_result_after_refreshing_admission():
    scan = AsyncMock(return_value={"symbols": ["BTCUSDT"]})
    admission = AsyncMock()
    loop = UniverseScanLoop(scan, [admission])

    assert await loop.run_once() == {"symbols": ["BTCUSDT"]}
    admission.on_universe_scan.assert_awaited_once()


def test_exchange_symbol_snapshot_blocks_every_non_perpetual_trading_contract():
    now_ms = 1_780_000_000_000
    day_ms = 24 * 60 * 60 * 1000
    exchange_info = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "onboardDate": now_ms - day_ms,
                "deliveryDate": now_ms + 365 * day_ms,
            },
            {
                "symbol": "HFTUSDT",
                "contractType": "PERPETUAL",
                "status": "SETTLING",
                "onboardDate": now_ms - day_ms,
                "deliveryDate": now_ms + 10 * day_ms,
            },
            {
                "symbol": "VANRYUSDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "onboardDate": now_ms - day_ms,
                "deliveryDate": now_ms + DEFAULT_DELISTING_FREEZE_DAYS * day_ms,
            },
            {
                "symbol": "BTCUSDT_260925",
                "contractType": "CURRENT_QUARTER",
                "status": "TRADING",
                "onboardDate": now_ms - day_ms,
                "deliveryDate": 1_795_747_200_000,
            },
            {
                "symbol": "FUTUREUSDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "onboardDate": now_ms + day_ms,
                "deliveryDate": now_ms + 365 * day_ms,
            },
        ]
    }

    snapshot = classify_exchange_symbols(
        exchange_info,
        [
            "BTCUSDT",
            "HFTUSDT",
            "VANRYUSDT",
            "BTCUSDT_260925",
            "FUTUREUSDT",
            "MISSINGUSDT",
        ],
        now_ms=now_ms,
        freeze_days=DEFAULT_DELISTING_FREEZE_DAYS,
    )

    assert snapshot.allowed_symbols == frozenset({"BTCUSDT"})
    assert snapshot.blocked_symbols == frozenset(
        {
            "HFTUSDT",
            "VANRYUSDT",
            "BTCUSDT_260925",
            "FUTUREUSDT",
            "MISSINGUSDT",
        }
    )
    assert snapshot.blocked_reasons == {
        "BTCUSDT_260925": "contract_type:CURRENT_QUARTER",
        "FUTUREUSDT": "not_onboarded",
        "HFTUSDT": "status:SETTLING",
        "MISSINGUSDT": "missing_exchange_info",
        "VANRYUSDT": "delivery_within_freeze_window",
    }


@pytest.mark.asyncio
async def test_exchange_symbol_sync_retries_before_returning_snapshot():
    now_ms = 1_780_000_000_000
    fetch = AsyncMock(
        side_effect=[
            RuntimeError("temporary-1"),
            RuntimeError("temporary-2"),
            {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                        "onboardDate": now_ms - 24 * 60 * 60 * 1000,
                        "deliveryDate": now_ms + 365 * 24 * 60 * 60 * 1000,
                    }
                ]
            },
        ]
    )
    sleep = AsyncMock()

    snapshot = await fetch_exchange_symbol_snapshot(
        fetch,
        ["BTCUSDT"],
        now_ms=now_ms,
        attempts=3,
        retry_base_seconds=1,
        sleep=sleep,
    )

    assert snapshot.allowed_symbols == frozenset({"BTCUSDT"})
    assert fetch.await_count == 3
    assert [call.args for call in sleep.await_args_list] == [(1,), (2,)]
