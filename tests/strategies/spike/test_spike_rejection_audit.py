"""测试信号链路统一拒绝审计（_record_signal_rejection）。

覆盖 v2.2 启用的价格拦截（reject_below_current / origin_floor / prior_high）
与版本指标过滤（box / premature / vwap）被拒绝时统一记录 signal_rejected。
"""
from decimal import Decimal

import pytest

from trading_platform.shared.events import Bar1s, Kline
from trading_platform.strategies.spike.short import DynamicSpikeShortStrategy
from trading_platform.strategies.spike.v2_1 import SpikeV21Strategy


class TestSignalRejectionAudit:
    def test_record_signal_rejection_writes_audit_event(self):
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )
        strategy._record_signal_rejection(
            event_time=10_000,
            rejection_stage="price_sanity_entry_filter",
            rejection_reasons=("reject_below_current",),
            trigger_price=Decimal("100"),
            rise_5s=Decimal("0.01"),
            volume_multiple_5s=Decimal("3"),
            extra={"entry_tier": "99.5", "current_close": "100"},
        )
        audit = strategy.drain_audit_events()
        assert len(audit) == 1
        event = audit[0]
        assert event.event_type == "signal_rejected"
        assert event.campaign_id == "spike_short:BTCUSDT:10000"
        assert event.details["rejection_stage"] == "price_sanity_entry_filter"
        assert event.details["rejection_reasons"] == ["reject_below_current"]
        assert event.details["trigger_price"] == "100"
        assert event.details["entry_tier"] == "99.5"
        assert event.details["current_close"] == "100"

    def test_record_signal_rejection_dedup_within_cooldown(self):
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )
        for event_time in (10_000, 20_000, 2_000_000):
            strategy._record_signal_rejection(
                event_time=event_time,
                rejection_stage="vwap_deviation_entry_filter",
                rejection_reasons=("spike_vwap_deviation_max_pct",),
                trigger_price=Decimal("100"),
            )
        audit = strategy.drain_audit_events()
        # 冷却窗口内（默认 180s）只记一条；窗口外重新记录。
        assert len(audit) == 2

    def test_record_signal_rejection_different_reasons_both_recorded(self):
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )
        strategy._record_signal_rejection(
            event_time=10_000,
            rejection_stage="price_sanity_entry_filter",
            rejection_reasons=("origin_floor",),
            trigger_price=Decimal("100"),
        )
        strategy._record_signal_rejection(
            event_time=10_000,
            rejection_stage="price_sanity_entry_filter",
            rejection_reasons=("reject_below_current",),
            trigger_price=Decimal("100"),
        )
        assert len(strategy.drain_audit_events()) == 2


def _build_triggering_strategy(
    *, reject_below_current: bool = False, spike_vwap_deviation_max_pct: float = 0
) -> SpikeV21Strategy:
    """构造一个信号链路完整可达的 v2.1 策略（基于既有测试模式）。"""
    minute = 60_000
    lookback_minutes = 7 * 24 * 60
    minute_start = lookback_minutes * minute
    strategy = SpikeV21Strategy(
        "BTCUSDT",
        total_notional=Decimal("1000"),
        prior_high_lookback_minutes=6 * 60,
        rise_low_lookback_minutes=lookback_minutes,
        min_rise_duration_minutes=24 * 60,
        reject_below_current=reject_below_current,
        spike_vwap_deviation_max_pct=spike_vwap_deviation_max_pct,
    )
    low_open_time = minute_start - lookback_minutes * minute
    strategy.klines_1m = [
        Kline(
            symbol="BTCUSDT",
            interval="1m",
            open_time=index * minute,
            close_time=(index + 1) * minute - 1,
            available_time=(index + 1) * minute,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("80") if index * minute == low_open_time else Decimal("85"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )
        for index in range(lookback_minutes)
    ]
    strategy.klines_5m = [
        Kline(
            symbol="BTCUSDT",
            interval="5m",
            open_time=minute_start - (15 - index) * 5 * minute,
            close_time=minute_start - (14 - index) * 5 * minute - 1,
            available_time=minute_start - (14 - index) * 5 * minute,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("98"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )
        for index in range(15)
    ]
    closes = [Decimal("100")] * 56 + [
        Decimal("112"), Decimal("114"), Decimal("116"),
        Decimal("118"), Decimal("120"),
    ]
    strategy.bars_1s = [
        Bar1s(
            symbol="BTCUSDT",
            timestamp=minute_start - (60 - index) * 1_000,
            available_time=minute_start - (59 - index) * 1_000,
            open=close,
            high=Decimal("120") if index == 60 else close,
            low=close,
            close=close,
            volume=Decimal("4") if index >= 56 else Decimal("1"),
            trade_count=1,
            vwap=close,
        )
        for index, close in enumerate(closes)
    ]
    return strategy


class TestRejectBelowCurrentAudit:
    def test_reject_below_current_blocks_and_records(self):
        """reject_below_current=True 时挂单价不高于现价的信号被拒绝并记录。"""
        strategy = _build_triggering_strategy(reject_below_current=True)
        signal = strategy._detect_signal(strategy.bars_1s[-1])
        assert signal is None
        audit = strategy.drain_audit_events()
        rejected = [e for e in audit if e.event_type == "signal_rejected"]
        assert rejected, "拒绝必须写入 signal_rejected 审计"
        assert rejected[0].details["rejection_stage"] == "price_sanity_entry_filter"
        assert rejected[0].details["rejection_reasons"] == ["reject_below_current"]
        assert "entry_tier" in rejected[0].details
        assert "current_close" in rejected[0].details

    def test_reject_below_current_false_keeps_signal_and_no_rejection(self):
        """reject_below_current=False（默认）时同场景信号正常放行。"""
        strategy = _build_triggering_strategy(reject_below_current=False)
        signal = strategy._detect_signal(strategy.bars_1s[-1])
        assert signal is not None
        audit = strategy.drain_audit_events()
        assert not [e for e in audit if e.event_type == "signal_rejected"]

    def test_vwap_deviation_filter_records_rejection(self):
        """vwap 偏离超阈值时拒绝并记录统一审计。"""
        strategy = _build_triggering_strategy(spike_vwap_deviation_max_pct=1.0)
        signal = strategy._detect_signal(strategy.bars_1s[-1])
        assert signal is None
        audit = strategy.drain_audit_events()
        rejected = [e for e in audit if e.event_type == "signal_rejected"]
        assert rejected
        assert rejected[0].details["rejection_stage"] == "vwap_deviation_entry_filter"
        assert rejected[0].details["rejection_reasons"] == ["spike_vwap_deviation_max_pct"]
        assert rejected[0].details["spike_vwap_deviation_pct"] is not None

    def test_vwap_deviation_filter_zero_disabled(self):
        """阈值=0 时 vwap 过滤关闭，信号正常放行且无拒绝记录。"""
        strategy = _build_triggering_strategy(spike_vwap_deviation_max_pct=0)
        signal = strategy._detect_signal(strategy.bars_1s[-1])
        assert signal is not None
        audit = strategy.drain_audit_events()
        assert not [e for e in audit if e.event_type == "signal_rejected"]


class TestBoxPrematureOrLogic:
    """box 突破满足时豁免 premature 过滤（OR 逻辑）。"""

    def _strategy(self, box_minutes: int, box_passed: bool, premature_rejected: bool):
        strategy = _build_triggering_strategy()
        strategy.box_duration_min_minutes = box_minutes
        strategy.spike_avg_deviation_max_pct = 28.0
        strategy.spike_range_max_pct = 28.0

        def fake_box(minute_start, current_close):
            return {
                "box_break_minutes": 6 * 60 if box_passed else 30,
                "box_breakthrough": Decimal("0.155"),
                "box_break_lower": Decimal("0.145"),
                "box_break_first_time": 0,
                "box_break_hours": 6.0 if box_passed else 0.5,
                "box_upper_3d": Decimal("0.155"),
                "box_upper_7d": Decimal("0.155"),
                "box_lower_3d": Decimal("0.145"),
                "box_lower_7d": Decimal("0.145"),
            }

        def fake_premature(minute_start, current_close):
            return {
                "spike_avg_deviation_pct": 40.0,
                "spike_range_pct": 60.0,
                "rejected": premature_rejected,
            }

        strategy._box_breakthrough = fake_box
        strategy._premature_spike_filter = fake_premature
        return strategy

    def test_box_passed_exempts_premature(self):
        """box 突破达标（≥4h）时即使 premature 触发也放行，无拒绝记录。"""
        strategy = self._strategy(box_minutes=4 * 60, box_passed=True, premature_rejected=True)
        signal = strategy._detect_signal(strategy.bars_1s[-1])
        assert signal is not None
        audit = strategy.drain_audit_events()
        assert not [e for e in audit if e.event_type == "signal_rejected"]

    def test_box_not_passed_premature_blocks(self):
        """box 未达标时 premature 触发则拒绝，记录 premature 拒绝。"""
        strategy = self._strategy(box_minutes=4 * 60, box_passed=False, premature_rejected=True)
        signal = strategy._detect_signal(strategy.bars_1s[-1])
        assert signal is None
        audit = strategy.drain_audit_events()
        rejected = [e for e in audit if e.event_type == "signal_rejected"]
        assert rejected, "拒绝必须写入 signal_rejected 审计"
        assert rejected[-1].details["rejection_stage"] == "premature_spike_entry_filter"
        assert rejected[-1].details["rejection_reasons"] == ["spike_avg_deviation_max_pct"]

    def test_box_not_passed_premature_clear_keeps_signal(self):
        """box 未达标且 premature 未触发时信号放行；box 拒绝仅记录不阻断。"""
        strategy = self._strategy(box_minutes=4 * 60, box_passed=False, premature_rejected=False)
        signal = strategy._detect_signal(strategy.bars_1s[-1])
        assert signal is not None
        audit = strategy.drain_audit_events()
        rejected = [e for e in audit if e.event_type == "signal_rejected"]
        assert rejected
        assert rejected[-1].details["rejection_stage"] == "box_breakthrough_entry_filter"

    def test_box_disabled_premature_blocks(self):
        """box 关闭（0）时 premature 触发仍拒绝（维持既有行为）。"""
        strategy = self._strategy(box_minutes=0, box_passed=False, premature_rejected=True)
        signal = strategy._detect_signal(strategy.bars_1s[-1])
        assert signal is None
        audit = strategy.drain_audit_events()
        rejected = [e for e in audit if e.event_type == "signal_rejected"]
        assert rejected
        assert rejected[-1].details["rejection_stage"] == "premature_spike_entry_filter"
