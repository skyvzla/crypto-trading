"""让 Spike 回测使用与线上相同的动态资金池规则。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading_platform.shared.events import Bar1s, Fill, Kline, OrderIntent
from trading_platform.strategies.spike.capital import (
    CapitalPolicy,
    CapitalPolicyConfig,
    CapitalSettlement,
    CapitalState,
)


@dataclass(slots=True)
class _CampaignFills:
    symbol: str
    entry_quantity: Decimal = Decimal("0")
    exit_quantity: Decimal = Decimal("0")
    entry_quote: Decimal = Decimal("0")
    exit_quote: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")

    def add(self, fill: Fill) -> None:
        if fill.symbol != self.symbol:
            raise RuntimeError("one Capital Campaign cannot span multiple symbols")
        if fill.commission_asset != "USDT":
            raise RuntimeError("capital replay requires USDT-denominated commission")
        if fill.side == "SELL":
            if self.exit_quantity:
                raise RuntimeError("entry fill arrived after Campaign exit started")
            self.entry_quantity += fill.quantity
            self.entry_quote += fill.price * fill.quantity
        else:
            if self.entry_quantity <= 0:
                raise RuntimeError("exit fill arrived before Campaign entry")
            self.exit_quantity += fill.quantity
            self.exit_quote += fill.price * fill.quantity
            if self.exit_quantity > self.entry_quantity:
                raise RuntimeError("Campaign exit quantity exceeds entry quantity")
        self.commission += fill.commission

    @property
    def closed(self) -> bool:
        return self.entry_quantity > 0 and self.exit_quantity == self.entry_quantity

    @property
    def net_pnl(self) -> Decimal:
        return self.entry_quote - self.exit_quote - self.commission + self.funding


class CapitalManagedSpikeStrategy:
    """组合现有 Spike 策略，在完整平仓后更新下一轮名义金额。"""

    def __init__(self, delegate: Any, config: CapitalPolicyConfig) -> None:
        self.delegate = delegate
        self.policy = CapitalPolicy(config)
        self.capital_state = self.policy.initial_state()
        self.settlements: list[CapitalSettlement] = []
        self._campaign: _CampaignFills | None = None
        self._external_entry_enabled = True
        self._set_order_notional(self.capital_state.trading_capital)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def bind_account(self, account: Any) -> None:
        self.delegate.bind_account(account)

    def set_entry_enabled(self, enabled: bool) -> None:
        self._external_entry_enabled = bool(enabled)
        self._apply_entry_gate()

    def on_bar1s(self, bar: Bar1s) -> list[OrderIntent]:
        self._apply_entry_gate()
        return self.delegate.on_bar1s(bar)

    def on_kline(self, kline: Kline) -> list[OrderIntent]:
        return self.delegate.on_kline(kline)

    def on_fill(self, fill: Fill) -> None:
        self.delegate.on_fill(fill)
        if self._campaign is None:
            if fill.side != "SELL":
                raise RuntimeError("exit fill arrived without an active Capital Campaign")
            self._campaign = _CampaignFills(fill.symbol)
        self._campaign.add(fill)
        if not self._campaign.closed:
            return
        settlement = self.policy.settle(
            self.capital_state, self._campaign.net_pnl
        )
        self.capital_state = settlement.state_after
        self.settlements.append(settlement)
        self._campaign = None
        self._set_order_notional(self.capital_state.trading_capital)
        self._apply_entry_gate()

    def add_funding(self, amount: Decimal) -> None:
        """把当前 Campaign 的资金费事实计入净盈亏（收入为正、支出为负）。"""

        if self._campaign is None:
            raise RuntimeError("funding requires an active Capital Campaign")
        self._campaign.funding += Decimal(amount)

    def _set_order_notional(self, amount: Decimal) -> None:
        for strategy in self.delegate.strategies.values():
            strategy.total_notional = amount

    def _apply_entry_gate(self) -> None:
        self.delegate.set_entry_enabled(
            self._external_entry_enabled and self.policy.can_open(self.capital_state)
        )
