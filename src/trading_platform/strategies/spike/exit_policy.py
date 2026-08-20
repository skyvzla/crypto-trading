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
    profit_drawdown: bool = False
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

        elapsed_ms = observation.event_time - observation.first_fill_time
        if elapsed_ms >= self.min_risk_age_ms and (
            observation.time_risk or observation.momentum_risk
        ):
            self.exit_requested = True
            reason = "time_risk" if observation.time_risk else "momentum_risk"
            return ExitDecision(ExitAction.EXIT_ALL, reason)

        if observation.profit_drawdown:
            self.exit_requested = True
            return ExitDecision(ExitAction.EXIT_ALL, "profit_drawdown")

        return ExitDecision(ExitAction.HOLD, "no_exit_condition")


@dataclass(frozen=True)
class CandidateV1Config:
    """D-027 的 replay/testnet 候选阈值，不是生产参数。"""

    risk_start_ms: int = 90_000
    medium_age_ms: int = 300_000
    strict_age_ms: int = 900_000
    channel_review_ms: int = 24 * 60 * 60 * 1_000
    flat_momentum_agreement: int | None = None
    time_risk_grace_ms: int = 0
    time_risk_grace_loss_ratio: Decimal = Decimal("0.01")
    strong_strict_age_ms: int | None = None
    weak_strict_age_ms: int | None = None
    strong_bucket_strict_age_ms: int | None = None
    weak_bucket_strict_age_ms: int | None = None
    profit_unlock_ratio: Decimal | None = None
    profit_drawdown_ratio: Decimal | None = None
    profit_drawdown_peak_ratio: Decimal | None = None

    def momentum_agreement_required(self, elapsed_ms: int) -> int | None:
        if elapsed_ms < self.risk_start_ms:
            return None
        if self.flat_momentum_agreement is not None:
            return self.flat_momentum_agreement
        if elapsed_ms < self.medium_age_ms:
            return 3
        if elapsed_ms < self.strict_age_ms:
            return 2
        return 1


def candidate_v1_risks(
    *,
    elapsed_ms: int,
    decay_agreement: int | None,
    net_pnl: Decimal,
    down_channel_5m: bool | None,
    down_channel_15m: bool | None,
    config: CandidateV1Config = CandidateV1Config(),
    notional: Decimal | None = None,
    profit_unlocked: bool = False,
    entry_bucket: str | None = None,
) -> tuple[bool, bool]:
    """把候选指标转换成状态机的 time/momentum 风险输入。

    profit_unlocked: 由策略层粘滞维护——持仓期峰值浮盈曾达到
    profit_unlock_ratio 后一直为 True（价格回撤不解除），此时动量分档
    时间限制直接降到最低（required=1），即"盈利解锁弱化时间"。

    entry_bucket: 入场时静态确定的强弱桶（"strong"/"weak"，信号快照
    rise_from_12h_low 定桶，持仓期不变）。配置 strong_bucket_strict_age_ms /
    weak_bucket_strict_age_ms 时按静态桶分档，优先于动态 decay 分档。
    """
    alive = decay_agreement is not None and decay_agreement >= 1
    if entry_bucket == "strong":
        strict_age = (
            config.strict_age_ms
            if config.strong_bucket_strict_age_ms is None
            else config.strong_bucket_strict_age_ms
        )
    elif entry_bucket == "weak":
        strict_age = (
            config.strict_age_ms
            if config.weak_bucket_strict_age_ms is None
            else config.weak_bucket_strict_age_ms
        )
    else:
        strict_age = (
            config.strong_strict_age_ms
            if alive and config.strong_strict_age_ms is not None
            else config.weak_strict_age_ms
            if not alive and config.weak_strict_age_ms is not None
            else config.strict_age_ms
        )
    if profit_unlocked:
        required = 1 if decay_agreement is not None else None
    elif elapsed_ms < config.risk_start_ms:
        required = None
    elif config.flat_momentum_agreement is not None:
        required = config.flat_momentum_agreement
    elif elapsed_ms < config.medium_age_ms:
        required = 3
    elif elapsed_ms < strict_age:
        required = 2
    else:
        required = 1
    momentum_risk = (
        required is not None
        and decay_agreement is not None
        and decay_agreement >= required
    )
    time_risk = False
    if config.time_risk_grace_ms > 0 and elapsed_ms >= strict_age:
        grace_end_ms = strict_age + config.time_risk_grace_ms
        if elapsed_ms < grace_end_ms:
            loss_floor = (
                notional * config.time_risk_grace_loss_ratio
                if notional is not None
                else None
            )
            if loss_floor is not None:
                time_risk = net_pnl <= 0 and -net_pnl >= loss_floor
        else:
            time_risk = net_pnl <= 0
    else:
        time_risk = elapsed_ms >= strict_age and net_pnl <= 0
    if elapsed_ms >= config.channel_review_ms:
        confirmed_down_channel = bool(down_channel_5m and down_channel_15m)
        time_risk = time_risk or not confirmed_down_channel
    return time_risk, momentum_risk
