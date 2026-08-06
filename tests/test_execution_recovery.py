import json
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from trading_platform.shared.binance import BinanceOrderExecutor
from trading_platform.shared.events import OrderIntent
from trading_platform.shared.execution_recovery import (
    OrderWAL,
    SubmitUnknownResolver,
)


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
    assert len((tmp_path / "orders.jsonl").read_text().splitlines()) == 2
    assert unknown.client_order_id == "cid-1"


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
    executor = BinanceOrderExecutor(
        rest,
        OrderWAL(tmp_path / "orders.jsonl"),
        account_id="account-1",
        now_ms=iter([1000, 1001]).__next__,
    )

    first = await executor.submit(make_intent())
    second = await executor.submit(make_intent())

    assert first.status == "SUBMIT_UNKNOWN"
    assert second == first
    assert rest.post_order.await_count == 1


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
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="account-1",
        now_ms=iter([2000, 2001]).__next__,
    )

    results = await executor.resolve_recovered_unknowns_once()

    assert results["cid-1"].resolved is True
    assert results["cid-2"].reason == "order_not_found"
    assert rest.query_order.await_count == 2
    latest = wal.recover_latest()
    assert latest["cid-1"].status == "NEW"
    assert latest["cid-2"].status == "SUBMIT_UNKNOWN"
