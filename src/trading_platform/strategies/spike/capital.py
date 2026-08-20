"""账户级动态资金池策略。

该模块只负责资金规则，不读取交易所或数据库。交易所成交、手续费和资金费
应在调用 :meth:`CapitalPolicy.settle` 前汇总为一个已确认的净盈亏事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class CapitalPolicyError(ValueError):
    """资金策略参数或状态不满足业务约束。"""


def _decimal(value: Decimal | int | float | str, *, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CapitalPolicyError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise CapitalPolicyError(f"{name} must be a finite decimal")
    return result


@dataclass(frozen=True, slots=True)
class CapitalPolicyConfig:
    """四个线上/回测共用的资金策略参数。"""

    initial_account_capital: Decimal | int | float | str
    initial_trading_capital: Decimal | int | float | str
    profit_reinvest_ratio: Decimal | int | float | str
    minimum_trading_capital: Decimal | int | float | str

    def __post_init__(self) -> None:
        account = _decimal(self.initial_account_capital, name="initial_account_capital")
        trading = _decimal(self.initial_trading_capital, name="initial_trading_capital")
        ratio = _decimal(self.profit_reinvest_ratio, name="profit_reinvest_ratio")
        minimum = _decimal(self.minimum_trading_capital, name="minimum_trading_capital")
        if account < 0:
            raise CapitalPolicyError("initial_account_capital must be >= 0")
        if trading < 0 or trading > account:
            raise CapitalPolicyError(
                "initial_trading_capital must be between 0 and initial_account_capital"
            )
        if ratio < 0 or ratio > 1:
            raise CapitalPolicyError("profit_reinvest_ratio must be between 0 and 1")
        if minimum < 0:
            raise CapitalPolicyError("minimum_trading_capital must be >= 0")
        object.__setattr__(self, "initial_account_capital", account)
        object.__setattr__(self, "initial_trading_capital", trading)
        object.__setattr__(self, "profit_reinvest_ratio", ratio)
        object.__setattr__(self, "minimum_trading_capital", minimum)


@dataclass(frozen=True, slots=True)
class CapitalState:
    """一个账户/策略的已结算资金状态。"""

    account_capital: Decimal
    trading_capital: Decimal
    reserve_capital: Decimal
    capital_breached: bool = False

    def __post_init__(self) -> None:
        values = (
            ("account_capital", self.account_capital),
            ("trading_capital", self.trading_capital),
            ("reserve_capital", self.reserve_capital),
        )
        for name, value in values:
            if not isinstance(value, Decimal) or not value.is_finite():
                raise CapitalPolicyError(f"{name} must be a finite Decimal")
        if self.trading_capital < 0:
            raise CapitalPolicyError("trading_capital must be >= 0")
        if self.account_capital != self.trading_capital + self.reserve_capital:
            raise CapitalPolicyError(
                "account_capital must equal trading_capital + reserve_capital"
            )


@dataclass(frozen=True, slots=True)
class CapitalSettlement:
    """一次已确认净盈亏的结算结果。"""

    net_pnl: Decimal
    state_before: CapitalState
    state_after: CapitalState
    reinvested_profit: Decimal
    reserve_change: Decimal
    reserve_consumed: Decimal
    event_type: str


class CapitalPolicy:
    """按动态交易资金池规则计算开仓额度和结算结果。"""

    def __init__(self, config: CapitalPolicyConfig) -> None:
        self.config = config

    def initial_state(self) -> CapitalState:
        reserve = self.config.initial_account_capital - self.config.initial_trading_capital
        return CapitalState(
            account_capital=self.config.initial_account_capital,
            trading_capital=self.config.initial_trading_capital,
            reserve_capital=reserve,
        )

    def can_open(self, state: CapitalState) -> bool:
        """低于或等于最低交易资金，或已发生资本越界时停止开仓。"""

        return (
            not state.capital_breached
            and state.trading_capital > self.config.minimum_trading_capital
        )

    def order_notional(self, state: CapitalState) -> Decimal:
        """每轮使用全部当前交易资金；杠杆不扩大名义金额。"""

        return state.trading_capital if self.can_open(state) else Decimal("0")

    def settle(
        self, state: CapitalState, net_pnl: Decimal | int | float | str
    ) -> CapitalSettlement:
        pnl = _decimal(net_pnl, name="net_pnl")
        if pnl > 0:
            reinvested = pnl * self.config.profit_reinvest_ratio
            reserve_change = pnl - reinvested
            trading_after = state.trading_capital + reinvested
            reserve_after = state.reserve_capital + reserve_change
            consumed = Decimal("0")
            event_type = "PROFIT_SETTLED"
        else:
            reinvested = Decimal("0")
            trading_after = state.trading_capital + pnl
            consumed = max(Decimal("0"), -trading_after)
            if consumed:
                # 交易池耗尽后，剩余损失如实从同一 Cross Margin 账户的储备中扣除。
                trading_after = Decimal("0")
                reserve_after = state.reserve_capital - consumed
                event_type = "CAPITAL_BREACH"
            else:
                reserve_after = state.reserve_capital
                event_type = "LOSS_SETTLED"
            reserve_change = reserve_after - state.reserve_capital

        after = CapitalState(
            account_capital=trading_after + reserve_after,
            trading_capital=trading_after,
            reserve_capital=reserve_after,
            capital_breached=state.capital_breached or event_type == "CAPITAL_BREACH",
        )
        return CapitalSettlement(
            net_pnl=pnl,
            state_before=state,
            state_after=after,
            reinvested_profit=reinvested,
            reserve_change=reserve_change,
            reserve_consumed=consumed,
            event_type=event_type,
        )
