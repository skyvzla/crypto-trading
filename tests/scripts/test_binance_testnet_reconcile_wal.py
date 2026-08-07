import argparse
import importlib.util
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.shared.events import OrderIntent
from trading_platform.shared.execution_recovery import OrderWAL


SCRIPT = Path(__file__).parents[2] / "scripts" / "binance_testnet_reconcile_wal.py"
SPEC = importlib.util.spec_from_file_location("binance_testnet_reconcile_wal", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
reconcile_wal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconcile_wal)


@pytest.fixture(autouse=True)
def isolated_execution_lease(monkeypatch):
    events = []

    @asynccontextmanager
    async def lease(account_id):
        events.append(("acquire", account_id))
        try:
            yield
        finally:
            events.append(("release", account_id))

    monkeypatch.setattr(reconcile_wal, "exclusive_testnet_account", lease)
    return events


def set_testnet_environment(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://demo-fapi.binance.com")
    monkeypatch.setenv("BINANCE_API_KEY", "test-api-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-api-secret")


def write_unknown(wal_path: Path, client_order_id: str, *, symbol="BTCUSDT"):
    wal = OrderWAL(wal_path)
    intent = OrderIntent(
        symbol=symbol,
        side="SELL",
        price=Decimal("100"),
        quantity=Decimal("0.01"),
        client_order_id=client_order_id,
    )
    record = wal.record_intent(intent, account_id="testnet", recorded_at=1)
    wal.record_submit_unknown(record, recorded_at=2, error="timeout")


def args(wal_path: Path, *, execute=False):
    return argparse.Namespace(
        account_id="testnet",
        wal_path=wal_path,
        symbol=None,
        execute=execute,
        confirm=reconcile_wal.CONFIRMATION if execute else None,
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.closed = False

    async def query_order(self, symbol, *, orig_client_order_id):
        response = self.responses[orig_client_order_id]
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self):
        self.closed = True


def order_response(client_order_id, *, status="FILLED", symbol="BTCUSDT"):
    return {
        "symbol": symbol,
        "orderId": 42,
        "clientOrderId": client_order_id,
        "status": status,
        "side": "SELL",
        "type": "LIMIT",
        "origQty": "0.01",
        "executedQty": "0.01",
        "price": "100",
    }


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("BINANCE_TESTNET", "false", "TESTNET_FLAG_REQUIRED"),
        ("BINANCE_BASE_URL", "https://fapi.binance.com", "TESTNET_ENDPOINT_REQUIRED"),
        ("BINANCE_BASE_URL", "http://demo-fapi.binance.com", "TESTNET_ENDPOINT_REQUIRED"),
        ("BINANCE_BASE_URL", "https://demo-fapi.binance.com/fapi", "TESTNET_ENDPOINT_REQUIRED"),
    ],
)
def test_environment_requires_strict_testnet(
    monkeypatch, capsys, name, value, code
):
    set_testnet_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit) as failure:
        reconcile_wal.validate_environment(False, None)

    assert failure.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == code


@pytest.mark.parametrize("confirmation", [None, "", "wrong"])
def test_execute_requires_exact_confirmation(monkeypatch, capsys, confirmation):
    set_testnet_environment(monkeypatch)

    with pytest.raises(SystemExit) as failure:
        reconcile_wal.validate_environment(True, confirmation)

    assert failure.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == (
        "CONFIRMATION_REQUIRED"
    )


@pytest.mark.asyncio
async def test_dry_run_reports_known_status_without_writing_wal(monkeypatch, tmp_path):
    set_testnet_environment(monkeypatch)
    wal_path = tmp_path / "orders.jsonl"
    write_unknown(wal_path, "order-1")
    before = wal_path.read_bytes()
    client = FakeClient({"order-1": order_response("order-1")})
    monkeypatch.setattr(reconcile_wal, "BinanceRestClient", lambda **kwargs: client)

    report = await reconcile_wal.reconcile(args(wal_path))

    assert report["result"] == "WAL_RECONCILE_OK"
    assert report["orders"][0]["resolved"] is True
    assert report["orders"][0]["wal_written"] is False
    assert wal_path.read_bytes() == before
    assert client.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exchange_status", "wal_status"),
    [("CANCELED", "CANCELLED"), ("REJECTED", "REJECTED")],
)
async def test_execute_writes_verified_known_status(
    monkeypatch, tmp_path, exchange_status, wal_status
):
    set_testnet_environment(monkeypatch)
    wal_path = tmp_path / "orders.jsonl"
    write_unknown(wal_path, "order-1")
    client = FakeClient({
        "order-1": order_response("order-1", status=exchange_status),
    })
    monkeypatch.setattr(reconcile_wal, "BinanceRestClient", lambda **kwargs: client)

    report = await reconcile_wal.reconcile(args(wal_path, execute=True))

    latest = OrderWAL(wal_path).recover_latest()["order-1"]
    assert report["result"] == "WAL_RECONCILE_OK"
    assert report["orders"][0]["wal_written"] is True
    assert latest.record_type == "exchange_status"
    assert latest.status == wal_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_response", "reason"),
    [
        (None, "order_not_found"),
        (order_response("other-order"), "identity_mismatch"),
        (order_response("order-2", symbol="ETHUSDT"), "identity_mismatch"),
    ],
)
async def test_unresolved_identity_fails_closed_without_any_wal_write(
    monkeypatch, tmp_path, bad_response, reason
):
    set_testnet_environment(monkeypatch)
    wal_path = tmp_path / "orders.jsonl"
    write_unknown(wal_path, "order-1")
    write_unknown(wal_path, "order-2")
    before = wal_path.read_bytes()
    client = FakeClient({
        "order-1": order_response("order-1"),
        "order-2": bad_response,
    })
    monkeypatch.setattr(reconcile_wal, "BinanceRestClient", lambda **kwargs: client)

    report = await reconcile_wal.reconcile(args(wal_path, execute=True))

    assert report["result"] == "FAIL_CLOSED"
    assert report["orders"][1]["reason"] == reason
    assert all(item["wal_written"] is False for item in report["orders"])
    assert wal_path.read_bytes() == before


@pytest.mark.asyncio
async def test_execute_refuses_when_spike_owns_account(monkeypatch, capsys, tmp_path):
    set_testnet_environment(monkeypatch)
    wal_path = tmp_path / "orders.jsonl"
    write_unknown(wal_path, "order-1")
    before = wal_path.read_bytes()
    client = FakeClient({"order-1": order_response("order-1")})
    monkeypatch.setattr(reconcile_wal, "BinanceRestClient", lambda **kwargs: client)

    @asynccontextmanager
    async def unavailable(_account_id):
        raise reconcile_wal.ExecutionLeaseUnavailableError("owned")
        yield

    monkeypatch.setattr(reconcile_wal, "exclusive_testnet_account", unavailable)

    with pytest.raises(SystemExit) as failure:
        await reconcile_wal.reconcile(args(wal_path, execute=True))

    assert failure.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == (
        "EXECUTION_LEASE_UNAVAILABLE"
    )
    assert wal_path.read_bytes() == before
    assert client.closed is True


def test_main_does_not_print_credentials_from_unexpected_exception(
    monkeypatch, capsys, tmp_path
):
    secret = "secret-in-signed-request"

    async def raise_sensitive_exception(_args):
        raise RuntimeError(f"request failed api-key=test-key signature={secret}")

    monkeypatch.setattr(reconcile_wal, "parse_args", lambda: args(tmp_path / "wal"))
    monkeypatch.setattr(reconcile_wal, "reconcile", raise_sensitive_exception)

    assert reconcile_wal.main() == 2
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["result"] == "FAIL_CLOSED"
    assert payload["error"]["message"] == "unexpected reconciliation failure"
    assert "test-key" not in output
    assert secret not in output
