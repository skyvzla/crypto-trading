from unittest.mock import AsyncMock

import pytest

from trading_platform.strategies.universe import (
    UNIVERSE_SCAN_INTERVAL_SECONDS,
    UniverseScanLoop,
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
