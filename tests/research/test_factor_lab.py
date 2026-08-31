from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_platform.research.factor_lab.analysis import analyze_factor
from trading_platform.research.factor_lab.correlation import (
    factor_correlation_matrix,
    high_correlation_pairs,
)
from trading_platform.research.factor_lab.dataset import build_event_dataset, build_factor_frame
from trading_platform.research.factor_lab.derivatives import attach_derivative_factors
from trading_platform.research.factor_lab.event import SpikeEventConfig
from trading_platform.research.factor_lab.horizon import analyze_signal_horizon
from trading_platform.research.factor_lab.labels import SpikeLabelConfig
from trading_platform.research.factor_lab.lift import (
    apply_quantile_pair_rule,
    base_rate_stats,
    evaluate_time_oos_bands,
    fit_quantile_pair_rule,
    mfe_mae_potential_score,
    quantile_lift,
    render_lift_report,
    render_time_oos_report,
    rule_combination_lifts,
    scan_factor_lifts,
    terrain_table,
    threshold_sensitivity,
)
from trading_platform.research.factor_lab.pre_event import (
    add_pre_spike_factors,
    cooldown_alert_mask,
    event_capture_stats,
    future_event_labels,
    recent_event_mask,
)
from trading_platform.research.factor_lab.report import render_factor_report


def _bars(count: int = 140) -> pd.DataFrame:
    timestamps = np.arange(count, dtype=np.int64) * 1_000
    close = np.full(count, 100.0)
    volume = np.ones(count)
    buy = np.full(count, 0.6)
    sell = np.full(count, 0.4)

    # 65 秒出现 5s > 5% 的异动；61~65 秒同时爆量。
    close[65:] = 106.0
    volume[61:66] = 10.0
    buy[61:66] = 8.0
    sell[61:66] = 2.0
    # 事件后回落，为做空标签提供正 MFE。
    close[70:] = 100.0

    high = close.copy()
    low = close.copy()
    high[65:70] = 107.0
    low[70:] = 99.0
    quote_volume = volume * close
    return pd.DataFrame({
        "symbol": "TESTUSDT",
        "timestamp_ms": timestamps,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "vwap": close,
        "quote_volume": quote_volume,
        "trade_count": np.full(count, 2),
        "raw_trade_count": np.full(count, 4),
        "taker_buy_volume": buy,
        "taker_sell_volume": sell,
        "taker_buy_quote_volume": buy * close,
        "taker_sell_quote_volume": sell * close,
        "taker_buy_trade_count": np.full(count, 2),
        "taker_sell_trade_count": np.full(count, 2),
        "taker_buy_agg_trade_count": np.full(count, 1),
        "taker_sell_agg_trade_count": np.full(count, 1),
        "max_agg_trade_quantity": np.maximum(buy, sell),
        "max_taker_buy_agg_trade_quantity": buy,
        "max_taker_sell_agg_trade_quantity": sell,
    })


def test_build_factor_frame_uses_existing_orderflow_without_new_storage() -> None:
    factors = build_factor_frame(_bars())
    event_row = factors[factors["timestamp_ms"] == 65_000].iloc[0]

    assert event_row["rise_5s"] == pytest.approx(0.06)
    assert event_row["volume_multiple_5s"] == pytest.approx(10.0)
    assert event_row["cvd_5s"] == pytest.approx(30.0)
    assert event_row["taker_buy_ratio_5s"] == pytest.approx(0.8)
    assert event_row["quote_cvd_5s"] > 0


def test_build_event_dataset_separates_causal_factors_from_future_labels() -> None:
    bars = _bars()
    dataset = build_event_dataset(
        bars,
        event_config=SpikeEventConfig(
            rise_threshold=0.05,
            volume_multiple_threshold=5.0,
            cooldown_seconds=60,
        ),
        label_config=SpikeLabelConfig(
            horizons_seconds=(10, 30),
            success_horizon_seconds=30,
            success_mfe_threshold=0.02,
        ),
    )

    assert len(dataset) == 1
    event = dataset.iloc[0]
    assert event["timestamp_ms"] == 65_000
    assert event["short_mfe_30s"] > 0.02
    assert bool(event["success"]) is True
    assert event["event_id"] == "TESTUSDT:65000"


def test_labels_drop_right_censored_horizon() -> None:
    bars = _bars(count=80)
    dataset = build_event_dataset(
        bars,
        event_config=SpikeEventConfig(
            rise_threshold=0.05,
            volume_multiple_threshold=5.0,
            cooldown_seconds=60,
        ),
        label_config=SpikeLabelConfig(
            horizons_seconds=(30,),
            success_horizon_seconds=30,
        ),
    )
    assert len(dataset) == 1
    assert pd.isna(dataset.iloc[0]["short_mfe_30s"])
    assert int(dataset.iloc[0]["future_observations_30s"]) == 0


def test_event_requires_continuous_history() -> None:
    bars = _bars().drop(index=[40]).reset_index(drop=True)
    dataset = build_event_dataset(
        bars,
        event_config=SpikeEventConfig(
            rise_threshold=0.05,
            volume_multiple_threshold=5.0,
        ),
        label_config=SpikeLabelConfig(
            horizons_seconds=(30,),
            success_horizon_seconds=30,
        ),
    )
    assert dataset.empty


def test_chunk_boundary_does_not_duplicate_an_event_started_before_chunk() -> None:
    bars = _bars()
    dataset = build_event_dataset(
        bars,
        event_config=SpikeEventConfig(
            rise_threshold=0.05,
            volume_multiple_threshold=5.0,
            cooldown_seconds=60,
        ),
        label_config=SpikeLabelConfig(
            horizons_seconds=(30,),
            success_horizon_seconds=30,
        ),
        event_start_ms=66_000,
        event_end_ms=100_000,
    )
    assert dataset.empty


def test_derivative_join_uses_metrics_available_time_not_snapshot_time() -> None:
    events = pd.DataFrame({
        "event_id": ["TESTUSDT:400000"],
        "symbol": ["TESTUSDT"],
        "timestamp_ms": [400_000],
        "return_300s": [0.08],
        "close": [108.0],
    })
    metrics = pd.DataFrame({
        "symbol": ["TESTUSDT", "TESTUSDT", "TESTUSDT"],
        "snapshot_time_ms": [0, 300_000, 600_000],
        # 第三条 snapshot 虽然时间较晚，本身也尚未 available，不能被事件看到。
        "available_time_ms": [300_000, 600_000, 900_000],
        "sum_open_interest": [100.0, 120.0, 150.0],
        "sum_open_interest_value": [1_000.0, 1_300.0, 1_800.0],
        "count_toptrader_long_short_ratio": [1.0, 1.2, 1.4],
        "sum_toptrader_long_short_ratio": [1.0, 1.1, 1.2],
        "count_long_short_ratio": [1.0, 1.3, 1.5],
        "sum_taker_long_short_vol_ratio": [1.0, 1.4, 1.6],
    })

    joined = attach_derivative_factors(events, metrics)
    row = joined.iloc[0]
    assert row["metrics_available_time_ms"] == 300_000
    assert row["sum_open_interest"] == 100.0
    assert row["metrics_age_ms"] == 100_000


def test_derivative_join_drops_stale_metrics() -> None:
    events = pd.DataFrame({
        "event_id": ["TESTUSDT:1000000"],
        "symbol": ["TESTUSDT"],
        "timestamp_ms": [1_000_000],
        "return_300s": [0.08],
        "close": [108.0],
    })
    metrics = pd.DataFrame({
        "symbol": ["TESTUSDT"],
        "snapshot_time_ms": [0],
        "available_time_ms": [300_000],
        "sum_open_interest": [100.0],
        "sum_open_interest_value": [1_000.0],
        "count_toptrader_long_short_ratio": [1.0],
        "sum_toptrader_long_short_ratio": [1.0],
        "count_long_short_ratio": [1.0],
        "sum_taker_long_short_vol_ratio": [1.0],
    })
    joined = attach_derivative_factors(events, metrics, max_age_ms=600_000)
    assert joined.iloc[0]["metrics_age_ms"] == 700_000
    assert pd.isna(joined.iloc[0]["sum_open_interest"])


def test_factor_analysis_and_correlation_report() -> None:
    count = 40
    dataset = pd.DataFrame({
        "timestamp_ms": np.arange(count, dtype=np.int64) * 31 * 24 * 3_600_000,
        "factor_good": np.arange(count, dtype=float),
        "factor_duplicate": np.arange(count, dtype=float) * 2,
        "factor_noise": np.tile([0.0, 1.0], count // 2),
        "short_mfe_30m": np.arange(count, dtype=float) / 100.0,
    })
    result = analyze_factor(
        dataset,
        "factor_good",
        target="short_mfe_30m",
        min_bucket_samples=3,
    )
    assert result.spearman_ic == pytest.approx(1.0)
    assert result.samples == count
    assert len(result.quantiles) == 5

    matrix = factor_correlation_matrix(
        dataset, ["factor_good", "factor_duplicate", "factor_noise"]
    )
    pairs = high_correlation_pairs(matrix, threshold=0.9)
    assert len(pairs) == 1
    assert pairs.iloc[0]["factor_a"] == "factor_good"
    assert pairs.iloc[0]["factor_b"] == "factor_duplicate"

    summary = pd.DataFrame([result.as_summary()])
    report = render_factor_report(
        summary,
        {"factor_good": result},
        target="short_mfe_30m",
        event_count=count,
        correlation_pairs=pairs,
    )
    assert "Spike Factor Lab Report" in report
    assert "factor_good" in report


def test_signal_horizon_reports_peak_and_discrete_half_life() -> None:
    count = 80
    factor = np.arange(count, dtype=float)
    alternating = np.tile([1.0, -1.0], count // 2)
    dataset = pd.DataFrame({
        "timestamp_ms": np.arange(count, dtype=np.int64) * 86_400_000,
        "factor": factor,
        "short_return_5m": factor,
        "short_return_15m": factor * 0.8 + alternating * 5,
        "short_return_30m": alternating,
        "short_return_1h": alternating[::-1],
    })

    result = analyze_signal_horizon(dataset, "factor")

    assert result.peak_horizon_seconds == 300
    assert result.half_life_seconds == 1_800
    assert list(result.points["horizon_seconds"]) == [300, 900, 1_800, 3_600]


def _lift_dataset(count: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    strong = rng.uniform(0.0, 1.0, count)
    weak = rng.uniform(0.0, 1.0, count)
    target = 0.01 + strong * 0.04 + weak * 0.005
    return pd.DataFrame({
        "timestamp_ms": np.arange(count, dtype=np.int64) * 3_600_000,
        "symbol": "TESTUSDT",
        "close": np.full(count, 100.0),
        "rise_5s": 0.05 + strong * 0.2,
        "volume_multiple_5s": 5.0 + weak * 40.0,
        "factor_strong": strong,
        "factor_weak": weak,
        "short_mfe_30m": target,
        "short_mae_30m": 0.02 - strong * 0.005,
        "success": target > 0.03,
    })


def test_base_rate_stats_reports_center_and_success() -> None:
    dataset = _lift_dataset()
    stats = base_rate_stats(dataset, "short_mfe_30m")
    assert stats["samples"] == len(dataset)
    assert stats["mean"] == pytest.approx(float(dataset["short_mfe_30m"].mean()))
    assert stats["success_rate"] == pytest.approx(float(dataset["success"].mean()))


def test_quantile_lift_top_bucket_beats_base() -> None:
    table = quantile_lift(_lift_dataset(), "factor_strong", "short_mfe_30m", quantiles=4, min_bucket=10)
    assert {"lift_mean", "lift_valid_mean", "lift_median", "lift_valid_median"}.issubset(table.columns)
    assert table["lift_mean"].iloc[-1] > 1.0
    assert table["lift_mean"].iloc[0] < 1.0


def test_time_oos_bands_keep_train_thresholds_under_distribution_shift() -> None:
    dataset = pd.DataFrame(
        {
            "timestamp_ms": np.arange(24, dtype=np.int64) * 1_000,
            "factor": [*np.arange(18, dtype=float), *np.arange(100, 106, dtype=float)],
            "short_mfe_5s": [*np.linspace(0.01, 0.18, 18), *np.linspace(0.2, 0.7, 6)],
            "short_mae_5s": [*np.linspace(0.2, 0.03, 18), *np.linspace(0.08, 0.03, 6)],
        }
    )

    result = evaluate_time_oos_bands(
        dataset,
        ["factor"],
        split_ms=18_000,
        target="short_mfe_5s",
        quantiles=3,
    )

    top = result[result["band"] == "Q3/3"].iloc[0]
    assert top["train_samples"] == 13
    assert top["test_samples"] == 6
    # Every shifted test value remains above the threshold fitted on train.
    # Re-running qcut on test would incorrectly select only two rows.
    assert top["test_selected"] == 6
    assert top["test_coverage"] == pytest.approx(1.0)
    assert top["base_mfe"] == pytest.approx(top["selected_mfe"])
    assert top["selected_mae"] == pytest.approx(
        dataset.loc[18:, "short_mae_5s"].mean()
    )

    report = render_time_oos_report(
        result,
        split_ms=18_000,
        target="short_mfe_5s",
    )
    assert "Factor Time OOS Report" in report
    assert "Train N" in report
    assert "Selected MAE" in report


def test_quantile_lift_reports_global_and_valid_subset_bases() -> None:
    dataset = pd.DataFrame({
        "factor": [0.0, 1.0, np.nan, np.nan],
        "target": [1.0, 2.0, 100.0, 100.0],
    })
    table = quantile_lift(dataset, "factor", "target", quantiles=2, min_bucket=1)
    assert table["lift_mean"].iloc[-1] < 1.0
    assert table["lift_valid_mean"].iloc[-1] > 1.0


def test_scan_factor_lifts_orders_by_top_lift() -> None:
    summary, details = scan_factor_lifts(
        _lift_dataset(), ["factor_strong", "factor_weak"], "short_mfe_30m"
    )
    assert summary.iloc[0]["factor"] == "factor_strong"
    assert bool(summary.iloc[0]["monotonic"]) is True
    assert set(details) == {"factor_strong", "factor_weak"}


def test_terrain_table_counts_cells() -> None:
    terrain = terrain_table(_lift_dataset(), "short_mfe_30m")
    assert int(terrain["samples"].sum()) == 120
    assert {"rise_bin", "volume_bin", "samples", "mean", "median"}.issubset(terrain.columns)


def test_rule_combination_lifts_sorted_descending() -> None:
    rules = rule_combination_lifts(
        _lift_dataset(),
        ["factor_strong", "factor_weak"],
        "short_mfe_30m",
        quantiles=3,
        min_samples=10,
    )
    assert not rules.empty
    lifts = rules["lift_mean"].to_numpy(float)
    assert np.all(np.diff(lifts) <= 0)
    assert {"factor_a", "a_quantile", "factor_b", "b_quantile", "hit_rate"}.issubset(rules.columns)


def test_rule_combinations_use_pairwise_complete_cases() -> None:
    dataset = _lift_dataset()
    dataset["all_missing"] = np.nan
    rules = rule_combination_lifts(
        dataset,
        ["factor_strong", "factor_weak", "all_missing"],
        "short_mfe_30m",
        quantiles=3,
        min_samples=10,
    )
    assert not rules.empty
    assert set(rules["factor_a"]) | set(rules["factor_b"]) <= {
        "factor_strong",
        "factor_weak",
    }


def test_quantile_pair_rule_freezes_train_thresholds_for_test() -> None:
    train = pd.DataFrame({"a": np.arange(90.0), "b": np.arange(90.0)})
    test = pd.DataFrame({"a": np.arange(100.0, 190.0), "b": np.arange(100.0, 190.0)})
    rule = fit_quantile_pair_rule(
        train,
        factor_a="a",
        a_quantile=1,
        factor_b="b",
        b_quantile=1,
        quantiles=3,
    )
    assert int(apply_quantile_pair_rule(train, rule).sum()) > 0
    assert int(apply_quantile_pair_rule(test, rule).sum()) == 0


def test_threshold_sensitivity_covers_all_perturbations() -> None:
    table = threshold_sensitivity(_lift_dataset(), "factor_strong", "short_mfe_30m")
    assert list(table["quantile"]) == [0.6, 0.7, 0.8, 0.9]
    assert list(table["samples"]) == [48, 36, 24, 12]
    assert table["lift_mean"].iloc[0] > 1.0


def test_mfe_mae_potential_score_manual_check() -> None:
    dataset = pd.DataFrame({
        "short_mfe_30m": [0.05, 0.03],
        "short_mae_30m": [0.01, 0.02],
    })
    costs = mfe_mae_potential_score(
        dataset,
        pd.Series([True, True]),
        fee_rate=0.0005,
        slippage_rate=0.0005,
    )
    p_win = 1.0
    expected = p_win * 0.04 - 0.0 - 0.001
    assert costs["potential_score"] == pytest.approx(expected)


def test_render_lift_report_handles_empty_and_keyword_output() -> None:
    empty = render_lift_report(pd.DataFrame(), [], "short_mfe_30m")
    assert "无可用样本" in empty

    report = render_lift_report(
        _lift_dataset(),
        ["factor_strong", "factor_weak"],
        "short_mfe_30m",
    )
    for keyword in ("Factor Lift Report", "地形图", "单因子分位 lift", "两因子规则组合"):
        assert keyword in report


def test_future_event_labels_do_not_turn_censored_tail_into_negatives() -> None:
    hits, eligible = future_event_labels(np.array([False, False, True, False]), (2,))[2]
    assert list(eligible) == [True, True, False, False]
    assert list(hits[:2]) == [True, True]


def test_future_event_labels_do_not_cross_data_gaps() -> None:
    hits, eligible = future_event_labels(
        np.array([False, False, True, False]),
        (2,),
        segment_ids=np.array([1, 1, 2, 2]),
    )[2]
    assert bool(eligible[0]) is False
    assert bool(hits[0]) is False


def test_cooldown_alert_mask_clusters_dense_candidates() -> None:
    mask = cooldown_alert_mask(
        np.array([False, True, True, False, True, False, False, True]),
        cooldown_bars=2,
    )
    assert list(np.flatnonzero(mask)) == [1, 7]


def test_cooldown_alert_mask_resets_after_data_gap() -> None:
    mask = cooldown_alert_mask(
        np.array([False, True, True, True]),
        cooldown_bars=10,
        segment_ids=np.array([1, 1, 2, 2]),
    )
    assert list(np.flatnonzero(mask)) == [1, 2]


def test_recent_event_mask_respects_segments() -> None:
    recent = recent_event_mask(
        np.array([False, True, False, False]),
        lookback_bars=2,
        segment_ids=np.array([1, 1, 2, 2]),
    )
    assert list(recent) == [False, True, False, False]


def test_event_capture_stats_reports_recall_and_lead() -> None:
    stats = event_capture_stats(
        np.array([False, True, False, False, False]),
        np.array([False, False, False, True, False]),
        horizon_bars=3,
    )
    assert stats["events"] == 1
    assert stats["captured_events"] == 1
    assert stats["recall"] == pytest.approx(1.0)
    assert stats["mean_lead_bars"] == pytest.approx(2.0)


def test_pre_spike_factors_reset_at_one_minute_gap() -> None:
    count = 90
    timestamps = np.arange(count, dtype=np.int64) * 60_000
    timestamps[60:] += 60_000
    frame = pd.DataFrame({
        "open_ms": timestamps,
        "open": np.full(count, 100.0),
        "high": np.full(count, 101.0),
        "low": np.full(count, 99.0),
        "close": np.full(count, 100.0),
        "volume": np.full(count, 10.0),
    })
    factors = add_pre_spike_factors(frame)
    first_after_gap = factors[factors["open_ms"] == timestamps[60]].iloc[0]
    assert pd.isna(first_after_gap["atr_mult"])
    assert pd.isna(first_after_gap["return_5m"])
