from decimal import Decimal

import pandas as pd
from pandas.testing import assert_frame_equal

from trading_platform.research.spike_exit_calibration import (
    CalibrationConfig,
    channel_breakout_candidates as research_channels,
    momentum_indicators as research_momentum,
)
from trading_platform.shared.events import Kline
from trading_platform.strategies.spike_exit_features import (
    CandidateFeatureConfig,
    candidate_feature_snapshot,
    channel_breakout_candidates,
    momentum_indicators,
)


def _klines(interval: str, count: int, step_ms: int, *, descending: bool = False):
    result = []
    for index in range(count):
        center = Decimal("120") - Decimal(index) if descending else Decimal("100") + Decimal(index) / 10
        result.append(
            Kline(
                symbol="AKEUSDT",
                interval=interval,
                open_time=index * step_ms,
                close_time=(index + 1) * step_ms - 1,
                available_time=(index + 1) * step_ms,
                open=center,
                high=center + Decimal("1"),
                low=center - Decimal("1"),
                close=center,
                volume=Decimal("10"),
            )
        )
    return result


def test_candidate_features_require_completed_momentum_history():
    config = CandidateFeatureConfig()
    snapshot = candidate_feature_snapshot(
        _klines("1m", 40, 60_000),
        _klines("5m", 14, 300_000, descending=True),
        _klines("15m", 10, 900_000, descending=True),
        config=config,
    )

    assert snapshot is not None
    assert snapshot.event_time == 40 * 60_000
    assert snapshot.decay_agreement is not None
    assert snapshot.down_channel_5m is True
    assert snapshot.down_channel_15m is True


def test_candidate_features_report_missing_channel_without_guessing():
    snapshot = candidate_feature_snapshot(
        _klines("1m", 40, 60_000),
        [],
        [],
        config=CandidateFeatureConfig(),
    )
    assert snapshot is not None
    assert snapshot.down_channel_5m is None
    assert snapshot.down_channel_15m is None
    assert snapshot.stable_breakout_5m is False
    assert snapshot.stable_breakout_15m is False


def test_online_feature_math_matches_research_candidate_definitions():
    frame = pd.DataFrame(
        [
            {
                "available_ms": (index + 1) * 60_000,
                "open": 100 + index / 10,
                "high": 101 + index / 10,
                "low": 99 + index / 10,
                "close": 100 + index / 10 + ((index % 4) - 2) / 20,
                "volume": 10,
            }
            for index in range(60)
        ]
    )
    online_config = CandidateFeatureConfig()
    research_config = CalibrationConfig()

    assert_frame_equal(
        momentum_indicators(frame, online_config),
        research_momentum(frame, research_config),
    )
    assert_frame_equal(
        channel_breakout_candidates(
            frame,
            lookback=online_config.channel_5m_bars,
            width_sigma=online_config.channel_width_sigma,
            stable_closes=online_config.stable_closes,
        ),
        research_channels(
            frame,
            lookback=research_config.channel_5m_bars,
            width_sigma=research_config.channel_width_sigma,
            stable_closes=research_config.stable_closes,
        ),
    )
