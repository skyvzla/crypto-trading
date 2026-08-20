from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from trading_platform.backtest import run_spike_sweep_symbol, sweep
from trading_platform.backtest.engine import BacktestEngine
from trading_platform.backtest.run_spike_short import (
    load_funding_events,
    parse_args,
    resolve_settings,
)
from trading_platform.shared.config import BacktestConfig
from trading_platform.shared.events import Bar1s, OrderIntent
from trading_platform.strategies.spike.capital_replay import (
    CapitalManagedSpikeStrategy,
)


def _arguments(*extra: str):
    return parse_args(
        [
            "--symbol",
            "BTCUSDT",
            "--start",
            "1970-01-01T00:00:00+00:00",
            "--end",
            "1970-01-01T00:00:03+00:00",
            "--total-notional",
            "20",
            "--duckdb-path",
            "candles.duckdb",
            *extra,
        ]
    )


def _funding_snapshot(path: Path) -> None:
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            """
            CREATE TABLE account_income_events (
                account_id VARCHAR,
                transaction_id BIGINT,
                income_type VARCHAR,
                symbol VARCHAR,
                asset VARCHAR,
                amount DECIMAL(30, 12),
                event_time TIMESTAMPTZ
            );
            CREATE TABLE account_income_coverage (
                account_id VARCHAR,
                income_type VARCHAR,
                symbol VARCHAR,
                start_time TIMESTAMPTZ,
                end_time TIMESTAMPTZ
            );
            INSERT INTO account_income_coverage VALUES (
                'spike', 'FUNDING_FEE', 'BTCUSDT',
                to_timestamp(0), to_timestamp(3)
            );
            INSERT INTO account_income_events VALUES (
                'spike', 42, 'FUNDING_FEE', 'BTCUSDT', 'USDT', -2,
                to_timestamp(1.5)
            )
            """
        )
    finally:
        connection.close()


class ReplayStrategy:
    def __init__(self) -> None:
        self.strategies = {
            "BTCUSDT": SimpleNamespace(total_notional=Decimal("50"))
        }
        self.entry_enabled = True
        self.account = None
        self.fills = []

    def bind_account(self, account) -> None:
        self.account = account

    def set_entry_enabled(self, enabled: bool) -> None:
        self.entry_enabled = enabled

    def set_trading_enabled(self, enabled: bool) -> None:
        self.entry_enabled = enabled

    def on_bar1s(self, bar: Bar1s):
        if bar.available_time == 1_000:
            return [
                OrderIntent(
                    symbol="BTCUSDT",
                    side="SELL",
                    price=Decimal("100"),
                    quantity=Decimal("1"),
                    client_order_id="entry",
                    order_type="MARKET",
                )
            ]
        if bar.available_time == 2_000:
            return [
                OrderIntent(
                    symbol="BTCUSDT",
                    side="BUY",
                    price=Decimal("90"),
                    quantity=Decimal("1"),
                    client_order_id="exit",
                    order_type="MARKET",
                    reduce_only=True,
                )
            ]
        return []

    def on_kline(self, _kline):
        return []

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


def _bar(available_time: int, price: str) -> Bar1s:
    value = Decimal(price)
    return Bar1s(
        symbol="BTCUSDT",
        timestamp=available_time - 1_000,
        available_time=available_time,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1"),
        trade_count=1,
        vwap=value,
    )


def test_cli_funding_snapshot_flows_through_engine_into_capital_settlement(
    tmp_path: Path,
):
    snapshot = tmp_path / "funding.duckdb"
    _funding_snapshot(snapshot)
    args = _arguments(
        "--entry-tier-mode",
        "single-entry",
        "--initial-account-capital",
        "100",
        "--initial-trading-capital",
        "50",
        "--funding-duckdb-path",
        str(snapshot),
        "--funding-account-id",
        "spike",
    )
    settings = resolve_settings(args)
    funding_events = load_funding_events(settings, args.symbol)
    delegate = ReplayStrategy()
    strategy = CapitalManagedSpikeStrategy(
        delegate,
        settings.capital_config,
        funding_events=funding_events,
    )
    engine = BacktestEngine(
        strategy,
        [_bar(1_000, "100"), _bar(2_000, "90")],
        BacktestConfig(
            maker_fee_rate=0,
            taker_fee_rate=0,
            trading_start_ms=0,
        ),
    )

    engine.run()

    assert len(delegate.fills) == 2
    assert strategy.settlements[0].net_pnl == Decimal("8.000000000000")
    assert strategy.capital_state.trading_capital == Decimal("54.0000000000000")
    assert strategy.capital_state.reserve_capital == Decimal("54.0000000000000")


def test_dynamic_capital_rejects_missing_funding_cli_input():
    settings = resolve_settings(
        _arguments(
            "--entry-tier-mode",
            "single-entry",
            "--initial-account-capital",
            "100",
            "--initial-trading-capital",
            "50",
        )
    )

    with pytest.raises(ValueError, match="requires --funding-duckdb-path"):
        load_funding_events(settings, "BTCUSDT")


def test_fixed_capital_backtest_keeps_funding_input_optional():
    settings = resolve_settings(_arguments("--entry-tier-mode", "three-tier"))

    assert load_funding_events(settings, "BTCUSDT") is None


def test_fixed_capital_rejects_misleading_funding_flags(tmp_path: Path):
    settings = resolve_settings(
        _arguments(
            "--entry-tier-mode",
            "three-tier",
            "--funding-duckdb-path",
            str(tmp_path / "funding.duckdb"),
            "--funding-account-id",
            "spike",
        )
    )

    with pytest.raises(ValueError, match="only supported with dynamic capital"):
        load_funding_events(settings, "BTCUSDT")


def test_sweep_arguments_forward_dynamic_capital_funding_input(tmp_path: Path):
    spec = sweep.RunSpec(
        "run-1",
        "BTCUSDT",
        {
            "total_notional": 20,
            "initial_account_capital": 100,
            "initial_trading_capital": 50,
            "profit_reinvest_ratio": 0.5,
            "minimum_trading_capital": 5,
            "entry_tier_mode": "single-entry",
        },
    )
    arguments = sweep._run_arguments(
        spec,
        {
            "start": "1970-01-01T00:00:00+00:00",
            "end": "1970-01-01T00:00:03+00:00",
            "duckdb_path": "candles.duckdb",
            "funding_duckdb_path": tmp_path / "funding.duckdb",
            "funding_account_id": "spike",
        },
        tmp_path / "run",
    )

    assert arguments[arguments.index("--funding-duckdb-path") + 1] == str(
        tmp_path / "funding.duckdb"
    )
    assert arguments[arguments.index("--funding-account-id") + 1] == "spike"


def test_symbol_fanout_loads_identical_funding_snapshot_once(monkeypatch):
    argument_values = [
        "--symbol",
        "BTCUSDT",
        "--start",
        "1970-01-01T00:00:00+00:00",
        "--end",
        "1970-01-01T00:00:03+00:00",
        "--total-notional",
        "20",
        "--duckdb-path",
        "candles.duckdb",
        "--entry-tier-mode",
        "single-entry",
        "--initial-account-capital",
        "100",
        "--initial-trading-capital",
        "50",
        "--funding-duckdb-path",
        "funding.duckdb",
        "--funding-account-id",
        "spike",
    ]
    load_calls = []
    engine_calls = []

    def fake_load(settings, symbol):
        load_calls.append((settings.funding_duckdb_path, symbol))
        return []

    def fake_engine(_args, _settings, events, **kwargs):
        assert events == ()
        engine_calls.append(kwargs)
        return object()

    monkeypatch.setattr(run_spike_sweep_symbol, "load_funding_events", fake_load)
    monkeypatch.setattr(run_spike_sweep_symbol, "create_spike_engine", fake_engine)
    monkeypatch.setattr(run_spike_sweep_symbol, "_run_shift_group", lambda _plans: set())

    result = run_spike_sweep_symbol.run_symbol_task(
        {
            "runs": [
                {"run_id": "one", "arguments": argument_values},
                {"run_id": "two", "arguments": argument_values},
            ]
        }
    )

    assert result == 0
    assert load_calls == [(Path("funding.duckdb"), "BTCUSDT")]
    assert [call["preloaded_funding_events"] for call in engine_calls] == [[], []]
