#!/usr/bin/env python3
"""Run one attributable Campaign through the production execution/ledger path."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from trading_platform.ledger.binance_runtime import create_binance_execution_runtime
from trading_platform.ledger.db.models import LedgerDB, create_connection_pool
from trading_platform.shared.binance.live_executor import BinanceOrderExecutor
from trading_platform.shared.binance.rest_client import BinanceRestClient
from trading_platform.shared.binance.symbol_rules import BinanceSymbolRuleBook
from trading_platform.shared.config import BinanceConfig, DatabaseConfig
from trading_platform.shared.events import OrderIntent
from trading_platform.shared.execution_recovery import OrderWAL
from trading_platform.shared.postgres_lease import PostgresExecutionLease


TESTNET_HOST = "demo-fapi.binance.com"
TESTNET_WS_HOST = "stream.binancefuture.com"
EXECUTE_CONFIRMATION = "I_UNDERSTAND_TESTNET_ORDERS_ARE_REAL"
POSITION_CONFIRMATION = "I_UNDERSTAND_THIS_OPENS_A_TESTNET_POSITION"
ACCOUNT_ID = "spike_testnet"
STRATEGY_ID = "spike_short"


class RoundtripFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _strict_endpoint(value: str, *, scheme: str, host: str, name: str) -> str:
    parsed = urlsplit(value.strip().rstrip("/"))
    if (
        parsed.scheme != scheme
        or parsed.hostname != host
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RoundtripFailure(
            "TESTNET_ENDPOINT_REQUIRED",
            f"{name} must be {scheme}://{host}",
        )
    return value.strip().rstrip("/")


def validate_environment(
    *, execute: bool, confirmation: str | None, position_confirmation: str | None
) -> tuple[str, str, str]:
    if os.getenv("BINANCE_TESTNET", "").strip().lower() != "true":
        raise RoundtripFailure("TESTNET_FLAG_REQUIRED", "BINANCE_TESTNET must be exactly true")
    explicit_base_url = os.getenv("BINANCE_BASE_URL")
    if explicit_base_url is not None:
        _strict_endpoint(
            explicit_base_url,
            scheme="https",
            host=TESTNET_HOST,
            name="BINANCE_BASE_URL",
        )
    explicit_ws_url = os.getenv("BINANCE_WS_BASE_URL")
    if explicit_ws_url is not None:
        _strict_endpoint(
            explicit_ws_url,
            scheme="wss",
            host=TESTNET_WS_HOST,
            name="BINANCE_WS_BASE_URL",
        )
    binance = BinanceConfig()
    binance.base_url = _strict_endpoint(
        binance.base_url, scheme="https", host=TESTNET_HOST, name="BINANCE_BASE_URL"
    )
    binance.ws_base_url = _strict_endpoint(
        binance.ws_base_url,
        scheme="wss",
        host=TESTNET_WS_HOST,
        name="BINANCE_WS_BASE_URL",
    )
    if execute and confirmation != EXECUTE_CONFIRMATION:
        raise RoundtripFailure(
            "EXECUTE_CONFIRMATION_REQUIRED", f"--confirm must equal {EXECUTE_CONFIRMATION}"
        )
    if execute and position_confirmation != POSITION_CONFIRMATION:
        raise RoundtripFailure(
            "POSITION_CONFIRMATION_REQUIRED",
            f"--confirm-position must equal {POSITION_CONFIRMATION}",
        )
    if execute and (not binance.api_key or not binance.api_secret):
        raise RoundtripFailure("CREDENTIALS_REQUIRED", "testnet API credentials are required")
    return binance.base_url, binance.api_key, binance.api_secret


async def assert_account_ready(client: Any, *, symbol: str) -> None:
    if (await client.get_position_mode()).get("dualSidePosition") is not False:
        raise RoundtripFailure("HEDGE_MODE_UNSUPPORTED", "account is not in one-way mode")
    try:
        positions = await client.get_position_risk()
    except TypeError:
        positions = await client.get_position_risk(symbol)
    if _nonzero_positions(positions):
        raise RoundtripFailure("PREFLIGHT_ACCOUNT_POSITION", "account has a pre-existing position")
    if await client.get_open_orders(symbol):
        raise RoundtripFailure("PREFLIGHT_OPEN_ORDERS", "symbol has a pre-existing open order")


async def assert_account_flat(client: Any, *, symbol: str) -> None:
    if _nonzero_positions(await client.get_position_risk(symbol)):
        raise RoundtripFailure("POSITION_NOT_FLAT", "position remains after roundtrip")
    if await client.get_open_orders(symbol):
        raise RoundtripFailure("FINAL_OPEN_ORDERS", "open orders remain after roundtrip")


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RoundtripFailure("EXCHANGE_RESPONSE_INVALID", f"invalid {field}") from exc
    if not result.is_finite():
        raise RoundtripFailure("EXCHANGE_RESPONSE_INVALID", f"invalid {field}")
    return result


def _nonzero_positions(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    result = []
    for row in rows:
        amount = _decimal(row.get("positionAmt", "0"), field="positionAmt")
        if amount:
            result.append(
                {
                    "symbol": str(row.get("symbol", "")),
                    "positionSide": str(row.get("positionSide", "")),
                    "positionAmt": str(amount),
                }
            )
    return result


def _safe_order(record: Any) -> dict[str, Any]:
    return {
        "client_order_id": record.client_order_id,
        "exchange_order_id": record.exchange_order_id,
        "status": record.status,
        "symbol": record.symbol,
        "side": record.side,
        "order_type": record.order_type,
        "quantity": record.quantity,
    }


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (Decimal, datetime)):
        return str(value)
    return value


async def _latest_close(client: BinanceRestClient, symbol: str) -> Decimal:
    rows = await client.get_klines(symbol, "1m", limit=1)
    if not rows or len(rows[0]) < 5:
        raise RoundtripFailure("REFERENCE_PRICE_UNAVAILABLE", "latest close is unavailable")
    close = _decimal(rows[0][4], field="latest close")
    if close <= 0:
        raise RoundtripFailure("REFERENCE_PRICE_UNAVAILABLE", "latest close is not positive")
    return close


async def _wait_order(
    client: BinanceRestClient,
    *,
    symbol: str,
    client_order_id: str,
    attempts: int,
    interval: float,
) -> dict[str, Any]:
    last: dict[str, Any] | None = None
    for attempt in range(attempts):
        last = await client.query_order(symbol, orig_client_order_id=client_order_id)
        if last is not None and last.get("status") in {
            "FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"
        }:
            return last
        if attempt + 1 < attempts:
            await asyncio.sleep(interval)
    raise RoundtripFailure(
        "ORDER_NOT_TERMINAL", f"order did not become terminal: {client_order_id}"
    )


async def _wait_position(
    client: BinanceRestClient,
    *,
    symbol: str,
    want_flat: bool,
    attempts: int,
    interval: float,
) -> Decimal:
    amount = Decimal("0")
    for attempt in range(attempts):
        rows = await client.get_position_risk(symbol)
        active = _nonzero_positions(rows)
        if len(active) > 1 or (active and active[0]["positionSide"] != "BOTH"):
            raise RoundtripFailure("HEDGE_MODE_UNSUPPORTED", "expected one-way position")
        amount = Decimal(active[0]["positionAmt"]) if active else Decimal("0")
        if (want_flat and amount == 0) or (not want_flat and amount < 0):
            return amount
        if attempt + 1 < attempts:
            await asyncio.sleep(interval)
    code = "POSITION_NOT_FLAT" if want_flat else "SHORT_POSITION_NOT_VISIBLE"
    raise RoundtripFailure(code, f"position check timed out with amount={amount}")


async def _wait_pnl(
    db: LedgerDB,
    *,
    campaign_id: str,
    attempts: int,
    interval: float,
) -> Any:
    summary = None
    for attempt in range(attempts):
        summary = await db.get_campaign_pnl(
            account_id=ACCOUNT_ID,
            strategy_id=STRATEGY_ID,
            campaign_id=campaign_id,
        )
        if summary is not None and summary.trade_count >= 2 and summary.remaining_quantity == 0:
            return summary
        if attempt + 1 < attempts:
            await asyncio.sleep(interval)
    raise RoundtripFailure("LEDGER_PNL_INCOMPLETE", "campaign fills were not fully visible in PostgreSQL")


async def _cleanup_owned_risk(
    client: BinanceRestClient,
    *,
    symbol: str,
    entry_client_id: str,
    exit_client_id: str,
    attempts: int,
    interval: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        entry = await client.query_order(symbol, orig_client_order_id=entry_client_id)
        if entry is not None and entry.get("status") in {"NEW", "PARTIALLY_FILLED"}:
            await client.cancel_order(symbol, orig_client_order_id=entry_client_id)
        result["entry_resolved"] = entry is not None
    except Exception as exc:
        result["entry_error"] = type(exc).__name__
    active = _nonzero_positions(await client.get_position_risk(symbol))
    if active:
        amount = Decimal(active[0]["positionAmt"])
        existing_exit = await client.query_order(symbol, orig_client_order_id=exit_client_id)
        if existing_exit is None:
            await client.post_order(
                symbol=symbol,
                side="BUY" if amount < 0 else "SELL",
                order_type="MARKET",
                quantity=abs(amount),
                new_client_order_id=exit_client_id,
                reduce_only=True,
            )
        try:
            await _wait_position(
                client,
                symbol=symbol,
                want_flat=True,
                attempts=attempts,
                interval=interval,
            )
        except RoundtripFailure:
            pass
    result["flat"] = not _nonzero_positions(await client.get_position_risk(symbol))
    result["open_orders"] = len(await client.get_open_orders(symbol))
    return result


async def cleanup_campaign_risk(
    client: Any,
    *,
    symbol: str,
    entry_client_order_id: str,
    exit_client_order_id: str,
    quantity: Decimal,
    query_attempts: int,
    query_interval_seconds: float,
) -> dict[str, Any]:
    # quantity is retained in the public contract for audit/test callers; the
    # exchange position snapshot remains the only safe cleanup quantity.
    del quantity
    return await _cleanup_owned_risk(
        client,
        symbol=symbol,
        entry_client_id=entry_client_order_id,
        exit_client_id=exit_client_order_id,
        attempts=query_attempts,
        interval=query_interval_seconds,
    )


async def run_roundtrip(args: argparse.Namespace, report: dict[str, Any]) -> None:
    base_url, api_key, api_secret = validate_environment(
        execute=args.execute,
        confirmation=args.confirm,
        position_confirmation=args.confirm_position,
    )
    binance = BinanceConfig()
    binance.base_url = base_url
    database = DatabaseConfig()
    report.update(
        {
            "mode": "execute" if args.execute else "dry-run",
            "endpoint": binance.base_url,
            "ws_endpoint": binance.ws_base_url,
            "account_id": ACCOUNT_ID,
            "strategy_id": STRATEGY_ID,
            "symbol": args.symbol,
        }
    )
    client = BinanceRestClient(api_key, api_secret, base_url=binance.base_url)
    pool = lease = runtime = None
    write_started = False
    epoch_ms = int(time.time() * 1000)
    campaign_id = f"{STRATEGY_ID}:{args.symbol}:{epoch_ms}"
    entry_client_id = f"tpcr_e_{epoch_ms}"
    exit_client_id = f"tpcr_x_{epoch_ms}"
    report.update(
        {
            "campaign_id": campaign_id,
            "entry_client_order_id": entry_client_id,
            "exit_client_order_id": exit_client_id,
        }
    )
    try:
        rules = BinanceSymbolRuleBook.from_exchange_info(
            await client.get_exchange_info(), symbols=[args.symbol]
        )
        reference_price = await _latest_close(client, args.symbol)
        entry_price = reference_price * (
            Decimal("1") - args.fill_distance_bps / Decimal("10000")
        )
        entry = rules.get(args.symbol).normalize_intent(
            OrderIntent(
                symbol=args.symbol,
                side="SELL",
                price=entry_price,
                quantity=args.quantity,
                client_order_id=entry_client_id,
                order_type="LIMIT",
                strategy_id=STRATEGY_ID,
                trigger_reason="testnet_campaign_roundtrip_entry",
                campaign_id=campaign_id,
            )
        )
        report["planned_entry"] = {
            "side": entry.side,
            "order_type": entry.order_type,
            "price": str(entry.price),
            "quantity": str(entry.quantity),
        }
        if entry.price > reference_price or entry.price < reference_price * Decimal("0.998"):
            raise RoundtripFailure(
                "ENTRY_PRICE_OUT_OF_RANGE", "normalized SELL LIMIT is not safely marketable"
            )
        if not args.execute:
            report["result"] = "DRY_RUN_OK"
            return

        await assert_account_ready(client, symbol=args.symbol)

        pool = await create_connection_pool(database.dsn)
        lease = PostgresExecutionLease(pool, ACCOUNT_ID)
        await lease.acquire()
        report["execution_lease_acquired"] = True
        db = LedgerDB(pool)
        wal = OrderWAL(args.wal_path)
        executor = BinanceOrderExecutor(
            client,
            wal,
            account_id=ACCOUNT_ID,
            symbol_rules=rules,
        )
        runtime = create_binance_execution_runtime(
            rest_client=client,
            executor=executor,
            db=db,
            account_id=ACCOUNT_ID,
            strategy_id=STRATEGY_ID,
            managed_symbols=[args.symbol],
            dedicated_strategy_account=True,
            ws_base_url=binance.ws_base_url,
            poll_interval_seconds=args.query_interval_seconds,
            max_poll_attempts=args.query_attempts,
        )
        await runtime.start()
        write_started = True
        entry_record = await executor.submit(entry, reference_price=reference_price)
        report["entry_submission"] = _safe_order(entry_record)
        if entry_record.status == "SUBMIT_UNKNOWN":
            raise RoundtripFailure("ENTRY_SUBMIT_UNKNOWN", "entry submission is unresolved")
        entry_order = await _wait_order(
            client,
            symbol=args.symbol,
            client_order_id=entry_client_id,
            attempts=args.query_attempts,
            interval=args.query_interval_seconds,
        )
        if entry_order.get("status") != "FILLED":
            raise RoundtripFailure("ENTRY_NOT_FILLED", f"entry ended as {entry_order.get('status')}")
        amount = await _wait_position(
            client,
            symbol=args.symbol,
            want_flat=False,
            attempts=args.query_attempts,
            interval=args.query_interval_seconds,
        )
        exit_intent = OrderIntent(
            symbol=args.symbol,
            side="BUY",
            price=reference_price,
            quantity=abs(amount),
            client_order_id=exit_client_id,
            order_type="MARKET",
            reduce_only=True,
            strategy_id=STRATEGY_ID,
            trigger_reason="testnet_campaign_roundtrip_exit",
            campaign_id=campaign_id,
        )
        exit_record = await executor.submit(exit_intent, reference_price=reference_price)
        report["exit_submission"] = _safe_order(exit_record)
        if exit_record.status == "SUBMIT_UNKNOWN":
            raise RoundtripFailure("EXIT_SUBMIT_UNKNOWN", "reduce-only exit is unresolved")
        exit_order = await _wait_order(
            client,
            symbol=args.symbol,
            client_order_id=exit_client_id,
            attempts=args.query_attempts,
            interval=args.query_interval_seconds,
        )
        if exit_order.get("status") != "FILLED":
            raise RoundtripFailure("EXIT_NOT_FILLED", f"exit ended as {exit_order.get('status')}")
        await _wait_position(
            client,
            symbol=args.symbol,
            want_flat=True,
            attempts=args.query_attempts,
            interval=args.query_interval_seconds,
        )
        summary = await _wait_pnl(
            db,
            campaign_id=campaign_id,
            attempts=args.query_attempts,
            interval=args.query_interval_seconds,
        )
        await assert_account_flat(client, symbol=args.symbol)
        report["campaign_pnl"] = _jsonable(summary)
        report["result"] = "ROUNDTRIP_OK"
    finally:
        shutdown_errors: list[BaseException] = []
        if args.execute and write_started:
            try:
                report["final_cleanup"] = await _cleanup_owned_risk(
                    client,
                    symbol=args.symbol,
                    entry_client_id=entry_client_id,
                    exit_client_id=exit_client_id,
                    attempts=args.query_attempts,
                    interval=args.query_interval_seconds,
                )
            except Exception as exc:
                report["final_cleanup"] = {"flat": False, "error": type(exc).__name__}
        if runtime is not None:
            try:
                await runtime.stop()
            except BaseException as exc:
                shutdown_errors.append(exc)
        if lease is not None:
            try:
                await lease.release()
            except BaseException as exc:
                shutdown_errors.append(exc)
        if pool is not None:
            try:
                await pool.close()
            except BaseException as exc:
                shutdown_errors.append(exc)
        try:
            await client.close()
        except BaseException as exc:
            shutdown_errors.append(exc)
        if shutdown_errors:
            raise BaseExceptionGroup("testnet roundtrip shutdown failed", shutdown_errors)


def positive_decimal(value: str) -> Decimal:
    result = _decimal(value, field="argument")
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--quantity", type=positive_decimal, default=Decimal("0.001"))
    parser.add_argument("--fill-distance-bps", type=positive_decimal, default=Decimal("5"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--confirm-position")
    parser.add_argument("--query-attempts", type=int, default=20)
    parser.add_argument("--query-interval-seconds", type=float, default=1.0)
    parser.add_argument(
        "--wal-path",
        type=Path,
        default=Path("data/wal/testnet_campaign_roundtrip.jsonl"),
    )
    parser.add_argument("--report", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat()}
    try:
        if args.query_attempts <= 0 or args.query_interval_seconds < 0:
            raise RoundtripFailure("QUERY_POLICY_INVALID", "invalid query policy")
        asyncio.run(run_roundtrip(args, report))
        code = 0
    except RoundtripFailure as exc:
        report.update({"result": "FAIL_CLOSED", "error": {"code": exc.code, "message": str(exc)}})
        code = 2
    except Exception as exc:
        report.update(
            {
                "result": "FAIL_CLOSED",
                "error": {
                    "code": "UNEXPECTED_ERROR",
                    "type": type(exc).__name__,
                    "message": "unexpected failure; inspect WAL and reconcile manually",
                },
            }
        )
        code = 3
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(_jsonable(report), ensure_ascii=True, indent=2, sort_keys=True)
    print(payload)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(payload + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
