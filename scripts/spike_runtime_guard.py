#!/usr/bin/env python3
"""Side-effect-free Spike configuration and runtime health checks."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

STRATEGY_ID = "spike_short"
REQUIRED_HEALTH_GATES = frozenset(
    {
        "execution",
        "market",
        "bar_stream",
        "bar_continuity",
        "exchange_symbols",
        "event_queue",
        "capital",
        "metrics_5m",
    }
)


def _strict_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def validate_preflight(environ: Mapping[str, str] | None = None) -> None:
    """Validate the complete process configuration without opening resources."""

    from trading_platform.shared.config import (
        BinanceConfig,
        DatabaseConfig,
        RedisConfig,
        StrategyConfig,
    )
    from trading_platform.strategies.spike.live import SpikeLiveSettings
    from trading_platform.strategies.spike.main import SpikeLiveProcess

    env = os.environ if environ is None else environ
    settings = SpikeLiveSettings(_env_file=None, **_spike_values(env))
    account_id = env.get("STRATEGY_ACCOUNT_ID", "").strip()
    if not account_id:
        raise ValueError("STRATEGY_ACCOUNT_ID is required")

    testnet_raw = env.get("BINANCE_TESTNET")
    if testnet_raw is None:
        raise ValueError("BINANCE_TESTNET is required")
    testnet = _strict_bool(testnet_raw, name="BINANCE_TESTNET")
    expected_testnet = settings.mode == "testnet"
    if testnet is not expected_testnet:
        raise ValueError("SPIKE_MODE and BINANCE_TESTNET must select the same environment")
    if not env.get("BINANCE_API_KEY", "").strip() or not env.get(
        "BINANCE_API_SECRET", ""
    ).strip():
        raise ValueError("Binance API credentials are required")

    binance = BinanceConfig(
        api_key=env["BINANCE_API_KEY"],
        api_secret=env["BINANCE_API_SECRET"],
        testnet=testnet,
    )
    SpikeLiveProcess(
        settings,
        binance=binance,
        database=DatabaseConfig(),
        redis_config=RedisConfig(),
        strategy_config=StrategyConfig(account_id=account_id),
    )


def _spike_values(env: Mapping[str, str]) -> dict[str, str]:
    prefix = "SPIKE_"
    return {
        key.removeprefix(prefix).lower(): value
        for key, value in env.items()
        if key.startswith(prefix)
    }


def check_runtime_health(
    *,
    ledger_url: str,
    account_id: str,
    expected_mode: str,
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
        raise RuntimeError("expected exactly one Spike runtime status")
    item = items[0]
    if not isinstance(item, dict):
        raise RuntimeError("invalid Spike runtime status response")
    if item.get("mode") != expected_mode:
        raise RuntimeError("Spike runtime mode does not match container configuration")
    if (
        item.get("status") != "running"
        or item.get("effective_status") != "running"
        or item.get("halted") is not False
        or item.get("stopped_at") is not None
    ):
        raise RuntimeError("Spike runtime is not healthy")
    gates = item.get("gate_conditions")
    if not isinstance(gates, dict) or any(
        gates.get(name) is not True for name in REQUIRED_HEALTH_GATES
    ):
        raise RuntimeError("Spike runtime safety gates are not healthy")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight")
    health = subparsers.add_parser("health")
    health.add_argument(
        "--ledger-url", default=os.getenv("STRATEGY_LEDGER_API_URL", "http://ledger:8001")
    )
    health.add_argument("--account-id", default=os.getenv("SPIKE_ACCOUNT_ID", ""))
    health.add_argument("--mode", default=os.getenv("SPIKE_MODE", ""))
    health.add_argument("--timeout", type=float, default=2.0)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "preflight":
        validate_preflight()
        return
    if not args.account_id or args.mode not in {"testnet", "live"}:
        raise ValueError("SPIKE_ACCOUNT_ID and a valid SPIKE_MODE are required")
    check_runtime_health(
        ledger_url=args.ledger_url,
        account_id=args.account_id,
        expected_mode=args.mode,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()
