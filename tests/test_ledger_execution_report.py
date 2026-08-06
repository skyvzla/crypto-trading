from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from trading_platform.ledger.binance_reports import (
    BinanceExecutionReportLedger,
    ExecutionReportError,
    parse_execution_report,
)


def report(**overrides):
    value = {
        "s": "BTCUSDT",
        "c": "client-1",
        "i": 123,
        "S": "SELL",
        "o": "LIMIT",
        "X": "FILLED",
        "x": "TRADE",
        "ps": "SHORT",
        "q": "1.5",
        "p": "100",
        "sp": "0",
        "ap": "99.5",
        "z": "1.5",
        "l": "1.5",
        "L": "99.5",
        "Y": "149.25",
        "n": "0.03",
        "N": "USDT",
        "rp": "0.75",
        "m": True,
        "t": 987,
        "T": 1780000000000,
        "O": 1779999999000,
    }
    value.update(overrides)
    return value


def test_parse_trade_report_requires_explicit_ownership_and_maps_facts():
    parsed = parse_execution_report(
        report(), account_id="account-1", strategy_id="spike_short"
    )

    assert parsed.order.account_id == "account-1"
    assert parsed.order.strategy_id == "spike_short"
    assert parsed.order.status == "FILLED"
    assert parsed.order.filled_quantity == Decimal("1.5")
    assert parsed.trade is not None
    assert parsed.trade.trade_id == "987"
    assert parsed.trade.quote_quantity == Decimal("149.25")
    assert parsed.trade.commission == Decimal("0.03")
    assert parsed.trade.realized_pnl == Decimal("0.75")
    assert parsed.trade.is_maker is True
    assert parsed.trade.exchange_time.tzinfo is not None


def test_parse_non_trade_order_update_does_not_create_trade():
    parsed = parse_execution_report(
        report(X="PARTIALLY_FILLED", x="NEW", t=-1, l="0"),
        account_id="account-1",
        strategy_id="spike_short",
    )
    assert parsed.order.status == "PARTIALLY_FILLED"
    assert parsed.trade is None


@pytest.mark.parametrize(
    "changes",
    [{"X": "UNKNOWN"}, {"S": "HOLD"}, {"i": None}, {"T": "bad"}],
)
def test_parse_rejects_unknown_or_missing_exchange_facts(changes):
    with pytest.raises(ExecutionReportError):
        parse_execution_report(
            report(**changes), account_id="account-1", strategy_id="spike_short"
        )


def test_writer_requires_explicit_ownership_and_writes_atomically():
    with pytest.raises(ValueError):
        BinanceExecutionReportLedger(AsyncMock(), account_id="", strategy_id="s")


@pytest.mark.asyncio
async def test_writer_passes_order_and_trade_as_one_db_operation():
    db = AsyncMock()
    db.apply_execution_report.return_value = (11, 22)
    writer = BinanceExecutionReportLedger(
        db, account_id="account-1", strategy_id="spike_short"
    )

    result = await writer.handle(report())

    assert result == (11, 22)
    db.apply_execution_report.assert_awaited_once()
    order, trade = db.apply_execution_report.await_args.args
    assert order.order_id == "123"
    assert trade.trade_id == "987"
