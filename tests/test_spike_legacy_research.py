from decimal import Decimal
from unittest.mock import Mock

from trading_platform.shared.events import Bar1s, Fill
from trading_platform.strategies.spike_legacy_research import (
    LegacyScriptExitSpikeShortStrategy,
)


def bar(timestamp: int, *, high: str, low: str, close: str) -> Bar1s:
    return Bar1s(
        symbol="AKEUSDT",
        timestamp=timestamp,
        available_time=timestamp + 1_000,
        type_priority=1,
        sequence=timestamp,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
        trade_count=1,
        vwap=Decimal(close),
    )


def strategy_with_position():
    account = Mock()
    account.iter_orders.return_value = [
        Mock(
            strategy_id="spike_short",
            symbol="AKEUSDT",
            status="FILLED",
            client_order_id=f"spike_short_AKEUSDT_1000_tier{i}",
        )
        for i in (1, 2, 3)
    ]
    account.get_position.return_value = Mock(
        side="SHORT",
        quantity=Decimal("10"),
        entry_price=Decimal("100"),
    )
    strategy = LegacyScriptExitSpikeShortStrategy(
        "AKEUSDT", Decimal("1000"), account=account
    )
    strategy._legacy_invalid_price = Decimal("110")
    strategy._legacy_last_entry_fill_time = 1_000
    strategy._campaign_id_for_timing = "campaign-1"
    return strategy


def test_legacy_research_exits_at_two_r_target():
    strategy = strategy_with_position()

    intents = strategy._manage_non_positive_timeout(
        bar(2_000, high="101", low="79", close="80")
    )

    assert len(intents) == 1
    assert intents[0].trigger_reason == "legacy_target_exit"
    assert intents[0].order_type == "MARKET"
    assert intents[0].price == Decimal("80")


def test_legacy_research_timeout_starts_at_last_entry_fill():
    strategy = strategy_with_position()

    assert strategy._manage_non_positive_timeout(
        bar(899_000, high="101", low="99", close="100")
    ) == []
    intents = strategy._manage_non_positive_timeout(
        bar(901_000, high="101", low="99", close="99")
    )

    assert len(intents) == 1
    assert intents[0].trigger_reason == "legacy_timeout_exit"


def test_legacy_research_records_latest_entry_fill_time():
    strategy = strategy_with_position()
    order = Mock(
        strategy_id="spike_short",
        client_order_id="spike_short_AKEUSDT_1000_tier2",
    )
    strategy._account.get_order.return_value = order

    strategy.on_fill(
        Fill(
            fill_id="fill-1",
            order_id="order-1",
            symbol="AKEUSDT",
            side="SELL",
            price=Decimal("100"),
            quantity=Decimal("1"),
            commission=Decimal("0.02"),
            commission_asset="USDT",
            fill_time=5_000,
            is_maker=True,
        )
    )

    assert strategy._legacy_last_entry_fill_time == 5_000


def test_legacy_research_waits_for_all_entry_orders_to_finish():
    strategy = strategy_with_position()
    strategy._account.iter_orders.return_value[1].status = "NEW"

    intents = strategy._manage_non_positive_timeout(
        bar(2_000, high="101", low="79", close="80")
    )

    assert intents == []
