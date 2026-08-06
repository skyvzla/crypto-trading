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

    summary = ResultAnalyzer(result).analyze()
    assert summary["orders"]["fill_rate"] == 1.0
    assert summary["positions"]["open"] == 1
    assert summary["pnl"]["total_commission"] > 0
