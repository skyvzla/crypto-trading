from decimal import Decimal

import pytest

from trading_platform.strategies.spike_exit_policy import (
    ExitAction,
    ExitObservation,
    SpikeExitPolicyState,
)


def _observation(**changes) -> ExitObservation:
    values = {
        "event_time": 100_000,
        "first_fill_time": 10_000,
        "price": Decimal("90"),
        "origin_price": Decimal("90"),
    }
    values.update(changes)
    return ExitObservation(**values)


def test_origin_decay_reduces_once_and_continuing_momentum_holds():
    decayed = SpikeExitPolicyState()
    assert decayed.evaluate(_observation(decay_agreement=2)).action == ExitAction.REDUCE_HALF
    assert decayed.evaluate(_observation(decay_agreement=3)).action == ExitAction.HOLD

    continuing = SpikeExitPolicyState()
    decision = continuing.evaluate(_observation(decay_agreement=1))
    assert decision.action == ExitAction.HOLD
    assert decision.reason == "origin_momentum_continues"
    assert continuing.origin_checked is True


def test_missing_origin_features_waits_for_a_causal_observation():
    policy = SpikeExitPolicyState()
    assert policy.evaluate(_observation(decay_agreement=None)).action == ExitAction.HOLD
    assert policy.origin_checked is False
    assert policy.evaluate(_observation(decay_agreement=2)).action == ExitAction.REDUCE_HALF


@pytest.mark.parametrize("risk", ["time_risk", "momentum_risk"])
def test_time_or_momentum_risk_exits_after_90_seconds_only(risk):
    policy = SpikeExitPolicyState()
    before = _observation(event_time=99_999, decay_agreement=None, **{risk: True})
    assert policy.evaluate(before).action == ExitAction.HOLD

    at_threshold = _observation(event_time=100_000, decay_agreement=None, **{risk: True})
    decision = policy.evaluate(at_threshold)
    assert decision.action == ExitAction.EXIT_ALL
    assert decision.reason == risk
    assert policy.evaluate(at_threshold).reason == "exit_already_requested"


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("stable_breakout_5m", "stable_trend_breakout_5m"),
        ("stable_breakout_15m", "stable_trend_breakout_15m"),
    ],
)
def test_stable_trend_breakout_exits_all(field, reason):
    policy = SpikeExitPolicyState()
    decision = policy.evaluate(_observation(event_time=20_000, **{field: True}))
    assert decision.action == ExitAction.EXIT_ALL
    assert decision.reason == reason


def test_invalid_observation_is_rejected():
    policy = SpikeExitPolicyState()
    with pytest.raises(ValueError, match="predates"):
        policy.evaluate(_observation(event_time=9_999))
