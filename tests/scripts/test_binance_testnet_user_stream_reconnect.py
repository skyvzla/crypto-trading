import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "binance_testnet_user_stream_reconnect.py"
)
SPEC = importlib.util.spec_from_file_location(
    "binance_testnet_user_stream_reconnect", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
reconnect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconnect)


def set_testnet_environment(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://demo-fapi.binance.com")
    monkeypatch.setenv("BINANCE_WS_BASE_URL", "wss://stream.binancefuture.com")
    monkeypatch.setenv("BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")


@pytest.mark.parametrize(
    ("name", "value", "expected_code"),
    [
        ("BINANCE_BASE_URL", "https://fapi.binance.com", "TESTNET_ENDPOINT_REQUIRED"),
        (
            "BINANCE_BASE_URL",
            "https://demo-fapi.binance.com/v1",
            "TESTNET_ENDPOINT_REQUIRED",
        ),
        (
            "BINANCE_WS_BASE_URL",
            "wss://fstream.binance.com",
            "TESTNET_ENDPOINT_REQUIRED",
        ),
        (
            "BINANCE_WS_BASE_URL",
            "ws://stream.binancefuture.com",
            "TESTNET_ENDPOINT_REQUIRED",
        ),
    ],
)
def test_environment_rejects_non_testnet_rest_and_ws_endpoints(
    monkeypatch, name, value, expected_code
):
    set_testnet_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(reconnect.ReconnectFailure) as failure:
        reconnect.validate_environment(confirmation=reconnect.CONFIRMATION)

    assert failure.value.code == expected_code


@pytest.mark.parametrize("confirmation", [None, "", "wrong"])
def test_environment_requires_exact_confirmation(monkeypatch, confirmation):
    set_testnet_environment(monkeypatch)

    with pytest.raises(reconnect.ReconnectFailure) as failure:
        reconnect.validate_environment(confirmation=confirmation)

    assert failure.value.code == "CONFIRMATION_REQUIRED"


def test_environment_requires_explicit_testnet_flag(monkeypatch):
    set_testnet_environment(monkeypatch)
    monkeypatch.setenv("BINANCE_TESTNET", "false")

    with pytest.raises(reconnect.ReconnectFailure) as failure:
        reconnect.validate_environment(confirmation=reconnect.CONFIRMATION)

    assert failure.value.code == "TESTNET_FLAG_REQUIRED"


def test_environment_accepts_strict_testnet_rest_ws_and_confirmation(monkeypatch):
    set_testnet_environment(monkeypatch)

    binance, _database = reconnect.validate_environment(
        confirmation=reconnect.CONFIRMATION
    )

    assert binance.base_url == "https://demo-fapi.binance.com"
    assert binance.ws_base_url == "wss://stream.binancefuture.com"
    assert binance.api_key == "test-key"
    assert binance.api_secret == "test-secret"


class PreflightClient:
    def __init__(self, *, dual_side=False, open_orders=None, positions=None):
        self.dual_side = dual_side
        self.open_orders = list(open_orders or [])
        self.positions = list(positions or [])
        self.calls = []

    async def get_position_mode(self):
        self.calls.append(("get_position_mode", ()))
        return {"dualSidePosition": self.dual_side}

    async def get_open_orders(self, *args):
        self.calls.append(("get_open_orders", args))
        return list(self.open_orders)

    async def get_position_risk(self, *args):
        self.calls.append(("get_position_risk", args))
        return list(self.positions)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "expected_code"),
    [
        (PreflightClient(dual_side=True), "HEDGE_MODE_UNSUPPORTED"),
        (
            PreflightClient(open_orders=[{"symbol": "ETHUSDT", "status": "NEW"}]),
            "PREFLIGHT_OPEN_ORDERS",
        ),
        (
            PreflightClient(
                positions=[{"symbol": "ETHUSDT", "positionAmt": "0.01"}]
            ),
            "PREFLIGHT_POSITION",
        ),
    ],
)
async def test_preflight_requires_one_way_globally_idle_account(
    client, expected_code
):
    with pytest.raises(reconnect.ReconnectFailure) as failure:
        await reconnect.assert_idle_account(client)

    assert failure.value.code == expected_code
    for method, args in client.calls:
        if method in {"get_open_orders", "get_position_risk"}:
            assert args == ()


def reconnect_args(tmp_path):
    return argparse.Namespace(
        symbol="BTCUSDT",
        confirm=reconnect.CONFIRMATION,
        timeout_seconds=1.0,
        wal_path=tmp_path / "reconnect.jsonl",
    )


@pytest.mark.asyncio
async def test_run_reconnect_holds_account_lease_rotates_key_and_closes_in_order(
    monkeypatch, tmp_path
):
    events = []

    binance = SimpleNamespace(
        api_key="test-key",
        api_secret="test-secret",
        base_url="https://demo-fapi.binance.com",
        ws_base_url="wss://stream.binancefuture.com",
    )
    database = SimpleNamespace(dsn="postgresql://test")
    monkeypatch.setattr(
        reconnect,
        "validate_environment",
        lambda *, confirmation: (binance, database),
    )

    class Client(PreflightClient):
        def __init__(self):
            super().__init__()
            self.idle_checks = 0

        async def get_position_mode(self):
            events.append("account.position_mode")
            return {"dualSidePosition": False}

        async def get_open_orders(self, *args):
            assert args == ()
            events.append("account.open_orders")
            return []

        async def get_position_risk(self, *args):
            assert args == ()
            self.idle_checks += 1
            events.append("account.positions")
            return []

        async def get_exchange_info(self):
            events.append("exchange_info")
            return {"symbols": []}

        async def close(self):
            events.append("client.close")

    client = Client()
    monkeypatch.setattr(reconnect, "BinanceRestClient", lambda *args, **kwargs: client)

    class Pool:
        async def close(self):
            events.append("pool.close")

    pool = Pool()

    async def create_pool(dsn):
        assert dsn == database.dsn
        events.append("pool.create")
        return pool

    monkeypatch.setattr(reconnect, "create_connection_pool", create_pool)

    class Lease:
        def __init__(self, actual_pool, account_id):
            assert actual_pool is pool
            assert account_id == reconnect.ACCOUNT_ID

        async def acquire(self):
            events.append("lease.acquire")

        async def release(self):
            events.append("lease.release")

    monkeypatch.setattr(reconnect, "PostgresExecutionLease", Lease)

    class WebSocket:
        def __init__(self, runtime):
            self.runtime = runtime

        def close(self):
            events.append("ws.close")
            stream = self.runtime.user_stream
            stream.connected = False
            stream.on_disconnect()
            events.append("disconnect.callback")
            stream.listen_key = "listen-key-2"
            stream.connected = True
            self.runtime.on_recovered()
            events.append("recovered.callback")

    class Runtime:
        def __init__(self):
            self.user_stream = SimpleNamespace(
                connected=False,
                listen_key=None,
                ws=None,
                on_disconnect=None,
            )
            self.on_recovered = None
            self.user_stream.ws = WebSocket(self)

        async def start(self):
            events.append("runtime.start")
            self.user_stream.listen_key = "listen-key-1"
            self.user_stream.connected = True

        async def stop(self):
            events.append("runtime.stop")

    runtime = Runtime()

    def create_runtime(**kwargs):
        assert kwargs["account_id"] == reconnect.ACCOUNT_ID
        assert kwargs["strategy_id"] == reconnect.STRATEGY_ID
        assert kwargs["managed_symbols"] == ["BTCUSDT"]
        assert kwargs["dedicated_strategy_account"] is True
        assert kwargs["ws_base_url"] == binance.ws_base_url
        events.append("runtime.create")
        return runtime

    monkeypatch.setattr(reconnect, "create_binance_execution_runtime", create_runtime)
    monkeypatch.setattr(
        reconnect.BinanceSymbolRuleBook,
        "from_exchange_info",
        lambda exchange_info, *, symbols: SimpleNamespace(),
    )

    class Executor:
        def __init__(self, actual_client, wal, *, account_id, symbol_rules):
            assert actual_client is client
            assert account_id == reconnect.ACCOUNT_ID
            events.append("executor.create")

    monkeypatch.setattr(reconnect, "BinanceOrderExecutor", Executor)
    monkeypatch.setattr(reconnect, "LedgerDB", lambda actual_pool: SimpleNamespace())
    report = {}

    await reconnect.run_reconnect(reconnect_args(tmp_path), report)

    assert client.idle_checks == 2
    assert report == {
        "account_id": reconnect.ACCOUNT_ID,
        "strategy_id": reconnect.STRATEGY_ID,
        "symbol": "BTCUSDT",
        "endpoint": "https://demo-fapi.binance.com",
        "ws_endpoint": "wss://stream.binancefuture.com",
        "execution_lease_acquired": True,
        "initial_connected": True,
        "initial_listen_key_changed": True,
        "disconnect_observed": True,
        "recovery_observed": True,
        "reconnected": True,
        "final_open_orders": 0,
        "final_nonzero_positions": 0,
        "result": "RECONNECT_OK",
    }
    assert events == [
        "account.position_mode",
        "account.open_orders",
        "account.positions",
        "pool.create",
        "lease.acquire",
        "exchange_info",
        "executor.create",
        "runtime.create",
        "runtime.start",
        "ws.close",
        "disconnect.callback",
        "recovered.callback",
        "account.position_mode",
        "account.open_orders",
        "account.positions",
        "runtime.stop",
        "lease.release",
        "pool.close",
        "client.close",
    ]
