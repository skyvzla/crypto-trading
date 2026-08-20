from decimal import Decimal

import pytest

from trading_platform.backtest import run_spike_short
from trading_platform.backtest.run_spike_short import (
    create_spike_engine,
    parse_args,
    resolve_settings,
)
from trading_platform.strategies.spike.capital_replay import (
    CapitalManagedSpikeStrategy,
)


def arguments(*extra: str):
    return parse_args(
        [
            "--symbol", "BTCUSDT",
            "--start", "2026-01-01T00:00:00+00:00",
            "--end", "2026-01-02T00:00:00+00:00",
            "--total-notional", "20",
            "--duckdb-path", "candles.duckdb",
            *extra,
        ]
    )


def test_single_entry_capital_settings_override_legacy_fixed_notional():
    settings = resolve_settings(
        arguments(
            "--entry-tier-mode", "single-entry",
            "--initial-account-capital", "100",
            "--initial-trading-capital", "50",
            "--profit-reinvest-ratio", "0.5",
            "--minimum-trading-capital", "5",
        )
    )

    assert settings.capital_config is not None
    assert settings.capital_config.initial_account_capital == Decimal("100")
    assert settings.capital_config.initial_trading_capital == Decimal("50")
    assert settings.capital_config.profit_reinvest_ratio == Decimal("0.5")
    assert settings.capital_config.minimum_trading_capital == Decimal("5")


def test_single_entry_requires_both_initial_capital_values():
    with pytest.raises(ValueError, match="initial-account-capital"):
        resolve_settings(
            arguments(
                "--entry-tier-mode", "single-entry",
                "--initial-account-capital", "100",
            )
        )


def test_engine_uses_dynamic_initial_trading_capital(monkeypatch):
    args = arguments(
        "--entry-tier-mode", "single-entry",
        "--initial-account-capital", "100",
        "--initial-trading-capital", "50",
    )
    settings = resolve_settings(args)
    monkeypatch.setattr(run_spike_short, "load_symbol_rules", lambda *_: None)

    engine = create_spike_engine(args, settings, [])

    assert isinstance(engine.strategy, CapitalManagedSpikeStrategy)
    assert engine.strategy.delegate.strategies["BTCUSDT"].total_notional == Decimal("50")
