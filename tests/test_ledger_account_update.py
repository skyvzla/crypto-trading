from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from trading_platform.ledger.binance_account_updates import (
    AccountUpdateError,
    BinanceAccountUpdateLedger,
    parse_account_update,
)


def account_update(**position_overrides):
    position = {
        "s": "BTCUSDT",
        "pa": "-1.5",
        "ep": "100.25",
        "bep": "100.20",
        "cr": "0.75",
        "up": "2.5",
        "mt": "isolated",
        "iw": "10",
        "ps": "SHORT",
        "ma": "USDT",
    }
    position.update(position_overrides)
    return {
        "e": "ACCOUNT_UPDATE",
        "E": 1780000000100,
        "T": 1780000000000,
        "a": {
            "m": "ORDER",
            "B": [{"a": "USDT", "wb": "100", "cw": "90", "bc": "0"}],
            "P": [position],
        },
    }


def test_parse_account_update_maps_only_explicit_position_facts():
    parsed = parse_account_update(
        account_update(), account_id="account-1", strategy_id="spike_short"
    )

    assert parsed.reason == "ORDER"
    assert parsed.event_time.tzinfo is not None
    assert parsed.transaction_time.tzinfo is not None
    assert len(parsed.positions) == 1
    position = parsed.positions[0]
    assert position.account_id == "account-1"
    assert position.strategy_id == "spike_short"
    assert position.position_side == "SHORT"
    assert position.quantity == Decimal("-1.5")
    assert position.entry_price == Decimal("100.25")
    assert position.unrealized_pnl == Decimal("2.5")
    assert position.margin_type == "isolated"
    assert position.isolated_margin == Decimal("10")
    assert position.mark_price is None
    assert position.liquidation_price is None
    assert position.leverage is None
    assert position.exchange_time == parsed.transaction_time


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("e",), "ORDER_TRADE_UPDATE"),
        (("a", "m"), "FUTURE_REASON"),
        (("a", "P", 0, "ps"), "UNKNOWN"),
        (("a", "P", 0, "mt"), "portfolio"),
        (("a", "P", 0, "pa"), "NaN"),
        (("T",), "bad"),
    ],
)
def test_parse_rejects_unknown_events_enums_and_invalid_facts(path, value):
    event = account_update()
    target = event
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(AccountUpdateError):
        parse_account_update(event, account_id="account-1", strategy_id="strategy-1")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update({"future": "field"}),
        lambda event: event["a"].update({"future": "field"}),
        lambda event: event["a"]["B"][0].update({"future": "field"}),
        lambda event: event["a"]["P"][0].update({"future": "field"}),
    ],
)
def test_parse_rejects_unknown_fields_at_every_level(mutate):
    event = account_update()
    mutate(event)
    with pytest.raises(AccountUpdateError, match="unknown"):
        parse_account_update(event, account_id="account-1", strategy_id="strategy-1")


def test_parse_rejects_duplicate_position_snapshots_and_missing_ownership():
    event = account_update()
    event["a"]["P"].append(dict(event["a"]["P"][0]))
    with pytest.raises(AccountUpdateError, match="duplicate"):
        parse_account_update(event, account_id="account-1", strategy_id="strategy-1")
    with pytest.raises(AccountUpdateError, match="account_id"):
        parse_account_update(account_update(), account_id="", strategy_id="strategy-1")


@pytest.mark.asyncio
async def test_writer_passes_complete_snapshot_batch_as_one_db_operation():
    db = AsyncMock()
    db.apply_account_update.return_value = [11]
    writer = BinanceAccountUpdateLedger(
        db, account_id="account-1", strategy_id="spike_short"
    )

    result = await writer.handle(account_update())

    assert result == [11]
    db.apply_account_update.assert_awaited_once()
    positions = db.apply_account_update.await_args.args[0]
    assert len(positions) == 1
    assert positions[0].quantity == Decimal("-1.5")
