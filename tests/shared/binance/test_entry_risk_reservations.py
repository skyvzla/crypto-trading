"""Account-wide entry limits include orders awaiting exchange position facts."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.binance import BinanceOrderExecutor
from trading_platform.shared.binance.strategy_account import BinanceStrategyAccount
from trading_platform.shared.events import OrderIntent
from trading_platform.shared.execution_recovery import OrderWAL
from trading_platform.shared.risk import RiskConfig, RiskGuard


def entry(client_id: str, symbol: str, *, market: bool = False) -> OrderIntent:
    return OrderIntent(
        symbol=symbol,
        side="BUY",
        order_type="MARKET" if market else "LIMIT",
        price=Decimal("0") if market else Decimal("100"),
        quantity=Decimal("0.6"),
        client_order_id=client_id,
        campaign_id=f"test:{symbol}:1000",
    )


def executor_for(tmp_path, post_order):
    guard = RiskGuard(
        "account-1",
        RiskConfig(max_position_value_usdt=Decimal("100"), max_symbols=10),
    )
    wal = OrderWAL(tmp_path / "orders.jsonl")
    rest = Mock(post_order=post_order, query_order=AsyncMock())
    executor = BinanceOrderExecutor(rest, wal, account_id="account-1", risk_guard=guard)
    return executor, guard, wal


@pytest.mark.asyncio
@pytest.mark.parametrize("market", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
async def test_pending_entry_prevents_other_symbol_exceeding_account_limit(
    tmp_path, market, unknown
):
    post_order = AsyncMock(
        side_effect=RuntimeError("submit timed out") if unknown else None,
        return_value={"status": "NEW", "orderId": 42, "executedQty": "0"},
    )
    executor, guard, wal = executor_for(tmp_path, post_order)
    first_intent = entry("first", "BTCUSDT", market=market)

    first = await executor.submit(first_intent, reference_price=Decimal("100"))
    repeated = await executor.submit(first_intent, reference_price=Decimal("100"))

    assert repeated.status == first.status == ("SUBMIT_UNKNOWN" if unknown else "NEW")
    assert guard.get_total_position_value() == 0
    with pytest.raises(PermissionError, match="risk guard"):
        await executor.submit(
            entry("second", "ETHUSDT", market=market), reference_price=Decimal("100")
        )
    post_order.assert_awaited_once()
    assert "second" not in wal.recover_latest()

    # Retrying the original id must not consume the remaining 40 USDT twice.
    fits = entry("fits", "ETHUSDT", market=market)
    fits.quantity = Decimal("0.4")
    await executor.submit(fits, reference_price=Decimal("100"))
    assert post_order.await_count == 2
    assert "fits" in wal.recover_latest()


@pytest.mark.asyncio
async def test_entry_reserves_capacity_before_awaiting_exchange_response(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def post_order(**_kwargs):
        started.set()
        await release.wait()
        return {"status": "NEW", "orderId": 42, "executedQty": "0"}

    post = AsyncMock(side_effect=post_order)
    executor, _guard, wal = executor_for(tmp_path, post)
    first = asyncio.create_task(executor.submit(entry("first", "BTCUSDT")))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        with pytest.raises(PermissionError, match="risk guard"):
            await asyncio.wait_for(
                executor.submit(entry("second", "ETHUSDT")), timeout=1
            )
        assert "second" not in wal.recover_latest()
    finally:
        release.set()
        await asyncio.wait_for(first, timeout=1)
    post.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("account_first", [False, True])
async def test_fill_and_position_confirmation_converge_in_either_order(
    tmp_path, account_first
):
    post = AsyncMock(return_value={"status": "NEW", "orderId": 42, "executedQty": "0"})
    executor, guard, wal = executor_for(tmp_path, post)
    account = BinanceStrategyAccount(
        executor.rest_client, wal, account_id="account-1", strategy_id="test",
        risk_guard=guard,
    )
    await executor.submit(entry("first", "BTCUSDT"))
    report = {
        "c": "first", "s": "BTCUSDT", "i": 42, "X": "FILLED", "x": "TRADE",
        "z": "0.6", "l": "0.6", "L": "100", "t": 1, "T": 2000,
    }
    position = {
        "e": "ACCOUNT_UPDATE", "T": 2000,
        "a": {"P": [{"s": "BTCUSDT", "pa": "0.6", "ep": "100", "up": "0", "ps": "BOTH"}]},
    }
    if account_first:
        await account.handle_account_update(position)
    executor.handle_order_trade_update(report)
    account.handle_execution_report(report)
    if not account_first:
        assert not guard.check_can_open("ETHUSDT", Decimal("50"))[0]
        await account.handle_account_update(position)

    assert guard.get_total_position_value() == Decimal("60")
    assert guard.check_can_open("ETHUSDT", Decimal("40"))[0]
    assert not guard.check_can_open("ETHUSDT", Decimal("41"))[0]
