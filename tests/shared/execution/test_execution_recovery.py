import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from trading_platform.shared.binance import BinanceOrderExecutor
from trading_platform.shared.events import OrderIntent
from trading_platform.shared.execution_recovery import (
    OrderWAL,
    SubmitUnknownPollingService,
    SubmitUnknownResolver,
)
from trading_platform.shared.risk import RiskConfig, RiskGuard


def make_intent(client_order_id: str = "cid-1") -> OrderIntent:
    return OrderIntent(
        symbol="BTCUSDT",
        side="SELL",
        price=Decimal("100"),
        quantity=Decimal("0.1"),
        client_order_id=client_order_id,
    )


def test_wal_fsync_and_recover_latest(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    intent = wal.record_intent(make_intent(), account_id="a-1", recorded_at=1000)
    unknown = wal.record_submit_unknown(intent, recorded_at=1001, error="timeout")

    latest = wal.recover_latest()
    assert latest["cid-1"].record_type == "submit_unknown"
    assert latest["cid-1"].status == "SUBMIT_UNKNOWN"
    assert latest["cid-1"].payload["error"] == "timeout"
    assert latest["cid-1"].payload["reduce_only"] is False
    assert len((tmp_path / "orders.jsonl").read_text().splitlines()) == 2
    assert unknown.client_order_id == "cid-1"


def test_wal_preserves_intent_created_at_across_order_statuses_and_restart(tmp_path):
    path = tmp_path / "orders.jsonl"
    wal = OrderWAL(path)
    intent = wal.record_intent(make_intent(), account_id="a-1", recorded_at=1000)
    new = wal.record_exchange_status(
        intent, {"orderId": 42, "status": "NEW"}, recorded_at=2000
    )
    wal.record_exchange_status(
        new,
        {"orderId": 42, "status": "PARTIALLY_FILLED", "executedQty": "0.05"},
        recorded_at=3000,
    )

    latest = OrderWAL(path).recover_latest()["cid-1"]

    assert latest.recorded_at == 3000
    assert latest.intent_created_at == 1000


def test_old_wal_derives_intent_created_at_from_first_intent_row(tmp_path):
    path = tmp_path / "orders.jsonl"
    rows = [
        {
            "record_type": "intent",
            "recorded_at": 1000,
            "account_id": "a-1",
            "client_order_id": "cid-1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": "0.1",
            "price": "100",
            "status": None,
            "exchange_order_id": None,
            "payload": {"ttl_ms": 10_000},
        },
        {
            "record_type": "exchange_status",
            "recorded_at": 3000,
            "account_id": "a-1",
            "client_order_id": "cid-1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": "0.1",
            "price": "100",
            "status": "PARTIALLY_FILLED",
            "exchange_order_id": "42",
            "payload": {"ttl_ms": 10_000},
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    latest = OrderWAL(path).recover_latest()["cid-1"]

    assert latest.intent_created_at == 1000


def test_wal_rejects_corrupt_record(tmp_path):
    path = tmp_path / "orders.jsonl"
    wal = OrderWAL(path)
    wal.record_intent(make_intent(), account_id="a-1", recorded_at=1000)
    with path.open("a") as stream:
        stream.write("not-json\n")
    with pytest.raises(ValueError, match="line 2"):
        OrderWAL(path).recover_latest()


class QueryClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def query_order(self, symbol, *, orig_client_order_id):
        assert symbol == "BTCUSDT"
        assert orig_client_order_id == "cid-1"
        if self.error:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_resolver_records_explicit_exchange_status(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    unknown = wal.record_submit_unknown(
        wal.record_intent(make_intent(), account_id="a-1", recorded_at=1000),
        recorded_at=1001,
        error="timeout",
    )
    result = await SubmitUnknownResolver(
        wal, QueryClient({"orderId": 42, "status": "PARTIALLY_FILLED"})
    ).resolve_once(unknown, recorded_at=1002)

    assert result.resolved is True
    assert result.status == "PARTIALLY_FILLED"
    latest = wal.recover_latest()["cid-1"]
    assert latest.record_type == "exchange_status"
    assert latest.exchange_order_id == "42"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "reason"),
    [(None, "order_not_found"), ({"status": "UNKNOWN"}, "unknown_exchange_status")],
)
async def test_resolver_keeps_unknown_without_guessing(tmp_path, response, reason):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    unknown = wal.record_submit_unknown(
        wal.record_intent(make_intent(), account_id="a-1", recorded_at=1000),
        recorded_at=1001,
        error="timeout",
    )
    result = await SubmitUnknownResolver(wal, QueryClient(response)).resolve_once(
        unknown, recorded_at=1002
    )
    assert result.resolved is False
    assert result.status is None
    assert result.reason == reason
    assert wal.recover_latest()["cid-1"].status == "SUBMIT_UNKNOWN"


@pytest.mark.asyncio
async def test_resolver_keeps_unknown_on_query_failure(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    unknown = wal.record_submit_unknown(
        wal.record_intent(make_intent(), account_id="a-1", recorded_at=1000),
        recorded_at=1001,
        error="timeout",
    )
    result = await SubmitUnknownResolver(
        wal, QueryClient(error=RuntimeError("network down"))
    ).resolve_once(unknown, recorded_at=1002)
    assert result.resolved is False
    assert result.reason == "query_failed:RuntimeError"
    assert len((tmp_path / "orders.jsonl").read_text().splitlines()) == 2


@pytest.mark.asyncio
async def test_live_executor_writes_wal_before_submit_and_records_exchange_fact(tmp_path):
    rest = Mock(
        post_order=AsyncMock(return_value={"orderId": 42, "status": "NEW"}),
        query_order=AsyncMock(),
    )
    executor = BinanceOrderExecutor(
        rest,
        OrderWAL(tmp_path / "orders.jsonl"),
        account_id="account-1",
        now_ms=iter([1000, 1001]).__next__,
    )

    record = await executor.submit(make_intent())

    assert record.record_type == "exchange_status"
    assert record.status == "NEW"
    assert record.exchange_order_id == "42"
    rest.post_order.assert_awaited_once_with(
        symbol="BTCUSDT",
        side="SELL",
        order_type="LIMIT",
        quantity=Decimal("0.1"),
        price=Decimal("100"),
        new_client_order_id="cid-1",
        reduce_only=False,
    )
    lines = (tmp_path / "orders.jsonl").read_text().splitlines()
    assert [json.loads(line)["record_type"] for line in lines] == [
        "intent", "exchange_status"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submit_error",
    [httpx.ReadTimeout("timeout"), RuntimeError("connection reset")],
)
async def test_live_executor_transport_failure_stays_unknown_and_does_not_resubmit(
    tmp_path, submit_error
):
    rest = Mock(
        post_order=AsyncMock(side_effect=submit_error),
        query_order=AsyncMock(),
    )
    guard = RiskGuard("account-1", RiskConfig())
    executor = BinanceOrderExecutor(
        rest,
        OrderWAL(tmp_path / "orders.jsonl"),
        account_id="account-1",
        now_ms=iter([1000, 1001]).__next__,
        risk_guard=guard,
    )

    first = await executor.submit(make_intent())
    second = await executor.submit(make_intent())

    assert first.status == "SUBMIT_UNKNOWN"
    assert second == first
    assert rest.post_order.await_count == 1
    assert "BTCUSDT" in guard.blocked_symbols


@pytest.mark.asyncio
async def test_live_executor_recovered_intent_is_not_resubmitted(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    wal.record_intent(make_intent(), account_id="account-1", recorded_at=1000)
    rest = Mock(post_order=AsyncMock(), query_order=AsyncMock())
    executor = BinanceOrderExecutor(
        rest, wal, account_id="account-1", now_ms=lambda: 1001
    )

    record = await executor.submit(make_intent())

    assert record.status == "SUBMIT_UNKNOWN"
    assert record.payload["error"] == "recovered_unresolved_intent"
    rest.post_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_executor_resolves_each_recovered_unknown_once(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    for index, client_order_id in enumerate(("cid-1", "cid-2")):
        intent = wal.record_intent(
            make_intent(client_order_id),
            account_id="account-1",
            recorded_at=1000 + index * 2,
        )
        wal.record_submit_unknown(
            intent,
            recorded_at=1001 + index * 2,
            error="timeout",
        )
    rest = Mock(
        post_order=AsyncMock(),
        query_order=AsyncMock(
            side_effect=[
                {"orderId": 41, "status": "NEW"},
                None,
            ]
        ),
    )
    guard = RiskGuard("account-1", RiskConfig())
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="account-1",
        now_ms=iter([2000, 2001]).__next__,
        risk_guard=guard,
    )

    results = await executor.resolve_recovered_unknowns_once()

    assert results["cid-1"].resolved is True
    assert results["cid-2"].reason == "order_not_found"
    assert rest.query_order.await_count == 2
    latest = wal.recover_latest()
    assert latest["cid-1"].status == "NEW"
    assert latest["cid-2"].status == "SUBMIT_UNKNOWN"
    assert "BTCUSDT" in guard.blocked_symbols


@pytest.mark.asyncio
async def test_live_executor_unblocks_symbol_after_last_unknown_is_resolved(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    intent = wal.record_intent(make_intent(), account_id="account-1", recorded_at=1000)
    unknown = wal.record_submit_unknown(intent, recorded_at=1001, error="timeout")
    rest = Mock(
        post_order=AsyncMock(),
        query_order=AsyncMock(return_value={"orderId": 42, "status": "NEW"}),
    )
    guard = RiskGuard("account-1", RiskConfig())
    guard.block_symbol("BTCUSDT", "SUBMIT_UNKNOWN pending")
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="account-1",
        now_ms=lambda: 2000,
        risk_guard=guard,
    )

    result = await executor.resolve_submit_unknown(unknown)

    assert result.resolved is True
    assert "BTCUSDT" not in guard.blocked_symbols


@pytest.mark.asyncio
async def test_startup_recovery_converts_bare_intent_to_blocking_unknown(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    wal.record_intent(make_intent(), account_id="account-1", recorded_at=1000)
    rest = Mock(post_order=AsyncMock(), query_order=AsyncMock(return_value=None))
    guard = RiskGuard("account-1", RiskConfig())
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="account-1",
        now_ms=iter([2000, 2001]).__next__,
        risk_guard=guard,
    )

    result = await executor.resolve_recovered_unknowns_once()

    assert result["cid-1"].reason == "order_not_found"
    assert wal.recover_latest()["cid-1"].status == "SUBMIT_UNKNOWN"
    assert "BTCUSDT" in guard.blocked_symbols
    rest.post_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange_status", ["NEW", "FILLED"])
async def test_unknown_poller_retries_until_known_and_unblocks_symbol(
    tmp_path, exchange_status
):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    intent = wal.record_intent(make_intent(), account_id="account-1", recorded_at=1000)
    wal.record_submit_unknown(intent, recorded_at=1001, error="timeout")
    rest = Mock(
        post_order=AsyncMock(),
        query_order=AsyncMock(
            side_effect=[None, {"orderId": 42, "status": exchange_status}]
        ),
    )
    guard = RiskGuard("account-1", RiskConfig())
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="account-1",
        now_ms=iter([2000, 2001]).__next__,
        risk_guard=guard,
    )
    poller = SubmitUnknownPollingService(
        executor, poll_interval_seconds=0, max_attempts=3
    )

    results = await poller.run()

    assert results["cid-1"].resolved is True
    assert poller.attempts == 2
    assert wal.recover_latest()["cid-1"].status == exchange_status
    assert "BTCUSDT" not in guard.blocked_symbols


@pytest.mark.asyncio
async def test_unknown_poller_attempt_limit_keeps_symbol_blocked(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    intent = wal.record_intent(make_intent(), account_id="account-1", recorded_at=1000)
    wal.record_submit_unknown(intent, recorded_at=1001, error="timeout")
    rest = Mock(post_order=AsyncMock(), query_order=AsyncMock(return_value=None))
    guard = RiskGuard("account-1", RiskConfig())
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="account-1",
        now_ms=iter([2000, 2001]).__next__,
        risk_guard=guard,
    )
    poller = SubmitUnknownPollingService(
        executor, poll_interval_seconds=0, max_attempts=2
    )

    results = await poller.run()

    assert results["cid-1"].reason == "order_not_found"
    assert poller.attempts == 2
    assert rest.query_order.await_count == 2
    assert wal.recover_latest()["cid-1"].status == "SUBMIT_UNKNOWN"
    assert "BTCUSDT" in guard.blocked_symbols
    failure = await asyncio.wait_for(poller.wait_fatal(), timeout=1)
    assert str(failure) == "SUBMIT_UNKNOWN resolution attempts exhausted"


@pytest.mark.asyncio
async def test_unknown_poller_query_errors_fail_closed(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    intent = wal.record_intent(make_intent(), account_id="account-1", recorded_at=1000)
    wal.record_submit_unknown(intent, recorded_at=1001, error="timeout")
    rest = Mock(
        post_order=AsyncMock(),
        query_order=AsyncMock(side_effect=RuntimeError("network down")),
    )
    guard = RiskGuard("account-1", RiskConfig())
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="account-1",
        now_ms=iter([2000, 2001]).__next__,
        risk_guard=guard,
    )
    poller = SubmitUnknownPollingService(
        executor, poll_interval_seconds=0, max_attempts=2
    )

    results = await poller.run()

    assert results["cid-1"].reason == "query_failed:RuntimeError"
    assert rest.query_order.await_count == 2
    assert wal.recover_latest()["cid-1"].status == "SUBMIT_UNKNOWN"
    assert "BTCUSDT" in guard.blocked_symbols
    assert poller.fatal_exception is not None


@pytest.mark.asyncio
async def test_unknown_poller_recovers_from_orchestration_error_fail_closed():
    resolver = Mock(
        resolve_recovered_unknowns_once=AsyncMock(
            side_effect=[RuntimeError("temporary failure"), {}]
        )
    )
    poller = SubmitUnknownPollingService(
        resolver, poll_interval_seconds=0, max_attempts=2
    )

    results = await poller.run()

    assert results == {}
    assert poller.attempts == 2
    assert poller.last_error is None
    assert resolver.resolve_recovered_unknowns_once.await_count == 2


@pytest.mark.asyncio
async def test_unknown_poller_start_is_idempotent_and_stop_cancels_task():
    entered_sleep = asyncio.Event()

    async def wait_forever(_seconds):
        entered_sleep.set()
        await asyncio.Event().wait()

    resolver = Mock(
        resolve_recovered_unknowns_once=AsyncMock(
            return_value={"cid-1": Mock(resolved=False)}
        )
    )
    poller = SubmitUnknownPollingService(
        resolver,
        poll_interval_seconds=1,
        max_attempts=2,
        sleep=wait_forever,
    )

    first = poller.start()
    second = poller.start()
    await entered_sleep.wait()
    await poller.stop()

    assert first is second
    assert first.cancelled()
    assert poller.is_running is False
