"""V2.2 candidate selection/exit with V3-style pullback entry."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from trading_platform.shared.events import Bar1s, OrderIntent
from trading_platform.strategies.spike.short import (
    MS_PER_SECOND,
    SpikeSignal,
)
from trading_platform.strategies.spike.v2_1 import SpikeV21Strategy
from trading_platform.strategies.spike.v2_2 import V22


class SpikeV22PullbackStrategy(SpikeV21Strategy):
    """Keep V2.2 signal and campaign exit semantics, replacing order placement."""

    strategy_name = "v2.2-pullback"

    def __init__(
        self,
        *args,
        min_spike_rise: Decimal = Decimal("0.15"),
        retrace_frac: Decimal = Decimal("0.30"),
        wait_seconds: int = 90,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.min_spike_rise = Decimal(str(min_spike_rise))
        self.retrace_frac = Decimal(str(retrace_frac))
        self.wait_ms = int(wait_seconds) * MS_PER_SECOND
        if self.min_spike_rise < 0:
            raise ValueError("min_spike_rise must not be negative")
        if not Decimal("0") < self.retrace_frac < Decimal("1"):
            raise ValueError("retrace_frac must be between 0 and 1")
        if self.wait_ms <= 0:
            raise ValueError("wait_seconds must be positive")

    def _detect_signal(self, bar: Bar1s) -> Optional[SpikeSignal]:
        signal = super()._detect_signal(bar)
        if signal is None:
            return None
        impulse_window = list(self.bars_1s)[-61:]
        if any(
            current.timestamp - previous.timestamp != MS_PER_SECOND
            for previous, current in zip(impulse_window, impulse_window[1:])
        ):
            return None
        impulse_base = self.bars_1s[-61].close
        if impulse_base <= 0:
            return None
        signal.impulse_base_price = impulse_base
        signal.pullback_spike_high = bar.high
        signal.pullback_last_time = signal.signal_time
        signal.expire_time = signal.signal_time + self.wait_ms
        return signal

    def _manage_signals(self, bar: Bar1s) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        for signal in list(self.active_signals):
            client_order_id = self._client_order_id(signal, 3)
            if client_order_id in signal.placed_client_order_ids:
                if bar.timestamp > signal.expire_time:
                    cancelled = self._cancel_signal_orders(signal)
                    self._record_signal_terminal(
                        signal, "pullback_order_expired", bar.timestamp, cancelled
                    )
                    self.active_signals.remove(signal)
                continue

            ready_time = signal.pullback_ready_time
            expired = bar.timestamp > signal.expire_time
            if ready_time is None:
                expired = expired or bar.timestamp >= signal.expire_time
            else:
                expired = expired or ready_time >= signal.expire_time
            if expired:
                self._finish_pullback_signal(
                    signal, "pullback_timeout", bar.timestamp
                )
                continue

            if (
                signal.pullback_last_time is None
                or bar.timestamp != signal.pullback_last_time + MS_PER_SECOND
            ):
                self._finish_pullback_signal(
                    signal, "pullback_data_gap", bar.timestamp
                )
                continue
            signal.pullback_last_time = bar.timestamp

            if signal.pullback_ready_time is not None:
                if bar.timestamp != signal.pullback_ready_time + MS_PER_SECOND:
                    self._finish_pullback_signal(signal, "pullback_data_gap", bar.timestamp)
                    continue
                if (
                    bar.open >= signal.invalid_price
                    or not self._entry_price_allowed(signal, bar.open)
                ):
                    self._finish_pullback_signal(
                        signal, "pullback_next_open_invalid", bar.timestamp
                    )
                    continue
                if self._account is not None and self._account.has_open_position(self.symbol):
                    self._finish_pullback_signal(
                        signal, "pullback_position_already_open", bar.timestamp
                    )
                    continue
                intents.append(
                    OrderIntent(
                        symbol=self.symbol,
                        side="SELL",
                        price=bar.open,
                        quantity=self.total_notional / bar.open,
                        client_order_id=client_order_id,
                        order_type="MARKET",
                        reduce_only=False,
                        strategy_id="spike_short",
                        trigger_reason="pullback_entry",
                        campaign_id=self._campaign_id(signal),
                    )
                )
                signal.placed_client_order_ids.add(client_order_id)
                self._record_audit(
                    event_time=bar.available_time,
                    event_type="pullback_entry_placed",
                    campaign_id=self._campaign_id(signal),
                    details={
                        "candidate": str(signal.pullback_candidate),
                        "entry_price": str(bar.open),
                        "impulse_base_price": str(signal.impulse_base_price),
                        "spike_high": str(signal.pullback_spike_high),
                        "retrace_frac": str(self.retrace_frac),
                    },
                )
                continue

            impulse_base = signal.impulse_base_price
            spike_high = signal.pullback_spike_high
            if impulse_base is None or spike_high is None:
                self._finish_pullback_signal(
                    signal, "pullback_state_missing", bar.timestamp
                )
                continue
            if bar.high >= signal.invalid_price:
                self._finish_pullback_signal(
                    signal, "pullback_v22_invalid_price", bar.timestamp
                )
                continue
            if bar.low < impulse_base:
                self._finish_pullback_signal(
                    signal, "pullback_impulse_base_breached", bar.timestamp
                )
                continue

            # Only highs from completed prior bars affect this bar's candidate.
            if spike_high >= impulse_base * (Decimal("1") + self.min_spike_rise):
                candidate = spike_high - self.retrace_frac * (
                    spike_high - impulse_base
                )
                signal.pullback_candidate = candidate
                if bar.low <= candidate and self._entry_price_allowed(signal, candidate):
                    signal.pullback_ready_time = bar.timestamp
                    signal.pullback_time = bar.timestamp
                    signal.pullback_low = bar.low
                    self._record_audit(
                        event_time=bar.available_time,
                        event_type="pullback_entry_ready",
                        campaign_id=self._campaign_id(signal),
                        details={
                            "candidate": str(candidate),
                            "bar_low": str(bar.low),
                            "impulse_base_price": str(impulse_base),
                            "spike_high": str(spike_high),
                        },
                    )
                    continue

            if bar.high > spike_high:
                signal.pullback_spike_high = bar.high
        return intents

    def _entry_price_allowed(self, signal: SpikeSignal, price: Decimal) -> bool:
        if price <= 0 or price <= (signal.impulse_base_price or Decimal("0")):
            return False
        if signal.origin_floor is not None and price < signal.origin_floor:
            return False
        if signal.prior_high is None:
            return True
        allowed_prior_high = signal.prior_high * (
            Decimal("1")
            - self.prior_high_tolerance_percent / Decimal("100")
        )
        if self.prior_high_tolerance_percent == 0:
            return price > allowed_prior_high
        return price >= allowed_prior_high

    def _finish_pullback_signal(
        self, signal: SpikeSignal, reason: str, event_time: int
    ) -> None:
        self._record_signal_terminal(signal, reason, event_time, 0)
        self.active_signals.remove(signal)


class V22Pullback:
    name = "v2.2-pullback"
    strategy_class = SpikeV22PullbackStrategy
    shared_feature_provider = V22.shared_feature_provider
    data_requirements = V22.data_requirements
    defaults = V22.defaults
    supported_parameters = V22.supported_parameters | frozenset(
        {"min_spike_rise", "retrace_frac", "wait_seconds"}
    )
    internal_parameters = V22.internal_parameters
