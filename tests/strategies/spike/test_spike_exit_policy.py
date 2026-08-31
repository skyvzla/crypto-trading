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


@pytest.mark.parametrize(
    ("field", "reason"),
    [("hard_stop", "hard_stop"), ("gate_stop", "gate_stop")],
)
def test_immediate_stops_take_priority_and_latch_full_exit(field, reason):
    policy = SpikeExitPolicyState()
    decision = policy.evaluate(
        _observation(
            event_time=20_000,
            stable_breakout_5m=True,
            **{field: True},
        )
    )

    assert decision.action == ExitAction.EXIT_ALL
    assert decision.reason == reason
    assert policy.exit_requested is True
    assert policy.evaluate(_observation(event_time=21_000)).reason == (
        "exit_already_requested"
    )


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


def test_candidate_v1_flat_mode_uses_single_agreement_after_start():
    config = CandidateV1Config(
        risk_start_ms=180_000,
        medium_age_ms=180_000,
        strict_age_ms=180_000,
        flat_momentum_agreement=1,
    )
    assert config.momentum_agreement_required(0) is None
    assert config.momentum_agreement_required(179_999) is None
    assert config.momentum_agreement_required(180_000) == 1
    assert config.momentum_agreement_required(3_600_000) == 1


def test_candidate_v1_flat_mode_risk_and_time_risk_at_same_age():
    config = CandidateV1Config(
        risk_start_ms=180_000,
        medium_age_ms=180_000,
        strict_age_ms=180_000,
        flat_momentum_agreement=1,
    )
    assert candidate_v1_risks(
        elapsed_ms=180_000,
        decay_agreement=1,
        net_pnl=Decimal("1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
    ) == (False, True)
    assert candidate_v1_risks(
        elapsed_ms=180_000,
        decay_agreement=0,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
    ) == (True, False)
    assert candidate_v1_risks(
        elapsed_ms=120_000,
        decay_agreement=1,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
    ) == (False, False)


def test_candidate_v1_time_risk_grace_small_loss_defers_exit():
    config = CandidateV1Config(
        time_risk_grace_ms=900_000,
        time_risk_grace_loss_ratio=Decimal("0.01"),
    )
    notional = Decimal("1000")
    small_loss = Decimal("-5")
    big_loss = Decimal("-20")
    assert candidate_v1_risks(
        elapsed_ms=config.strict_age_ms,
        decay_agreement=0,
        net_pnl=small_loss,
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        notional=notional,
    )[0] is False
    assert candidate_v1_risks(
        elapsed_ms=config.strict_age_ms + 1,
        decay_agreement=0,
        net_pnl=small_loss,
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        notional=notional,
    )[0] is False


def test_candidate_v1_time_risk_grace_big_loss_still_exits():
    config = CandidateV1Config(
        time_risk_grace_ms=900_000,
        time_risk_grace_loss_ratio=Decimal("0.01"),
    )
    notional = Decimal("1000")
    assert candidate_v1_risks(
        elapsed_ms=config.strict_age_ms,
        decay_agreement=0,
        net_pnl=Decimal("-20"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        notional=notional,
    )[0] is True


def test_candidate_v1_time_risk_grace_expiry_exits():
    config = CandidateV1Config(
        time_risk_grace_ms=900_000,
        time_risk_grace_loss_ratio=Decimal("0.01"),
    )
    notional = Decimal("1000")
    assert candidate_v1_risks(
        elapsed_ms=config.strict_age_ms + 900_000,
        decay_agreement=0,
        net_pnl=Decimal("-5"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        notional=notional,
    )[0] is True


def test_candidate_v1_time_risk_grace_disabled_by_default():
    config = CandidateV1Config()
    assert candidate_v1_risks(
        elapsed_ms=config.strict_age_ms,
        decay_agreement=0,
        net_pnl=Decimal("-0.01"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
    )[0] is True


def test_candidate_v1_time_risk_grace_momentum_still_exits_during_grace():
    config = CandidateV1Config(
        time_risk_grace_ms=900_000,
        time_risk_grace_loss_ratio=Decimal("0.01"),
    )
    assert candidate_v1_risks(
        elapsed_ms=config.strict_age_ms,
        decay_agreement=1,
        net_pnl=Decimal("-5"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        notional=Decimal("1000"),
    ) == (False, True)


def test_candidate_v1_dynamic_strict_age_alive_uses_strong():
    config = CandidateV1Config(
        strict_age_ms=900_000,
        strong_strict_age_ms=1_500_000,
        weak_strict_age_ms=600_000,
    )
    assert candidate_v1_risks(
        elapsed_ms=1_500_000,
        decay_agreement=1,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
    )[0] is True
    assert candidate_v1_risks(
        elapsed_ms=900_000,
        decay_agreement=1,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
    )[0] is False


def test_candidate_v1_dynamic_strict_age_dead_uses_weak():
    config = CandidateV1Config(
        strict_age_ms=900_000,
        strong_strict_age_ms=1_500_000,
        weak_strict_age_ms=600_000,
    )
    assert candidate_v1_risks(
        elapsed_ms=600_000,
        decay_agreement=0,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
    )[0] is True
    assert candidate_v1_risks(
        elapsed_ms=599_000,
        decay_agreement=0,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
    )[0] is False
    assert candidate_v1_risks(
        elapsed_ms=1_500_000,
        decay_agreement=0,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
    )[0] is True


def test_candidate_v1_dynamic_strict_age_defaults_to_strict():
    config = CandidateV1Config(strong_strict_age_ms=None, weak_strict_age_ms=None)
    assert candidate_v1_risks(
        elapsed_ms=config.strict_age_ms,
        decay_agreement=1,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
    )[0] is True


def test_candidate_v1_static_bucket_strong_uses_strong_bucket_age():
    config = CandidateV1Config(
        strict_age_ms=900_000,
        strong_strict_age_ms=1_500_000,
        weak_strict_age_ms=600_000,
        strong_bucket_strict_age_ms=1_200_000,
        weak_bucket_strict_age_ms=300_000,
    )
    assert candidate_v1_risks(
        elapsed_ms=1_200_000,
        decay_agreement=1,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        entry_bucket="strong",
    )[0] is True
    assert candidate_v1_risks(
        elapsed_ms=1_100_000,
        decay_agreement=1,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        entry_bucket="strong",
    )[0] is False


def test_candidate_v1_static_bucket_weak_uses_weak_bucket_age():
    config = CandidateV1Config(
        strict_age_ms=900_000,
        strong_strict_age_ms=1_500_000,
        weak_strict_age_ms=600_000,
        strong_bucket_strict_age_ms=1_200_000,
        weak_bucket_strict_age_ms=300_000,
    )
    assert candidate_v1_risks(
        elapsed_ms=300_000,
        decay_agreement=0,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        entry_bucket="weak",
    )[0] is True
    assert candidate_v1_risks(
        elapsed_ms=299_000,
        decay_agreement=0,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        entry_bucket="weak",
    )[0] is False


def test_candidate_v1_static_bucket_overrides_dynamic_decay_agreement():
    config = CandidateV1Config(
        strict_age_ms=900_000,
        strong_strict_age_ms=1_500_000,
        weak_strict_age_ms=600_000,
        strong_bucket_strict_age_ms=1_200_000,
        weak_bucket_strict_age_ms=300_000,
    )
    assert candidate_v1_risks(
        elapsed_ms=1_100_000,
        decay_agreement=1,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        entry_bucket="weak",
    )[0] is True
    assert candidate_v1_risks(
        elapsed_ms=600_000,
        decay_agreement=0,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        entry_bucket="strong",
    )[0] is False


def test_candidate_v1_static_bucket_disabled_when_none():
    config = CandidateV1Config(
        strict_age_ms=900_000,
        strong_strict_age_ms=1_500_000,
        weak_strict_age_ms=600_000,
        strong_bucket_strict_age_ms=None,
        weak_bucket_strict_age_ms=None,
    )
    assert candidate_v1_risks(
        elapsed_ms=900_000,
        decay_agreement=1,
        net_pnl=Decimal("-1"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        entry_bucket="strong",
    )[0] is True


def test_candidate_v1_profit_unlock_ratio_skips_time_tiers():
    config = CandidateV1Config(profit_unlock_ratio=Decimal("0.10"))
    assert candidate_v1_risks(
        elapsed_ms=60_000,
        decay_agreement=1,
        net_pnl=Decimal("150"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        notional=Decimal("1000"),
        profit_unlocked=True,
    )[1] is True
    assert candidate_v1_risks(
        elapsed_ms=60_000,
        decay_agreement=1,
        net_pnl=Decimal("150"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        notional=Decimal("1000"),
        profit_unlocked=False,
    )[1] is False
    assert candidate_v1_risks(
        elapsed_ms=60_000,
        decay_agreement=2,
        net_pnl=Decimal("150"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        notional=Decimal("1000"),
        profit_unlocked=True,
    )[1] is True


def test_candidate_v1_profit_unlock_requires_positive_agreement():
    config = CandidateV1Config(profit_unlock_ratio=Decimal("0.10"))
    assert candidate_v1_risks(
        elapsed_ms=60_000,
        decay_agreement=None,
        net_pnl=Decimal("150"),
        down_channel_5m=True,
        down_channel_15m=True,
        config=config,
        notional=Decimal("1000"),
        profit_unlocked=True,
    )[1] is False


def test_exit_state_profit_drawdown_priority():
    state = SpikeExitPolicyState()
    observation = ExitObservation(
        event_time=60_000,
        first_fill_time=0,
        price=Decimal("100"),
        origin_price=Decimal("110"),
        decay_agreement=None,
        profit_drawdown=True,
    )
    decision = state.evaluate(observation)
    assert decision.action == ExitAction.EXIT_ALL
    assert decision.reason == "profit_drawdown"


def test_exit_state_profit_drawdown_no_trigger_when_false():
    state = SpikeExitPolicyState()
    observation = ExitObservation(
        event_time=60_000,
        first_fill_time=0,
        price=Decimal("100"),
        origin_price=Decimal("110"),
        decay_agreement=None,
        profit_drawdown=False,
    )
    assert state.evaluate(observation).action == ExitAction.HOLD
