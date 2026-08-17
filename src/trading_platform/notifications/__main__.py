"""Command-line entry point for the notification worker."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import signal
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import redis.asyncio as redis

from trading_platform.ledger.db.models import create_connection_pool
from trading_platform.notifications.adapters import (
    AdapterRegistry,
    EnvironmentSecretResolver,
    TelegramAdapter,
    WebhookAdapter,
)
from trading_platform.notifications.repository import NotificationRepository
from trading_platform.notifications.domain import Severity
from trading_platform.notifications.sources import (
    DomainEventBridge,
    PostgresNotificationSource,
    SourceNotification,
)
from trading_platform.notifications.wakeup import PollingWakeup, RedisWakeup
from trading_platform.notifications.worker import NotificationWorker
from trading_platform.shared.config import DatabaseConfig


def build_worker(
    repository: NotificationRepository,
    *,
    redis_client: Any | None = None,
    secret_resolver: Any | None = None,
    **worker_options: Any,
) -> NotificationWorker:
    """Build the production worker while keeping adapters injectable in tests."""

    resolver = secret_resolver or EnvironmentSecretResolver()
    adapters = AdapterRegistry(
        {
            "telegram": TelegramAdapter(resolver),
            "webhook": WebhookAdapter(resolver),
        }
    )
    wakeup = RedisWakeup(redis_client) if redis_client is not None else PollingWakeup()
    return NotificationWorker(
        repository,
        adapters,
        wakeup=wakeup,
        **worker_options,
    )


async def _run(args: argparse.Namespace) -> None:
    dsn = args.dsn or os.getenv("NOTIFICATION_DB_DSN") or DatabaseConfig().dsn
    pool = await create_connection_pool(
        dsn,
        min_size=max(1, args.db_min_size),
        max_size=max(args.db_min_size, args.db_max_size),
    )
    redis_client = None
    if not args.no_redis:
        redis_url = args.redis_url or os.getenv(
            "NOTIFICATION_REDIS_URL", "redis://localhost:6379/0"
        )
        redis_client = redis.from_url(redis_url, decode_responses=False)
    worker = build_worker(
        NotificationRepository(pool),
        redis_client=redis_client,
        concurrency=args.concurrency,
        batch_size=args.batch_size,
        lease_seconds=args.lease_seconds,
        max_attempts=args.max_attempts,
        poll_interval_seconds=args.poll_interval,
    )

    repository = worker.repository
    bridge = DomainEventBridge(
        PostgresNotificationSource(pool),
        lambda event: _publish_source_event(repository, event, worker.wakeup),
        signal_lookback=timedelta(seconds=max(1, args.bridge_lookback_seconds)),
    )
    worker.source_bridge = bridge

    loop = asyncio.get_running_loop()
    for signal_name in ("SIGTERM", "SIGINT"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is not None:
            try:
                loop.add_signal_handler(signal_value, worker.stop)
            except (NotImplementedError, RuntimeError):
                pass
    try:
        if args.once:
            await worker.run_once()
        else:
            await worker.run()
    finally:
        await worker.aclose()
        if redis_client is not None:
            await redis_client.aclose()
        result = pool.close()
        if inspect.isawaitable(result):
            await result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Notification delivery worker")
    parser.add_argument("--dsn", help="PostgreSQL DSN; defaults to DB_* settings")
    parser.add_argument("--redis-url", help="Redis URL for best-effort wakeups")
    parser.add_argument("--no-redis", action="store_true")
    parser.add_argument("--once", action="store_true", help="process one batch and exit")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument(
        "--bridge-lookback-seconds",
        type=int,
        default=int(os.getenv("NOTIFICATION_BRIDGE_LOOKBACK_SECONDS", "900")),
    )
    parser.add_argument("--db-min-size", type=int, default=1)
    parser.add_argument("--db-max-size", type=int, default=4)
    return parser


async def _publish_source_event(
    repository: NotificationRepository,
    event: SourceNotification,
    wakeup: Any,
) -> object:
    result = await repository.publish_event(
        event_type=event.event_type,
        severity=Severity(event.severity),
        source=event.source,
        title=event.title,
        body=event.body,
        payload=event.payload,
        idempotency_key=event.idempotency_key,
        correlation_id=event.correlation_id,
        fingerprint=event.fingerprint,
        occurred_at=event.occurred_at,
        expires_at=event.expires_at,
    )
    event_id = getattr(result.event, "id", None)
    if event_id is not None:
        await wakeup.notify(str(event_id))
    return result


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
