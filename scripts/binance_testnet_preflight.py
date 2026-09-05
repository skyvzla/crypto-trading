#!/usr/bin/env python3
"""Fail-closed, non-matching preflight for a Binance USD-M testnet account."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from trading_platform.shared.binance.rest_client import BinanceRestClient
from trading_platform.shared.binance.symbol_rules import (
    BinanceSymbolRuleBook,
    BinanceSymbolRules,
    SymbolRuleViolation,
)
from trading_platform.shared.config import BinanceConfig
from trading_platform.shared.events import OrderIntent


TESTNET_REST_HOST = "demo-fapi.binance.com"
TESTNET_WS_HOST = "stream.binancefuture.com"


class PreflightFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strict_url(value: str, *, scheme: str, host: str, name: str) -> str:
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise PreflightFailure(
            "TESTNET_ENDPOINT_REQUIRED",
            f"{name} must be {scheme}://{host}",
        ) from exc
    if (
        parsed.scheme != scheme
        or parsed.hostname != host
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise PreflightFailure(
            "TESTNET_ENDPOINT_REQUIRED",
            f"{name} must be {scheme}://{host}",
        )
    return normalized


def validate_environment() -> BinanceConfig:
    if os.getenv("BINANCE_TESTNET", "").strip().lower() != "true":
        raise PreflightFailure(
            "TESTNET_FLAG_REQUIRED", "BINANCE_TESTNET must be exactly true"
        )
    # Validate explicit values before BinanceConfig applies its testnet
    # endpoint defaults; otherwise an explicit production endpoint would be
    # silently replaced by the demo endpoint and incorrectly accepted.
    explicit_base_url = os.getenv("BINANCE_BASE_URL")
    if explicit_base_url is not None:
        _strict_url(
            explicit_base_url,
            scheme="https",
            host=TESTNET_REST_HOST,
            name="BINANCE_BASE_URL",
        )
    explicit_ws_url = os.getenv("BINANCE_WS_BASE_URL")
    if explicit_ws_url is not None:
        _strict_url(
            explicit_ws_url,
            scheme="wss",
            host=TESTNET_WS_HOST,
            name="BINANCE_WS_BASE_URL",
        )

    config = BinanceConfig()
    config.base_url = _strict_url(
        config.base_url,
        scheme="https",
        host=TESTNET_REST_HOST,
        name="BINANCE_BASE_URL",
    )
    config.ws_base_url = _strict_url(
        config.ws_base_url,
        scheme="wss",
        host=TESTNET_WS_HOST,
        name="BINANCE_WS_BASE_URL",
    )
    if not config.api_key or not config.api_secret:
        raise PreflightFailure(
            "CREDENTIALS_REQUIRED", "Binance testnet credentials are required"
        )
    return config


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PreflightFailure(
            "EXCHANGE_RESPONSE_INVALID", f"invalid {field}"
        ) from exc
    if not result.is_finite():
        raise PreflightFailure("EXCHANGE_RESPONSE_INVALID", f"invalid {field}")
    return result


def _usdt_balance(account: dict[str, Any]) -> dict[str, str]:
    assets = account.get("assets")
    if not isinstance(assets, list):
        raise PreflightFailure(
            "EXCHANGE_RESPONSE_INVALID", "account assets are missing"
        )
    if any(not isinstance(row, dict) for row in assets):
        raise PreflightFailure(
            "EXCHANGE_RESPONSE_INVALID", "account assets contain an invalid row"
        )
    matches = [row for row in assets if row.get("asset") == "USDT"]
    if len(matches) != 1:
        raise PreflightFailure(
            "EXCHANGE_RESPONSE_INVALID", "expected one USDT account asset"
        )
    row = matches[0]
    wallet = _decimal(row.get("walletBalance"), field="USDT wallet balance")
    available = _decimal(row.get("availableBalance"), field="USDT available balance")
    return {"wallet": str(wallet), "available": str(available)}


def _nonzero_position_symbols(rows: list[dict[str, Any]]) -> list[str]:
    result: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PreflightFailure(
                "EXCHANGE_RESPONSE_INVALID", "positions contain an invalid row"
            )
        amount = _decimal(row.get("positionAmt", "0"), field="position amount")
        if amount:
            result.add(str(row.get("symbol") or "unknown"))
    return sorted(result)


def _order_test_intent(
    rules: BinanceSymbolRules,
    *,
    reference_price: Decimal,
) -> OrderIntent:
    # Keep the SELL LIMIT away from the market while staying inside the
    # exchange's usual PERCENT_PRICE upper bound. The order/test endpoint
    # validates exchange filters even though it does not enter the book.
    price = reference_price * Decimal("1.01")
    target_notional = max(Decimal("10"), rules.min_notional * Decimal("1.10"))
    quantity = max(rules.min_quantity, target_notional / price)
    quantity = (
        quantity / rules.lot_step_size
    ).to_integral_value(rounding=ROUND_CEILING) * rules.lot_step_size
    return rules.normalize_intent(
        OrderIntent(
            symbol=rules.symbol,
            side="SELL",
            price=price,
            quantity=quantity,
            client_order_id=f"tp_preflight_{uuid4().hex[:20]}",
            order_type="LIMIT",
            trigger_reason="testnet_preflight_non_matching",
        )
    )


async def run_preflight(args: argparse.Namespace, report: dict[str, Any]) -> None:
    config = validate_environment()
    report.update(
        {
            "endpoint": config.base_url,
            "mode": "testnet",
            "symbol": args.symbol,
            "writes_exchange_state": False,
        }
    )
    client = BinanceRestClient(
        api_key=config.api_key,
        api_secret=config.api_secret,
        base_url=config.base_url,
    )
    try:
        started = time.time()
        account, position_mode, open_orders, positions, exchange_info = (
            await asyncio.gather(
                client.get_account(),
                client.get_position_mode(),
                client.get_open_orders(),
                client.get_position_risk(),
                client.get_exchange_info(),
            )
        )
        signed_read_ms = round((time.time() - started) * 1000, 1)
        if not isinstance(account, dict):
            raise PreflightFailure(
                "EXCHANGE_RESPONSE_INVALID", "account response is invalid"
            )
        if not isinstance(position_mode, dict):
            raise PreflightFailure(
                "EXCHANGE_RESPONSE_INVALID", "position mode response is invalid"
            )
        if account.get("canTrade") is not True:
            raise PreflightFailure(
                "ACCOUNT_TRADING_DISABLED", "Binance account canTrade is not true"
            )
        if position_mode.get("dualSidePosition") is not False:
            raise PreflightFailure(
                "HEDGE_MODE_UNSUPPORTED", "account must use one-way position mode"
            )
        if not isinstance(open_orders, list) or not isinstance(positions, list):
            raise PreflightFailure(
                "EXCHANGE_RESPONSE_INVALID", "orders or positions response is invalid"
            )

        balances = _usdt_balance(account)
        available = _decimal(balances["available"], field="USDT available balance")
        nonzero_positions = _nonzero_position_symbols(positions)
        if any(not isinstance(order, dict) for order in open_orders):
            raise PreflightFailure(
                "EXCHANGE_RESPONSE_INVALID", "open orders contain an invalid row"
            )
        open_order_symbols = sorted(
            {str(order.get("symbol") or "unknown") for order in open_orders}
        )
        report["account"] = {
            "signed_read": True,
            "signed_read_latency_ms": signed_read_ms,
            "can_trade": True,
            "one_way_mode": True,
            "usdt": balances,
            "open_order_count": len(open_orders),
            "open_order_symbols": open_order_symbols,
            "nonzero_position_count": len(nonzero_positions),
            "nonzero_position_symbols": nonzero_positions,
        }
        if available < args.minimum_available_usdt:
            raise PreflightFailure(
                "AVAILABLE_BALANCE_TOO_LOW",
                "USDT available balance is below the configured minimum",
            )
        if args.require_flat and (open_orders or nonzero_positions):
            raise PreflightFailure(
                "ACCOUNT_NOT_FLAT",
                "dedicated testnet account has open orders or nonzero positions",
            )

        rules = BinanceSymbolRuleBook.from_exchange_info(
            exchange_info, symbols=[args.symbol]
        ).get(args.symbol)
        klines = await client.get_klines(args.symbol, "1m", limit=1)
        if not klines or len(klines[0]) < 5:
            raise PreflightFailure(
                "REFERENCE_PRICE_UNAVAILABLE", "latest 1m close is unavailable"
            )
        reference_price = _decimal(klines[0][4], field="latest 1m close")
        intent = _order_test_intent(rules, reference_price=reference_price)
        await client.test_order(
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            quantity=intent.quantity,
            price=intent.price,
            new_client_order_id=intent.client_order_id,
        )
        report["order_test"] = {
            "validated": True,
            "endpoint": "/fapi/v1/order/test",
            "matching_engine_submission": False,
            "symbol": intent.symbol,
            "side": intent.side,
            "type": intent.order_type,
            "price": str(intent.price),
            "quantity": str(intent.quantity),
        }
        report["result"] = "PREFLIGHT_OK"
    finally:
        await client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbol",
        default=os.getenv("SPIKE_SYMBOLS", "BTCUSDT").split(",", 1)[0].strip(),
        type=str.upper,
    )
    parser.add_argument(
        "--minimum-available-usdt",
        default=os.getenv("SPIKE_INITIAL_ACCOUNT_CAPITAL", "0"),
    )
    parser.add_argument(
        "--require-flat",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="fail when the dedicated account has any open order or nonzero position",
    )
    parser.add_argument("--report", type=Path)
    return parser


def write_report(report: dict[str, Any], destination: Path | None) -> None:
    payload = json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True)
    print(payload)
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload + "\n", encoding="utf-8")


def main() -> int:
    args = build_parser().parse_args()
    report: dict[str, Any] = {"started_at": utc_now()}
    try:
        try:
            args.minimum_available_usdt = Decimal(str(args.minimum_available_usdt))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PreflightFailure(
                "BALANCE_THRESHOLD_INVALID",
                "minimum available USDT must be a valid decimal",
            ) from exc
        if not args.symbol:
            raise PreflightFailure("SYMBOL_REQUIRED", "symbol must not be empty")
        if not args.minimum_available_usdt.is_finite() or args.minimum_available_usdt < 0:
            raise PreflightFailure(
                "BALANCE_THRESHOLD_INVALID",
                "minimum available USDT must be non-negative",
            )
        asyncio.run(run_preflight(args, report))
        exit_code = 0
    except (PreflightFailure, SymbolRuleViolation) as exc:
        report.update(
            {
                "result": "FAIL_CLOSED",
                "error": {
                    "code": getattr(exc, "code", "SYMBOL_RULE_VIOLATION"),
                    "message": str(exc),
                },
            }
        )
        exit_code = 2
    except Exception as exc:
        report.update(
            {
                "result": "FAIL_CLOSED",
                "error": {
                    "code": "UNEXPECTED_ERROR",
                    "type": type(exc).__name__,
                    "message": "unexpected failure; no order was submitted",
                },
            }
        )
        exit_code = 3
    report["finished_at"] = utc_now()
    write_report(report, args.report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
