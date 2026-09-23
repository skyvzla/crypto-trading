from datetime import UTC, datetime, timedelta

import pytest

from trading_platform.notifications.sources import DomainEventBridge


class FakeSource:
    def __init__(self, signals=(), statuses=(), order_failures=(), metrics=None):
        self.signals = signals
        self.statuses = statuses
        self.order_failures = order_failures
        self.metrics = metrics or {
            "signals": 0,
            "order_failures": 0,
            "fills": 0,
            "open_positions": 0,
            "pending_deliveries": 0,
            "retry_deliveries": 0,
            "dead_deliveries": 0,
            "sent_deliveries": 0,
        }
        self.since = None
        self.metric_windows = []

    async def recent_signal_events(self, *, since):
        self.since = since
        return self.signals

    async def runtime_statuses(self):
        return self.statuses

    async def unpublished_order_failures(self):
        return self.order_failures

    async def hourly_metrics(self, *, start, end):
        self.metric_windows.append((start, end))
        return self.metrics


class FakeMarketQuality:
    def __init__(self, snapshots):
        self.snapshots = tuple(snapshots)
        self.calls = 0

    async def snapshot(self):
        snapshot = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        return snapshot

    async def aclose(self):
        return None


def collector(items):
    async def collect(event):
        items.append(event)

    return collect


@pytest.mark.asyncio
async def test_bridge_publishes_signal_and_halted_runtime_events():
    now = datetime(2026, 8, 16, 8, tzinfo=UTC)
    source = FakeSource(
        signals=[
            {
                "event_key": "a" * 64,
                "account_id": "spike_testnet",
                "event_time": int(now.timestamp() * 1000),
                "symbol": "AKEUSDT",
                "strategy_id": "spike_short",
                "campaign_id": "spike_short:AKEUSDT:1",
                "details": {"trigger_price": "1.25"},
            }
        ],
        statuses=[
            {
                "account_id": "spike_testnet",
                "strategy_id": "spike_short",
                "instance_id": "instance-1",
                "mode": "testnet",
                "status": "fatal",
                "halted": True,
                "halt_reason": "submit status unknown",
                "gate_conditions": {"execution": False},
                "heartbeat_at": now,
            }
        ],
    )
    published = []
    bridge = DomainEventBridge(source, collector(published))

    assert await bridge.run_once(now=now) == 2
    assert [item.event_type for item in published] == [
        "trading.signal.triggered",
        "risk.halted",
    ]
    assert published[0].severity == "warning"
    assert published[0].idempotency_key == f"strategy-audit:{'a' * 64}"
    assert published[1].severity == "critical"
    assert published[1].payload["halt_reason"] == "submit status unknown"


@pytest.mark.asyncio
async def test_bridge_marks_stale_runtime_critical_and_ignores_healthy():
    now = datetime(2026, 8, 16, 8, tzinfo=UTC)
    stale = now - timedelta(minutes=2)
    base = {
        "account_id": "account",
        "strategy_id": "spike_short",
        "mode": "live",
        "halted": False,
        "halt_reason": None,
        "gate_conditions": {},
    }
    source = FakeSource(
        statuses=[
            {**base, "instance_id": "stale", "status": "running", "heartbeat_at": stale},
            {**base, "instance_id": "healthy", "status": "running", "heartbeat_at": now},
        ]
    )
    published = []
    bridge = DomainEventBridge(source, collector(published))

    assert await bridge.run_once(now=now) == 1
    assert published[0].event_type == "system.strategy.unhealthy"
    assert published[0].severity == "critical"


@pytest.mark.asyncio
async def test_bridge_uses_lookback_and_ignores_non_alerting_runtime_status():
    now = datetime(2026, 8, 16, 8, tzinfo=UTC)
    source = FakeSource(
        statuses=[
            {
                "account_id": "account",
                "strategy_id": "spike_short",
                "instance_id": "healthy",
                "mode": "testnet",
                "status": "running",
                "halted": False,
                "halt_reason": None,
                "gate_conditions": {"execution": True},
                "heartbeat_at": now,
            }
        ]
    )
    published = []
    bridge = DomainEventBridge(
        source,
        collector(published),
        signal_lookback=timedelta(hours=2),
    )

    assert await bridge.run_once(now=now) == 0
    assert source.since == now - timedelta(hours=2)
    assert published == []


@pytest.mark.asyncio
async def test_bridge_publishes_runtime_recovery_only_on_health_transition():
    now = datetime(2026, 8, 16, 8, tzinfo=UTC)
    status = {
        "account_id": "account",
        "strategy_id": "spike_short",
        "instance_id": "instance-1",
        "mode": "live",
        "status": "running",
        "halted": False,
        "halt_reason": None,
        "gate_conditions": {},
        "heartbeat_at": now - timedelta(minutes=2),
    }
    source = FakeSource(statuses=[status])
    published = []
    bridge = DomainEventBridge(
        source,
        collector(published),
        runtime_stale_after=timedelta(minutes=1),
    )

    assert await bridge.run_once(now=now) == 1
    assert published[-1].event_type == "system.strategy.unhealthy"

    assert await bridge.run_once(now=now + timedelta(seconds=10)) == 0

    source.statuses = [{**status, "heartbeat_at": now + timedelta(seconds=20)}]
    assert await bridge.run_once(now=now + timedelta(seconds=20)) == 1
    assert published[-1].event_type == "system.strategy.recovered"

    assert await bridge.run_once(now=now + timedelta(seconds=30)) == 0
    assert [event.event_type for event in published] == [
        "system.strategy.unhealthy",
        "system.strategy.recovered",
    ]


@pytest.mark.asyncio
async def test_bridge_publishes_risk_resumed_only_after_halt_is_cleared():
    now = datetime(2026, 8, 16, 8, tzinfo=UTC)
    status = {
        "account_id": "account",
        "strategy_id": "spike_short",
        "instance_id": "instance-1",
        "mode": "testnet",
        "status": "fatal",
        "halted": True,
        "halt_reason": "submit status unknown",
        "gate_conditions": {"execution": False},
        "heartbeat_at": now,
    }
    source = FakeSource(statuses=[status])
    published = []
    bridge = DomainEventBridge(source, collector(published))

    assert await bridge.run_once(now=now) == 1
    assert published[-1].event_type == "risk.halted"
    assert await bridge.run_once(now=now + timedelta(seconds=10)) == 0

    source.statuses = [
        {
            **status,
            "status": "running",
            "halted": False,
            "halt_reason": None,
            "gate_conditions": {"execution": True},
        }
    ]
    assert await bridge.run_once(now=now + timedelta(seconds=20)) == 1
    assert published[-1].event_type == "risk.resumed"
    assert await bridge.run_once(now=now + timedelta(seconds=30)) == 0


@pytest.mark.asyncio
async def test_bridge_does_not_mark_stopped_runtime_with_old_heartbeat_unhealthy():
    now = datetime(2026, 8, 16, 8, tzinfo=UTC)
    source = FakeSource(
        statuses=[
            {
                "account_id": "account",
                "strategy_id": "spike_short",
                "instance_id": "stopped-instance",
                "mode": "live",
                "status": "stopped",
                "halted": False,
                "halt_reason": None,
                "gate_conditions": {},
                "heartbeat_at": now - timedelta(days=3),
                "stopped_at": now - timedelta(days=2),
            }
        ]
    )
    published = []
    bridge = DomainEventBridge(source, collector(published))

    assert await bridge.run_once(now=now) == 0
    assert published == []


@pytest.mark.asyncio
async def test_bridge_publishes_each_order_submission_failure_independently():
    now = datetime(2026, 8, 16, 8, tzinfo=UTC)
    failures = [
        {
            "event_id": "failed-1",
            "event_time": int(now.timestamp() * 1000),
            "event_type": "execution.order_submit_failed",
            "account_id": "account",
            "strategy_id": "spike_short",
            "symbol": "BTCUSDT",
            "client_order_id": "client-1",
            "exchange_order_id": None,
            "details": {
                "error_type": "TimeoutError",
                "error_message": "request timed out",
                "intent": {"side": "BUY", "order_type": "LIMIT"},
            },
        },
        {
            "event_id": "rejected-1",
            "event_time": int(now.timestamp() * 1000),
            "event_type": "execution.order_submit_result",
            "account_id": "account",
            "strategy_id": "spike_short",
            "symbol": "ETHUSDT",
            "client_order_id": "client-2",
            "exchange_order_id": None,
            "details": {"status": "REJECTED", "error_message": "bad price"},
        },
        {
            "event_id": "unknown-1",
            "event_time": int(now.timestamp() * 1000),
            "event_type": "execution.order_submit_result",
            "account_id": "account",
            "strategy_id": "spike_short",
            "symbol": "SOLUSDT",
            "client_order_id": "client-3",
            "exchange_order_id": None,
            "details": {"status": "SUBMIT_UNKNOWN", "error_message": "unknown"},
        },
    ]
    source = FakeSource(order_failures=failures)
    published = []
    bridge = DomainEventBridge(source, collector(published))

    assert await bridge.run_once(now=now) == 3
    assert [event.event_type for event in published] == [
        "trading.order.failed",
        "trading.order.failed",
        "trading.order.failed",
    ]
    assert [event.payload["status"] for event in published] == [
        "execution.order_submit_failed",
        "REJECTED",
        "SUBMIT_UNKNOWN",
    ]
    assert [event.idempotency_key for event in published] == [
        "execution-event:failed-1",
        "execution-event:rejected-1",
        "execution-event:unknown-1",
    ]
    assert published[-1].severity == "critical"


@pytest.mark.asyncio
async def test_bridge_publishes_market_degraded_and_recovered_on_transitions():
    now = datetime(2026, 8, 16, 8, tzinfo=UTC)
    degraded = {
        "ready": True,
        "pubsub_delivery_ready": True,
        "streams": {"binance": {"status": "degraded", "issue": "lagging"}},
        "pubsub_channels": {},
        "metrics": {},
    }
    healthy = {
        "ready": True,
        "pubsub_delivery_ready": True,
        "streams": {"binance": {"status": "healthy"}},
        "pubsub_channels": {},
        "metrics": {},
    }
    source = FakeSource()
    market = FakeMarketQuality([degraded, healthy])
    published = []
    bridge = DomainEventBridge(
        source,
        collector(published),
        market_quality=market,
        market_poll_interval=timedelta(seconds=30),
    )

    assert await bridge.run_once(now=now) == 1
    assert published[-1].event_type == "market.data.degraded"
    assert await bridge.run_once(now=now + timedelta(seconds=10)) == 0
    assert await bridge.run_once(now=now + timedelta(seconds=31)) == 1
    assert published[-1].event_type == "market.data.recovered"
    assert await bridge.run_once(now=now + timedelta(seconds=40)) == 0
    assert market.calls == 2


@pytest.mark.asyncio
async def test_bridge_publishes_at_most_one_hourly_summary_per_shanghai_hour():
    now = datetime(2026, 8, 16, 16, 30, tzinfo=UTC)
    source = FakeSource(
        statuses=[
            {
                "account_id": "account",
                "strategy_id": "spike_short",
                "instance_id": "instance-1",
                "mode": "testnet",
                "status": "running",
                "entry_enabled": True,
                "halted": False,
                "halt_reason": None,
                "gate_conditions": {"execution": True},
                "heartbeat_at": now,
            }
        ],
        metrics={
            "signals": 2,
            "order_failures": 3,
            "fills": 1,
            "open_positions": 4,
            "pending_deliveries": 5,
            "retry_deliveries": 6,
            "dead_deliveries": 7,
            "sent_deliveries": 8,
        },
    )
    published = []
    bridge = DomainEventBridge(
        source,
        collector(published),
        enable_hourly_summary=True,
        runtime_stale_after=timedelta(hours=2),
    )

    assert await bridge.run_once(now=now) == 1
    assert await bridge.run_once(now=now + timedelta(minutes=20)) == 0
    assert await bridge.run_once(now=now + timedelta(minutes=30)) == 1

    summaries = [
        event for event in published if event.event_type == "system.hourly_summary"
    ]
    assert len(summaries) == 2
    assert summaries[0].payload["signals"] == 2
    assert summaries[0].payload["order_failures"] == 3
    assert summaries[0].payload["fills"] == 1
    assert summaries[0].payload["open_positions"] == 4
    assert summaries[0].payload["pending_deliveries"] == 5
    assert summaries[0].payload["retry_deliveries"] == 6
    assert summaries[0].payload["dead_deliveries"] == 7
    assert summaries[0].payload["sent_deliveries"] == 8
    assert "统计时段：2026-08-16 23:00 至 00:00（上海）" in summaries[0].body
    assert summaries[0].idempotency_key == "hourly-summary:2026081623"
    assert summaries[1].idempotency_key == "hourly-summary:2026081700"
    assert len(source.metric_windows) == 2


@pytest.mark.asyncio
async def test_hourly_summary_reports_runtime_status_query_failure_as_unknown():
    now = datetime(2026, 8, 16, 8, tzinfo=UTC)

    class BrokenRuntimeSource(FakeSource):
        async def runtime_statuses(self):
            raise RuntimeError("runtime table unavailable")

    source = BrokenRuntimeSource()
    published = []
    bridge = DomainEventBridge(
        source,
        collector(published),
        enable_hourly_summary=True,
    )

    assert await bridge.run_once(now=now) == 1
    summary = published[0]
    assert summary.event_type == "system.hourly_summary"
    assert summary.severity == "warning"
    assert summary.payload["strategy_status_available"] is False
    assert summary.payload["strategies"] == []
    assert "策略状态未知" in summary.body
