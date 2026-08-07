#!/usr/bin/env python3
"""Fail-closed Binance USD-M Futures testnet execution smoke harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from trading_platform.shared.binance.rest_client import BinanceRestClient
from trading_platform.shared.binance.symbol_rules import (
    BinanceSymbolRuleBook,
    SymbolRuleViolation,
)
from trading_platform.shared.events import OrderIntent


TESTNET_HOST = "demo-fapi.binance.com"
EXECUTE_CONFIRMATION = "I_UNDERSTAND_TESTNET_ORDERS_ARE_REAL"
POSITION_CONFIRMATION = "I_UNDERSTAND_THIS_OPENS_A_TESTNET_POSITION"
SCENARIO_CANCEL_OPEN = "cancel-open"
SCENARIO_FILL_AND_EXIT = "fill-and-exit"
OPEN_ORDER_STATUSES = {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}
TERMINAL_ORDER_STATUSES = {
    "CANCELED",
    "EXPIRED",
    "EXPIRED_IN_MATCH",
    "FILLED",
    "REJECTED",
}
CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,36}$")


class SmokeFailure(RuntimeError):
    """Expected fail-closed refusal with a machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_decimal(value: str, *, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"{name} must be a decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError(f"{name} must be positive")
    return parsed


def validate_testnet_environment(*, execute: bool, confirmation: str | None) -> tuple[str, str, str]:
    if os.getenv("BINANCE_TESTNET", "").strip().lower() != "true":
        raise SmokeFailure("TESTNET_FLAG_REQUIRED", "BINANCE_TESTNET must be exactly true")

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
        raise SmokeFailure(
            "TESTNET_ENDPOINT_REQUIRED",
            f"BINANCE_BASE_URL must be https://{TESTNET_HOST}",
        )

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if execute:
        if confirmation != EXECUTE_CONFIRMATION:
            raise SmokeFailure(
                "EXECUTE_CONFIRMATION_REQUIRED",
                f"--confirm must equal {EXECUTE_CONFIRMATION}",
            )
        if not api_key or not api_secret:
            raise SmokeFailure("CREDENTIALS_REQUIRED", "testnet API credentials are required")
    return base_url, api_key, api_secret


def validate_scenario_authorization(args: argparse.Namespace) -> None:
    if (
        args.execute
        and args.scenario == SCENARIO_FILL_AND_EXIT
        and args.confirm_position != POSITION_CONFIRMATION
    ):
        raise SmokeFailure(
            "POSITION_CONFIRMATION_REQUIRED",
            f"--confirm-position must equal {POSITION_CONFIRMATION}",
        )


def safe_order_view(order: dict[str, Any] | None) -> dict[str, Any] | None:
    if order is None:
        return None
    fields = (
        "symbol",
        "clientOrderId",
        "orderId",
        "side",
        "type",
        "status",
        "price",
        "origQty",
        "executedQty",
        "reduceOnly",
        "timeInForce",
        "updateTime",
    )
    return {field: order[field] for field in fields if field in order}


def safe_position_view(position: dict[str, Any]) -> dict[str, Any]:
    fields = ("symbol", "positionSide", "positionAmt", "entryPrice", "markPrice")
    return {field: position[field] for field in fields if field in position}


def rule_book_for_symbol(exchange_info: dict[str, Any], symbol: str) -> BinanceSymbolRuleBook:
    symbols = exchange_info.get("symbols")
    if not isinstance(symbols, list):
        raise SmokeFailure("EXCHANGE_INFO_INVALID", "exchangeInfo did not contain symbols")
    selected = [value for value in symbols if isinstance(value, dict) and value.get("symbol") == symbol]
    if len(selected) != 1:
        raise SmokeFailure("SYMBOL_RULE_MISSING", f"expected one rule set for {symbol}")
    return BinanceSymbolRuleBook.from_exchange_info({"symbols": selected})


def validate_order_response(order: dict[str, Any], *, symbol: str, client_order_id: str) -> str:
    if order.get("symbol") != symbol or order.get("clientOrderId") != client_order_id:
        raise SmokeFailure("ORDER_IDENTITY_MISMATCH", "exchange order identity did not match intent")
    status = order.get("status")
    if status not in OPEN_ORDER_STATUSES | TERMINAL_ORDER_STATUSES:
        raise SmokeFailure("ORDER_STATUS_UNKNOWN", f"unrecognized order status: {status!r}")
    return str(status)


def order_has_fill(order: dict[str, Any]) -> bool:
    if order.get("status") == "FILLED":
        return True
    try:
        return Decimal(str(order.get("executedQty", "0"))) > 0
    except InvalidOperation as exc:
        raise SmokeFailure("ORDER_RESPONSE_INVALID", "invalid executed quantity") from exc


async def query_until_known(
    client: BinanceRestClient,
    *,
    symbol: str,
    client_order_id: str,
    attempts: int,
    interval_seconds: float,
) -> dict[str, Any] | None:
    for attempt in range(attempts):
        try:
            order = await client.query_order(
                symbol=symbol,
                orig_client_order_id=client_order_id,
            )
        except Exception:
            order = None
        if order is not None:
            validate_order_response(order, symbol=symbol, client_order_id=client_order_id)
            return order
        if attempt + 1 < attempts:
            await asyncio.sleep(interval_seconds)
    return None


async def query_until_terminal(
    client: BinanceRestClient,
    *,
    symbol: str,
    client_order_id: str,
    attempts: int,
    interval_seconds: float,
) -> dict[str, Any] | None:
    for attempt in range(attempts):
        order = await query_until_known(
            client,
            symbol=symbol,
            client_order_id=client_order_id,
            attempts=1,
            interval_seconds=0,
        )
        if order is not None and order.get("status") in TERMINAL_ORDER_STATUSES:
            return order
        if attempt + 1 < attempts:
            await asyncio.sleep(interval_seconds)
    return None


async def query_until_position(
    client: BinanceRestClient,
    *,
    symbol: str,
    attempts: int,
    interval_seconds: float,
) -> dict[str, Any] | None:
    for attempt in range(attempts):
        position = nonzero_one_way_position(
            await client.get_position_risk(symbol), symbol=symbol
        )
        if position is not None:
            return position
        if attempt + 1 < attempts:
            await asyncio.sleep(interval_seconds)
    return None


async def query_until_flat(
    client: BinanceRestClient,
    *,
    symbol: str,
    attempts: int,
    interval_seconds: float,
) -> dict[str, Any] | None:
    for attempt in range(attempts):
        position = nonzero_one_way_position(
            await client.get_position_risk(symbol), symbol=symbol
        )
        if position is None:
            return None
        if attempt + 1 < attempts:
            await asyncio.sleep(interval_seconds)
    return position


async def emergency_cleanup(
    client: BinanceRestClient,
    *,
    args: argparse.Namespace,
    rules: Any,
    reference_price: Decimal,
    exit_attempted: bool,
) -> dict[str, Any]:
    """只清理通过空仓空单前检后，本轮 client ID 可能产生的风险。"""
    result: dict[str, Any] = {}
    entry = await query_until_known(
        client,
        symbol=args.symbol,
        client_order_id=args.client_order_id,
        attempts=args.query_attempts,
        interval_seconds=args.query_interval_seconds,
    )
    if entry is None:
        try:
            await client.cancel_order(
                symbol=args.symbol,
                orig_client_order_id=args.client_order_id,
            )
        except Exception:
            pass
        entry = await query_until_terminal(
            client,
            symbol=args.symbol,
            client_order_id=args.client_order_id,
            attempts=args.query_attempts,
            interval_seconds=args.query_interval_seconds,
        )
        result["entry"] = safe_order_view(entry) if entry else "unknown"
    elif entry.get("status") in OPEN_ORDER_STATUSES:
        try:
            await client.cancel_order(
                symbol=args.symbol,
                orig_client_order_id=args.client_order_id,
            )
        except Exception:
            pass
        entry = await query_until_terminal(
            client,
            symbol=args.symbol,
            client_order_id=args.client_order_id,
            attempts=args.query_attempts,
            interval_seconds=args.query_interval_seconds,
        )
        result["entry"] = safe_order_view(entry) if entry else "cancel_unknown"
    else:
        result["entry"] = safe_order_view(entry)
    result["entry_resolved"] = entry is not None

    position = nonzero_one_way_position(
        await client.get_position_risk(args.symbol), symbol=args.symbol
    )
    if position is None:
        result["flat"] = True
        result["risk_resolved"] = result["entry_resolved"]
        return result

    amount = Decimal(str(position["positionAmt"]))
    mark_price = Decimal(str(position.get("markPrice") or reference_price))
    exit_client_id = f"{args.client_order_id[:25]}_exit"
    exit_order = await query_until_known(
        client,
        symbol=args.symbol,
        client_order_id=exit_client_id,
        attempts=1,
        interval_seconds=0,
    )
    if exit_order is None and not exit_attempted:
        exit_intent = rules.normalize_intent(
            OrderIntent(
                symbol=args.symbol,
                side="BUY" if amount < 0 else "SELL",
                price=mark_price,
                quantity=abs(amount),
                client_order_id=exit_client_id,
                order_type="MARKET",
                trigger_reason="testnet_execution_smoke_cleanup",
            ),
            reference_price=mark_price,
        )
        try:
            await client.post_order(
                symbol=args.symbol,
                side=exit_intent.side,
                order_type="MARKET",
                quantity=exit_intent.quantity,
                new_client_order_id=exit_client_id,
                reduce_only=True,
            )
        except Exception:
            pass
        exit_order = await query_until_known(
            client,
            symbol=args.symbol,
            client_order_id=exit_client_id,
            attempts=args.query_attempts,
            interval_seconds=args.query_interval_seconds,
        )
    result["exit"] = safe_order_view(exit_order) if exit_order else "unknown"
    remaining = nonzero_one_way_position(
        await client.get_position_risk(args.symbol), symbol=args.symbol
    )
    result["flat"] = remaining is None
    result["risk_resolved"] = result["entry_resolved"] and remaining is None
    if remaining is not None:
        result["remaining_position"] = safe_position_view(remaining)
    return result


def nonzero_one_way_position(positions: list[dict[str, Any]], *, symbol: str) -> dict[str, Any] | None:
    active: list[dict[str, Any]] = []
    for position in positions:
        if position.get("symbol") != symbol:
            continue
        try:
            amount = Decimal(str(position.get("positionAmt", "0")))
        except InvalidOperation as exc:
            raise SmokeFailure("POSITION_RESPONSE_INVALID", "invalid position amount") from exc
        if amount:
            active.append(position)
    if not active:
        return None
    if len(active) != 1 or active[0].get("positionSide", "BOTH") != "BOTH":
        raise SmokeFailure("HEDGE_MODE_UNSUPPORTED", "only one-way mode can be reduced safely")
    return active[0]


async def run_smoke(args: argparse.Namespace, report: dict[str, Any]) -> None:
    base_url, api_key, api_secret = validate_testnet_environment(
        execute=args.execute,
        confirmation=args.confirm,
    )
    report["mode"] = "execute" if args.execute else "dry-run"
    report["endpoint"] = base_url
    report["symbol"] = args.symbol
    report["client_order_id"] = args.client_order_id
    report["scenario"] = args.scenario
    validate_scenario_authorization(args)

    client = BinanceRestClient(api_key=api_key, api_secret=api_secret, base_url=base_url)
    rules = None
    reference_price: Decimal | None = None
    write_started = False
    exit_attempted = False
    try:
        exchange_info = await client.get_exchange_info()
        rules = rule_book_for_symbol(exchange_info, args.symbol).get(args.symbol)
        entry_intent = rules.normalize_intent(
            OrderIntent(
                symbol=args.symbol,
                side="SELL",
                price=args.limit_price,
                quantity=args.quantity,
                client_order_id=args.client_order_id,
                order_type="LIMIT",
                trigger_reason="testnet_execution_smoke",
            )
        )
        report["normalized_entry"] = {
            "side": entry_intent.side,
            "type": entry_intent.order_type,
            "price": str(entry_intent.price),
            "quantity": str(entry_intent.quantity),
        }
        if args.scenario == SCENARIO_CANCEL_OPEN:
            report["planned_steps"] = [
                "submit_non_marketable_sell_limit_once",
                "query_by_client_order_id",
                "cancel_if_open",
                "reduce_only_market_exit_if_accidentally_filled",
            ]
        else:
            report["planned_steps"] = [
                "submit_marketable_sell_limit_once",
                "verify_entry_filled_and_short_position_visible",
                "submit_reduce_only_buy_market_once",
                "verify_exit_filled_and_position_flat",
            ]
        klines = await client.get_klines(args.symbol, "1m", limit=1)
        if not klines or len(klines[0]) < 5:
            raise SmokeFailure("REFERENCE_PRICE_UNAVAILABLE", "could not read reference price")
        reference_price = Decimal(str(klines[0][4]))
        if args.scenario == SCENARIO_CANCEL_OPEN:
            minimum_limit = reference_price * (
                Decimal("1") + args.min_distance_bps / Decimal("10000")
            )
            if entry_intent.price < minimum_limit:
                raise SmokeFailure(
                    "MARKETABLE_ENTRY_REFUSED",
                    "cancel-open SELL LIMIT is too close to the latest close; increase --limit-price",
                )
        else:
            minimum_fill_limit = reference_price * (
                Decimal("1") - args.max_fill_distance_bps / Decimal("10000")
            )
            if entry_intent.price > reference_price or entry_intent.price < minimum_fill_limit:
                raise SmokeFailure(
                    "FILL_PRICE_OUT_OF_RANGE",
                    "fill-and-exit SELL LIMIT must be at or up to max-fill-distance-bps below the latest close",
                )
        report["reference_price"] = str(reference_price)

        if not args.execute:
            report["result"] = "DRY_RUN_OK"
            return

        position_mode = await client.get_position_mode()
        if position_mode.get("dualSidePosition") is not False:
            raise SmokeFailure(
                "HEDGE_MODE_UNSUPPORTED",
                "testnet execution requires one-way position mode",
            )

        if await client.get_open_orders(args.symbol):
            raise SmokeFailure(
                "PREFLIGHT_OPEN_ORDERS",
                "symbol has pre-existing open orders; use a dedicated idle testnet account",
            )
        if nonzero_one_way_position(
            await client.get_position_risk(args.symbol), symbol=args.symbol
        ) is not None:
            raise SmokeFailure(
                "PREFLIGHT_POSITION",
                "symbol has a pre-existing position; cleanup is not allowed to own it",
            )
        if await client.query_order(
            args.symbol, orig_client_order_id=args.client_order_id
        ) is not None:
            raise SmokeFailure(
                "CLIENT_ORDER_ID_REUSED",
                "client order ID already exists on the exchange",
            )

        submitted: dict[str, Any] | None = None
        try:
            write_started = True
            submitted = await client.post_order(
                symbol=args.symbol,
                side="SELL",
                order_type="LIMIT",
                quantity=entry_intent.quantity,
                price=entry_intent.price,
                time_in_force="GTC",
                new_client_order_id=args.client_order_id,
            )
            validate_order_response(
                submitted,
                symbol=args.symbol,
                client_order_id=args.client_order_id,
            )
        except Exception:
            submitted = await query_until_known(
                client,
                symbol=args.symbol,
                client_order_id=args.client_order_id,
                attempts=args.query_attempts,
                interval_seconds=args.query_interval_seconds,
            )
            if submitted is None:
                raise SmokeFailure(
                    "SUBMIT_UNKNOWN",
                    "submission could not be resolved; do not repeat this client order ID",
                )
        report["submitted_order"] = safe_order_view(submitted)

        queried = await query_until_known(
            client,
            symbol=args.symbol,
            client_order_id=args.client_order_id,
            attempts=args.query_attempts,
            interval_seconds=args.query_interval_seconds,
        )
        if queried is None:
            raise SmokeFailure("QUERY_UNKNOWN", "submitted order could not be queried")
        report["queried_order"] = safe_order_view(queried)

        status = validate_order_response(
            queried,
            symbol=args.symbol,
            client_order_id=args.client_order_id,
        )
        if args.scenario == SCENARIO_FILL_AND_EXIT and status in OPEN_ORDER_STATUSES:
            filled = await query_until_terminal(
                client,
                symbol=args.symbol,
                client_order_id=args.client_order_id,
                attempts=args.query_attempts,
                interval_seconds=args.query_interval_seconds,
            )
            if filled is not None:
                queried = filled
                status = validate_order_response(
                    queried,
                    symbol=args.symbol,
                    client_order_id=args.client_order_id,
                )
                report["queried_order"] = safe_order_view(queried)
        resolved_entry = queried
        if status in OPEN_ORDER_STATUSES:
            try:
                canceled = await client.cancel_order(
                    symbol=args.symbol,
                    orig_client_order_id=args.client_order_id,
                )
            except Exception:
                canceled = await query_until_terminal(
                    client,
                    symbol=args.symbol,
                    client_order_id=args.client_order_id,
                    attempts=args.query_attempts,
                    interval_seconds=args.query_interval_seconds,
                )
                if canceled is None:
                    raise SmokeFailure(
                        "CANCEL_UNKNOWN",
                        "cancel could not be resolved; manual reconciliation required",
                    )
            cancel_status = validate_order_response(
                canceled,
                symbol=args.symbol,
                client_order_id=args.client_order_id,
            )
            if cancel_status in OPEN_ORDER_STATUSES:
                canceled = await query_until_terminal(
                    client,
                    symbol=args.symbol,
                    client_order_id=args.client_order_id,
                    attempts=args.query_attempts,
                    interval_seconds=args.query_interval_seconds,
                )
                if canceled is None:
                    raise SmokeFailure(
                        "CANCEL_UNKNOWN",
                        "order did not reach a terminal state after cancel",
                    )
            report["cancel_result"] = safe_order_view(canceled)
            resolved_entry = canceled
            if args.scenario == SCENARIO_FILL_AND_EXIT:
                raise SmokeFailure(
                    "ENTRY_NOT_FILLED",
                    "fill-and-exit entry did not fill before timeout and was canceled",
                )
        else:
            report["cancel_result"] = {"skipped": True, "reason": f"order_{status.lower()}"}

        if args.scenario == SCENARIO_FILL_AND_EXIT and status != "FILLED":
            raise SmokeFailure(
                "ENTRY_NOT_FILLED",
                f"fill-and-exit entry reached terminal status {status!r} without a fill",
            )

        if args.scenario == SCENARIO_FILL_AND_EXIT or order_has_fill(resolved_entry):
            position = await query_until_position(
                client,
                symbol=args.symbol,
                attempts=args.query_attempts,
                interval_seconds=args.query_interval_seconds,
            )
            if position is None:
                raise SmokeFailure(
                    "FILLED_POSITION_NOT_VISIBLE",
                    "entry filled but the attributable position was not visible before timeout",
                )
        else:
            positions = await client.get_position_risk(args.symbol)
            position = nonzero_one_way_position(positions, symbol=args.symbol)
        report["position_before_exit"] = safe_position_view(position) if position else None
        if position is None:
            report["reduce_only_exit"] = {"skipped": True, "reason": "no_position"}
            report["result"] = "EXECUTION_OK"
            return

        amount = Decimal(str(position["positionAmt"]))
        if args.scenario == SCENARIO_FILL_AND_EXIT and amount >= 0:
            raise SmokeFailure(
                "POSITION_DIRECTION_MISMATCH",
                "filled SELL entry did not produce a short one-way position",
            )
        mark_price = Decimal(str(position.get("markPrice") or reference_price))
        exit_side = "BUY" if amount < 0 else "SELL"
        exit_intent = rules.normalize_intent(
            OrderIntent(
                symbol=args.symbol,
                side=exit_side,
                price=mark_price,
                quantity=abs(amount),
                client_order_id=f"{args.client_order_id[:25]}_exit",
                order_type="MARKET",
                trigger_reason="testnet_execution_smoke_cleanup",
            ),
            reference_price=mark_price,
        )
        report["reduce_only_exit_intent"] = {
            "client_order_id": exit_intent.client_order_id,
            "side": exit_side,
            "quantity": str(exit_intent.quantity),
            "reduce_only": True,
        }
        try:
            exit_attempted = True
            exit_order = await client.post_order(
                symbol=args.symbol,
                side=exit_side,
                order_type="MARKET",
                quantity=exit_intent.quantity,
                new_client_order_id=exit_intent.client_order_id,
                reduce_only=True,
            )
        except Exception:
            exit_order = await query_until_known(
                client,
                symbol=args.symbol,
                client_order_id=exit_intent.client_order_id,
                attempts=args.query_attempts,
                interval_seconds=args.query_interval_seconds,
            )
            if exit_order is None:
                raise SmokeFailure(
                    "EXIT_SUBMIT_UNKNOWN",
                    "reduce-only exit could not be resolved; do not resubmit",
                )
        exit_status = validate_order_response(
            exit_order,
            symbol=args.symbol,
            client_order_id=exit_intent.client_order_id,
        )
        if exit_status in OPEN_ORDER_STATUSES:
            exit_order = await query_until_terminal(
                client,
                symbol=args.symbol,
                client_order_id=exit_intent.client_order_id,
                attempts=args.query_attempts,
                interval_seconds=args.query_interval_seconds,
            )
            if exit_order is None:
                raise SmokeFailure(
                    "EXIT_NOT_TERMINAL",
                    "reduce-only market exit did not reach a terminal state",
                )
            exit_status = validate_order_response(
                exit_order,
                symbol=args.symbol,
                client_order_id=exit_intent.client_order_id,
            )
        if args.scenario == SCENARIO_FILL_AND_EXIT and exit_status != "FILLED":
            raise SmokeFailure(
                "EXIT_NOT_FILLED",
                f"reduce-only market exit reached terminal status {exit_status!r}",
            )
        report["reduce_only_exit"] = safe_order_view(exit_order)

        remaining = await query_until_flat(
            client,
            symbol=args.symbol,
            attempts=args.query_attempts,
            interval_seconds=args.query_interval_seconds,
        )
        report["position_after_exit"] = safe_position_view(remaining) if remaining else None
        if remaining is not None:
            raise SmokeFailure("POSITION_NOT_FLAT", "position remains after reduce-only exit")
        report["result"] = "EXECUTION_OK"
    finally:
        if args.execute and write_started and rules is not None and reference_price is not None:
            try:
                report["final_cleanup"] = await emergency_cleanup(
                    client,
                    args=args,
                    rules=rules,
                    reference_price=reference_price,
                    exit_attempted=exit_attempted,
                )
            except Exception as exc:
                report["final_cleanup"] = {
                    "flat": False,
                    "error": type(exc).__name__,
                }
        await client.close()


def synthetic_exchange_info() -> dict[str, Any]:
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "0", "maxPrice": "1000000"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
        ]
    }


def run_self_check() -> dict[str, Any]:
    rules = BinanceSymbolRuleBook.from_exchange_info(synthetic_exchange_info()).get("BTCUSDT")
    intent = rules.normalize_intent(
        OrderIntent(
            symbol="BTCUSDT",
            side="SELL",
            price=Decimal("100.01"),
            quantity=Decimal("0.0519"),
            client_order_id="tp_smoke_self_check",
        )
    )
    if intent.price != Decimal("100.10") or intent.quantity != Decimal("0.051"):
        raise SmokeFailure("SELF_CHECK_FAILED", "unexpected symbol-rule normalization")
    return {"result": "SELF_CHECK_OK", "price": str(intent.price), "quantity": str(intent.quantity)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT", type=str.upper)
    parser.add_argument("--quantity", type=lambda value: parse_decimal(value, name="quantity"), default=Decimal("0.001"))
    parser.add_argument("--limit-price", type=lambda value: parse_decimal(value, name="limit-price"), default=Decimal("100000"))
    parser.add_argument("--client-order-id", default=f"tp_smoke_{uuid4().hex[:20]}")
    parser.add_argument(
        "--scenario",
        choices=(SCENARIO_CANCEL_OPEN, SCENARIO_FILL_AND_EXIT),
        default=SCENARIO_CANCEL_OPEN,
        help="safe cancel-open by default; fill-and-exit explicitly opens then closes a position",
    )
    parser.add_argument("--min-distance-bps", type=lambda value: parse_decimal(value, name="min-distance-bps"), default=Decimal("100"))
    parser.add_argument(
        "--max-fill-distance-bps",
        type=lambda value: parse_decimal(value, name="max-fill-distance-bps"),
        default=Decimal("20"),
    )
    parser.add_argument("--query-attempts", type=int, default=12)
    parser.add_argument("--query-interval-seconds", type=float, default=5.0)
    parser.add_argument("--execute", action="store_true", help="allow testnet write operations")
    parser.add_argument("--confirm", help="required fixed phrase for --execute")
    parser.add_argument(
        "--confirm-position",
        help="additional fixed phrase required when fill-and-exit executes",
    )
    parser.add_argument("--report", type=Path, help="also write the sanitized JSON report to this path")
    parser.add_argument("--self-check", action="store_true", help="offline rule-normalization check")
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
        if not CLIENT_ORDER_ID_RE.fullmatch(args.client_order_id):
            raise SmokeFailure("CLIENT_ORDER_ID_INVALID", "client order ID must be 1-36 safe characters")
        if args.query_attempts <= 0 or args.query_interval_seconds < 0:
            raise SmokeFailure("QUERY_POLICY_INVALID", "query attempts must be positive and interval non-negative")
        if args.self_check:
            report.update(run_self_check())
        else:
            asyncio.run(run_smoke(args, report))
        exit_code = 0
    except SmokeFailure as exc:
        report.update({"result": "FAIL_CLOSED", "error": {"code": exc.code, "message": str(exc)}})
        exit_code = 2
    except SymbolRuleViolation as exc:
        report.update(
            {
                "result": "FAIL_CLOSED",
                "error": {"code": "SYMBOL_RULE_VIOLATION", "message": str(exc)},
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
                    "message": "unexpected failure; inspect service logs without retrying an unknown order",
                },
            }
        )
        exit_code = 3
    report["finished_at"] = utc_now()
    write_report(report, args.report)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
