#!/usr/bin/env python3
"""Observe one already-running Spike testnet instance for a fixed duration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode, urlsplit

import httpx

from trading_platform.shared.binance.rest_client import BinanceRestClient
from trading_platform.shared.config import BinanceConfig


TESTNET_HOST = "demo-fapi.binance.com"
CONFIRMATION = "I_UNDERSTAND_THIS_OBSERVES_THE_TESTNET_ACCOUNT"
REQUIRED_GATES = ("execution", "market", "bar_stream")


class SoakFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _strict_testnet_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
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
        raise SoakFailure(
            "TESTNET_ENDPOINT_REQUIRED",
            f"BINANCE_BASE_URL must be https://{TESTNET_HOST}",
        )
    return normalized


def validate_environment(confirmation: str | None) -> BinanceConfig:
    if os.getenv("BINANCE_TESTNET", "").strip().lower() != "true":
        raise SoakFailure("TESTNET_FLAG_REQUIRED", "BINANCE_TESTNET must be exactly true")
    explicit = os.getenv("BINANCE_BASE_URL")
    if explicit is not None:
        _strict_testnet_url(explicit)
    config = BinanceConfig()
    _strict_testnet_url(config.base_url)
    if confirmation != CONFIRMATION:
        raise SoakFailure(
            "CONFIRMATION_REQUIRED", f"--confirm must equal {CONFIRMATION}"
        )
    if not config.api_key or not config.api_secret:
        raise SoakFailure("CREDENTIALS_REQUIRED", "testnet API credentials are required")
    return config


def _parse_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise SoakFailure("RUNTIME_RESPONSE_INVALID", f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SoakFailure(
            "RUNTIME_RESPONSE_INVALID", f"{field} must be an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise SoakFailure("RUNTIME_RESPONSE_INVALID", f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _nonzero_positions(rows: list[dict[str, Any]]) -> list[str]:
    symbols: list[str] = []
    for row in rows:
        try:
            amount = Decimal(str(row.get("positionAmt", "0")))
        except (InvalidOperation, ValueError) as exc:
            raise SoakFailure(
                "POSITION_RESPONSE_INVALID", "invalid account position amount"
            ) from exc
        if not amount.is_finite():
            raise SoakFailure(
                "POSITION_RESPONSE_INVALID", "invalid account position amount"
            )
        if amount:
            symbols.append(str(row.get("symbol") or "unknown"))
    return sorted(symbols)


def validate_runtime_sample(
    item: dict[str, Any],
    *,
    expected_instance_id: str | None,
    expected_entry_enabled: str,
    now: datetime,
    heartbeat_max_age_seconds: float,
) -> tuple[str, datetime]:
    instance_id = item.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise SoakFailure("RUNTIME_RESPONSE_INVALID", "runtime instance_id is missing")
    if expected_instance_id is not None and instance_id != expected_instance_id:
        raise SoakFailure("INSTANCE_CHANGED", "Spike instance changed during soak")
    if item.get("mode") != "testnet":
        raise SoakFailure("RUNTIME_MODE_INVALID", "runtime mode is not testnet")
    if item.get("halted") is not False:
        raise SoakFailure("RUNTIME_HALTED", "runtime risk guard is halted")
    effective_status = item.get("effective_status")
    if effective_status == "degraded":
        raise SoakFailure(
            "RUNTIME_DEGRADED", "runtime is temporarily degraded"
        )
    if effective_status != "running":
        raise SoakFailure(
            "RUNTIME_NOT_RUNNING",
            f"runtime effective status is {effective_status}",
        )
    entry_enabled = item.get("entry_enabled")
    if expected_entry_enabled != "any":
        expected = expected_entry_enabled == "true"
        if entry_enabled is not expected:
            raise SoakFailure(
                "ENTRY_STATE_MISMATCH",
                f"entry_enabled must remain {str(expected).lower()}",
            )
    gates = item.get("gate_conditions")
    if not isinstance(gates, dict):
        raise SoakFailure("RUNTIME_RESPONSE_INVALID", "gate_conditions is missing")
    blocked = [name for name in REQUIRED_GATES if gates.get(name) is not True]
    if blocked:
        raise SoakFailure(
            "SAFETY_GATE_CLOSED", f"required safety gates closed: {','.join(blocked)}"
        )
    heartbeat = _parse_time(item.get("heartbeat_at"), field="heartbeat_at")
    age = (now - heartbeat).total_seconds()
    if age < -5 or age > heartbeat_max_age_seconds:
        raise SoakFailure(
            "HEARTBEAT_STALE", f"runtime heartbeat age is {age:.3f} seconds"
        )
    return instance_id, heartbeat


async def collect_sample(
    *,
    http: httpx.AsyncClient,
    rest: Any,
    ledger_url: str,
    market_url: str,
    account_id: str,
    strategy_id: str,
    expected_instance_id: str | None,
    expected_entry_enabled: str,
    heartbeat_max_age_seconds: float,
    require_flat: bool,
    now: datetime,
) -> dict[str, Any]:
    query = urlencode({"account_id": account_id, "strategy_id": strategy_id})
    ledger_health, runtime_response, market_health, market_quality = await asyncio.gather(
        http.get(f"{ledger_url}/api/v1/health"),
        http.get(f"{ledger_url}/api/v1/strategy-runtime-status?{query}"),
        http.get(f"{market_url}/health"),
        http.get(f"{market_url}/quality"),
    )
    if ledger_health.status_code < 200 or ledger_health.status_code >= 300:
        raise SoakFailure("LEDGER_UNHEALTHY", "ledger health endpoint is unavailable")
    if runtime_response.status_code < 200 or runtime_response.status_code >= 300:
        raise SoakFailure("LEDGER_UNHEALTHY", "ledger runtime endpoint is unavailable")
    if market_health.status_code < 200 or market_health.status_code >= 300:
        raise SoakFailure("MARKET_UNHEALTHY", "market health endpoint is unavailable")
    if market_quality.status_code < 200 or market_quality.status_code >= 300:
        raise SoakFailure("MARKET_UNHEALTHY", "market quality endpoint is unavailable")
    if ledger_health.json().get("status") != "healthy":
        raise SoakFailure("LEDGER_UNHEALTHY", "ledger health is not healthy")
    runtime_page = runtime_response.json()
    if runtime_page.get("total") != 1 or len(runtime_page.get("items", [])) != 1:
        raise SoakFailure("RUNTIME_NOT_FOUND", "expected exactly one Spike runtime")
    runtime = runtime_page["items"][0]
    instance_id, heartbeat = validate_runtime_sample(
        runtime,
        expected_instance_id=expected_instance_id,
        expected_entry_enabled=expected_entry_enabled,
        now=now,
        heartbeat_max_age_seconds=heartbeat_max_age_seconds,
    )
    market_health_payload = market_health.json()
    if (
        market_health_payload.get("status") != "ready"
        or market_health_payload.get("binance_testnet") is not True
        or market_quality.json().get("ready") is not True
    ):
        raise SoakFailure("MARKET_UNHEALTHY", "market health or quality is not ready")
    position_mode, orders, positions = await asyncio.gather(
        rest.get_position_mode(),
        rest.get_open_orders(),
        rest.get_position_risk(),
    )
    if position_mode.get("dualSidePosition") is not False:
        raise SoakFailure("HEDGE_MODE_UNSUPPORTED", "account is not in one-way mode")
    position_symbols = _nonzero_positions(positions)
    order_symbols = sorted({str(order.get("symbol") or "unknown") for order in orders})
    if require_flat and (orders or position_symbols):
        raise SoakFailure(
            "ACCOUNT_NOT_FLAT", "control-plane soak requires zero orders and positions"
        )
    return {
        "observed_at": now.isoformat(),
        "instance_id": instance_id,
        "heartbeat_at": heartbeat.isoformat(),
        "heartbeat_age_seconds": round((now - heartbeat).total_seconds(), 3),
        "entry_enabled": runtime.get("entry_enabled"),
        "gates": runtime.get("gate_conditions"),
        "open_order_count": len(orders),
        "open_order_symbols": order_symbols,
        "nonzero_position_count": len(position_symbols),
        "nonzero_position_symbols": position_symbols,
    }


def _transient_dependency_error_type(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (httpx.NetworkError, httpx.TimeoutException)):
            return type(current).__name__
        current = current.__cause__ or current.__context__
    return None


async def run_soak(
    args: argparse.Namespace,
    report: dict[str, Any],
    *,
    rest: Any,
    http: httpx.AsyncClient,
    monotonic: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    started = monotonic()
    deadline = started + args.duration_seconds
    instance_id: str | None = None
    last_heartbeat: datetime | None = None
    last_heartbeat_progress = started
    consecutive_errors = 0
    samples: list[dict[str, Any]] = []
    transient_errors: list[dict[str, Any]] = []
    runtime_recoveries: list[dict[str, Any]] = []
    recovery_started: float | None = None
    report.update(
        {
            "instance_id": None,
            "sample_count": 0,
            "actual_duration_seconds": 0.0,
            "last_sample": None,
            "transient_errors": transient_errors,
            "runtime_recoveries": runtime_recoveries,
        }
    )
    try:
        while True:
            sample_now = datetime.now(timezone.utc)
            try:
                sample = await collect_sample(
                    http=http,
                    rest=rest,
                    ledger_url=args.ledger_url.rstrip("/"),
                    market_url=args.market_url.rstrip("/"),
                    account_id=args.account_id,
                    strategy_id=args.strategy_id,
                    expected_instance_id=instance_id,
                    expected_entry_enabled=args.expect_entry_enabled,
                    heartbeat_max_age_seconds=args.heartbeat_max_age_seconds,
                    require_flat=args.require_flat,
                    now=sample_now,
                )
            except SoakFailure as exc:
                if (
                    exc.code != "RUNTIME_DEGRADED"
                    or args.runtime_recovery_seconds <= 0
                ):
                    raise
                current = monotonic()
                if recovery_started is None:
                    recovery_started = current
                    runtime_recoveries.append(
                        {
                            "started_at": sample_now.isoformat(),
                            "recovered_at": None,
                            "duration_seconds": None,
                        }
                    )
                if current - recovery_started > args.runtime_recovery_seconds:
                    raise SoakFailure(
                        "RUNTIME_RECOVERY_TIMEOUT",
                        "runtime did not recover within the configured window",
                    ) from exc
            except Exception as exc:
                error_type = _transient_dependency_error_type(exc)
                if error_type is None:
                    raise
                consecutive_errors += 1
                transient_errors.append(
                    {
                        "observed_at": sample_now.isoformat(),
                        "type": error_type,
                    }
                )
                if consecutive_errors > args.max_consecutive_errors:
                    raise SoakFailure(
                        "DEPENDENCY_UNREACHABLE",
                        "consecutive observer dependency failures exceeded limit",
                    ) from exc
            else:
                consecutive_errors = 0
                if recovery_started is not None:
                    runtime_recoveries[-1].update(
                        {
                            "recovered_at": sample_now.isoformat(),
                            "duration_seconds": round(
                                monotonic() - recovery_started, 3
                            ),
                        }
                    )
                    recovery_started = None
                heartbeat = _parse_time(sample["heartbeat_at"], field="heartbeat_at")
                if instance_id is None:
                    instance_id = sample["instance_id"]
                    report["instance_id"] = instance_id
                    last_heartbeat_progress = monotonic()
                if last_heartbeat is not None and heartbeat < last_heartbeat:
                    raise SoakFailure("HEARTBEAT_REVERSED", "runtime heartbeat moved backward")
                if last_heartbeat is None or heartbeat > last_heartbeat:
                    last_heartbeat_progress = monotonic()
                elif monotonic() - last_heartbeat_progress > args.heartbeat_max_age_seconds:
                    raise SoakFailure("HEARTBEAT_STALLED", "runtime heartbeat stopped advancing")
                last_heartbeat = heartbeat
                samples.append(sample)
            if monotonic() >= deadline:
                break
            await sleep(min(args.sample_seconds, max(0.0, deadline - monotonic())))
    finally:
        report.update(
            {
                "sample_count": len(samples),
                "actual_duration_seconds": round(monotonic() - started, 3),
                "last_sample": samples[-1] if samples else None,
                "transient_errors": transient_errors,
                "runtime_recoveries": runtime_recoveries,
            }
        )
    if recovery_started is not None:
        raise SoakFailure(
            "RUNTIME_NOT_RECOVERED",
            "runtime was still recovering when the soak duration ended",
        )
    if not samples:
        raise SoakFailure("NO_SAMPLES", "soak completed without a valid sample")
    report.update(
        {
            "result": "SOAK_OK",
            "sample_count": len(samples),
            "actual_duration_seconds": round(monotonic() - started, 3),
            "max_heartbeat_age_seconds": max(
                sample["heartbeat_age_seconds"] for sample in samples
            ),
            "max_open_order_count": max(sample["open_order_count"] for sample in samples),
            "max_nonzero_position_count": max(
                sample["nonzero_position_count"] for sample in samples
            ),
            "first_sample": samples[0],
            "last_sample": samples[-1],
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    parser.add_argument("--heartbeat-max-age-seconds", type=float, default=15.0)
    parser.add_argument("--runtime-recovery-seconds", type=float, default=0.0)
    parser.add_argument("--max-consecutive-errors", type=int, default=2)
    parser.add_argument("--account-id", default="spike_testnet")
    parser.add_argument("--strategy-id", default="spike_short")
    parser.add_argument("--ledger-url", default="http://127.0.0.1:8001")
    parser.add_argument("--market-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--expect-entry-enabled", choices=("false", "true", "any"), default="false"
    )
    parser.add_argument("--require-flat", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--report", type=Path)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.duration_seconds <= 0:
        raise SoakFailure("DURATION_INVALID", "duration must be positive")
    if args.sample_seconds <= 0:
        raise SoakFailure("SAMPLE_INTERVAL_INVALID", "sample interval must be positive")
    if args.heartbeat_max_age_seconds <= 0:
        raise SoakFailure("HEARTBEAT_LIMIT_INVALID", "heartbeat limit must be positive")
    if args.runtime_recovery_seconds < 0:
        raise SoakFailure(
            "RUNTIME_RECOVERY_LIMIT_INVALID",
            "runtime recovery limit must be non-negative",
        )
    if args.max_consecutive_errors < 0:
        raise SoakFailure("ERROR_LIMIT_INVALID", "error limit must be non-negative")


async def _run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    config = validate_environment(args.confirm)
    report.update(
        {
            "account_id": args.account_id,
            "strategy_id": args.strategy_id,
            "duration_seconds": args.duration_seconds,
            "sample_seconds": args.sample_seconds,
            "runtime_recovery_seconds": args.runtime_recovery_seconds,
            "require_flat": args.require_flat,
            "expect_entry_enabled": args.expect_entry_enabled,
            "endpoint": config.base_url,
        }
    )
    rest = BinanceRestClient(
        config.api_key, config.api_secret, base_url=config.base_url
    )
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            loop = asyncio.get_running_loop()
            await run_soak(
                args,
                report,
                rest=rest,
                http=http,
                monotonic=loop.time,
                sleep=asyncio.sleep,
            )
    finally:
        await rest.close()


def main() -> int:
    args = build_parser().parse_args()
    report: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat()}
    try:
        _validate_args(args)
        asyncio.run(_run(args, report))
        code = 0
    except KeyboardInterrupt:
        report.update(
            {
                "result": "INTERRUPTED",
                "error": {"code": "INTERRUPTED", "message": "soak was interrupted"},
            }
        )
        code = 130
    except SoakFailure as exc:
        report.update(
            {"result": "FAIL_CLOSED", "error": {"code": exc.code, "message": str(exc)}}
        )
        code = 2
    except Exception as exc:
        report.update(
            {
                "result": "FAIL_CLOSED",
                "error": {
                    "code": type(exc).__name__,
                    "message": "unexpected soak observer failure",
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
