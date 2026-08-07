#!/usr/bin/env python3
"""Exercise one real Binance testnet User Stream disconnect and recovery."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
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
from trading_platform.shared.execution_recovery import OrderWAL
from trading_platform.shared.postgres_lease import PostgresExecutionLease


TESTNET_HOST = "demo-fapi.binance.com"
TESTNET_WS_HOST = "stream.binancefuture.com"
CONFIRMATION = "I_UNDERSTAND_THIS_DISCONNECTS_THE_TESTNET_USER_STREAM"
ACCOUNT_ID = "spike_testnet"
STRATEGY_ID = "spike_short"


class ReconnectFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _strict_url(value: str, *, scheme: str, host: str, env_name: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
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
        raise ReconnectFailure(
            "TESTNET_ENDPOINT_REQUIRED",
            f"{env_name} must be {scheme}://{host}",
        )
    return normalized


def validate_environment(*, confirmation: str | None) -> tuple[BinanceConfig, DatabaseConfig]:
    if os.getenv("BINANCE_TESTNET", "").strip().lower() != "true":
        raise ReconnectFailure("TESTNET_FLAG_REQUIRED", "BINANCE_TESTNET must be exactly true")
    explicit_rest = os.getenv("BINANCE_BASE_URL")
    if explicit_rest is not None:
        _strict_url(
            explicit_rest,
            scheme="https",
            host=TESTNET_HOST,
            env_name="BINANCE_BASE_URL",
        )
    explicit_ws = os.getenv("BINANCE_WS_BASE_URL")
    if explicit_ws is not None:
        _strict_url(
            explicit_ws,
            scheme="wss",
            host=TESTNET_WS_HOST,
            env_name="BINANCE_WS_BASE_URL",
        )
    binance = BinanceConfig()
    _strict_url(
        binance.base_url,
        scheme="https",
        host=TESTNET_HOST,
        env_name="BINANCE_BASE_URL",
    )
    _strict_url(
        binance.ws_base_url,
        scheme="wss",
        host=TESTNET_WS_HOST,
        env_name="BINANCE_WS_BASE_URL",
    )
    if confirmation != CONFIRMATION:
        raise ReconnectFailure(
            "CONFIRMATION_REQUIRED", f"--confirm must equal {CONFIRMATION}"
        )
    if not binance.api_key or not binance.api_secret:
        raise ReconnectFailure("CREDENTIALS_REQUIRED", "testnet API credentials are required")
    return binance, DatabaseConfig()


def _has_nonzero_position(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        try:
            amount = Decimal(str(row.get("positionAmt", "0")))
        except (InvalidOperation, ValueError) as exc:
            raise ReconnectFailure(
                "POSITION_RESPONSE_INVALID", "invalid account position amount"
            ) from exc
        if not amount.is_finite():
            raise ReconnectFailure(
                "POSITION_RESPONSE_INVALID", "invalid account position amount"
            )
        if amount:
            return True
    return False


async def assert_idle_account(client: Any) -> None:
    if (await client.get_position_mode()).get("dualSidePosition") is not False:
        raise ReconnectFailure("HEDGE_MODE_UNSUPPORTED", "account is not in one-way mode")
    if await client.get_open_orders():
        raise ReconnectFailure("PREFLIGHT_OPEN_ORDERS", "account has pre-existing open orders")
    if _has_nonzero_position(await client.get_position_risk()):
        raise ReconnectFailure("PREFLIGHT_POSITION", "account has a pre-existing position")


async def run_reconnect(args: argparse.Namespace, report: dict[str, Any]) -> None:
    binance, database = validate_environment(confirmation=args.confirm)
    report.update(
        {
            "account_id": ACCOUNT_ID,
            "strategy_id": STRATEGY_ID,
            "symbol": args.symbol,
            "endpoint": binance.base_url,
            "ws_endpoint": binance.ws_base_url,
        }
    )
    client = BinanceRestClient(
        binance.api_key,
        binance.api_secret,
        base_url=binance.base_url,
    )
    pool = lease = runtime = None
    shutdown_errors: list[BaseException] = []
    try:
        await assert_idle_account(client)
        pool = await create_connection_pool(database.dsn)
        lease = PostgresExecutionLease(pool, ACCOUNT_ID)
        await lease.acquire()
        report["execution_lease_acquired"] = True
        rules = BinanceSymbolRuleBook.from_exchange_info(
            await client.get_exchange_info(), symbols=[args.symbol]
        )
        executor = BinanceOrderExecutor(
            client,
            OrderWAL(args.wal_path),
            account_id=ACCOUNT_ID,
            symbol_rules=rules,
        )
        runtime = create_binance_execution_runtime(
            rest_client=client,
            executor=executor,
            db=LedgerDB(pool),
            account_id=ACCOUNT_ID,
            strategy_id=STRATEGY_ID,
            managed_symbols=[args.symbol],
            dedicated_strategy_account=True,
            ws_base_url=binance.ws_base_url,
            poll_interval_seconds=5,
            max_poll_attempts=12,
        )
        disconnected = asyncio.Event()
        recovered = asyncio.Event()

        def on_disconnected() -> None:
            disconnected.set()

        def on_recovered() -> None:
            recovered.set()

        runtime.user_stream.on_disconnect = on_disconnected
        runtime.on_recovered = on_recovered
        await runtime.start()
        first_listen_key = runtime.user_stream.listen_key
        if not runtime.user_stream.connected or not first_listen_key:
            raise ReconnectFailure("INITIAL_CONNECT_FAILED", "initial User Stream is not ready")
        report["initial_connected"] = True
        report["initial_listen_key_changed"] = False
        websocket = runtime.user_stream.ws
        if websocket is None:
            raise ReconnectFailure("WEBSOCKET_MISSING", "connected stream has no websocket")
        websocket.close()
        try:
            async with asyncio.timeout(args.timeout_seconds):
                await disconnected.wait()
                await recovered.wait()
        except TimeoutError as exc:
            raise ReconnectFailure(
                "RECONNECT_TIMEOUT", "User Stream did not recover before timeout"
            ) from exc
        second_listen_key = runtime.user_stream.listen_key
        if not runtime.user_stream.connected:
            raise ReconnectFailure("RECONNECT_NOT_READY", "User Stream remained disconnected")
        if not second_listen_key or second_listen_key == first_listen_key:
            raise ReconnectFailure(
                "LISTEN_KEY_NOT_ROTATED", "reconnect did not create a new listenKey"
            )
        await assert_idle_account(client)
        report.update(
            {
                "disconnect_observed": True,
                "recovery_observed": True,
                "reconnected": True,
                "initial_listen_key_changed": True,
                "final_open_orders": 0,
                "final_nonzero_positions": 0,
                "result": "RECONNECT_OK",
            }
        )
    finally:
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
            raise BaseExceptionGroup("User Stream reconnect harness shutdown failed", shutdown_errors)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--confirm")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--wal-path",
        type=Path,
        default=Path("data/wal/testnet_user_stream_reconnect.jsonl"),
    )
    parser.add_argument("--report", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat()}
    try:
        if args.timeout_seconds <= 0:
            raise ReconnectFailure("TIMEOUT_INVALID", "timeout must be positive")
        asyncio.run(run_reconnect(args, report))
        code = 0
    except ReconnectFailure as exc:
        report.update(
            {"result": "FAIL_CLOSED", "error": {"code": exc.code, "message": str(exc)}}
        )
        code = 2
    except Exception as exc:
        report.update(
            {
                "result": "FAIL_CLOSED",
                "error": {
                    "code": "UNEXPECTED_ERROR",
                    "type": type(exc).__name__,
                    "message": "unexpected failure; inspect process logs",
                },
            }
        )
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
