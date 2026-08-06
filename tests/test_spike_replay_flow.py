from decimal import Decimal

from trading_platform.backtest.engine import BacktestEngine
from trading_platform.backtest.result import ResultAnalyzer
from trading_platform.shared.config import BacktestConfig
from trading_platform.shared.events import Bar1s, Kline
from trading_platform.strategies.spike_short import DynamicSpikeBacktestStrategy


MINUTE = 60_000


def _kline(interval: str, open_time: int, duration: int) -> Kline:
    return Kline(
        symbol="BTCUSDT",
        interval=interval,
        open_time=open_time,
        close_time=open_time + duration - 1,
        available_time=open_time + duration,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("80") if interval == "1m" else Decimal("98"),
        close=Decimal("100"),
        volume=Decimal("10"),
    )


def _bar(timestamp: int, close: str, high: str, volume: str = "1") -> Bar1s:
    price = Decimal(close)
    return Bar1s(
        symbol="BTCUSDT",
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


def test_spike_replay_runs_from_warmup_through_three_fills():
    minute_start = 16 * 60 * MINUTE
    events = [
        _kline("1m", index * MINUTE, MINUTE)
        for index in range(16 * 60)
    ]
    events.extend(
        _kline("5m", minute_start - (15 - index) * 5 * MINUTE, 5 * MINUTE)
        for index in range(15)
    )

    bar_start = minute_start - MINUTE
    for index in range(56):
        events.append(_bar(bar_start + index * 1_000, "100", "100"))
    for offset, close in enumerate(("100", "101", "102", "104", "106"), start=56):
        events.append(
            _bar(
                bar_start + offset * 1_000,
                close,
                "120" if offset == 60 else close,
                "4",
            )
        )

    # 信号后的第一秒挂单，再下一秒穿透三档但不触及失效价。
    events.append(_bar(minute_start + 1_000, "110", "110"))
    events.append(_bar(minute_start + 2_000, "110", "119"))
    events.sort(
        key=lambda event: (
            event.available_time,
            event.type_priority,
            event.symbol,
            event.sequence,
        )
    )

    strategy = DynamicSpikeBacktestStrategy(
        symbols=["BTCUSDT"], total_notional=Decimal("1000")
    )
    result = BacktestEngine(
        strategy,
        events,
        BacktestConfig(trading_start_ms=minute_start + 1_000),
    ).run()

    assert len(result.orders) == 3
    assert len(result.fills) == 3
    assert all(order.status == "FILLED" for order in result.orders)
    assert len(result.positions) == 1
    assert result.positions[0].side == "SHORT"
    assert result.positions[0].status == "OPEN"
    assert strategy.strategies["BTCUSDT"].first_fill_time == minute_start + 3_000
    assert {event.event_type for event in result.audit_events} == {
        "signal_triggered",
        "entry_plan_created",
        "campaign_first_fill",
    }
    assert len([
        event for event in result.audit_events
        if event.event_type == "campaign_first_fill"
    ]) == 1

    summary = ResultAnalyzer(result).analyze()
    assert summary["orders"]["fill_rate"] == 1.0
    assert summary["positions"]["open"] == 1
    assert summary["pnl"]["total_commission"] > 0


def _warmup_klines(minute_start: int, *, extra_minutes: int = 0) -> list[Kline]:
    """构造无缺口的 1m/5m 指标输入，供固定生命周期案例复用。"""
    events: list[Kline] = [
        _kline("1m", index * MINUTE, MINUTE)
        for index in range(16 * 60 + extra_minutes)
    ]
    events.extend(
        _kline("5m", minute_start - (15 - index) * 5 * MINUTE, 5 * MINUTE)
        for index in range(15)
    )
    return events


def _signal_bars(bar_start: int) -> list[Bar1s]:
    """固定的 5 秒涨幅/成交量尖峰，和全成交案例使用同一冻结输入。"""
    bars = [_bar(bar_start + index * 1_000, "100", "100") for index in range(56)]
    bars.extend(
        _bar(
            bar_start + offset * 1_000,
            close,
            "120" if offset == 60 else close,
            "4",
        )
        for offset, close in enumerate(("100", "101", "102", "104", "106"), start=56)
    )
    return bars


def test_spike_replay_expires_unfilled_entry_window():
    """订单 TTL 到期时撤销入场计划，并保留可追溯的终态审计。"""
    minute_start = 16 * 60 * MINUTE
    events = _warmup_klines(minute_start)
    events.extend(_signal_bars(minute_start - MINUTE))
    # 触发后 1 秒挂出三档，但价格没有穿透任何档位。
    events.append(_bar(minute_start + 1_000, "100", "100"))
    # active_time + ORDER_TTL = signal_time + 181 秒。
    events.append(_bar(minute_start + 181_000, "100", "100"))
    events.sort(key=lambda event: (event.available_time, event.type_priority, event.symbol, event.sequence))

    strategy = DynamicSpikeBacktestStrategy(["BTCUSDT"], Decimal("1000"))
    result = BacktestEngine(
        strategy,
        events,
        BacktestConfig(trading_start_ms=minute_start + 1_000),
    ).run()

    assert len(result.orders) == 3
    assert all(order.status == "EXPIRED" for order in result.orders)
    assert not result.fills
    assert [event.event_type for event in result.audit_events] == [
        "signal_triggered",
        "entry_plan_created",
        "signal_expired",
    ]
    assert result.audit_events[-1].details == {"cancelled_orders": 0}


def test_spike_replay_rejects_second_signal_during_cooldown():
    """首个信号失效后，冷却窗口内的第二个合格尖峰不得再次入场。"""
    minute_start = 16 * 60 * MINUTE
    events = _warmup_klines(minute_start, extra_minutes=2)
    events.extend(_signal_bars(minute_start - MINUTE))
    # 第一信号不下单即触及失效价，确保之后没有活跃 campaign 干扰冷却断言。
    events.append(_bar(minute_start + 1_000, "100", "135"))
    # 第二次尖峰距第一次 120 秒，仍小于冻结的 180 秒冷却时间。
    for timestamp in range(minute_start + 2_000, minute_start + 116_000, 1_000):
        events.append(_bar(timestamp, "100", "100"))
    events.extend(
        _bar(minute_start + offset * 1_000, close, close, "4")
        for offset, close in enumerate(("100", "101", "102", "104", "106"), start=116)
    )
    events.sort(key=lambda event: (event.available_time, event.type_priority, event.symbol, event.sequence))

    strategy = DynamicSpikeBacktestStrategy(["BTCUSDT"], Decimal("1000"))
    result = BacktestEngine(
        strategy,
        events,
        BacktestConfig(trading_start_ms=minute_start + 1_000),
    ).run()

    assert len(result.orders) == 0
    assert not result.fills
    assert [event.event_type for event in result.audit_events] == [
        "signal_triggered",
        "entry_plan_created",
        "signal_invalidated",
    ]
