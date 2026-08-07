#!/usr/bin/env python3
"""Reconcile SUBMIT_UNKNOWN WAL records against Binance Futures testnet.

The command never submits, cancels, or retries an exchange order. By default it
only reports exchange facts. ``--execute`` appends a verified terminal/known
status to the local WAL after an explicit confirmation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from trading_platform.shared.binance.rest_client import BinanceRestClient
from trading_platform.shared.execution_recovery import OrderWAL


TESTNET_HOST = "demo-fapi.binance.com"
CONFIRMATION = "I_UNDERSTAND_WAL_RECONCILIATION_WRITES_LOCAL_STATE"
KNOWN_STATUSES = {
    "NEW",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELED",
    "EXPIRED",
}


def fail(code: str, message: str) -> None:
    print(json.dumps({"result": "FAIL_CLOSED", "error": {"code": code, "message": message}}))
    raise SystemExit(2)


def validate_environment(execute: bool, confirmation: str | None) -> tuple[str, str, str]:
    if os.getenv("BINANCE_TESTNET", "").strip().lower() != "true":
        fail("TESTNET_FLAG_REQUIRED", "BINANCE_TESTNET must be exactly true")
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
        fail("TESTNET_ENDPOINT_REQUIRED", f"BINANCE_BASE_URL must be https://{TESTNET_HOST}")
    if execute and confirmation != CONFIRMATION:
        fail("CONFIRMATION_REQUIRED", f"--confirm must equal {CONFIRMATION}")
    key = os.getenv("BINANCE_API_KEY", "")
    secret = os.getenv("BINANCE_API_SECRET", "")
    if not key or not secret:
        fail("CREDENTIALS_REQUIRED", "Binance testnet credentials are required")
    return base_url, key, secret


def safe_response(response: dict | None) -> dict | None:
    if response is None:
        return None
    fields = (
        "symbol",
        "orderId",
        "clientOrderId",
        "status",
        "side",
        "type",
        "origQty",
        "executedQty",
        "price",
        "updateTime",
    )
    return {field: response[field] for field in fields if field in response}


async def reconcile(args: argparse.Namespace) -> dict:
    base_url, key, secret = validate_environment(args.execute, args.confirm)
    wal = OrderWAL(Path(args.wal_path))
    records = [
        record
        for record in wal.recover_latest().values()
        if record.account_id == args.account_id
        and record.status == "SUBMIT_UNKNOWN"
        and (args.symbol is None or record.symbol == args.symbol)
    ]
    report = {
        "result": "WAL_RECONCILE_OK",
        "mode": "execute" if args.execute else "dry-run",
        "account_id": args.account_id,
        "wal_path": str(Path(args.wal_path)),
        "orders": [],
    }
    client = BinanceRestClient(api_key=key, api_secret=secret, base_url=base_url)
    try:
        for record in sorted(records, key=lambda item: item.client_order_id):
            response = await client.query_order(
                record.symbol,
                orig_client_order_id=record.client_order_id,
            )
            item = {
                "client_order_id": record.client_order_id,
                "symbol": record.symbol,
                "exchange": safe_response(response),
                "resolved": False,
                "wal_written": False,
            }
            if response is None:
                item["reason"] = "order_not_found"
            elif (
                response.get("symbol") != record.symbol
                or response.get("clientOrderId") != record.client_order_id
            ):
                item["reason"] = "identity_mismatch"
                report["orders"].append(item)
                continue
            elif response.get("status") not in KNOWN_STATUSES:
                item["reason"] = "unknown_exchange_status"
            else:
                item["resolved"] = True
                if args.execute:
                    wal.record_exchange_status(
                        record,
                        response,
                        recorded_at=int(time.time() * 1000),
                    )
                    item["wal_written"] = True
            report["orders"].append(item)
    finally:
        await client.close()
    if any(not item["resolved"] for item in report["orders"]):
        report["result"] = "FAIL_CLOSED"
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--wal-path", required=True)
    parser.add_argument("--symbol")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm")
    return parser.parse_args()


def main() -> int:
    try:
        report = asyncio.run(reconcile(parse_args()))
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"result": "FAIL_CLOSED", "error": {"code": type(exc).__name__, "message": str(exc)}}))
        return 2
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["result"] == "WAL_RECONCILE_OK" else 2


if __name__ == "__main__":
    sys.exit(main())
