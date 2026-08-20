"""评分模块与评分准入/动态溢价测试。"""

from decimal import Decimal

import pytest

from trading_platform.strategies.spike.scoring import (
    compute_score,
    inverted_u_score,
    monotonic_score,
    premium_pct,
)

BOUNDS = dict(p20=4.0, p40=10.0, p60=16.0, p80=20.0, p95=30.0)
CONFIG = {
    "admission_threshold": 0.5,
    "dimensions": [
        {"feature": "vwap_dev_5m", "weight": 0.5, "mode": "inverted_u", **BOUNDS},
        {"feature": "rsi_5m", "weight": 0.5, "mode": "inverted_u", **BOUNDS},
    ],
}


class TestInvertedUScore:
    def test_below_p20_zero(self):
        assert inverted_u_score(4.0, **BOUNDS) == 0.0

    def test_at_p40_six(self):
        assert inverted_u_score(10.0, **BOUNDS) == pytest.approx(0.6)

    def test_at_p60_one(self):
        assert inverted_u_score(16.0, **BOUNDS) == pytest.approx(1.0)

    def test_at_p80_seven(self):
        assert inverted_u_score(20.0, **BOUNDS) == pytest.approx(0.7)

    def test_above_p95_floor(self):
        assert inverted_u_score(40.0, **BOUNDS) == pytest.approx(0.2)

    def test_midpoint_between_p40_p60(self):
        assert inverted_u_score(13.0, **BOUNDS) == pytest.approx(0.8)

    def test_boundaries_must_increase(self):
        with pytest.raises(ValueError):
            inverted_u_score(5, p20=10, p40=10, p60=16, p80=20, p95=30)


class TestMonotonicScore:
    def test_below_low_zero(self):
        assert monotonic_score(0, low=1, high=8) == 0.0

    def test_above_high_one(self):
        assert monotonic_score(9, low=1, high=8) == 1.0

    def test_midpoint(self):
        assert monotonic_score(4.5, low=1, high=8) == pytest.approx(0.5)


class TestComputeScore:
    def test_weighted_sum(self):
        s = compute_score({"vwap_dev_5m": 13.0, "rsi_5m": 13.0}, CONFIG)
        assert s == pytest.approx(0.8)

    def test_missing_feature_zero_contribution(self):
        s = compute_score({"vwap_dev_5m": 13.0}, CONFIG)
        assert s == pytest.approx(0.4)

    def test_capped_at_one(self):
        cfg = {
            "dimensions": [
                {
                    "feature": "vwap_dev_5m",
                    "weight": 2.0,
                    "mode": "inverted_u",
                    **BOUNDS,
                }
            ]
        }
        assert compute_score({"vwap_dev_5m": 13.0}, cfg) == 1.0


class TestPremiumPct:
    def test_formula(self):
        p = premium_pct(0.8, predicted_up_pct=10.0, mult=0.7, base_pct=1.0, cap_pct=35.0)
        assert p == pytest.approx(6.6)

    def test_cap(self):
        p = premium_pct(1.0, predicted_up_pct=100.0, mult=1.0, base_pct=1.0, cap_pct=35.0)
        assert p == pytest.approx(35.0)

    def test_no_cap(self):
        p = premium_pct(1.0, predicted_up_pct=100.0, mult=1.0, base_pct=1.0, cap_pct=0.0)
        assert p == pytest.approx(101.0)

    def test_floor_base(self):
        p = premium_pct(0.0, predicted_up_pct=10.0, mult=0.7, base_pct=1.0, cap_pct=35.0)
        assert p == pytest.approx(1.0)