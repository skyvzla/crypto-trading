from decimal import Decimal

import pytest

from trading_platform.shared.binance import (
    BinanceSymbolRuleBook,
    BinanceSymbolRules,
    SymbolRuleViolation,
)
from trading_platform.shared.events import OrderIntent


def symbol_info(**overrides):
    value = {
        "symbol": "AKEUSDT",
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.0000010",
                "maxPrice": "10",
                "tickSize": "0.0000001",
            },
            {
                "filterType": "LOT_SIZE",
                "minQty": "1",
                "maxQty": "1000000",
                "stepSize": "1",
            },
            {
                "filterType": "MARKET_LOT_SIZE",
                "minQty": "1",
                "maxQty": "500000",
                "stepSize": "1",
            },
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }
    value.update(overrides)
    return value


def intent(*, side="SELL", price="0.00025815", quantity="20000", order_type="LIMIT"):
    return OrderIntent(
        symbol="AKEUSDT",
        side=side,
        price=Decimal(price),
        quantity=Decimal(quantity),
        client_order_id="cid-1",
        order_type=order_type,
    )


def test_limit_entry_quantizes_without_making_fill_more_aggressive():
    rules = BinanceSymbolRules.from_exchange_info(symbol_info())

    sell = rules.normalize_intent(intent(quantity="20000.9"))
    buy = rules.normalize_intent(intent(side="BUY", price="0.00025819"))

    assert sell.price == Decimal("0.0002582")
    assert sell.quantity == Decimal("20000")
    assert buy.price == Decimal("0.0002581")


def test_market_exit_requires_reference_price_and_uses_market_lot_filter():
    rules = BinanceSymbolRules.from_exchange_info(symbol_info())
    market = intent(order_type="MARKET", quantity="20000.9")

    with pytest.raises(SymbolRuleViolation, match="reference_price"):
        rules.normalize_intent(market)

    normalized = rules.normalize_intent(
        market, reference_price=Decimal("0.0003")
    )
    assert normalized.quantity == Decimal("20000")


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        (intent(quantity="0.9"), "quantity below"),
        (intent(quantity="1000001"), "quantity above"),
        (intent(quantity="1"), "notional below"),
    ],
)
def test_invalid_order_is_rejected_before_submission(candidate, message):
    rules = BinanceSymbolRules.from_exchange_info(symbol_info())

    with pytest.raises(SymbolRuleViolation, match=message):
        rules.normalize_intent(candidate)


def test_rule_book_keeps_only_trading_perpetual_contracts():
    delivery = symbol_info(symbol="DELIVERY", contractType="CURRENT_QUARTER")
    halted = symbol_info(symbol="HALTED", status="SETTLING")
    rules = BinanceSymbolRuleBook.from_exchange_info(
        {"symbols": [symbol_info(), delivery, halted]}
    )

    assert rules.get("AKEUSDT").symbol == "AKEUSDT"
    with pytest.raises(SymbolRuleViolation, match="missing symbol rules"):
        rules.get("DELIVERY")


def test_rule_book_can_keep_settling_perpetual_rules_for_protective_exits():
    settling = symbol_info(symbol="HFTUSDT", status="SETTLING")

    rules = BinanceSymbolRuleBook.from_exchange_info(
        {"symbols": [settling]},
        symbols=["HFTUSDT"],
        require_trading=False,
    )

    assert rules.get("HFTUSDT").symbol == "HFTUSDT"


def test_malformed_or_incomplete_rules_fail_closed():
    incomplete = symbol_info()
    incomplete["filters"] = incomplete["filters"][:-1]

    with pytest.raises(SymbolRuleViolation, match="MIN_NOTIONAL"):
        BinanceSymbolRules.from_exchange_info(incomplete)
    with pytest.raises(SymbolRuleViolation, match="exchangeInfo"):
        BinanceSymbolRuleBook.from_exchange_info({})


def test_requested_symbol_subset_ignores_unrelated_invalid_exchange_rule():
    unrelated = symbol_info(symbol="TUSDT")
    unrelated["filters"][1]["minQty"] = "0"

    book = BinanceSymbolRuleBook.from_exchange_info(
        {"symbols": [symbol_info(), unrelated]},
        symbols=["AKEUSDT"],
    )

    assert book.get("AKEUSDT").symbol == "AKEUSDT"


def test_requested_symbol_must_exist_and_be_valid():
    with pytest.raises(SymbolRuleViolation, match="missing trading"):
        BinanceSymbolRuleBook.from_exchange_info(
            {"symbols": [symbol_info()]}, symbols=["BTCUSDT"]
        )

    invalid = symbol_info()
    invalid["filters"][1]["minQty"] = "0"
    with pytest.raises(SymbolRuleViolation, match="minQty"):
        BinanceSymbolRuleBook.from_exchange_info(
            {"symbols": [invalid]}, symbols=["AKEUSDT"]
        )
