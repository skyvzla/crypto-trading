from decimal import Decimal

import pytest

from trading_platform.shared.events import Fill, Kline
from trading_platform.strategies.long_breakout import (
    LongBreakoutConfig,
    LongBreakoutPosition,
    LongBreakoutStrategy,
)


def kline(
    number: int,
    close: str,
    *,
    high: str | None = None,
    interval: str = "4h",
    symbol: str = "BTCUSDT",
) -> Kline:
    duration = 4 * 60 * 60 * 1000 if interval == "4h" else 24 * 60 * 60 * 1000
    open_time = number * duration
    return Kline(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=open_time + duration - 1,
        available_time=open_time + duration,
        open=Decimal(close),
        high=Decimal(high or close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal("1"),
    )


def fill(
    side: str,
    price: str,
    quantity: str = "1",
    *,
    symbol: str = "BTCUSDT",
) -> Fill:
    return Fill(
        fill_id=f"fill-{side}-{price}",
        order_id=f"order-{side}-{price}",
        symbol=symbol,
        side=side,
        price=Decimal(price),
        quantity=Decimal(quantity),
        commission=Decimal("0"),
        commission_asset="USDT",
        fill_time=1,
        is_maker=False,
    )


def test_4h_uses_42_completed_bars_and_excludes_current_bar_from_box():
    strategy = LongBreakoutStrategy(symbol="BTCUSDT", timeframe="4h", quantity="2")

    for index in range(42):
        assert strategy.on_kline(kline(index, "100", high="100")) == []

    intents = strategy.on_kline(kline(42, "101", high="150"))

    assert len(intents) == 1
    intent = intents[0]
    assert intent.side == "BUY"
    assert intent.quantity == Decimal("2")
    assert intent.price == Decimal("101")
    assert intent.strategy_id == "long_breakout"
    assert intent.trigger_reason == "7d_box_breakout_4h"
    assert intent.reduce_only is False


def test_1d_defaults_to_seven_bars_and_equal_close_is_not_breakout():
    strategy = LongBreakoutStrategy(symbol="ETHUSDT", timeframe="1d")
    for index in range(7):
        assert strategy.on_kline(kline(index, "100", interval="1d", symbol="ETHUSDT")) == []

    assert strategy.on_kline(
        kline(7, "100", high="200", interval="1d", symbol="ETHUSDT")
    ) == []
    assert strategy.on_kline(
        kline(8, "200.01", high="200.01", interval="1d", symbol="ETHUSDT")
    )


def test_duplicate_kline_does_not_duplicate_entry():
    strategy = LongBreakoutStrategy(symbol="BTCUSDT", timeframe="4h")
    for index in range(42):
        strategy.on_kline(kline(index, "100"))
    current = kline(42, "101")

    first = strategy.on_kline(current)
    second = strategy.on_kline(current)

    assert len(first) == 1
    assert second == []
    assert strategy.pending_entry("BTCUSDT") == first[0].client_order_id


def test_one_symbol_cannot_open_second_position_until_exit_fill():
    strategy = LongBreakoutStrategy(
        symbol="BTCUSDT", timeframe="4h", take_profit_pct="0.02", stop_loss_pct="0.01"
    )
    for index in range(42):
        strategy.on_kline(kline(index, "100"))
    entry = strategy.on_kline(kline(42, "101"))[0]
    strategy.on_fill(fill("BUY", "101"))
    strategy.on_order_terminal(
        "BTCUSDT",
        client_order_id=entry.client_order_id,
        reduce_only=False,
    )

    assert strategy.on_kline(kline(43, "103")) == []
    exit_intents = strategy.on_kline(kline(44, "104"))
    assert len(exit_intents) == 1
    assert exit_intents[0].side == "SELL"
    assert exit_intents[0].reduce_only is True
    assert exit_intents[0].quantity == Decimal("1")

    # Pending exit blocks repeated exits until the worker reports the fill.
    assert strategy.on_kline(kline(45, "105")) == []
    strategy.on_fill(fill("SELL", "104"))
    assert strategy.has_position("BTCUSDT") is False
    assert entry.strategy_id == strategy.strategy_id


def test_stop_loss_and_take_profit_are_configurable():
    strategy = LongBreakoutStrategy(
        symbol="BTCUSDT",
        config=LongBreakoutConfig(
            timeframe="4h", take_profit_pct=None, stop_loss_pct="0.05"
        ),
    )
    strategy.restore_position("BTCUSDT", quantity="3", entry_price="100")

    assert strategy.on_kline(kline(0, "104")) == []
    intents = strategy.on_kline(kline(1, "94"))

    assert len(intents) == 1
    assert intents[0].trigger_reason == "stop_loss"
    assert intents[0].quantity == Decimal("3")
    assert intents[0].reduce_only is True


def test_direct_none_disables_take_profit():
    strategy = LongBreakoutStrategy(
        symbol="BTCUSDT", timeframe="4h", take_profit_pct=None, stop_loss_pct=None
    )
    strategy.restore_position("BTCUSDT", quantity="1", entry_price="100")

    assert strategy.on_kline(kline(0, "200")) == []

    base = LongBreakoutConfig(timeframe="4h", take_profit_pct="0.02")
    overridden = LongBreakoutStrategy(
        symbol="BTCUSDT", config=base, take_profit_pct=None
    )
    assert overridden.config.take_profit_pct is None


def test_restore_position_and_partial_fills_keep_single_long_position():
    strategy = LongBreakoutStrategy(symbol="BTCUSDT")
    restored = strategy.restore_position("BTCUSDT", "2", "100")
    assert restored == LongBreakoutPosition("BTCUSDT", Decimal("2"), Decimal("100"))

    strategy.on_fill(fill("BUY", "110", "1"))
    position = strategy.position("BTCUSDT")
    assert position is not None
    assert position.quantity == Decimal("3")
    assert position.entry_price == Decimal("103.3333333333333333333333333")

    strategy.on_fill(fill("SELL", "110", "1"))
    assert strategy.position("BTCUSDT").quantity == Decimal("2")


def test_partial_entry_fill_blocks_exit_until_entry_order_is_terminal():
    strategy = LongBreakoutStrategy(symbol="BTCUSDT", timeframe="4h")
    strategy.seed_history([kline(index, "100") for index in range(42)])
    entry = strategy.on_kline(kline(42, "101"))[0]

    strategy.on_fill(
        fill("BUY", "101", "0.4"), campaign_id=entry.campaign_id
    )

    assert strategy.pending_entry("BTCUSDT") == entry.client_order_id
    assert strategy.position("BTCUSDT").quantity == Decimal("0.4")
    assert strategy.on_kline(kline(43, "104")) == []

    strategy.on_order_terminal(
        "BTCUSDT",
        client_order_id=entry.client_order_id,
        reduce_only=False,
    )
    exit_intents = strategy.on_kline(kline(44, "104"))

    assert len(exit_intents) == 1
    assert exit_intents[0].side == "SELL"
    assert exit_intents[0].quantity == Decimal("0.4")


def test_wrong_interval_or_unknown_symbol_is_ignored():
    strategy = LongBreakoutStrategy(symbol="BTCUSDT", timeframe="4h")
    assert strategy.on_kline(kline(0, "101", interval="1d")) == []
    assert strategy.on_kline(kline(0, "101", symbol="ETHUSDT")) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeframe": "5m"},
        {"timeframe": "4h", "quantity": "0"},
        {"timeframe": "4h", "stop_loss_pct": "-0.1"},
        {"timeframe": "4h", "lookback_bars": 0},
        {"timeframe": "4h", "lookback_bars": 7},
        {"timeframe": "1d", "lookback_bars": 42},
    ],
)
def test_config_rejects_invalid_rules(kwargs):
    with pytest.raises(ValueError):
        LongBreakoutStrategy(**kwargs)


def test_seed_history_warms_box_without_emitting_and_next_contiguous_breakout_enters():
    strategy = LongBreakoutStrategy(symbol="BTCUSDT", timeframe="4h")
    warmup = [kline(index, "100", high="100") for index in range(42)]

    strategy.seed_history(reversed(warmup))

    assert strategy.box_high("BTCUSDT") == Decimal("100")
    assert strategy.last_close_time("BTCUSDT") == warmup[-1].close_time
    intents = strategy.on_kline(kline(42, "101", high="101"))

    assert len(intents) == 1
    assert intents[0].side == "BUY"
    assert strategy.pending_entry("BTCUSDT") == intents[0].client_order_id


def test_entry_gate_blocks_new_entries_but_does_not_block_position_exit():
    strategy = LongBreakoutStrategy(symbol="BTCUSDT", timeframe="4h")
    warmup = [kline(index, "100", high="100") for index in range(42)]
    strategy.seed_history(warmup)
    strategy.set_entry_enabled(False)

    assert strategy.on_kline(kline(42, "101", high="101")) == []
    assert strategy.pending_entry("BTCUSDT") is None

    strategy.restore_position("BTCUSDT", quantity="1", entry_price="100")
    assert strategy.on_kline(kline(43, "104", high="104"))[0].trigger_reason == (
        "take_profit"
    )


def test_seed_history_and_live_stream_reject_non_contiguous_klines():
    strategy = LongBreakoutStrategy(symbol="BTCUSDT", timeframe="4h")
    with pytest.raises(ValueError, match="non-contiguous warmup"):
        strategy.seed_history([kline(0, "100"), kline(2, "100")])

    strategy.seed_history([kline(index, "100") for index in range(42)])
    with pytest.raises(ValueError, match="non-contiguous K-line"):
        strategy.on_kline(kline(43, "101"))
    assert strategy.last_close_time("BTCUSDT") == kline(41, "100").close_time

    # The missing candle can still be processed later, and is the first valid
    # opportunity to evaluate a breakout after the continuity gap is repaired.
    assert len(strategy.on_kline(kline(42, "101"))) == 1


def test_entry_and_exit_keep_the_same_campaign_id():
    strategy = LongBreakoutStrategy(symbol="BTCUSDT", timeframe="4h")
    strategy.seed_history([kline(index, "100") for index in range(42)])
    entry = strategy.on_kline(kline(42, "101"))[0]
    assert entry.campaign_id == f"long_breakout:BTCUSDT:{kline(42, '101').close_time}"

    strategy.on_fill(fill("BUY", "101"), campaign_id=entry.campaign_id)
    strategy.on_order_terminal(
        "BTCUSDT",
        client_order_id=entry.client_order_id,
        reduce_only=False,
    )
    exit_intent = strategy.on_kline(kline(43, "104"))[0]

    assert exit_intent.side == "SELL"
    assert exit_intent.reduce_only is True
    assert exit_intent.campaign_id == entry.campaign_id


def test_entry_and_exit_client_order_ids_fit_binance_36_character_limit():
    symbol = "ABCDEFGHIJKLMNOPQRST"
    strategy = LongBreakoutStrategy(symbol=symbol, timeframe="4h")
    strategy.seed_history(
        [kline(index, "100", symbol=symbol) for index in range(42)]
    )
    entry = strategy.on_kline(kline(42, "101", symbol=symbol))[0]
    assert len(entry.client_order_id) <= 36

    strategy.on_fill(
        fill("BUY", "101", symbol=symbol), campaign_id=entry.campaign_id
    )
    strategy.on_order_terminal(
        symbol,
        client_order_id=entry.client_order_id,
        reduce_only=False,
    )
    exit_intent = strategy.on_kline(kline(43, "104", symbol=symbol))[0]
    assert len(exit_intent.client_order_id) <= 36
