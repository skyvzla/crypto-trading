#!/usr/bin/env python3
"""Fail-closed emergency flatten for a dedicated Binance Futures testnet account.

This command is deliberately scoped to an explicit symbol allow-list. It never
uses a position snapshot to infer ownership and all exits are reduce-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from trading_platform.shared.binance.rest_client import BinanceRestClient
from trading_platform.shared.binance.symbol_rules import BinanceSymbolRuleBook
from trading_platform.shared.events import OrderIntent


TESTNET_HOST = "demo-fapi.binance.com"
CONFIRMATION = "I_UNDERSTAND_TESTNET_EMERGENCY_FLATTEN"
TERMINAL_ORDER_STATUSES = {
    "CANCELED",
    "CANCELLED",
    "EXPIRED",
    "EXPIRED_IN_MATCH",
    "FILLED",
    "REJECTED",
}


class FlattenFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_environment(*, execute: bool, confirmation: str | None) -> tuple[str, str, str]:
    if os.getenv("BINANCE_TESTNET", "").strip().lower() != "true":
        raise FlattenFailure("TESTNET_FLAG_REQUIRED", "BINANCE_TESTNET must be exactly true")
    base_url = os.getenv("BINANCE_BASE_URL", "").strip().rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != TESTNET_HOST
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise FlattenFailure("TESTNET_ENDPOINT_REQUIRED", f"BINANCE_BASE_URL must be https://{TESTNET_HOST}")
    if execute and confirmation != CONFIRMATION:
        raise FlattenFailure("CONFIRMATION_REQUIRED", f"--confirm must equal {CONFIRMATION}")
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        raise FlattenFailure("CREDENTIALS_REQUIRED", "testnet API credentials are required to inspect account state")
    return base_url, api_key, api_secret


def parse_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip()))
    if not symbols or any(not symbol.endswith("USDT") for symbol in symbols):
        raise argparse.ArgumentTypeError("--symbols must contain one or more USDT symbols")
    return symbols


def position_for_symbol(positions: list[dict], symbol: str) -> dict | None:
    active = []
    for position in positions:
        if position.get("symbol") != symbol:
            continue
        try:
            amount = Decimal(str(position.get("positionAmt", "0")))
        except InvalidOperation as exc:
            raise FlattenFailure("POSITION_RESPONSE_INVALID", f"invalid position amount for {symbol}") from exc
        if amount:
            active.append(position)
    if not active:
        return None
    if len(active) != 1 or active[0].get("positionSide", "BOTH") != "BOTH":
        raise FlattenFailure("HEDGE_MODE_UNSUPPORTED", "emergency flatten only supports one-way positions")
    return active[0]


def safe_order(order: dict) -> dict:
    fields = ("symbol", "clientOrderId", "orderId", "side", "type", "status", "executedQty", "reduceOnly")
    return {field: order[field] for field in fields if field in order}


def safe_position(position: dict | None) -> dict | None:
    if position is None:
        return None
    fields = (
        "symbol",
        "positionSide",
        "positionAmt",
        "entryPrice",
        "markPrice",
        "unRealizedProfit",
        "liquidationPrice",
        "leverage",
        "marginType",
    )
    return {field: position[field] for field in fields if field in position}


async def resolve_exit_order(
    client: BinanceRestClient,
    *,
    symbol: str,
    client_order_id: str,
    initial: dict,
    attempts: int,
    interval_seconds: float,
) -> dict:
    order = initial
    for attempt in range(attempts):
        if order.get("symbol") != symbol or order.get("clientOrderId") != client_order_id:
            raise FlattenFailure("EXIT_IDENTITY_MISMATCH", f"exit identity mismatch on {symbol}")
        status = order.get("status")
        if status in TERMINAL_ORDER_STATUSES:
            if status != "FILLED":
                raise FlattenFailure("EXIT_NOT_FILLED", f"reduce-only exit ended as {status!r} on {symbol}")
            return order
        if attempt + 1 < attempts:
            await asyncio.sleep(interval_seconds)
            order = await client.query_order(
                symbol, orig_client_order_id=client_order_id
            )
            if order is None:
                raise FlattenFailure("EXIT_UNKNOWN", f"reduce-only exit became unknown on {symbol}")
    raise FlattenFailure("EXIT_NOT_TERMINAL", f"reduce-only exit did not become terminal on {symbol}")


async def flatten(args: argparse.Namespace) -> dict:
    base_url, key, secret = validate_environment(execute=args.execute, confirmation=args.confirm)
    client = BinanceRestClient(api_key=key, api_secret=secret, base_url=base_url)
    report: dict = {"mode": "execute" if args.execute else "dry-run", "symbols": list(args.symbols), "orders": [], "positions": []}
    try:
        mode = await client.get_position_mode()
        if mode.get("dualSidePosition") is not False:
            raise FlattenFailure("HEDGE_MODE_UNSUPPORTED", "account is not in one-way mode")
        exchange_info = await client.get_exchange_info()
        rules = BinanceSymbolRuleBook.from_exchange_info(
            exchange_info, symbols=args.symbols
        )
        for symbol in args.symbols:
            rule = rules.get(symbol)
            position = position_for_symbol(await client.get_position_risk(symbol), symbol)
            open_orders = await client.get_open_orders(symbol)
            symbol_report = {"symbol": symbol, "position_before": safe_position(position), "open_orders_before": [safe_order(o) for o in open_orders]}
            if args.execute:
                for order in open_orders:
                    order_id = order.get("orderId")
                    client_id = order.get("clientOrderId")
                    if not order_id and not client_id:
                        raise FlattenFailure("ORDER_IDENTITY_MISSING", f"cannot cancel unidentified order on {symbol}")
                    canceled = await client.cancel_order(symbol, order_id=order_id, orig_client_order_id=client_id)
                    if not isinstance(canceled, dict):
                        raise FlattenFailure("CANCEL_UNKNOWN", f"cancel response was not attributable on {symbol}")
                    report["orders"].append(safe_order(canceled))
                remaining_orders = await client.get_open_orders(symbol)
                symbol_report["open_orders_after_cancel"] = [safe_order(o) for o in remaining_orders]
                if remaining_orders:
                    raise FlattenFailure("OPEN_ORDERS_REMAIN", f"open orders remain on {symbol} after cancellation")
                position = position_for_symbol(await client.get_position_risk(symbol), symbol)
                if position is not None:
                    amount = Decimal(str(position["positionAmt"]))
                    mark = Decimal(str(position.get("markPrice") or position.get("entryPrice") or "0"))
                    if mark <= 0:
                        raise FlattenFailure("MARK_PRICE_INVALID", f"missing positive mark price for {symbol}")
                    client_id = f"flatten_{symbol[:10]}_{uuid4().hex[:16]}"
                    intent = rule.normalize_intent(OrderIntent(symbol=symbol, side="BUY" if amount < 0 else "SELL", price=mark, quantity=abs(amount), client_order_id=client_id, order_type="MARKET", trigger_reason="manual_emergency_flatten"), reference_price=mark)
                    exited = await client.post_order(symbol=symbol, side=intent.side, order_type="MARKET", quantity=intent.quantity, new_client_order_id=client_id, reduce_only=True)
                    exited = await resolve_exit_order(
                        client,
                        symbol=symbol,
                        client_order_id=client_id,
                        initial=exited,
                        attempts=args.query_attempts,
                        interval_seconds=args.query_interval_seconds,
                    )
                    symbol_report["exit"] = safe_order(exited)
                remaining = position_for_symbol(await client.get_position_risk(symbol), symbol)
                symbol_report["position_after"] = safe_position(remaining)
                if remaining is not None:
                    raise FlattenFailure("POSITION_NOT_FLAT", f"position remains on {symbol}")
            report["positions"].append(symbol_report)
        report["result"] = "FLATTEN_OK" if args.execute else "DRY_RUN_OK"
        return report
    finally:
        await client.close()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", required=True, type=parse_symbols, help="explicit comma-separated USDT symbols")
    p.add_argument("--execute", action="store_true", help="perform cancellations and reduce-only exits")
    p.add_argument("--confirm", help="required fixed confirmation phrase")
    p.add_argument("--query-attempts", type=int, default=12)
    p.add_argument("--query-interval-seconds", type=float, default=5.0)
    p.add_argument("--report", type=Path)
    return p


def main() -> int:
    args = parser().parse_args()
    report = {"started_at": datetime.now(timezone.utc).isoformat()}
    try:
        if args.query_attempts <= 0 or args.query_interval_seconds < 0:
            raise FlattenFailure(
                "QUERY_POLICY_INVALID",
                "query attempts must be positive and interval non-negative",
            )
        report.update(asyncio.run(flatten(args)))
        code = 0
    except FlattenFailure as exc:
        report.update({"result": "FAIL_CLOSED", "error": {"code": exc.code, "message": str(exc)}})
        code = 2
    except Exception as exc:
        report.update({"result": "FAIL_CLOSED", "error": {"code": "UNEXPECTED_ERROR", "type": type(exc).__name__, "message": "unexpected failure; reconcile manually"}})
        code = 3
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True)
    print(payload)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(payload + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
