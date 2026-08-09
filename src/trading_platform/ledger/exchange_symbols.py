"""Binance USD-M 交易对元数据同步及运维入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from trading_platform.shared.binance.rest_client import BinanceRestClient
from trading_platform.shared.config import DatabaseConfig

from .db.models import LedgerDB, create_connection_pool


logger = logging.getLogger(__name__)
BINANCE_USDM_METADATA_BASE_URL = "https://fapi.binance.com"
DEFAULT_SYMBOL_SYNC_INTERVAL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class ExchangeSymbolSyncReport:
    synced_symbols: int
    tradable_symbols: int


async def fetch_exchange_info_with_retry(
    fetch: Callable[[], Awaitable[dict[str, Any]]],
    *,
    attempts: int = 3,
    retry_base_seconds: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, int, Exception], None] | None = None,
) -> dict[str, Any]:
    """Fetch a structurally valid exchangeInfo payload with bounded retries."""

    total_attempts = max(1, attempts)
    last_error: Exception | None = None
    for attempt in range(total_attempts):
        try:
            exchange_info = await fetch()
            if not isinstance(exchange_info, dict) or not isinstance(
                exchange_info.get("symbols"), list
            ):
                raise ValueError("Binance exchangeInfo has incompatible symbol metadata")
            return exchange_info
        except asyncio.CancelledError:
            raise
        except Exception as error:
            last_error = error
        if attempt + 1 < total_attempts:
            assert last_error is not None
            if on_retry is not None:
                on_retry(attempt + 2, total_attempts, last_error)
            await sleep(max(0.0, retry_base_seconds) * (2**attempt))
    assert last_error is not None
    raise last_error


async def sync_exchange_symbol_metadata(
    db: LedgerDB,
    fetch: Callable[[], Awaitable[dict[str, Any]]],
    *,
    attempts: int = 3,
    retry_base_seconds: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, int, Exception], None] | None = None,
) -> tuple[int, dict[str, Any]]:
    exchange_info = await fetch_exchange_info_with_retry(
        fetch,
        attempts=attempts,
        retry_base_seconds=retry_base_seconds,
        sleep=sleep,
        on_retry=on_retry,
    )
    synced = await db.sync_exchange_symbols(exchange_info)
    return synced, exchange_info


async def run_exchange_symbol_sync_once(
    *,
    dsn: str,
    freeze_days: int = 15,
    attempts: int = 3,
    timeout: float = 10.0,
    on_retry: Callable[[int, int, Exception], None] | None = None,
) -> ExchangeSymbolSyncReport:
    rest = BinanceRestClient(
        "",
        "",
        base_url=BINANCE_USDM_METADATA_BASE_URL,
        timeout=timeout,
    )
    pool = None
    db = None
    try:
        pool = await create_connection_pool(dsn, min_size=1, max_size=2)
        db = LedgerDB(pool)
        synced, _ = await sync_exchange_symbol_metadata(
            db,
            rest.get_exchange_info,
            attempts=attempts,
            on_retry=on_retry,
        )
        tradable = len(
            await db.list_tradeable_exchange_symbols(freeze_days=freeze_days)
        )
        return ExchangeSymbolSyncReport(synced, tradable)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if db is not None:
            try:
                await db.mark_exchange_symbol_sync_failed(error)
            except Exception:
                logger.exception("failed to record exchange symbol sync failure")
        raise
    finally:
        await rest.close()
        if pool is not None:
            await pool.close()


async def run_exchange_symbol_sync_loop(
    *,
    dsn: str,
    interval_seconds: float = DEFAULT_SYMBOL_SYNC_INTERVAL_SECONDS,
    freeze_days: int = 15,
    attempts: int = 3,
    timeout: float = 10.0,
) -> None:
    while True:
        delay = interval_seconds
        try:
            report = await run_exchange_symbol_sync_once(
                dsn=dsn,
                freeze_days=freeze_days,
                attempts=attempts,
                timeout=timeout,
                on_retry=lambda attempt, total, error: logger.warning(
                    "exchangeInfo retry %s/%s: %s: %s",
                    attempt,
                    total,
                    type(error).__name__,
                    error,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("exchange symbol synchronization failed")
            delay = min(interval_seconds, 300.0)
        else:
            logger.info(
                "exchange symbol synchronization complete: synced=%s tradable=%s",
                report.synced_symbols,
                report.tradable_symbols,
            )
        await asyncio.sleep(delay)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize Binance USD-M symbols and categories to PostgreSQL."
    )
    parser.add_argument("--dsn", help="PostgreSQL DSN (default: DB_* settings)")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--freeze-days",
        type=int,
        default=os.getenv("SPIKE_DELISTING_FREEZE_DAYS", "15"),
    )
    parser.add_argument("--watch", action="store_true")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=os.getenv(
            "EXCHANGE_SYMBOL_SYNC_INTERVAL_SECONDS",
            str(DEFAULT_SYMBOL_SYNC_INTERVAL_SECONDS),
        ),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.attempts < 1:
        parser.error("--attempts must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.freeze_days < 0:
        parser.error("--freeze-days must be non-negative")
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    dsn = args.dsn or DatabaseConfig().dsn
    retry = lambda attempt, total, error: print(
        f"Retry {attempt}/{total} exchangeInfo: {type(error).__name__}: {error}",
        file=sys.stderr,
        flush=True,
    )
    try:
        if args.watch:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            )
            asyncio.run(
                run_exchange_symbol_sync_loop(
                    dsn=dsn,
                    interval_seconds=args.interval_seconds,
                    freeze_days=args.freeze_days,
                    attempts=args.attempts,
                    timeout=args.timeout,
                )
            )
            return 0
        report = asyncio.run(
            run_exchange_symbol_sync_once(
                dsn=dsn,
                freeze_days=args.freeze_days,
                attempts=args.attempts,
                timeout=args.timeout,
                on_retry=retry,
            )
        )
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        if args.json:
            print(json.dumps({"status": "failed", "error": str(error)}))
        else:
            print(f"Failed: {error}", file=sys.stderr)
        return 1
    payload = {
        "status": "complete",
        "synced_symbols": report.synced_symbols,
        "tradable_symbols": report.tradable_symbols,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            f"Synchronized {report.synced_symbols} exchange symbols; "
            f"{report.tradable_symbols} currently tradable."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
