from datetime import UTC, datetime, timedelta

import pytest

from trading_platform.notifications.sources import DomainEventBridge


class FakeSource:
    def __init__(self, signals=(), statuses=()):
        self.signals = signals
        self.statuses = statuses
        self.since = None

    async def recent_signal_events(self, *, since):
        self.since = since
        return self.signals

    async def runtime_statuses(self):
        return self.statuses


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
