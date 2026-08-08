from decimal import Decimal

import pytest

from trading_platform.strategies.spike.exit_policy import (
    CandidateV1Config,
    ExitAction,
    ExitObservation,
    SpikeExitPolicyState,
    candidate_v1_risks,
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


def test_origin_rule_has_priority_over_generic_momentum_exit():
    policy = SpikeExitPolicyState()
    decision = policy.evaluate(
        _observation(decay_agreement=3, momentum_risk=True)
    )
    assert decision.action == ExitAction.REDUCE_HALF


@pytest.mark.parametrize(
    ("elapsed_ms", "agreement", "expected"),
    [
        (89_999, 3, False),
        (90_000, 2, False),
        (90_000, 3, True),
        (300_000, 2, True),
        (900_000, 1, True),
    ],
)
def test_candidate_v1_momentum_threshold_tightens_with_time(
    elapsed_ms, agreement, expected
):
    _, momentum = candidate_v1_risks(
        elapsed_ms=elapsed_ms,
        decay_agreement=agreement,
        net_pnl=Decimal("1"),
        down_channel_5m=True,
        down_channel_15m=True,
    )
    assert momentum is expected


def test_candidate_v1_time_risk_and_24h_channel_review():
    config = CandidateV1Config()
    assert candidate_v1_risks(
        elapsed_ms=config.strict_age_ms,
        decay_agreement=0,
        net_pnl=Decimal("-0.01"),
        down_channel_5m=True,
        down_channel_15m=True,
    )[0] is True
    assert candidate_v1_risks(
        elapsed_ms=config.channel_review_ms,
        decay_agreement=0,
        net_pnl=Decimal("1"),
        down_channel_5m=True,
        down_channel_15m=False,
    )[0] is True
    assert candidate_v1_risks(
        elapsed_ms=config.channel_review_ms,
        decay_agreement=0,
        net_pnl=Decimal("1"),
        down_channel_5m=True,
        down_channel_15m=True,
    )[0] is False
