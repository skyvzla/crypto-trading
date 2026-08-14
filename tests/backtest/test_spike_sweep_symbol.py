from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from trading_platform.backtest.strategy_definition import FeatureSpec
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

    assert args.chunk_hours == 24 * 180


def test_v2_resolves_confirmed_defaults_and_seven_day_warmup():
    args = run_spike_short.parse_args([
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-02T00:00:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--total-notional", "1000",
        "--strategy", "trading_platform.strategies.spike.v2:V2",
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


def test_v11_uses_existing_candidate_exit_policy_by_default():
    args = run_spike_short.parse_args([
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-02T00:00:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--metrics-root", "metrics",
        "--total-notional", "1000",
        "--strategy", "trading_platform.strategies.spike.v1_1:V11",
    ])

    settings = run_spike_short.resolve_settings(args)

    assert settings.strategy_version == "v1.1"
    assert args.exit_policy == "candidate-v1"
    assert settings.required_kline_intervals == ("1m", "5m", "15m")


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


def test_shared_feature_providers_are_opt_in_and_grouped_by_replay_context():
    created = []

    class FakeProvider:
        def __init__(self, *, shared_features):
            self.shared_features = shared_features
            self.consumers = []
            created.append(self)

        def bind(self, consumer):
            self.consumers.append(consumer)

    feature = FeatureSpec(name="rise_5s", timeframe="1s")
    definition = SimpleNamespace(
        shared_feature_provider=FakeProvider,
        data_requirements=SimpleNamespace(shared_features=frozenset({feature})),
    )

    def plan(run_id, load_start_ms, *, feature_definition=definition):
        settings = SimpleNamespace(
            strategy_definition=feature_definition,
            load_start_ms=load_start_ms,
            required_kline_intervals=("1m",),
            requires_bar1s=True,
        )
        engine = SimpleNamespace(strategy=object())
        return symbol_runner.SymbolRunPlan(
            run_id=run_id,
            args=SimpleNamespace(),
            settings=settings,
            engine=engine,
        )

    no_features = SimpleNamespace(
        data_requirements=SimpleNamespace(shared_features=frozenset())
    )
    plans = [
        plan("five-percent", 1_000),
        plan("ten-percent", 1_000),
        plan("different-warmup", 0),
        plan("unshared", 1_000, feature_definition=no_features),
    ]

    groups = symbol_runner._build_shared_provider_groups(plans)

    assert len(groups) == 2
    assert len(created) == 2
    assert sorted(len(provider.consumers) for provider in created) == [1, 2]
    assert all(provider.shared_features == frozenset({feature}) for provider in created)


def test_v11_shared_features_keep_threshold_decisions_per_strategy(
    tmp_path: Path, monkeypatch
):
    signal_time = 1_782_864_000_000
    minute = 60_000
    history_start = signal_time - 16 * 60 * minute
    events = [
        Kline(
            symbol="AKEUSDT",
            interval="1m",
            open_time=history_start + index * minute,
            close_time=history_start + (index + 1) * minute - 1,
            available_time=history_start + (index + 1) * minute,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("80"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for index in range(16 * 60)
    ]
    for interval, duration, count in (
        ("5m", 5 * minute, 15),
        ("15m", 15 * minute, 16),
    ):
        events.extend(
            Kline(
                symbol="AKEUSDT",
                interval=interval,
                open_time=signal_time - (count - index) * duration,
                close_time=signal_time - (count - index - 1) * duration - 1,
                available_time=signal_time - (count - index - 1) * duration,
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("98"),
                close=Decimal("100"),
                volume=Decimal("10"),
            )
            for index in range(count)
        )

    bar_start = signal_time - minute
    closes = [Decimal("100")] * 56 + [
        Decimal("100"), Decimal("101"), Decimal("102"),
        Decimal("104"), Decimal("106"),
    ]
    events.extend(
        Bar1s(
            symbol="AKEUSDT",
            timestamp=bar_start + index * 1_000,
            available_time=bar_start + (index + 1) * 1_000,
            open=close,
            high=Decimal("120") if index == 60 else close,
            low=close,
            close=close,
            volume=Decimal("4") if index >= 56 else Decimal("1"),
            trade_count=1,
            vwap=close,
        )
        for index, close in enumerate(closes)
    )
    events.append(
        Bar1s(
            symbol="AKEUSDT",
            timestamp=signal_time + 1_000,
            available_time=signal_time + 2_000,
            open=Decimal("106"),
            high=Decimal("106"),
            low=Decimal("106"),
            close=Decimal("106"),
            volume=Decimal("1"),
            trade_count=1,
            vwap=Decimal("106"),
        )
    )
    events.sort(
        key=lambda event: (
            event.available_time,
            0 if isinstance(event, Kline) else 1,
        )
    )

    class FakeLoader:
        def __init__(self, **kwargs):
            pass

        def iter_all(self, **kwargs):
            return iter(events)

    results = {}
    metrics_loads = []

    def fake_load_metrics(metrics_root, symbol):
        metrics_loads.append((Path(metrics_root), symbol))
        return [(history_start, 1.0, 1.0)]

    monkeypatch.setattr(symbol_runner, "BacktestDataLoader", FakeLoader)
    monkeypatch.setattr(
        symbol_runner,
        "load_metrics_series",
        fake_load_metrics,
    )
    monkeypatch.setattr(
        symbol_runner,
        "save_backtest_result",
        lambda result, output: results.setdefault(Path(output).name, result),
    )

    common = [
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-01T00:01:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--metrics-root", str(tmp_path / "metrics"),
        "--total-notional", "1000",
        "--strategy", "trading_platform.strategies.spike.v1_1:V11",
    ]
    task = {
        "runs": [
            {
                "run_id": "five-percent",
                "arguments": [
                    *common,
                    "--output", str(tmp_path / "five-percent"),
                    "--rise-5s-threshold-percent", "5",
                ],
            },
            {
                "run_id": "ten-percent",
                "arguments": [
                    *common,
                    "--output", str(tmp_path / "ten-percent"),
                    "--rise-5s-threshold-percent", "10",
                ],
            },
        ]
    }

    assert symbol_runner.run_symbol_task(task) == 0

    assert metrics_loads == [(tmp_path / "metrics", "AKEUSDT")]
    five_percent = results["five-percent"]
    ten_percent = results["ten-percent"]
    assert len(five_percent.orders) == 3
    assert any(
        event.event_type == "signal_triggered"
        for event in five_percent.audit_events
    )
    assert ten_percent.orders == []
    assert not any(
        event.event_type == "signal_triggered"
        for event in ten_percent.audit_events
    )
