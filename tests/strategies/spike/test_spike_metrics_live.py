import asyncio
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from trading_platform.strategies.spike.live import CompositeEntryGate, SpikeLiveSettings
from trading_platform.strategies.spike.main import (
    METRICS_INTERVAL_MS,
    METRICS_MAX_AGE_MS,
    SpikeLiveProcess,
)
from trading_platform.strategies.spike.short import DynamicSpikeBacktestStrategy
from trading_platform.strategies.spike.v2_1 import SpikeV21Strategy


def _process() -> SpikeLiveProcess:
    process = SpikeLiveProcess(
        # V21 has the same metrics contract as the V22 production definition,
        # while avoiding any dependency on the user-owned pullback module.
        SpikeLiveSettings(
            account_id="metrics-test",
            symbols=["BTCUSDT"],
            total_notional="100",
            strategy_path="trading_platform.strategies.spike.v2_1:V21",
        ),
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="metrics-test"),
    )
    strategy = DynamicSpikeBacktestStrategy(
        ["BTCUSDT"], Decimal("100"), strategy_class=SpikeV21Strategy
    )
    process.coordinator = SimpleNamespace(
        strategy=strategy,
        account=SimpleNamespace(symbols_with_live_risk=lambda: set()),
        cancel_open_entry_orders=AsyncMock(),
        risk_guard=SimpleNamespace(halted=False, halt_reason=None),
    )
    process.gate = CompositeEntryGate(strategy)
    process.exchange_symbol_snapshot = SimpleNamespace(
        allowed_symbols=frozenset({"BTCUSDT"}),
        blocked_symbols=frozenset(),
        blocked_reasons={},
    )
    return process


def _payload(available_time: int, *, oi: float = 100.0, ls: float = 1.1) -> str:
    return json.dumps(
        {
            "symbol": "BTCUSDT",
            "snapshot_time": available_time - METRICS_INTERVAL_MS,
            "available_time": available_time,
            "open_interest": oi,
            "long_short_ratio": ls,
        }
    )


def test_metrics_snapshot_is_injected_into_matching_leaf_strategy():
    process = _process()

    assert process._ingest_metrics_payload(
        _payload(METRICS_INTERVAL_MS), now_ms=METRICS_INTERVAL_MS
    ) == "BTCUSDT"

    leaf = process.coordinator.strategy.strategies["BTCUSDT"]
    assert leaf.metrics_series == [(METRICS_INTERVAL_MS, 100.0, 1.1)]
    process._set_metrics_gate(process._metrics_gate_ready(now_ms=METRICS_INTERVAL_MS))
    assert process.gate.condition("metrics_5m") is True


def test_metrics_gap_fails_closed_then_recovers_after_contiguous_snapshot():
    process = _process()
    process._ingest_metrics_payload(_payload(METRICS_INTERVAL_MS), now_ms=METRICS_INTERVAL_MS)
    process._ingest_metrics_payload(
        _payload(2 * METRICS_INTERVAL_MS), now_ms=2 * METRICS_INTERVAL_MS
    )
    process._set_metrics_gate(process._metrics_gate_ready(now_ms=2 * METRICS_INTERVAL_MS))
    assert process.gate.condition("metrics_5m") is True

    # The 900,000 ms bucket is missing.
    process._ingest_metrics_payload(
        _payload(4 * METRICS_INTERVAL_MS), now_ms=4 * METRICS_INTERVAL_MS
    )
    assert process.gate.condition("metrics_5m") is False

    process._ingest_metrics_payload(
        _payload(5 * METRICS_INTERVAL_MS), now_ms=5 * METRICS_INTERVAL_MS
    )
    assert process._refresh_metrics_gate(now_ms=5 * METRICS_INTERVAL_MS) is True
    assert process.gate.condition("metrics_5m") is True


def test_metrics_gate_closes_when_latest_snapshot_expires():
    process = _process()
    available = METRICS_INTERVAL_MS
    process._ingest_metrics_payload(_payload(available), now_ms=available)
    assert process._refresh_metrics_gate(now_ms=available) is True

    assert process._refresh_metrics_gate(
        now_ms=available + METRICS_MAX_AGE_MS + 1
    ) is False
    assert process.gate.condition("metrics_5m") is False


@pytest.mark.asyncio
async def test_metrics_warmup_replays_stream_and_updates_strategy():
    process = _process()
    process.redis = Mock()
    process.redis.xrevrange = AsyncMock()
    process.redis.hget = AsyncMock(return_value=None)

    # The process helper uses wall clock time; use fresh real-time buckets for
    # the actual gate assertion while still checking stream ordering below.
    current = time.time_ns() // 1_000_000
    first = (current // METRICS_INTERVAL_MS) * METRICS_INTERVAL_MS
    process.redis.xrevrange.return_value = [
        ("2-0", {"data": _payload(first)}),
        ("1-0", {"data": _payload(first - METRICS_INTERVAL_MS)}),
    ]
    await process._warm_metrics_history()

    assert process.coordinator.strategy.strategies["BTCUSDT"].metrics_series == [
        (first - METRICS_INTERVAL_MS, 100.0, 1.1),
        (first, 100.0, 1.1),
    ]
    assert process.gate.condition("metrics_5m") is True
    assert process._metrics_stream_ids == {"BTCUSDT": "2-0"}


def test_invalid_metrics_payload_fails_closed_without_mutating_series():
    process = _process()
    assert process._ingest_metrics_payload(
        _payload(METRICS_INTERVAL_MS, oi=float("nan")),
        now_ms=METRICS_INTERVAL_MS,
    ) is None
    assert process._metrics_series == {}
    assert process.gate.condition("metrics_5m") is False


@pytest.mark.asyncio
async def test_metrics_loop_does_not_replay_warmup_rows_across_poll_rounds():
    process = _process()
    process.redis = Mock()
    process.redis.xrevrange = AsyncMock()
    process.redis.xrange = AsyncMock(side_effect=[[], []])
    process.redis.hget = AsyncMock(return_value=None)
    current = time.time_ns() // 1_000_000
    latest = (current // METRICS_INTERVAL_MS) * METRICS_INTERVAL_MS
    process.redis.xrevrange.return_value = [
        (b"2-0", {b"data": _payload(latest)}),
        (b"1-0", {b"data": _payload(latest - METRICS_INTERVAL_MS)}),
    ]

    await process._warm_metrics_history()
    with patch(
        "trading_platform.strategies.spike.main.asyncio.sleep",
        new=AsyncMock(side_effect=[None, asyncio.CancelledError]),
    ):
        with pytest.raises(asyncio.CancelledError):
            await process._metrics_loop()

    assert process.gate.condition("metrics_5m") is True
    assert process.coordinator.cancel_open_entry_orders.await_count == 0
    assert process.redis.xrange.await_count == 2
    assert all(
        call.kwargs["min"] == "(2-0"
        for call in process.redis.xrange.await_args_list
    )


@pytest.mark.asyncio
async def test_metrics_loop_accepts_new_point_and_latches_real_regression():
    process = _process()
    process.redis = Mock()
    current = time.time_ns() // 1_000_000
    latest = (current // METRICS_INTERVAL_MS) * METRICS_INTERVAL_MS
    process._ingest_metrics_payload(
        _payload(latest), now_ms=current, expected_symbol="BTCUSDT"
    )
    process._metrics_stream_ids["BTCUSDT"] = "2-0"
    process.redis.xrange = AsyncMock(
        side_effect=[
            [("3-0", {"data": _payload(latest + METRICS_INTERVAL_MS)})],
            [("4-0", {"data": _payload(latest)})],
        ]
    )

    with patch(
        "trading_platform.strategies.spike.main.time.time",
        return_value=(latest + METRICS_INTERVAL_MS) / 1000,
    ), patch(
        "trading_platform.strategies.spike.main.asyncio.sleep",
        new=AsyncMock(side_effect=[None, asyncio.CancelledError]),
    ):
        with pytest.raises(asyncio.CancelledError):
            await process._metrics_loop()

    assert process._metrics_stream_ids["BTCUSDT"] == "4-0"
    assert process._metrics_last_available_time["BTCUSDT"] == (
        latest + METRICS_INTERVAL_MS
    )
    assert process.gate.condition("metrics_5m") is False
    process.coordinator.cancel_open_entry_orders.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_payload",
    [
        _payload(METRICS_INTERVAL_MS).replace("BTCUSDT", "ETHUSDT"),
        "not-json",
    ],
)
async def test_metrics_stream_wrong_symbol_or_invalid_payload_latches_expected_symbol(
    bad_payload: str,
):
    process = _process()
    process.redis = Mock()
    process.redis.xrange = AsyncMock(
        side_effect=[
            [("2-0", {"data": bad_payload})],
            [("3-0", {"data": _payload(2 * METRICS_INTERVAL_MS)})],
        ]
    )
    process._metrics_stream_ids["BTCUSDT"] = "1-0"
    process._ingest_metrics_payload(
        _payload(METRICS_INTERVAL_MS),
        now_ms=METRICS_INTERVAL_MS,
        expected_symbol="BTCUSDT",
    )

    with patch(
        "trading_platform.strategies.spike.main.time.time",
        return_value=2 * METRICS_INTERVAL_MS / 1000,
    ), patch(
        "trading_platform.strategies.spike.main.asyncio.sleep",
        new=AsyncMock(side_effect=[None, asyncio.CancelledError]),
    ):
        with pytest.raises(asyncio.CancelledError):
            await process._metrics_loop()

    assert process._metrics_stream_ids["BTCUSDT"] == "3-0"
    assert process.gate.condition("metrics_5m") is True
    process.coordinator.cancel_open_entry_orders.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_metrics_make_runtime_status_degraded():
    process = _process()
    process.db = Mock(upsert_strategy_runtime_status=AsyncMock(return_value=True))
    for condition in ("execution", "market", "bar_stream", "event_queue"):
        process.gate.set_condition(condition, True)
    process._ingest_metrics_payload(
        _payload(METRICS_INTERVAL_MS), now_ms=METRICS_INTERVAL_MS
    )
    assert process._refresh_metrics_gate(
        now_ms=METRICS_INTERVAL_MS + METRICS_MAX_AGE_MS + 1
    ) is False

    await process._publish_runtime_status()

    runtime_status = process.db.upsert_strategy_runtime_status.await_args.args[0]
    assert runtime_status.status == "degraded"
