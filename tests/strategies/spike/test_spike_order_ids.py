import pytest

from trading_platform.strategies.spike.short import (
    build_entry_client_order_id,
    build_exit_client_order_id,
    parse_entry_client_order_id,
)


@pytest.mark.parametrize("symbol", ["AKEUSDT", "1000PEPEUSDT", "ABCDEFGHIJKLMNOPQRST"])
def test_entry_client_order_id_is_binance_safe_and_reversible(symbol):
    value = build_entry_client_order_id(symbol, 1_784_062_273_000, 3)

    assert len(value) <= 36
    assert parse_entry_client_order_id(value, expected_symbol=symbol) == (
        symbol,
        1_784_062_273_000,
    )


def test_existing_long_format_remains_recoverable():
    assert parse_entry_client_order_id(
        "spike_short_AKEUSDT_1784062273000_tier1",
        expected_symbol="AKEUSDT",
    ) == ("AKEUSDT", 1_784_062_273_000)


@pytest.mark.parametrize("reason", ["t", "r"])
def test_exit_client_order_id_is_within_binance_limit(reason):
    value = build_exit_client_order_id(
        "ABCDEFGHIJKLMNOPQRST", 1_784_062_273_000, reason
    )
    assert len(value) <= 36


def test_symbol_longer_than_supported_exchange_identity_fails_closed():
    with pytest.raises(ValueError, match="exceeds Binance"):
        build_entry_client_order_id("ABCDEFGHIJKLMNOPQRSTUVWXYZ", 1_784_062_273_000, 1)
