#!/usr/bin/env python3
"""Static preflight and persisted health checks for long_breakout."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trading_platform.shared.config import BinanceConfig
from trading_platform.strategies.long_breakout.runtime import (
    OPERATIONAL_GATES,
    STRATEGY_ID,
    TESTNET_REST_URL,
    TESTNET_WS_URL,
)
from trading_platform.strategies.long_breakout.settings import LongBreakoutSettings


def _strict_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def _long_breakout_values(env: Mapping[str, str]) -> dict[str, str]:
    prefix = "LONG_BREAKOUT_"
    return {
        key.removeprefix(prefix).lower(): value
        for key, value in env.items()
        if key.startswith(prefix)
    }


def _resolved_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def validate_preflight(environ: Mapping[str, str] | None = None) -> None:
    env = os.environ if environ is None else environ
    settings = LongBreakoutSettings(
        _env_file=None, **_long_breakout_values(env)
    )
    strategy_account_id = env.get("STRATEGY_ACCOUNT_ID", "").strip()
    if strategy_account_id != settings.account_id:
        raise ValueError(
            "STRATEGY_ACCOUNT_ID must match LONG_BREAKOUT_ACCOUNT_ID"
        )
    spike_account_id = env.get("SPIKE_ACCOUNT_ID", "").strip()
    if spike_account_id and spike_account_id == settings.account_id:
        raise ValueError("long_breakout and Spike must use different account IDs")
    spike_wal_path = env.get("SPIKE_WAL_PATH", "").strip()
    if spike_wal_path and _resolved_path(spike_wal_path) == _resolved_path(
        settings.wal_path
    ):
        raise ValueError("long_breakout and Spike must use different WAL paths")
    testnet_raw = env.get("BINANCE_TESTNET")
    if testnet_raw is None or not _strict_bool(testnet_raw, name="BINANCE_TESTNET"):
        raise ValueError("long_breakout requires BINANCE_TESTNET=true")
    if not env.get("BINANCE_API_KEY", "").strip() or not env.get(
        "BINANCE_API_SECRET", ""
    ).strip():
        raise ValueError("dedicated Binance testnet credentials are required")
    binance = BinanceConfig(
        _env_file=None,
        api_key=env["BINANCE_API_KEY"],
        api_secret=env["BINANCE_API_SECRET"],
        testnet=True,
        base_url=env.get("BINANCE_BASE_URL", TESTNET_REST_URL),
        ws_base_url=env.get("BINANCE_WS_BASE_URL", TESTNET_WS_URL),
    )
    if binance.base_url.rstrip("/") != TESTNET_REST_URL:
        raise ValueError("long_breakout requires the Binance Futures testnet REST URL")
    if binance.ws_base_url.rstrip("/") != TESTNET_WS_URL:
        raise ValueError("long_breakout requires the Binance Futures testnet WS URL")


def check_runtime_health(
    *,
    ledger_url: str,
    account_id: str,
    timeout: float = 2.0,
) -> None:
    query = urllib.parse.urlencode(
        {"account_id": account_id, "strategy_id": STRATEGY_ID, "limit": 2}
    )
    url = f"{ledger_url.rstrip('/')}/api/v1/strategy-runtime-status?{query}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload: Any = json.load(response)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        raise RuntimeError("expected exactly one long_breakout runtime status")
    item = items[0]
    if not isinstance(item, dict):
        raise RuntimeError("invalid long_breakout runtime status response")
    if item.get("mode") != "testnet":
        raise RuntimeError("long_breakout runtime is not in testnet mode")
    if (
        item.get("status") != "running"
        or item.get("effective_status") != "running"
        or item.get("halted") is not False
        or item.get("stopped_at") is not None
    ):
        raise RuntimeError("long_breakout runtime is not healthy")
    gates = item.get("gate_conditions")
    if not isinstance(gates, dict) or any(
        gates.get(name) is not True for name in OPERATIONAL_GATES
    ):
        raise RuntimeError("long_breakout operational gates are not healthy")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight")
    health = subparsers.add_parser("health")
    health.add_argument(
        "--ledger-url",
        default=os.getenv("STRATEGY_LEDGER_API_URL", "http://ledger:8001"),
    )
    health.add_argument(
        "--account-id", default=os.getenv("LONG_BREAKOUT_ACCOUNT_ID", "")
    )
    health.add_argument("--timeout", type=float, default=2.0)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "preflight":
        validate_preflight()
        return
    if not args.account_id:
        raise ValueError("LONG_BREAKOUT_ACCOUNT_ID is required")
    check_runtime_health(
        ledger_url=args.ledger_url,
        account_id=args.account_id,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()
