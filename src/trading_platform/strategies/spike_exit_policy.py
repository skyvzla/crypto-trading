"""Spike 最新退出规则的环境无关状态机。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class ExitAction(str, Enum):
    HOLD = "hold"
    REDUCE_HALF = "reduce_half"
    EXIT_ALL = "exit_all"


@dataclass(frozen=True)
class ExitObservation:
    """由候选指标层产生的因果观测，不包含订单执行细节。"""

    event_time: int
    first_fill_time: int
    price: Decimal
    origin_price: Decimal
    decay_agreement: int | None = None
    time_risk: bool = False
    momentum_risk: bool = False
    stable_breakout_5m: bool = False
    stable_breakout_15m: bool = False


@dataclass(frozen=True)
class ExitDecision:
    action: ExitAction
    reason: str


@dataclass
class SpikeExitPolicyState:
    """D-016 至 D-020 的纯状态机；阈值由外部候选配置产生。"""

    min_risk_age_ms: int = 90_000
    decay_agreement_required: int = 2
    origin_checked: bool = False
    reduced_at_origin: bool = False
    exit_requested: bool = False

    def evaluate(self, observation: ExitObservation) -> ExitDecision:
        if observation.event_time < observation.first_fill_time:
            raise ValueError("exit observation predates first fill")
        if observation.origin_price <= 0 or observation.price <= 0:
            raise ValueError("exit prices must be positive")
        if self.exit_requested:
            return ExitDecision(ExitAction.HOLD, "exit_already_requested")

        if observation.stable_breakout_5m or observation.stable_breakout_15m:
            self.exit_requested = True
            timeframe = "5m" if observation.stable_breakout_5m else "15m"
            return ExitDecision(ExitAction.EXIT_ALL, f"stable_trend_breakout_{timeframe}")

        elapsed_ms = observation.event_time - observation.first_fill_time
        if elapsed_ms >= self.min_risk_age_ms and (
            observation.time_risk or observation.momentum_risk
        ):
            self.exit_requested = True
            reason = "time_risk" if observation.time_risk else "momentum_risk"
            return ExitDecision(ExitAction.EXIT_ALL, reason)

        if (
            not self.origin_checked
            and observation.price <= observation.origin_price
            and observation.decay_agreement is not None
        ):
            self.origin_checked = True
            if observation.decay_agreement >= self.decay_agreement_required:
                self.reduced_at_origin = True
                return ExitDecision(ExitAction.REDUCE_HALF, "origin_momentum_decay")
            return ExitDecision(ExitAction.HOLD, "origin_momentum_continues")

        return ExitDecision(ExitAction.HOLD, "no_exit_condition")

