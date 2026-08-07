"""Binance Futures 交易规则解析与订单规范化。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from collections.abc import Iterable
from typing import Any

from trading_platform.shared.events import OrderIntent


class SymbolRuleViolation(ValueError):
    """订单无法在不增加风险的前提下满足交易所规则。"""


def _positive_decimal(value: Any, *, field: str, allow_zero: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SymbolRuleViolation(f"invalid {field}") from exc
    if not result.is_finite() or result < 0 or (result == 0 and not allow_zero):
        raise SymbolRuleViolation(f"invalid {field}")
    return result


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


@dataclass(frozen=True)
class BinanceSymbolRules:
    """单个 USD-M Futures 交易对的下单边界。"""

    symbol: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    lot_step_size: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    market_step_size: Decimal
    market_min_quantity: Decimal
    market_max_quantity: Decimal
    min_notional: Decimal

    @classmethod
    def from_exchange_info(cls, symbol_info: dict[str, Any]) -> "BinanceSymbolRules":
        symbol = symbol_info.get("symbol")
        filters_raw = symbol_info.get("filters")
        if not isinstance(symbol, str) or not symbol:
            raise SymbolRuleViolation("missing symbol")
        if not isinstance(filters_raw, list):
            raise SymbolRuleViolation(f"missing filters for {symbol}")

        filters: dict[str, dict[str, Any]] = {}
        for value in filters_raw:
            if not isinstance(value, dict) or not isinstance(value.get("filterType"), str):
                raise SymbolRuleViolation(f"invalid filter for {symbol}")
            filter_type = value["filterType"]
            if filter_type in filters:
                raise SymbolRuleViolation(f"duplicate {filter_type} filter for {symbol}")
            filters[filter_type] = value

        try:
            price = filters["PRICE_FILTER"]
            lot = filters["LOT_SIZE"]
            market_lot = filters.get("MARKET_LOT_SIZE", lot)
            notional = filters["MIN_NOTIONAL"]
        except KeyError as exc:
            raise SymbolRuleViolation(f"missing {exc.args[0]} filter for {symbol}") from exc

        return cls(
            symbol=symbol,
            tick_size=_positive_decimal(price.get("tickSize"), field="tickSize"),
            min_price=_positive_decimal(price.get("minPrice"), field="minPrice", allow_zero=True),
            max_price=_positive_decimal(price.get("maxPrice"), field="maxPrice", allow_zero=True),
            lot_step_size=_positive_decimal(lot.get("stepSize"), field="stepSize"),
            min_quantity=_positive_decimal(lot.get("minQty"), field="minQty"),
            max_quantity=_positive_decimal(lot.get("maxQty"), field="maxQty"),
            market_step_size=_positive_decimal(
                market_lot.get("stepSize"), field="marketStepSize"
            ),
            market_min_quantity=_positive_decimal(
                market_lot.get("minQty"), field="marketMinQty"
            ),
            market_max_quantity=_positive_decimal(
                market_lot.get("maxQty"), field="marketMaxQty"
            ),
            min_notional=_positive_decimal(
                notional.get("notional"), field="minNotional", allow_zero=True
            ),
        )

    def normalize_intent(
        self,
        intent: OrderIntent,
        *,
        reference_price: Decimal | None = None,
    ) -> OrderIntent:
        """向降低风险的方向量化价格和数量，并验证最小名义价值。"""
        if intent.symbol != self.symbol:
            raise SymbolRuleViolation(
                f"symbol rules mismatch: {intent.symbol} != {self.symbol}"
            )
        quantity = _positive_decimal(intent.quantity, field="quantity")
        price = _positive_decimal(intent.price, field="price")

        if intent.order_type == "LIMIT":
            quantity = _floor_to_step(quantity, self.lot_step_size)
            price = (
                _ceil_to_step(price, self.tick_size)
                if intent.side == "SELL"
                else _floor_to_step(price, self.tick_size)
            )
            min_quantity = self.min_quantity
            max_quantity = self.max_quantity
            notional_price = price
            if self.min_price and price < self.min_price:
                raise SymbolRuleViolation(f"price below minimum for {self.symbol}")
            if self.max_price and price > self.max_price:
                raise SymbolRuleViolation(f"price above maximum for {self.symbol}")
        else:
            quantity = _floor_to_step(quantity, self.market_step_size)
            min_quantity = self.market_min_quantity
            max_quantity = self.market_max_quantity
            if reference_price is None:
                raise SymbolRuleViolation("market order requires reference_price")
            notional_price = _positive_decimal(reference_price, field="reference_price")

        if quantity < min_quantity:
            raise SymbolRuleViolation(f"quantity below minimum for {self.symbol}")
        if quantity > max_quantity:
            raise SymbolRuleViolation(f"quantity above maximum for {self.symbol}")
        if quantity * notional_price < self.min_notional:
            raise SymbolRuleViolation(f"notional below minimum for {self.symbol}")
        return replace(intent, price=price, quantity=quantity)


class BinanceSymbolRuleBook:
    """一次 exchangeInfo 快照中的可交易 symbol 规则。"""

    def __init__(self, rules: dict[str, BinanceSymbolRules]):
        self._rules = dict(rules)

    @classmethod
    def from_exchange_info(
        cls,
        exchange_info: dict[str, Any],
        *,
        symbols: Iterable[str] | None = None,
    ) -> "BinanceSymbolRuleBook":
        response_symbols = exchange_info.get("symbols")
        if not isinstance(response_symbols, list):
            raise SymbolRuleViolation("invalid exchangeInfo symbols")
        requested = None if symbols is None else set(symbols)
        if requested is not None and (not requested or any(not value for value in requested)):
            raise SymbolRuleViolation("requested symbols must be non-empty")
        rules: dict[str, BinanceSymbolRules] = {}
        for value in response_symbols:
            if not isinstance(value, dict):
                raise SymbolRuleViolation("invalid exchangeInfo symbol")
            symbol = value.get("symbol")
            if requested is not None and symbol not in requested:
                continue
            if value.get("contractType") != "PERPETUAL":
                continue
            if value.get("status") != "TRADING":
                continue
            rule = BinanceSymbolRules.from_exchange_info(value)
            if rule.symbol in rules:
                raise SymbolRuleViolation(f"duplicate symbol rules: {rule.symbol}")
            rules[rule.symbol] = rule
        if requested is not None:
            missing = requested - rules.keys()
            if missing:
                raise SymbolRuleViolation(
                    f"missing trading symbol rules: {sorted(missing)}"
                )
        return cls(rules)

    def get(self, symbol: str) -> BinanceSymbolRules:
        try:
            return self._rules[symbol]
        except KeyError as exc:
            raise SymbolRuleViolation(f"missing symbol rules: {symbol}") from exc
