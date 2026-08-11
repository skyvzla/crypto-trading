from decimal import Decimal
from pathlib import Path

from trading_platform.shared.events import Bar1s, Kline
from trading_platform.backtest import run_spike_sweep_symbol as symbol_runner
from trading_platform.backtest import run_spike_short


def test_symbol_runner_keeps_the_ninety_day_default_read_window():
    args = symbol_runner.parse_run_args([
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-02T00:00:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--total-notional", "1000",
    ])

    assert args.chunk_hours == 24 * 90


def test_v2_resolves_confirmed_defaults_and_seven_day_warmup():
    args = run_spike_short.parse_args([
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-02T00:00:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--total-notional", "1000",
        "--strategy-version", "v2",
    ])

    settings = run_spike_short.resolve_settings(args)

    assert settings.strategy_version == "v2"
    assert settings.prior_high_lookback_minutes == 6 * 60
    assert settings.rise_low_lookback_minutes == 7 * 24 * 60
    assert settings.min_rise_duration_minutes == 24 * 60
    assert settings.entry_tier_mode == "tier3-only"
    assert settings.early_profit_unlock_ratio == Decimal("0.015")
    assert settings.start_ms - settings.load_start_ms == 7 * 24 * 3_600_000


def test_v1_defaults_remain_unchanged():
    args = run_spike_short.parse_args([
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-02T00:00:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--total-notional", "1000",
    ])

    settings = run_spike_short.resolve_settings(args)

    assert settings.strategy_version == "v1"
    assert settings.prior_high_lookback_minutes == 4 * 60
    assert settings.rise_low_lookback_minutes == 0
    assert settings.min_rise_duration_minutes == 0
    assert settings.entry_tier_mode == "three-tier"
    assert settings.early_profit_unlock_ratio is None
    assert settings.start_ms - settings.load_start_ms == 16 * 3_600_000


def test_symbol_runner_reads_market_data_once_for_multiple_parameters(
    tmp_path: Path, monkeypatch
):
    loader_calls = []
    engines = {}
    saved = []
    start_ms = 1_782_864_000_000

    bar = Bar1s(
        symbol="AKEUSDT", timestamp=start_ms, available_time=start_ms + 1_000,
        open=Decimal("1"), high=Decimal("1"), low=Decimal("1"),
        close=Decimal("1"), volume=Decimal("1"), trade_count=1,
        vwap=Decimal("1"),
    )
    early_bar = Bar1s(
        symbol="AKEUSDT", timestamp=start_ms - 20 * 3_600_000,
        available_time=start_ms - 20 * 3_600_000 + 1_000,
        open=Decimal("1"), high=Decimal("1"), low=Decimal("1"),
        close=Decimal("1"), volume=Decimal("1"), trade_count=1,
        vwap=Decimal("1"),
    )
    kline_15m = Kline(
        symbol="AKEUSDT", interval="15m", open_time=start_ms,
        close_time=start_ms + 899_999, available_time=start_ms + 900_000,
        open=Decimal("1"), high=Decimal("1"), low=Decimal("1"),
        close=Decimal("1"), volume=Decimal("1"),
    )

    class FakeLoader:
        def __init__(self, **kwargs):
            loader_calls.append({"init": kwargs})

        def iter_all(self, **kwargs):
            loader_calls.append({"iter_all": kwargs})
            return iter([early_bar, bar, kline_15m])

    class FakeEngine:
        def __init__(self, run_id):
            self.run_id = run_id
            self.events = []

        def process_event(self, event):
            self.events.append(event)

        def finish(self):
            return self.run_id

    def fake_create_engine(args, settings, events):
        run_id = Path(args.output).name
        engine = FakeEngine(run_id)
        engines[run_id] = engine
        return engine

    monkeypatch.setattr(symbol_runner, "BacktestDataLoader", FakeLoader)
    monkeypatch.setattr(symbol_runner, "create_spike_engine", fake_create_engine)
    monkeypatch.setattr(
        symbol_runner,
        "save_backtest_result",
        lambda result, output: saved.append((result, Path(output).name)),
    )

    common = [
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-02T00:00:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--total-notional", "1000",
        "--chunk-hours", "2160",
    ]
    task = {
        "runs": [
            {"run_id": "confirmed", "arguments": [
                *common, "--output", str(tmp_path / "confirmed"),
                "--exit-policy", "confirmed",
            ]},
            {"run_id": "candidate", "arguments": [
                *common, "--output", str(tmp_path / "candidate"),
                "--exit-policy", "candidate-v1", "--warmup-hours", "24",
            ]},
        ]
    }

    assert symbol_runner.run_symbol_task(task) == 0

    assert len([call for call in loader_calls if "init" in call]) == 1
    init_call = next(call["init"] for call in loader_calls if "init" in call)
    assert init_call["start_ms"] == start_ms - 24 * 3_600_000
    iter_call = next(call["iter_all"] for call in loader_calls if "iter_all" in call)
    assert iter_call["chunk_hours"] == 2160
    assert engines["confirmed"].events == [bar]
    assert engines["candidate"].events == [early_bar, bar, kline_15m]
    assert saved == [("confirmed", "confirmed"), ("candidate", "candidate")]
