import json
from decimal import Decimal

import pytest

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
