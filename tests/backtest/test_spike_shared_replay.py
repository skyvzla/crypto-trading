from decimal import Decimal

import pytest

from trading_platform.backtest.engine import BacktestEngine
from trading_platform.backtest.result import ResultAnalyzer
from trading_platform.shared.config import BacktestConfig
from trading_platform.shared.events import Bar1s, Kline
from trading_platform.strategies.spike.definition import load_strategy_definition
from trading_platform.strategies.spike.shared_features import (
    SpikeSharedFeatureProvider,
)
from trading_platform.strategies.spike.short import DynamicSpikeBacktestStrategy


MINUTE = 60_000
WARMUP_MINUTES = 7 * 24 * 60
SYMBOL = "BTCUSDT"


def _kline(
    interval: str,
    open_time: int,
    duration: int,
    *,
    close: str = "100",
    high: str = "102",
    low: str = "99",
) -> Kline:
    return Kline(
        symbol=SYMBOL,
        interval=interval,
        open_time=open_time,
        close_time=open_time + duration - 1,
        available_time=open_time + duration,
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
    )


def _bar(timestamp: int, close: str, high: str, volume: str = "1") -> Bar1s:
    price = Decimal(close)
    return Bar1s(
        symbol=SYMBOL,
        timestamp=timestamp,
        available_time=timestamp + 1_000,
        open=price,
        high=Decimal(high),
        low=min(price, Decimal("99")),
        close=price,
        volume=Decimal(volume),
        trade_count=1,
        vwap=price,
    )


def _events(
    *, signal_minute: int, include_exit_bar: bool = False
) -> list[Bar1s | Kline]:
    events: list[Bar1s | Kline] = []
    for index in range(WARMUP_MINUTES):
        open_time = index * MINUTE
        # Keep a low older than the final hour so the short rise-low window
        # remains valid while exercising the provider's seven-day box window.
        low = "80" if index == WARMUP_MINUTES - 120 else "99"
        close = "110" if index >= WARMUP_MINUTES - 120 else "100"
        high = "112" if index >= WARMUP_MINUTES - 120 else "102"
        events.append(
            _kline(
                "1m",
                open_time,
                MINUTE,
                close=close,
                high=high,
                low=low,
            )
        )

    for index in range(15):
        events.append(
            _kline(
                "5m",
                signal_minute - (15 - index) * 5 * MINUTE,
                5 * MINUTE,
            )
        )
    for index in range(16):
        events.append(
            _kline(
                "15m",
                signal_minute - (16 - index) * 15 * MINUTE,
                15 * MINUTE,
            )
        )

    bar_start = signal_minute - MINUTE
    events.extend(
        _bar(bar_start + index * 1_000, "100", "100")
        for index in range(56)
    )
    events.extend(
        _bar(
            bar_start + offset * 1_000,
            close,
            "120" if offset == 60 else close,
            "4",
        )
        for offset, close in enumerate(
            ("100", "101", "102", "104", "106"), start=56
        )
    )
    # The first bar activates the entry plan; the next bar crosses all tiers.
    events.extend(
        (
            _bar(signal_minute + 1_000, "110", "110"),
            _bar(signal_minute + 2_000, "110", "119"),
        )
    )
    if include_exit_bar:
        events.append(_bar(signal_minute + 5 * MINUTE, "130", "130"))
    return sorted(
        events,
        key=lambda event: (
            event.available_time,
            event.type_priority,
            event.symbol,
            event.sequence,
        ),
    )


def _strategy(
    strategy_class: type,
    *,
    rise_threshold: str = "0.05",
    metrics_series: list[tuple[int, float, float]] | None = None,
    max_oi_change_pct: float = 0.0,
) -> DynamicSpikeBacktestStrategy:
    parameters: dict[str, object] = {
        "box_duration_min_minutes": 60,
        "rise_5s_threshold": Decimal(rise_threshold),
    }
    if metrics_series is not None:
        parameters.update(
            metrics_series=metrics_series,
            max_oi_change_pct=max_oi_change_pct,
            oi_stop_enabled=True,
            oi_stop_oi_rise_pct=5.0,
            oi_stop_loss_pct=0.0,
        )
    return DynamicSpikeBacktestStrategy(
        [SYMBOL],
        Decimal("1000"),
        exit_policy="candidate-v1",
        prior_high_lookback_minutes=4 * 60,
        rise_low_lookback_minutes=3 * 60,
        min_rise_duration_minutes=60,
        strategy_class=strategy_class,
        strategy_parameters=parameters,
    )


def _private_state(strategy: DynamicSpikeBacktestStrategy) -> tuple[object, ...]:
    leaf = strategy.strategies[SYMBOL]
    return (
        getattr(leaf, "_metrics_idx", None),
        getattr(leaf, "_oi_stop_campaign", None),
        getattr(leaf, "_oi_stop_checked", None),
        getattr(leaf, "_campaign_id_for_timing", None),
    )


def _run(
    strategy: DynamicSpikeBacktestStrategy,
    events: list[Bar1s | Kline],
    provider: SpikeSharedFeatureProvider | None = None,
    state_trace: list[tuple[object, ...]] | None = None,
) -> BacktestEngine:
    engine = BacktestEngine(
        strategy,
        [],
        BacktestConfig(trading_start_ms=events[0].available_time),
    )
    if provider is not None:
        provider.bind(strategy)
    for event in events:
        if provider is not None:
            provider.process_event(event)
        engine.process_event(event)
        if state_trace is not None:
            state_trace.append(_private_state(strategy))
    return engine


def _run_shared(
    strategies: list[DynamicSpikeBacktestStrategy],
    events: list[Bar1s | Kline],
    provider: SpikeSharedFeatureProvider,
    state_traces: list[list[tuple[object, ...]]] | None = None,
) -> list[BacktestEngine]:
    engines = [
        BacktestEngine(
            strategy,
            [],
            BacktestConfig(trading_start_ms=events[0].available_time),
        )
        for strategy in strategies
    ]
    for strategy in strategies:
        provider.bind(strategy)
    for event in events:
        provider.process_event(event)
        for index, engine in enumerate(engines):
            engine.process_event(event)
            if state_traces is not None:
                state_traces[index].append(_private_state(strategies[index]))
    return engines


def _snapshot(engine: BacktestEngine) -> dict[str, object]:
    result = engine.finish()
    analyzer = ResultAnalyzer(result)
    fields = {
        "orders": (
            "client_order_id",
            "symbol",
            "side",
            "type",
            "price",
            "quantity",
            "status",
            "filled_quantity",
            "reduce_only",
            "trigger_reason",
            "campaign_id",
        ),
        "fills": (
            "order_id",
            "symbol",
            "side",
            "price",
            "quantity",
            "commission",
            "fill_time",
            "is_maker",
        ),
        "positions": (
            "symbol",
            "side",
            "entry_price",
            "quantity",
            "status",
            "opened_at",
            "closed_at",
            "realized_pnl",
            "unrealized_pnl",
        ),
    }
    snapshot: dict[str, object] = {}
    for name, names in fields.items():
        records = getattr(result, name)
        snapshot[name] = [
            tuple(getattr(record, field) for field in names)
            for record in records
        ]
    snapshot["audits"] = [
        (
            event.event_time,
            event.event_type,
            event.symbol,
            event.campaign_id,
            event.details,
        )
        for event in result.audit_events
    ]
    trade_fields = (
        "symbol",
        "side",
        "campaign_id",
        "entry_price",
        "entry_quantity",
        "entry_fill_count",
        "status",
        "net_pnl",
    )
    snapshot["trades"] = [
        tuple(row[field] for field in trade_fields)
        for row in analyzer.dfs["trades"].to_dict("records")
    ]
    return snapshot


@pytest.mark.parametrize(
    "definition_path",
    (
        "trading_platform.strategies.spike.v2:V2",
        "trading_platform.strategies.spike.v2_1:V21",
        "trading_platform.strategies.spike.v2_2:V22",
    ),
)
def test_shared_spike_replay_matches_isolated_engine_records(definition_path):
    definition = load_strategy_definition(definition_path)
    signal_minute = WARMUP_MINUTES * MINUTE
    events = _events(
        signal_minute=signal_minute,
        include_exit_bar=definition.name in {"v2.1", "v2.2"},
    )
    strategy_class = definition.strategy_class

    if definition.name in {"v2.1", "v2.2"}:
        metrics_a = [
            (signal_minute - 5 * MINUTE, 100.0, 1.0),
            (signal_minute, 110.0, 1.0),
            (signal_minute + 5 * MINUTE, 112.0, 1.0),
        ]
        metrics_b = [
            (signal_minute - 5 * MINUTE, 100.0, 1.0),
            (signal_minute, 101.0, 1.0),
            (signal_minute + 5 * MINUTE, 110.0, 1.0),
        ]
        isolated_strategies = [
            _strategy(
                strategy_class,
                metrics_series=metrics_a,
                max_oi_change_pct=5.0,
            ),
            _strategy(
                strategy_class,
                metrics_series=metrics_b,
                max_oi_change_pct=20.0,
            ),
        ]
        shared_strategies = [
            _strategy(
                strategy_class,
                metrics_series=list(metrics_a),
                max_oi_change_pct=5.0,
            ),
            _strategy(
                strategy_class,
                metrics_series=list(metrics_b),
                max_oi_change_pct=20.0,
            ),
        ]
    else:
        isolated_strategies = [
            _strategy(strategy_class),
            _strategy(strategy_class, rise_threshold="0.10"),
        ]
        shared_strategies = [
            _strategy(strategy_class),
            _strategy(strategy_class, rise_threshold="0.10"),
        ]

    isolated_traces = [[] for _ in isolated_strategies]
    isolated = [
        _run(strategy, events, state_trace=isolated_traces[index])
        for index, strategy in enumerate(isolated_strategies)
    ]
    provider = SpikeSharedFeatureProvider(
        shared_features=definition.data_requirements.shared_features
    )
    shared_traces = [[] for _ in shared_strategies]
    shared = _run_shared(
        shared_strategies,
        events,
        provider,
        state_traces=shared_traces,
    )

    assert [_snapshot(engine) for engine in shared] == [
        _snapshot(engine) for engine in isolated
    ]
    assert shared_traces == isolated_traces
    assert any(engine.orders for engine in shared)
    assert any(engine.fills for engine in shared)
    assert any(engine.audit_records for engine in shared)
    assert any(engine.position_records for engine in shared)

    if definition.name in {"v2.1", "v2.2"}:
        strategies = [strategy.strategies[SYMBOL] for strategy in shared_strategies]
        assert not hasattr(provider, "metrics_series")
        assert strategies[0].metrics_series is not strategies[1].metrics_series
        assert strategies[0].metrics_series == metrics_a
        assert strategies[1].metrics_series == metrics_b
        assert len(shared[0].orders) == 0
        assert len(shared[1].orders) == 4
        assert shared_traces[0][-1] == (2, None, False, None)
        assert shared_traces[1][-1] == (
            2,
            signal_minute,
            True,
            f"spike_short:{SYMBOL}:{signal_minute}",
        )
