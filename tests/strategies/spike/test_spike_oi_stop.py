"""Spike v2.1 OI 止损（candidate_oi_stop_exit）单测。"""

from decimal import Decimal

from trading_platform.shared.events import Position
from trading_platform.strategies.spike.v2_1 import SpikeV21Strategy

FIRST_FILL_MS = 1_000


class OiStopPositionAccount:
    def __init__(self, quantity: str = "2", entry_price: str = "100"):
        self.orders = []
        self.position = Position(
            symbol="AKEUSDT",
            side="SHORT",
            entry_price=Decimal(entry_price),
            quantity=Decimal(quantity),
            total_commission=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            realized_pnl=Decimal("0"),
            opened_at=FIRST_FILL_MS,
        )

    def get_position(self, symbol):
        if self.position is None:
            return None
        return self.position if symbol == self.position.symbol else None

    def iter_orders(self):
        return tuple(self.orders)

    def cancel_order(self, order_id):
        return False


def _strategy(**kwargs):
    account = OiStopPositionAccount()
    defaults = dict(
        total_notional=Decimal("20"),
        account=account,
        exit_policy="candidate-v1",
        oi_stop_enabled=True,
        oi_stop_oi_rise_pct=5.0,
        oi_stop_loss_pct=3.0,
        metrics_series=[
            (100_000, 100.0, 1.0),
            (400_000, 90.0, 1.0),
            (700_000, 92.0, 1.0),
        ],
    )
    defaults.update(kwargs)
    strategy = SpikeV21Strategy("AKEUSDT", **defaults)
    strategy.restore_campaign_timing(
        "spike_short:AKEUSDT:200_000", FIRST_FILL_MS, origin_price=Decimal("100")
    )
    return strategy, account


def test_oi_stop_disabled_by_default():
    strategy = SpikeV21Strategy(
        "AKEUSDT",
        total_notional=Decimal("20"),
        metrics_series=[(100_000, 100.0, 1.0), (400_000, 90.0, 1.0)],
    )
    assert strategy.oi_stop_enabled is False
    assert strategy.oi_stop_oi_rise_pct == 5.0
    assert strategy.oi_stop_loss_pct == 3.0


def test_oi_stop_parameter_validation():
    import pytest

    with pytest.raises(ValueError):
        _strategy(oi_stop_oi_rise_pct=-1.0)
    with pytest.raises(ValueError):
        _strategy(oi_stop_loss_pct=-1.0)


def test_oi_stop_fires_when_oi_rises_and_loss_met():
    """插针后首个有效 OI 点（>signal_time）升幅超阈值且浮亏达标 → 止损。"""
    strategy, _ = _strategy(
        metrics_series=[
            (100_000, 100.0, 1.0),  # 基准点（<= signal_time 200_000 的最近点）
            (400_000, 112.0, 1.0),  # 确认点（+12%）
            (700_000, 110.0, 1.0),
        ]
    )
    # 确认点数据不可见时（event < 400_000）不动作
    assert strategy._manage_candidate_exit(300_000, Decimal("105")) == []
    assert strategy._oi_stop_checked is False

    intents = strategy._manage_candidate_exit(400_000, Decimal("105"))
    assert len(intents) == 1
    assert intents[0].trigger_reason == "candidate_oi_stop_exit"
    assert intents[0].reduce_only is True
    assert intents[0].side == "BUY"
    assert intents[0].quantity == Decimal("2")
    # 只评估一次
    assert strategy._oi_stop_checked is True
    assert strategy._manage_candidate_exit(401_000, Decimal("106")) == []


def test_oi_stop_not_fired_when_oi_not_rising():
    """确认点 OI 未超阈值 → 不触发，后续也不再评估。"""
    strategy, _ = _strategy(
        metrics_series=[
            (100_000, 100.0, 1.0),
            (400_000, 102.0, 1.0),  # +2% < 5%
        ]
    )
    assert strategy._manage_candidate_exit(400_000, Decimal("105")) == []
    assert strategy._oi_stop_checked is True


def test_oi_stop_not_fired_when_loss_below_threshold():
    """OI 升幅达标但浮亏不足 → 不触发。"""
    strategy, _ = _strategy(
        metrics_series=[
            (100_000, 100.0, 1.0),
            (400_000, 112.0, 1.0),  # +12% > 5%
        ]
    )
    # 浮亏仅 1%（mark 101 vs entry 100）
    assert strategy._manage_candidate_exit(400_000, Decimal("101")) == []
    assert strategy._oi_stop_checked is True


def test_oi_stop_requires_position():
    """无持仓 → 不触发。"""
    strategy, account = _strategy()
    account.position = None
    assert strategy._manage_candidate_exit(400_000, Decimal("105")) == []


def test_oi_stop_uses_signal_time_from_campaign_id():
    """基准点取 signal_time 前最近、确认点取 signal_time 后第一个。"""
    strategy, _ = _strategy(
        metrics_series=[
            (100_000, 100.0, 1.0),  # 基准
            (400_000, 110.0, 1.0),  # +10%，确认
        ]
    )
    intents = strategy._manage_candidate_exit(400_000, Decimal("105"))
    assert len(intents) == 1
    assert intents[0].trigger_reason == "candidate_oi_stop_exit"


def test_oi_stop_campaign_switch_resets_check():
    """换 campaign 后重新评估（同 signal_time 的 campaign 不重复评估）。"""
    strategy, _ = _strategy(
        metrics_series=[
            (100_000, 100.0, 1.0),
            (300_000, 103.0, 1.0),  # 相对 100_000 仅 +3%，不达标
            (400_000, 110.0, 1.0),
        ]
    )
    # campaign1: signal_time=200_000 → 基准=100_000、确认=300_000（+3%）
    assert strategy._manage_candidate_exit(400_000, Decimal("105")) == []
    assert strategy._oi_stop_checked is True

    # campaign2: signal_time=350_000 → 基准=300_000、确认=400_000（+6.8%）
    strategy.restore_campaign_timing(
        "spike_short:AKEUSDT:350_000", FIRST_FILL_MS, origin_price=Decimal("100")
    )
    intents = strategy._manage_candidate_exit(400_000, Decimal("105"))
    assert len(intents) == 1
    assert intents[0].trigger_reason == "candidate_oi_stop_exit"


def test_oi_stop_audit_event_recorded():
    strategy, _ = _strategy(
        metrics_series=[
            (100_000, 100.0, 1.0),
            (400_000, 112.0, 1.0),
        ]
    )
    strategy._manage_candidate_exit(400_000, Decimal("105"))
    events = strategy.drain_audit_events()
    assert any(
        e.event_type == "candidate_oi_stop_exit_requested" for e in events
    )