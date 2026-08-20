from decimal import Decimal
from types import SimpleNamespace

import pytest

from trading_platform.backtest.funding import FundingIncomeEvent
from trading_platform.shared.events import Bar1s, Fill
from trading_platform.strategies.spike.capital import CapitalPolicyConfig
from trading_platform.strategies.spike.capital_replay import CapitalManagedSpikeStrategy


class StrategyStub:
    def __init__(self):
        self.strategies = {
            "BTCUSDT": SimpleNamespace(total_notional=Decimal("500")),
            "ETHUSDT": SimpleNamespace(total_notional=Decimal("500")),
        }
        self.entry_enabled = True
        self.fills = []

    def set_entry_enabled(self, enabled):
        self.entry_enabled = enabled

    def on_bar1s(self, bar):
        return []

    def on_kline(self, kline):
        return []

    def on_fill(self, fill):
        self.fills.append(fill)


def fill(
    side: str,
    price: str,
    quantity: str,
    commission: str,
    *,
    fill_time: int = 1_000,
) -> Fill:
    return Fill(
        fill_id=f"{side}-{price}",
        order_id=f"{side}-{price}",
        symbol="BTCUSDT",
        side=side,
        price=Decimal(price),
        quantity=Decimal(quantity),
        commission=Decimal(commission),
        commission_asset="USDT",
        fill_time=fill_time,
        is_maker=True,
    )


def managed(
    funding_events=(),
) -> tuple[CapitalManagedSpikeStrategy, StrategyStub]:
    delegate = StrategyStub()
    strategy = CapitalManagedSpikeStrategy(
        delegate,
        CapitalPolicyConfig(
            initial_account_capital="1000",
            initial_trading_capital="500",
            profit_reinvest_ratio="0.5",
            minimum_trading_capital="100",
        ),
        funding_events=funding_events,
    )
    return strategy, delegate


def test_closed_campaign_updates_next_order_notional_from_net_pnl():
    strategy, delegate = managed()

    strategy.on_fill(fill("SELL", "100", "5", "1"))
    strategy.on_fill(fill("BUY", "90", "5", "1"))

    assert strategy.capital_state.trading_capital == Decimal("524")
    assert strategy.capital_state.reserve_capital == Decimal("524")
    assert [item.net_pnl for item in strategy.settlements] == [Decimal("48")]
    assert {
        child.total_notional for child in delegate.strategies.values()
    } == {Decimal("524")}


def test_loss_at_minimum_keeps_exit_processing_but_disables_next_entry():
    strategy, delegate = managed()
    strategy.on_fill(fill("SELL", "100", "5", "0"))
    strategy.on_fill(fill("BUY", "180", "5", "0"))

    bar = Bar1s(
        symbol="BTCUSDT",
        timestamp=2_000,
        available_time=3_000,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=Decimal("1"),
        trade_count=1,
        vwap=Decimal("100"),
    )
    assert strategy.on_bar1s(bar) == []

    assert strategy.capital_state.trading_capital == Decimal("100")
    assert strategy.capital_state.reserve_capital == Decimal("500")
    assert delegate.entry_enabled is False


def test_partial_exits_and_funding_settle_once_after_full_close():
    strategy, _ = managed()
    strategy.on_fill(fill("SELL", "100", "5", "1"))
    strategy.add_funding(Decimal("-3"))
    strategy.on_fill(fill("BUY", "90", "2", "0.5"))

    assert strategy.settlements == []
    assert strategy.capital_state.trading_capital == Decimal("500")

    strategy.on_fill(fill("BUY", "80", "3", "0.5"))

    assert len(strategy.settlements) == 1
    assert strategy.settlements[0].net_pnl == Decimal("75.0")
    assert strategy.capital_state.trading_capital == Decimal("537.50")
    assert strategy.capital_state.reserve_capital == Decimal("537.50")


def test_scheduled_funding_uses_symbol_and_inclusive_campaign_boundaries():
    strategy, _ = managed(
        [
            FundingIncomeEvent(1, "BTCUSDT", 999, Decimal("100")),
            FundingIncomeEvent(2, "btcusdt", 1_000, Decimal("-2")),
            FundingIncomeEvent(3, "BTCUSDT", 1_500, Decimal("1")),
            FundingIncomeEvent(4, "BTCUSDT", 2_000, Decimal("-3")),
            FundingIncomeEvent(5, "BTCUSDT", 2_001, Decimal("100")),
            FundingIncomeEvent(6, "ETHUSDT", 1_500, Decimal("100")),
        ]
    )

    strategy.on_fill(fill("SELL", "100", "5", "1", fill_time=1_000))
    strategy.on_fill(fill("BUY", "90", "2", "0.5", fill_time=1_800))
    assert strategy.settlements == []
    strategy.on_fill(fill("BUY", "80", "3", "0.5", fill_time=2_000))

    assert strategy.settlements[0].net_pnl == Decimal("74.0")
    assert strategy.capital_state.trading_capital == Decimal("537.00")


def test_duplicate_funding_fact_is_counted_once():
    duplicate = FundingIncomeEvent(7, "BTCUSDT", 1_500, Decimal("-4"))
    strategy, _ = managed([duplicate, duplicate])

    strategy.on_fill(fill("SELL", "100", "1", "0", fill_time=1_000))
    strategy.on_fill(fill("BUY", "90", "1", "0", fill_time=2_000))

    assert strategy.settlements[0].net_pnl == Decimal("6")


def test_boundary_funding_fact_cannot_be_consumed_by_two_campaigns():
    strategy, _ = managed(
        [FundingIncomeEvent(7, "BTCUSDT", 2_000, Decimal("5"))]
    )
    strategy.on_fill(fill("SELL", "100", "1", "0", fill_time=1_000))
    strategy.on_fill(fill("BUY", "90", "1", "0", fill_time=2_000))
    strategy.on_fill(fill("SELL", "100", "1", "0", fill_time=2_000))
    strategy.on_fill(fill("BUY", "90", "1", "0", fill_time=3_000))

    assert [item.net_pnl for item in strategy.settlements] == [
        Decimal("15"),
        Decimal("10"),
    ]


def test_missing_funding_input_fails_before_delegate_fill_state_changes():
    delegate = StrategyStub()
    strategy = CapitalManagedSpikeStrategy(
        delegate,
        CapitalPolicyConfig(
            initial_account_capital="1000",
            initial_trading_capital="500",
            profit_reinvest_ratio="0.5",
            minimum_trading_capital="100",
        ),
    )

    with pytest.raises(RuntimeError, match="requires a complete funding input"):
        strategy.on_fill(fill("SELL", "100", "1", "0"))

    assert delegate.fills == []


def test_conflicting_funding_fact_is_rejected():
    with pytest.raises(ValueError, match="transaction 7 has conflicting facts"):
        managed(
            [
                FundingIncomeEvent(7, "BTCUSDT", 1_500, Decimal("-4")),
                FundingIncomeEvent(7, "BTCUSDT", 1_500, Decimal("-5")),
            ]
        )
