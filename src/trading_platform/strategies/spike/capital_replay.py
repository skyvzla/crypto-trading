"""让 Spike 回测使用与线上相同的动态资金池规则。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from trading_platform.backtest.funding import FundingIncomeEvent
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
    opened_at: int
    last_fill_time: int
    entry_quantity: Decimal = Decimal("0")
    exit_quantity: Decimal = Decimal("0")
    entry_quote: Decimal = Decimal("0")
    exit_quote: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")

    def add(self, fill: Fill) -> None:
        if fill.symbol != self.symbol:
            raise RuntimeError("one Capital Campaign cannot span multiple symbols")
        if fill.fill_time < self.last_fill_time:
            raise RuntimeError("Campaign fills must be ordered by fill_time")
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
        self.last_fill_time = fill.fill_time

    @property
    def closed(self) -> bool:
        return self.entry_quantity > 0 and self.exit_quantity == self.entry_quantity

    @property
    def net_pnl(self) -> Decimal:
        return self.entry_quote - self.exit_quote - self.commission + self.funding


class CapitalManagedSpikeStrategy:
    """组合现有 Spike 策略，在完整平仓后更新下一轮名义金额。"""

    def __init__(
        self,
        delegate: Any,
        config: CapitalPolicyConfig,
        *,
        funding_events: Iterable[FundingIncomeEvent] | None = None,
    ) -> None:
        self.delegate = delegate
        self.policy = CapitalPolicy(config)
        self.capital_state = self.policy.initial_state()
        self.settlements: list[CapitalSettlement] = []
        self._campaign: _CampaignFills | None = None
        self._funding_events = self._normalize_funding_events(funding_events)
        self._consumed_funding_ids: set[int] = set()
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
        if self._funding_events is None:
            raise RuntimeError(
                "dynamic capital replay requires a complete funding input"
            )
        self.delegate.on_fill(fill)
        if self._campaign is None:
            if fill.side != "SELL":
                raise RuntimeError("exit fill arrived without an active Capital Campaign")
            self._campaign = _CampaignFills(
                symbol=fill.symbol,
                opened_at=fill.fill_time,
                last_fill_time=fill.fill_time,
            )
        self._campaign.add(fill)
        if not self._campaign.closed:
            return
        funding_ids, funding = self._funding_for_campaign(self._campaign)
        self._campaign.funding += funding
        settlement = self.policy.settle(
            self.capital_state, self._campaign.net_pnl
        )
        self.capital_state = settlement.state_after
        self.settlements.append(settlement)
        self._consumed_funding_ids.update(funding_ids)
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

    @staticmethod
    def _normalize_funding_events(
        events: Iterable[FundingIncomeEvent] | None,
    ) -> dict[str, tuple[FundingIncomeEvent, ...]] | None:
        if events is None:
            return None
        by_id: dict[int, FundingIncomeEvent] = {}
        for raw_event in events:
            event = FundingIncomeEvent(
                transaction_id=int(raw_event.transaction_id),
                symbol=raw_event.symbol.strip().upper(),
                event_time=int(raw_event.event_time),
                amount=Decimal(raw_event.amount),
            )
            if (
                event.transaction_id < 0
                or event.event_time < 0
                or not event.symbol
                or not event.amount.is_finite()
            ):
                raise ValueError("invalid funding event")
            previous = by_id.get(event.transaction_id)
            if previous is not None and previous != event:
                raise ValueError(
                    f"funding transaction {event.transaction_id} has conflicting facts"
                )
            by_id[event.transaction_id] = event
        by_symbol: dict[str, list[FundingIncomeEvent]] = {}
        for event in by_id.values():
            by_symbol.setdefault(event.symbol, []).append(event)
        return {
            symbol: tuple(
                sorted(items, key=lambda item: (item.event_time, item.transaction_id))
            )
            for symbol, items in by_symbol.items()
        }

    def _funding_for_campaign(
        self, campaign: _CampaignFills
    ) -> tuple[set[int], Decimal]:
        assert self._funding_events is not None
        selected = {
            event.transaction_id: event.amount
            for event in self._funding_events.get(campaign.symbol, ())
            if event.transaction_id not in self._consumed_funding_ids
            and campaign.opened_at <= event.event_time <= campaign.last_fill_time
        }
        return set(selected), sum(selected.values(), Decimal("0"))
