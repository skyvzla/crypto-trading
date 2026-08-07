import numpy as np
import pandas as pd

from trading_platform.research.spike_exit_calibration import (
    CalibrationConfig,
    channel_breakout_candidates,
    momentum_indicators,
    write_outputs,
)


def candles(closes: list[float], interval_ms: int = 60_000) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open_ms": np.arange(len(closes)) * interval_ms,
            "available_ms": (np.arange(len(closes)) + 1) * interval_ms - 1,
            "open": closes,
            "high": np.asarray(closes) * 1.001,
            "low": np.asarray(closes) * 0.999,
            "close": closes,
            "volume": 1.0,
        }
    )


def test_momentum_indicators_identify_slowing_down_move():
    first = np.exp(np.linspace(np.log(100), np.log(80), 50))
    slowing_returns = -0.002 * np.power(0.82, np.arange(20))
    slowing = 80 * np.exp(np.cumsum(slowing_returns))

    result = momentum_indicators(
        candles([*first, *slowing]), CalibrationConfig()
    )

    assert result.iloc[-1]["fast_log_slope_z"] < 0
    assert result.iloc[-1]["slow_log_slope_z"] < 0
    assert result.iloc[-1]["down_speed_ratio"] < 1
    assert np.isfinite(result.iloc[-1]["macd_hist_change_bps"])
    assert np.isfinite(result.iloc[-1]["adx"])


def test_channel_breakout_uses_prior_bars_and_requires_confirmation():
    descending = [100 - index for index in range(12)]
    result = channel_breakout_candidates(
        candles([*descending, 96, 97], interval_ms=300_000),
        lookback=6,
        width_sigma=0.0,
        stable_closes=2,
    )

    assert bool(result.iloc[-2]["channel_break_probe"])
    assert not bool(result.iloc[-2]["stable_breakout_probe"])
    assert bool(result.iloc[-1]["stable_breakout_probe"])
    assert result.iloc[-1]["stable_upper_excess_bps"] > 0


def test_channel_breakout_does_not_label_rising_channel():
    result = channel_breakout_candidates(
        candles(list(range(80, 100)), interval_ms=900_000),
        lookback=8,
        width_sigma=0.0,
        stable_closes=2,
    )

    assert not result["channel_break_probe"].any()


def test_momentum_calculation_is_causal():
    source = candles(list(np.linspace(100, 80, 80)))
    baseline = momentum_indicators(source, CalibrationConfig())
    changed = source.copy()
    changed.loc[60:, ["open", "high", "low", "close"]] *= 3

    recalculated = momentum_indicators(changed, CalibrationConfig())

    pd.testing.assert_series_equal(
        baseline.loc[:59, "fast_log_slope_z"],
        recalculated.loc[:59, "fast_log_slope_z"],
    )
    pd.testing.assert_series_equal(
        baseline.loc[:59, "macd_hist_bps"],
        recalculated.loc[:59, "macd_hist_bps"],
    )


def test_output_files_are_deterministic_and_marked_research_only(tmp_path):
    campaigns = pd.DataFrame(
        [
            {
                "campaign_id": "campaign-1",
                "origin_reached": False,
                "first_fill_ms": 0,
                "origin_touch_ms": np.nan,
                "minutes_to_origin": np.nan,
                "origin_down_speed_ratio": np.nan,
                "origin_macd_hist_change_bps": np.nan,
                "origin_adx": np.nan,
                "origin_minus_di": np.nan,
                "origin_decay_probe_agreement": np.nan,
                "5m_minutes_origin_to_breakout": np.nan,
                "15m_minutes_origin_to_breakout": np.nan,
            }
        ]
    )
    snapshots = pd.DataFrame([{"campaign_id": "campaign-1", "close": 1.0}])
    summary = {
        "research_only": True,
        "production_parameters_frozen": False,
        "config": {
            **CalibrationConfig().__dict__,
            "snapshot_minutes": list(CalibrationConfig().snapshot_minutes),
            "entry_snapshot_seconds": list(CalibrationConfig().entry_snapshot_seconds),
        },
        "coverage": {},
        "campaigns": {
            "filled": 1,
            "origin_reached": 0,
            "origin_not_reached_right_censored": 1,
            "minutes_to_origin": {},
        },
        "origin_momentum_quantiles": {},
        "origin_momentum_samples": {},
        "origin_decay_probe_agreement_counts": {},
        "entry_elapsed_momentum": {},
        "trend_breakout_candidates": {},
        "review_24h_candidates": {
            "observed": 0,
            "right_censored_at_month_end": 1,
            "5m_down_channel": 0,
            "15m_down_channel": 0,
            "both_timeframes_down_channel": 0,
        },
    }

    write_outputs(tmp_path, campaigns, snapshots, summary)
    first = (tmp_path / "summary.json").read_bytes()
    write_outputs(tmp_path, campaigns, snapshots, summary)

    assert (tmp_path / "summary.json").read_bytes() == first
    assert b'"production_parameters_frozen": false' in first
    assert b'"research_only": true' in first
