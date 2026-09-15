"""Stateful, network-free core for a long 7-day box breakout strategy.

The class in this module deliberately knows nothing about Redis, an exchange,
or order execution.  A caller feeds it completed :class:`~Kline` events and
forwards fills from the execution layer.  This keeps the signal rules usable
from both a live worker and a deterministic backtest.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Iterable, Literal, Mapping

from trading_platform.shared.events import Fill, Kline, OrderIntent


SUPPORTED_TIMEFRAMES: dict[str, int] = {"4h": 42, "1d": 7}
"""Supported timeframe to the default number of bars in a seven-day box."""

_INTERVAL_MILLISECONDS = {"4h": 4 * 60 * 60 * 1000, "1d": 24 * 60 * 60 * 1000}
_ORDER_TYPE = Literal["LIMIT", "MARKET"]
_UNSET = object()
_BINANCE_CLIENT_ORDER_ID_MAX_LENGTH = 36


def _decimal(value: Decimal | str | int | float, *, name: str) -> Decimal:
    """Convert configuration values without allowing NaN or infinity."""

    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _optional_decimal(
    value: Decimal | str | int | float | None, *, name: str
) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, name=name)


def _base36(value: int) -> str:
    if value < 0:
        raise ValueError("base36 value must be non-negative")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    encoded = ""
    while value:
        value, remainder = divmod(value, 36)
        encoded = alphabet[remainder] + encoded
    return encoded


@dataclass(frozen=True)
class LongBreakoutConfig:
    """Parameters for one long-breakout signal engine.

    Percentage values are ratios: ``Decimal("0.02")`` means 2%.  ``None``
    disables the corresponding exit rule.  The default values are deliberately
    conservative demo defaults and can be changed without changing the signal
    engine.
    """

    timeframe: str = "4h"
    lookback_bars: int | None = None
    quantity: Decimal | str | int | float = Decimal("1")
    breakout_buffer_pct: Decimal | str | int | float = Decimal("0")
    take_profit_pct: Decimal | str | int | float | None = Decimal("0.02")
    stop_loss_pct: Decimal | str | int | float | None = Decimal("0.01")
    entry_order_type: _ORDER_TYPE = "MARKET"
    exit_order_type: _ORDER_TYPE = "MARKET"
    entry_ttl_ms: int | None = None
    exit_ttl_ms: int | None = None

    def __post_init__(self) -> None:
        timeframe = self.timeframe.strip().lower()
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"unsupported timeframe {self.timeframe!r}; "
                f"choose one of {tuple(SUPPORTED_TIMEFRAMES)}"
            )
        object.__setattr__(self, "timeframe", timeframe)

        expected_lookback = SUPPORTED_TIMEFRAMES[timeframe]
        if self.lookback_bars is None:
            object.__setattr__(self, "lookback_bars", expected_lookback)
        elif (
            isinstance(self.lookback_bars, bool)
            or not isinstance(self.lookback_bars, int)
            or self.lookback_bars != expected_lookback
        ):
            raise ValueError(
                f"lookback_bars must be {expected_lookback} for a 7-day "
                f"{timeframe} box"
            )

        quantity = _decimal(self.quantity, name="quantity")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        object.__setattr__(self, "quantity", quantity)

        breakout_buffer_pct = _decimal(
            self.breakout_buffer_pct, name="breakout_buffer_pct"
        )
        if breakout_buffer_pct < 0:
            raise ValueError("breakout_buffer_pct must be non-negative")
        object.__setattr__(self, "breakout_buffer_pct", breakout_buffer_pct)

        take_profit_pct = _optional_decimal(
            self.take_profit_pct, name="take_profit_pct"
        )
        if take_profit_pct is not None and take_profit_pct <= 0:
            raise ValueError("take_profit_pct must be positive or None")
        object.__setattr__(self, "take_profit_pct", take_profit_pct)

        stop_loss_pct = _optional_decimal(self.stop_loss_pct, name="stop_loss_pct")
        if stop_loss_pct is not None and stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be positive or None")
        object.__setattr__(self, "stop_loss_pct", stop_loss_pct)

        if self.entry_order_type not in {"LIMIT", "MARKET"}:
            raise ValueError("entry_order_type must be LIMIT or MARKET")
        if self.exit_order_type not in {"LIMIT", "MARKET"}:
            raise ValueError("exit_order_type must be LIMIT or MARKET")
        for name in ("entry_ttl_ms", "exit_ttl_ms"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer or None")

    @property
    def box_bars(self) -> int:
        """Number of completed bars required before evaluating a breakout."""

        assert self.lookback_bars is not None
        return self.lookback_bars

    @property
    def bar_duration_ms(self) -> int:
        return _INTERVAL_MILLISECONDS[self.timeframe]


@dataclass
class LongBreakoutPosition:
    """The minimum external position state needed by the signal engine."""

    symbol: str
    quantity: Decimal
    entry_price: Decimal
    campaign_id: str | None = None


@dataclass
class _SymbolState:
    history: deque[Kline]
    last_close_time: int | None = None
    position: LongBreakoutPosition | None = None
    pending_entry: str | None = None
    pending_campaign_id: str | None = None
    pending_exit: str | None = None
    pending_exit_reason: str | None = None


class LongBreakoutStrategy:
    """Generate long entries and reduce-only exits from completed K-lines.

    ``symbol``/``symbols`` are optional so one instance can be used as a
    multi-symbol core.  Supplying either one restricts processing to the
    declared symbol set.  Each symbol owns an independent history and state,
    while the strategy enforces at most one long position per symbol.
    """

    STRATEGY_ID = "long_breakout"

    def __init__(
        self,
        symbol: str | None = None,
        *,
        symbols: Iterable[str] | None = None,
        config: LongBreakoutConfig | None = None,
        timeframe: str | None = None,
        lookback_bars: int | None = None,
        quantity: Decimal | str | int | float | None = None,
        breakout_buffer_pct: Decimal | str | int | float | None = None,
        take_profit_pct: Decimal | str | int | float | None | object = _UNSET,
        stop_loss_pct: Decimal | str | int | float | None | object = _UNSET,
        entry_order_type: _ORDER_TYPE | None = None,
        exit_order_type: _ORDER_TYPE | None = None,
        entry_ttl_ms: int | None = None,
        exit_ttl_ms: int | None = None,
    ) -> None:
        declared_symbols = []
        if symbol is not None:
            declared_symbols.append(symbol)
        if symbols is not None:
            declared_symbols.extend(symbols)
        normalized_symbols = {
            self._normalize_symbol(item) for item in declared_symbols if str(item).strip()
        }
        if declared_symbols and not normalized_symbols:
            raise ValueError("at least one non-empty symbol is required")
        self._symbols = frozenset(normalized_symbols)

        if config is None:
            config = LongBreakoutConfig(
                timeframe=timeframe or "4h",
                lookback_bars=lookback_bars,
                quantity=quantity if quantity is not None else Decimal("1"),
                breakout_buffer_pct=(
                    breakout_buffer_pct
                    if breakout_buffer_pct is not None
                    else Decimal("0")
                ),
                take_profit_pct=(
                    Decimal("0.02")
                    if take_profit_pct is _UNSET
                    else take_profit_pct
                ),
                stop_loss_pct=(
                    Decimal("0.01") if stop_loss_pct is _UNSET else stop_loss_pct
                ),
                entry_order_type=entry_order_type or "MARKET",
                exit_order_type=exit_order_type or "MARKET",
                entry_ttl_ms=entry_ttl_ms,
                exit_ttl_ms=exit_ttl_ms,
            )
        else:
            overrides = {
                name: value
                for name, value in {
                    "timeframe": timeframe,
                    "lookback_bars": lookback_bars,
                    "quantity": quantity,
                    "breakout_buffer_pct": breakout_buffer_pct,
                    "take_profit_pct": take_profit_pct,
                    "stop_loss_pct": stop_loss_pct,
                    "entry_order_type": entry_order_type,
                    "exit_order_type": exit_order_type,
                    "entry_ttl_ms": entry_ttl_ms,
                    "exit_ttl_ms": exit_ttl_ms,
                }.items()
                if value is not None and value is not _UNSET
            }
            # ``None`` is meaningful for the optional exit rules: it disables
            # that rule when a caller supplies a base config plus overrides.
            if take_profit_pct is not _UNSET:
                overrides["take_profit_pct"] = take_profit_pct
            if stop_loss_pct is not _UNSET:
                overrides["stop_loss_pct"] = stop_loss_pct
            config = replace(config, **overrides) if overrides else config
        self.config = config
        self._states: dict[str, _SymbolState] = {}
        self._entry_enabled = True

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        normalized = str(symbol).strip().upper()
        if not normalized:
            raise ValueError("symbol must not be empty")
        return normalized

    @property
    def strategy_id(self) -> str:
        return self.STRATEGY_ID

    @property
    def timeframe(self) -> str:
        return self.config.timeframe

    @property
    def positions(self) -> Mapping[str, LongBreakoutPosition]:
        """Read-only view of currently open positions by symbol."""

        return {
            symbol: state.position
            for symbol, state in self._states.items()
            if state.position is not None
        }

    @property
    def entry_enabled(self) -> bool:
        return self._entry_enabled

    def set_entry_enabled(self, enabled: bool) -> None:
        """Open or close new entries without disabling position exits."""

        self._entry_enabled = bool(enabled)

    def position(self, symbol: str) -> LongBreakoutPosition | None:
        state = self._states.get(self._normalize_symbol(symbol))
        return None if state is None else state.position

    def has_position(self, symbol: str) -> bool:
        return self.position(symbol) is not None

    def pending_entry(self, symbol: str) -> str | None:
        state = self._states.get(self._normalize_symbol(symbol))
        return None if state is None else state.pending_entry

    def pending_exit(self, symbol: str) -> str | None:
        state = self._states.get(self._normalize_symbol(symbol))
        return None if state is None else state.pending_exit

    def last_close_time(self, symbol: str) -> int | None:
        state = self._states.get(self._normalize_symbol(symbol))
        return None if state is None else state.last_close_time

    def _state_for(self, symbol: str) -> _SymbolState:
        state = self._states.get(symbol)
        if state is None:
            state = _SymbolState(
                history=deque(maxlen=self.config.box_bars),
            )
            self._states[symbol] = state
        return state

    def restore_position(
        self,
        symbol: str,
        quantity: Decimal | str | int | float,
        entry_price: Decimal | str | int | float,
        campaign_id: str | None = None,
    ) -> LongBreakoutPosition:
        """Restore an externally-held long position after a worker restart."""

        normalized = self._normalize_symbol(symbol)
        self._ensure_symbol_allowed(normalized)
        quantity_decimal = _decimal(quantity, name="quantity")
        price_decimal = _decimal(entry_price, name="entry_price")
        if quantity_decimal <= 0:
            raise ValueError("quantity must be positive")
        if price_decimal <= 0:
            raise ValueError("entry_price must be positive")
        state = self._state_for(normalized)
        state.position = LongBreakoutPosition(
            symbol=normalized,
            quantity=quantity_decimal,
            entry_price=price_decimal,
            campaign_id=campaign_id,
        )
        state.pending_entry = None
        state.pending_campaign_id = None
        state.pending_exit = None
        state.pending_exit_reason = None
        return state.position

    def restore_positions(
        self,
        positions: Mapping[str, LongBreakoutPosition | Mapping[str, object]],
    ) -> None:
        """Restore several positions from a worker/reconciliation snapshot."""

        for symbol, value in positions.items():
            if isinstance(value, LongBreakoutPosition):
                self.restore_position(
                    symbol, value.quantity, value.entry_price, value.campaign_id
                )
                continue
            try:
                quantity = value["quantity"]
                entry_price = value["entry_price"]
            except (KeyError, TypeError) as exc:
                raise ValueError(
                    "position snapshots require quantity and entry_price"
                ) from exc
            self.restore_position(  # type: ignore[arg-type]
                symbol,
                quantity,
                entry_price,
                value.get("campaign_id"),
            )

    def restore_pending_order(
        self,
        symbol: str,
        *,
        client_order_id: str,
        campaign_id: str,
        reduce_only: bool,
    ) -> None:
        """Restore one active owned order after WAL/exchange reconciliation."""

        normalized = self._normalize_symbol(symbol)
        self._ensure_symbol_allowed(normalized)
        if not client_order_id or not campaign_id:
            raise ValueError("pending order identity is required")
        state = self._state_for(normalized)
        if reduce_only:
            state.pending_exit = client_order_id
        else:
            state.pending_entry = client_order_id
            state.pending_campaign_id = campaign_id

    def seed_history(self, klines: Iterable[Kline]) -> None:
        """Warm the rolling boxes without producing entries or exits."""

        grouped: dict[str, list[Kline]] = {}
        for kline in klines:
            symbol = self._normalize_symbol(kline.symbol)
            if not self._is_symbol_allowed(symbol) or kline.interval != self.timeframe:
                continue
            grouped.setdefault(symbol, []).append(kline)
        for symbol, values in grouped.items():
            ordered = sorted(values, key=lambda value: value.close_time)
            for previous, current in zip(ordered, ordered[1:]):
                if current.open_time != previous.open_time + self.config.bar_duration_ms:
                    raise ValueError(f"non-contiguous warmup K-lines for {symbol}")
            state = self._state_for(symbol)
            state.history.clear()
            state.history.extend(ordered[-self.config.box_bars :])
            state.last_close_time = ordered[-1].close_time if ordered else None

    def clear_pending_entry(self, symbol: str) -> None:
        """Allow a new entry after an execution worker cancels/rejects one."""

        state = self._states.get(self._normalize_symbol(symbol))
        if state is not None:
            state.pending_entry = None
            state.pending_campaign_id = None

    def clear_pending_exit(self, symbol: str) -> None:
        """Allow a later exit retry after an execution worker cancels one."""

        state = self._states.get(self._normalize_symbol(symbol))
        if state is not None:
            state.pending_exit = None
            state.pending_exit_reason = None

    def reset_execution_state(self) -> None:
        """Drop recovered order/position state while retaining market history."""

        for state in self._states.values():
            state.position = None
            state.pending_entry = None
            state.pending_campaign_id = None
            state.pending_exit = None
            state.pending_exit_reason = None

    def on_order_terminal(
        self,
        symbol: str,
        *,
        client_order_id: str,
        reduce_only: bool,
    ) -> None:
        """Clear only the pending order represented by this terminal update."""

        state = self._states.get(self._normalize_symbol(symbol))
        if state is None:
            return
        pending = state.pending_exit if reduce_only else state.pending_entry
        if pending != client_order_id:
            return
        if reduce_only:
            self.clear_pending_exit(symbol)
        else:
            self.clear_pending_entry(symbol)

    def on_order_rejected(self, symbol: str, *, reduce_only: bool) -> None:
        if reduce_only:
            self.clear_pending_exit(symbol)
        else:
            self.clear_pending_entry(symbol)

    def on_order_cancelled(self, symbol: str, *, reduce_only: bool) -> None:
        self.on_order_rejected(symbol, reduce_only=reduce_only)

    def box_high(self, symbol: str) -> Decimal | None:
        """Return the current completed-window high, if the window is warm."""

        normalized = self._normalize_symbol(symbol)
        state = self._states.get(normalized)
        if state is None or len(state.history) < self.config.box_bars:
            return None
        return max(kline.high for kline in state.history)

    def on_kline(self, kline: Kline) -> list[OrderIntent]:
        """Process one completed K-line and return zero or one intent."""

        symbol = self._normalize_symbol(kline.symbol)
        if not self._is_symbol_allowed(symbol) or kline.interval != self.config.timeframe:
            return []
        state = self._state_for(symbol)

        # A completed K-line is an idempotency key.  Ignore replays and late
        # events rather than allowing them to create a second signal.
        if state.last_close_time is not None and kline.close_time <= state.last_close_time:
            return []
        if (
            state.last_close_time is not None
            and kline.open_time
            != state.last_close_time + 1
        ):
            raise ValueError(f"non-contiguous K-line for {symbol}")
        state.last_close_time = kline.close_time

        position = state.position
        # A partially-filled BUY remains an active entry until the exchange
        # reports a terminal order status. Do not exit its partial position.
        if state.pending_entry is not None:
            state.history.append(kline)
            return []
        if position is not None and state.pending_exit is None:
            exit_intent = self._exit_intent(symbol, state, kline, position)
            if exit_intent is not None:
                state.pending_exit = exit_intent.client_order_id
                state.pending_exit_reason = exit_intent.trigger_reason
                state.history.append(kline)
                return [exit_intent]

        # A pending exit or open position means execution has not resolved the
        # current campaign; neither state can open another position.
        if position is not None:
            state.history.append(kline)
            return []

        entry_intent = (
            self._entry_intent(symbol, state, kline)
            if self._entry_enabled
            else None
        )
        state.history.append(kline)
        if entry_intent is None:
            return []
        state.pending_entry = entry_intent.client_order_id
        state.pending_campaign_id = entry_intent.campaign_id
        return [entry_intent]

    # Names used by lightweight workers can be kept separate from the strategy
    # protocol without adding any runtime dependency.
    process_kline = on_kline
    handle_kline = on_kline

    def _is_symbol_allowed(self, symbol: str) -> bool:
        return not self._symbols or symbol in self._symbols

    def _ensure_symbol_allowed(self, symbol: str) -> None:
        if not self._is_symbol_allowed(symbol):
            raise ValueError(f"symbol {symbol!r} is not configured for this strategy")

    def _entry_intent(
        self, symbol: str, state: _SymbolState, kline: Kline
    ) -> OrderIntent | None:
        if len(state.history) < self.config.box_bars:
            return None
        box_high = max(item.high for item in state.history)
        breakout_level = box_high * (Decimal("1") + self.config.breakout_buffer_pct)
        if kline.close <= breakout_level:
            return None
        client_order_id = self._client_order_id(symbol, kline.close_time, "e")
        return OrderIntent(
            symbol=symbol,
            side="BUY",
            price=kline.close,
            quantity=self.config.quantity,
            client_order_id=client_order_id,
            ttl_ms=self.config.entry_ttl_ms,
            order_type=self.config.entry_order_type,
            reduce_only=False,
            strategy_id=self.STRATEGY_ID,
            trigger_reason=f"7d_box_breakout_{self.config.timeframe}",
            campaign_id=f"{self.STRATEGY_ID}:{symbol}:{kline.close_time}",
        )

    def _exit_intent(
        self,
        symbol: str,
        state: _SymbolState,
        kline: Kline,
        position: LongBreakoutPosition,
    ) -> OrderIntent | None:
        reason: str | None = None
        if (
            self.config.stop_loss_pct is not None
            and kline.close
            <= position.entry_price * (Decimal("1") - self.config.stop_loss_pct)
        ):
            reason = "stop_loss"
        elif (
            self.config.take_profit_pct is not None
            and kline.close
            >= position.entry_price * (Decimal("1") + self.config.take_profit_pct)
        ):
            reason = "take_profit"
        if reason is None:
            return None
        suffix = "s" if reason == "stop_loss" else "t"
        client_order_id = self._client_order_id(symbol, kline.close_time, suffix)
        return OrderIntent(
            symbol=symbol,
            side="SELL",
            price=kline.close,
            quantity=position.quantity,
            client_order_id=client_order_id,
            ttl_ms=self.config.exit_ttl_ms,
            order_type=self.config.exit_order_type,
            reduce_only=True,
            strategy_id=self.STRATEGY_ID,
            trigger_reason=reason,
            campaign_id=(
                position.campaign_id
                or f"{self.STRATEGY_ID}:{symbol}:recovered"
            ),
        )

    def on_fill(self, fill: Fill, *, campaign_id: str | None = None) -> None:
        """Apply an execution fill supplied by the independent worker."""

        symbol = self._normalize_symbol(fill.symbol)
        if not self._is_symbol_allowed(symbol):
            return
        quantity = _decimal(fill.quantity, name="fill.quantity")
        price = _decimal(fill.price, name="fill.price")
        if quantity <= 0 or price <= 0:
            raise ValueError("fill quantity and price must be positive")
        state = self._state_for(symbol)

        if fill.side == "BUY":
            if state.position is None:
                state.position = LongBreakoutPosition(
                    symbol=symbol,
                    quantity=quantity,
                    entry_price=price,
                    campaign_id=campaign_id or state.pending_campaign_id,
                )
            else:
                current = state.position
                total_quantity = current.quantity + quantity
                current.entry_price = (
                    current.entry_price * current.quantity + price * quantity
                ) / total_quantity
                current.quantity = total_quantity
            return

        if fill.side != "SELL" or state.position is None:
            return
        current = state.position
        if quantity >= current.quantity:
            state.position = None
            state.pending_exit = None
            state.pending_exit_reason = None
            return
        current.quantity -= quantity

    @staticmethod
    def _client_order_id(symbol: str, event_time: int, suffix: str) -> str:
        if not symbol.isalnum() or suffix not in {"e", "s", "t"}:
            raise ValueError("invalid long_breakout order identity")
        value = f"l_{symbol}_{_base36(event_time)}_{suffix}"
        if len(value) > _BINANCE_CLIENT_ORDER_ID_MAX_LENGTH:
            raise ValueError(
                f"long_breakout client order ID exceeds Binance limit: {symbol}"
            )
        return value
