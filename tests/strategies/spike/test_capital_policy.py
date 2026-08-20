from decimal import Decimal

import pytest

from trading_platform.strategies.spike.capital import (
    CapitalPolicy,
    CapitalPolicyConfig,
    CapitalPolicyError,
)


def policy() -> CapitalPolicy:
    return CapitalPolicy(
        CapitalPolicyConfig(
            initial_account_capital="1000",
            initial_trading_capital="500",
            profit_reinvest_ratio="0.5",
            minimum_trading_capital="100",
        )
    )


def test_initial_state_uses_account_minus_trading_as_reserve_and_no_leverage():
    strategy = policy()
    state = strategy.initial_state()

    assert state.account_capital == Decimal("1000")
    assert state.trading_capital == Decimal("500")
    assert state.reserve_capital == Decimal("500")
    assert strategy.can_open(state)
    assert strategy.order_notional(state) == Decimal("500")


def test_profit_is_split_by_reinvest_ratio():
    strategy = policy()
    result = strategy.settle(strategy.initial_state(), "100")

    assert result.event_type == "PROFIT_SETTLED"
    assert result.reinvested_profit == Decimal("50.0")
    assert result.state_after.trading_capital == Decimal("550.0")
    assert result.state_after.reserve_capital == Decimal("550.0")
    assert result.state_after.account_capital == Decimal("1100.0")


def test_loss_only_reduces_trading_pool_and_threshold_stops_new_entry():
    strategy = policy()
    result = strategy.settle(strategy.initial_state(), "-400")

    assert result.event_type == "LOSS_SETTLED"
    assert result.state_after.trading_capital == Decimal("100")
    assert result.state_after.reserve_capital == Decimal("500")
    assert result.state_after.account_capital == Decimal("600")
    assert not strategy.can_open(result.state_after)
    assert strategy.order_notional(result.state_after) == Decimal("0")


def test_loss_beyond_trading_pool_is_explicitly_consumed_from_reserve():
    strategy = policy()
    result = strategy.settle(strategy.initial_state(), "-600")

    assert result.event_type == "CAPITAL_BREACH"
    assert result.reserve_consumed == Decimal("100")
    assert result.state_after.trading_capital == Decimal("0")
    assert result.state_after.reserve_capital == Decimal("400")
    assert result.state_after.account_capital == Decimal("400")
    assert result.state_after.capital_breached
    assert not strategy.can_open(result.state_after)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"initial_account_capital": "-1", "initial_trading_capital": "0", "profit_reinvest_ratio": "0.5", "minimum_trading_capital": "0"},
        {"initial_account_capital": "100", "initial_trading_capital": "101", "profit_reinvest_ratio": "0.5", "minimum_trading_capital": "0"},
        {"initial_account_capital": "100", "initial_trading_capital": "50", "profit_reinvest_ratio": "1.1", "minimum_trading_capital": "0"},
        {"initial_account_capital": "100", "initial_trading_capital": "50", "profit_reinvest_ratio": "0.5", "minimum_trading_capital": "-1"},
    ],
)
def test_invalid_configuration_fails_closed(kwargs):
    with pytest.raises(CapitalPolicyError):
        CapitalPolicyConfig(**kwargs)
