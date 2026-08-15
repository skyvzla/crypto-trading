import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_platform.backtest.strategy_definition import FeatureSpec
from trading_platform.shared.events import Bar1s, Kline
from trading_platform.backtest import run_spike_sweep_symbol as symbol_runner
from trading_platform.backtest import run_spike_short
from trading_platform.backtest.sweep import RunSpec, _collect_signal_audit_events
from trading_platform.strategies.spike.short import DynamicSpikeShortStrategy


def test_symbol_runner_keeps_the_one_hundred_eighty_day_default_read_window():
    args = symbol_runner.parse_run_args([
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-02T00:00:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--total-notional", "1000",
    ])

    assert args.chunk_hours == 4320


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


def test_v11_rejects_volume_cap_below_the_existing_lower_threshold():
    args = run_spike_short.parse_args([
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-02T00:00:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--metrics-root", "metrics",
        "--total-notional", "1000",
        "--strategy", "trading_platform.strategies.spike.v1_1:V11",
        "--max-volume-multiple-5s", "2.9",
    ])

    with pytest.raises(ValueError, match="lower volume threshold"):
        run_spike_short.resolve_settings(args)


def test_v11_allows_volume_cap_at_the_existing_lower_threshold():
    args = run_spike_short.parse_args([
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-02T00:00:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--metrics-root", "metrics",
        "--total-notional", "1000",
        "--strategy", "trading_platform.strategies.spike.v1_1:V11",
        "--max-volume-multiple-5s", "3",
    ])

    settings = run_spike_short.resolve_settings(args)

    assert settings.max_volume_multiple_5s == Decimal("3")


def test_v21_allows_native_five_minute_top_maturity_parameters():
    args = run_spike_short.parse_args([
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-02T00:00:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--metrics-root", "metrics",
        "--total-notional", "1000",
        "--strategy", "trading_platform.strategies.spike.v2_1:V21",
        "--min-td-sell-setup-5m", "4",
        "--min-volume-multiple-5m", "10",
    ])

    settings = run_spike_short.resolve_settings(args)

    assert settings.min_td_sell_setup_5m == 4
    assert settings.min_volume_multiple_5m == Decimal("10")


def test_v11_rejects_caps_with_legacy_script_exit():
    args = run_spike_short.parse_args([
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-02T00:00:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--metrics-root", "metrics",
        "--total-notional", "1000",
        "--strategy", "trading_platform.strategies.spike.v1_1:V11",
        "--exit-policy", "legacy-script",
        "--max-rise-5s-percent", "7.5",
    ])

    with pytest.raises(ValueError, match="legacy-script"):
        run_spike_short.resolve_settings(args)


def test_cap_rejection_audit_cools_down_same_reason_but_records_changed_reason():
    strategy = DynamicSpikeShortStrategy(
        symbol="AKEUSDT",
        total_notional=Decimal("1000"),
        max_rise_5s_percent=Decimal("5.5"),
        max_volume_multiple_5s=Decimal("3.5"),
    )
    start_ms = 1_782_864_000_000
    details = {
        "trigger_price": Decimal("106"),
        "rise_5s": Decimal("0.06"),
        "volume_5s": Decimal("20"),
        "median_volume_1s": Decimal("1"),
        "volume_multiple_5s": Decimal("4"),
        "low_12h": Decimal("80"),
        "rise_from_12h_low": Decimal("0.325"),
    }
    same_reasons = ("max_rise_5s", "max_volume_multiple_5s")

    strategy._record_cap_rejection(
        event_time=start_ms,
        rejection_reasons=same_reasons,
        **details,
    )
    strategy._record_cap_rejection(
        event_time=start_ms + 1_000,
        rejection_reasons=same_reasons,
        **details,
    )
    strategy._record_cap_rejection(
        event_time=start_ms + 2_000,
        rejection_reasons=same_reasons,
        **details,
    )
    strategy._record_cap_rejection(
        event_time=start_ms + 3_000,
        rejection_reasons=("max_rise_5s",),
        **details,
    )

    rejections = [
        event
        for event in strategy.drain_audit_events()
        if event.event_type == "signal_rejected"
    ]

    assert [event.event_time for event in rejections] == [
        start_ms,
        start_ms + 3_000,
    ]
    assert [event.details["rejection_reasons"] for event in rejections] == [
        ["max_rise_5s", "max_volume_multiple_5s"],
        ["max_rise_5s"],
    ]
    assert rejections[1].event_time - rejections[0].event_time < (
        strategy.SIGNAL_COOLDOWN * 1_000
    )


def test_entry_filter_rejection_audit_cools_down_same_reason_but_records_changed_reason():
    strategy = DynamicSpikeShortStrategy(
        symbol="AKEUSDT",
        total_notional=Decimal("1000"),
    )
    start_ms = 1_000_000
    same_details = {
        "rejection_stage": "metrics_entry_filters",
        "rejection_reasons": ["max_ls_ratio"],
    }

    strategy._record_entry_filter_rejection(
        event_time=start_ms,
        details=same_details,
    )
    strategy._record_entry_filter_rejection(
        event_time=start_ms + 1_000,
        details=same_details,
    )
    strategy._record_entry_filter_rejection(
        event_time=start_ms + 2_000,
        details=same_details,
    )
    strategy._record_entry_filter_rejection(
        event_time=start_ms + 3_000,
        details={
            "rejection_stage": "metrics_entry_filters",
            "rejection_reasons": ["max_oi_change_pct"],
        },
    )

    rejections = [
        event
        for event in strategy.drain_audit_events()
        if event.event_type == "signal_rejected"
    ]

    assert [event.event_time for event in rejections] == [
        start_ms,
        start_ms + 3_000,
    ]
    assert [event.details["rejection_reasons"] for event in rejections] == [
        ["max_ls_ratio"],
        ["max_oi_change_pct"],
    ]
    assert rejections[1].event_time - rejections[0].event_time < (
        strategy.SIGNAL_COOLDOWN * 1_000
    )


def test_entry_filter_rejection_is_not_recorded_before_full_base_signal_checks(
    monkeypatch,
):
    minute = 60_000
    current_time = 720 * minute + 30_000
    minute_start = current_time - (current_time % minute)
    strategy = DynamicSpikeShortStrategy(
        symbol="AKEUSDT",
        total_notional=Decimal("1000"),
        rise_low_lookback_minutes=60,
        min_rise_duration_minutes=30,
    )
    strategy.klines_1m.extend(
        Kline(
            symbol="AKEUSDT",
            interval="1m",
            open_time=minute_start - (720 - index) * minute,
            close_time=minute_start - (719 - index) * minute - 1,
            available_time=minute_start - (719 - index) * minute,
            open=Decimal("1"),
            high=Decimal("2"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("1"),
        )
        for index in range(720)
    )
    strategy.bars_1s.extend(
        Bar1s(
            symbol="AKEUSDT",
            timestamp=current_time - (60 - index) * 1_000,
            available_time=current_time - (59 - index) * 1_000,
            open=Decimal("2"),
            high=Decimal("2"),
            low=Decimal("2"),
            close=Decimal("2") if index >= 56 else Decimal("1"),
            volume=Decimal("4") if index >= 56 else Decimal("1"),
            trade_count=1,
            vwap=Decimal("2"),
        )
        for index in range(61)
    )
    decisions = []

    def reject_with_metrics(timestamp: int):
        decisions.append(timestamp)
        return False, {
            "rejection_stage": "metrics_entry_filters",
            "rejection_reasons": ["max_ls_ratio"],
        }

    monkeypatch.setattr(strategy, "_entry_filter_decision", reject_with_metrics)

    assert strategy._detect_signal(strategy.bars_1s[-1]) is None
    assert decisions == []
    assert strategy.drain_audit_events() == []


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
        "--chunk-hours", "720",
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
    assert iter_call["chunk_hours"] == 720
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
        Decimal("104"), Decimal("106"), Decimal("107"), Decimal("108"),
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
            {
                "run_id": "six-percent",
                "arguments": [
                    *common,
                    "--output", str(tmp_path / "six-percent"),
                    "--rise-5s-threshold-percent", "6",
                ],
            },
            {
                "run_id": "rise-capped",
                "arguments": [
                    *common,
                    "--output", str(tmp_path / "rise-capped"),
                    "--max-rise-5s-percent", "5.5",
                ],
            },
            {
                "run_id": "volume-capped",
                "arguments": [
                    *common,
                    "--output", str(tmp_path / "volume-capped"),
                    "--max-volume-multiple-5s", "3.5",
                ],
            },
            {
                "run_id": "combined-capped",
                "arguments": [
                    *common,
                    "--output", str(tmp_path / "combined-capped"),
                    "--max-rise-5s-percent", "5.5",
                    "--max-volume-multiple-5s", "3.5",
                ],
            },
        ]
    }

    assert symbol_runner.run_symbol_task(task) == 0

    assert metrics_loads == [(tmp_path / "metrics", "AKEUSDT")]
    five_percent = results["five-percent"]
    ten_percent = results["ten-percent"]
    six_percent = results["six-percent"]
    rise_capped = results["rise-capped"]
    volume_capped = results["volume-capped"]
    combined_capped = results["combined-capped"]
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
    assert len(six_percent.orders) == 3
    six_percent_signal = next(
        event for event in six_percent.audit_events
        if event.event_type == "signal_triggered"
    )
    assert six_percent_signal.details["rise_threshold_5s"] == "0.06"
    assert rise_capped.orders == []
    rise_rejection = next(
        event for event in rise_capped.audit_events
        if event.event_type == "signal_rejected"
    )
    assert rise_rejection.details["rejection_reasons"] == ["max_rise_5s"]
    assert rise_rejection.details["rise_5s"] == "0.06"
    assert rise_rejection.details["max_rise_5s"] == "0.055"
    assert not any(
        event.event_type in {"signal_triggered", "entry_plan_created"}
        for event in rise_capped.audit_events
    )
    assert volume_capped.orders == []
    volume_rejection = next(
        event for event in volume_capped.audit_events
        if event.event_type == "signal_rejected"
    )
    assert volume_rejection.details["rejection_reasons"] == [
        "max_volume_multiple_5s"
    ]
    assert volume_rejection.details["volume_multiple_5s"] == "4"
    assert volume_rejection.details["max_volume_multiple_5s"] == "3.5"
    assert combined_capped.orders == []
    combined_rejection = next(
        event for event in combined_capped.audit_events
        if event.event_type == "signal_rejected"
    )
    assert combined_rejection.details["rejection_reasons"] == [
        "max_rise_5s", "max_volume_multiple_5s"
    ]


@pytest.mark.parametrize(
    ("strategy_path", "strategy_arguments"),
    [
        ("trading_platform.strategies.spike.v1_1:V11", []),
        (
            "trading_platform.strategies.spike.v2_1:V21",
            [
                "--rise-low-lookback-hours", "0",
                "--min-rise-duration-hours", "0",
            ],
        ),
    ],
)
def test_metrics_filter_rejections_reach_all_signals_from_symbol_replay(
    tmp_path: Path,
    monkeypatch,
    strategy_path: str,
    strategy_arguments: list[str],
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
    events.extend(
        Kline(
            symbol="AKEUSDT",
            interval="5m",
            open_time=signal_time - (15 - index) * 5 * minute,
            close_time=signal_time - (14 - index) * 5 * minute - 1,
            available_time=signal_time - (14 - index) * 5 * minute,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("98"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for index in range(15)
    )
    closes = [Decimal("100")] * 56 + [
        Decimal("100"), Decimal("101"), Decimal("102"),
        Decimal("104"), Decimal("106"),
    ]
    events.extend(
        Bar1s(
            symbol="AKEUSDT",
            timestamp=signal_time - minute + index * 1_000,
            available_time=signal_time - minute + (index + 1) * 1_000,
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

    def fake_load_metrics(metrics_root, symbol):
        assert Path(metrics_root) == tmp_path / "metrics"
        assert symbol == "AKEUSDT"
        return [
            (history_start, 100.0, 1.0),
            (signal_time, 130.0, 1.8),
            (signal_time + 1, 999.0, 9.0),
        ]

    monkeypatch.setattr(symbol_runner, "BacktestDataLoader", FakeLoader)
    monkeypatch.setattr(symbol_runner, "load_metrics_series", fake_load_metrics)

    output_root = tmp_path / "report"
    common = [
        "--symbol", "AKEUSDT",
        "--start", "2026-07-01T00:00:00+00:00",
        "--end", "2026-07-01T00:01:00+00:00",
        "--duckdb-path", "history.duckdb",
        "--metrics-root", str(tmp_path / "metrics"),
        "--total-notional", "1000",
        "--strategy", strategy_path,
        *strategy_arguments,
    ]
    filters = {
        "base": [],
        "oi": ["--max-oi-change-pct", "15"],
        "ls": ["--max-ls-ratio", "1.5"],
        "both": ["--max-oi-change-pct", "15", "--max-ls-ratio", "1.5"],
    }
    task = {
        "runs": [
            {
                "run_id": run_id,
                "arguments": [
                    *common,
                    "--output", str(output_root / "runs" / run_id),
                    *filter_arguments,
                ],
            }
            for run_id, filter_arguments in filters.items()
        ]
    }

    assert symbol_runner.run_symbol_task(task) == 0

    signals = _collect_signal_audit_events(
        output_root,
        [
            RunSpec(run_id=run_id, symbol="AKEUSDT", params={})
            for run_id in filters
        ],
    )
    assert len(signals) == len(filters)
    event_types_by_run = {
        row.run_id: row.event_type
        for row in signals.itertuples(index=False)
    }
    assert event_types_by_run == {
        "base": "signal_triggered",
        "oi": "signal_rejected",
        "ls": "signal_rejected",
        "both": "signal_rejected",
    }
    details_by_run = {
        row.run_id: json.loads(row.details)
        for row in signals.itertuples(index=False)
        if row.event_type == "signal_rejected"
    }
    assert details_by_run["oi"]["rejection_reasons"] == ["max_oi_change_pct"]
    assert details_by_run["ls"]["rejection_reasons"] == ["max_ls_ratio"]
    assert details_by_run["both"]["rejection_reasons"] == [
        "max_oi_change_pct", "max_ls_ratio"
    ]
    expected_thresholds = {
        "oi": (15.0, 0.0),
        "ls": (0.0, 1.5),
        "both": (15.0, 1.5),
    }
    for run_id, details in details_by_run.items():
        assert details["rejection_stage"] == "metrics_entry_filters"
        assert details["oi"] == 130.0
        assert details["previous_oi"] == 100.0
        assert details["oi_change_pct"] == 30.0
        assert details["ls_ratio"] == 1.8
        assert details["metrics_available_time"] == signal_time
        assert details["max_oi_change_pct"] == expected_thresholds[run_id][0]
        assert details["max_ls_ratio"] == expected_thresholds[run_id][1]
