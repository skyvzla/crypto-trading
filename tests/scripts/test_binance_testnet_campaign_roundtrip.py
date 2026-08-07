import importlib.util
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "binance_testnet_campaign_roundtrip.py"
)
SPEC = importlib.util.spec_from_file_location(
    "binance_testnet_campaign_roundtrip", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
roundtrip = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(roundtrip)


def set_testnet_environment(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://demo-fapi.binance.com")
    monkeypatch.setenv("BINANCE_WS_BASE_URL", "wss://stream.binancefuture.com")
    monkeypatch.setenv("BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")


def test_environment_rejects_production_endpoint(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://fapi.binance.com")

    with pytest.raises(roundtrip.RoundtripFailure, match="demo-fapi"):
        roundtrip.validate_environment(
            execute=True,
            confirmation=roundtrip.EXECUTE_CONFIRMATION,
            position_confirmation=roundtrip.POSITION_CONFIRMATION,
        )


@pytest.mark.parametrize(
    ("confirmation", "position_confirmation", "message"),
    [
        (None, None, "--confirm"),
        ("wrong", None, "--confirm"),
        (roundtrip.EXECUTE_CONFIRMATION, None, "--confirm-position"),
        (roundtrip.EXECUTE_CONFIRMATION, "wrong", "--confirm-position"),
    ],
)
def test_environment_requires_both_exact_confirmations(
    monkeypatch, confirmation, position_confirmation, message
):
    set_testnet_environment(monkeypatch)

    with pytest.raises(roundtrip.RoundtripFailure, match=message):
        roundtrip.validate_environment(
            execute=True,
            confirmation=confirmation,
            position_confirmation=position_confirmation,
        )


def test_environment_accepts_only_explicit_testnet_authorization(monkeypatch):
    set_testnet_environment(monkeypatch)

    base_url, key, secret = roundtrip.validate_environment(
        execute=True,
        confirmation=roundtrip.EXECUTE_CONFIRMATION,
        position_confirmation=roundtrip.POSITION_CONFIRMATION,
    )

    assert base_url == "https://demo-fapi.binance.com"
    assert key == "test-key"
    assert secret == "test-secret"


class AccountClient:
    def __init__(self, *, position=None, open_orders=None, dual_side=False):
        self.position = position
        self.open_orders = list(open_orders or [])
        self.dual_side = dual_side
        self.cancel_calls = []
        self.post_calls = []

    async def get_position_mode(self):
        return {"dualSidePosition": self.dual_side}

    async def get_exchange_info(self):
        return synthetic_exchange_info()

    async def get_klines(self, symbol, interval, *, limit):
        return [[0, "60000", "60000", "60000", "60000"]]

    async def get_position_risk(self, symbol=None):
        return [] if self.position is None else [self.position]

    async def get_open_orders(self, symbol):
        return list(self.open_orders)

    async def query_order(self, symbol, *, orig_client_order_id):
        for order in self.open_orders:
            if order.get("clientOrderId") == orig_client_order_id:
                return order
        return None

    async def cancel_order(self, symbol, *, orig_client_order_id):
        self.cancel_calls.append(orig_client_order_id)
        self.open_orders = [
            order
            for order in self.open_orders
            if order.get("clientOrderId") != orig_client_order_id
        ]
        return {
            "symbol": symbol,
            "clientOrderId": orig_client_order_id,
            "status": "CANCELED",
        }

    async def post_order(self, **kwargs):
        self.post_calls.append(kwargs)
        self.position = None
        return {
            "symbol": kwargs["symbol"],
            "clientOrderId": kwargs["new_client_order_id"],
            "status": "FILLED",
            "reduceOnly": kwargs.get("reduce_only"),
        }

    async def close(self):
        pass


def synthetic_exchange_info():
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "tickSize": "0.10",
                        "minPrice": "0",
                        "maxPrice": "1000000",
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "stepSize": "0.001",
                        "minQty": "0.001",
                        "maxQty": "100",
                    },
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "stepSize": "0.001",
                        "minQty": "0.001",
                        "maxQty": "100",
                    },
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
        ]
    }


def execution_args(tmp_path):
    return SimpleNamespace(
        symbol="BTCUSDT",
        quantity=Decimal("0.001"),
        fill_distance_bps=Decimal("5"),
        execute=True,
        confirm=roundtrip.EXECUTE_CONFIRMATION,
        confirm_position=roundtrip.POSITION_CONFIRMATION,
        query_attempts=1,
        query_interval_seconds=0,
        wal_path=tmp_path / "roundtrip.jsonl",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "code"),
    [
        (
            AccountClient(
                open_orders=[
                    {"symbol": "BTCUSDT", "clientOrderId": "existing", "status": "NEW"}
                ]
            ),
            "PREFLIGHT_OPEN_ORDERS",
        ),
        (
            AccountClient(
                position={
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "-0.001",
                }
            ),
            "PREFLIGHT_ACCOUNT_POSITION",
        ),
        (AccountClient(dual_side=True), "HEDGE_MODE_UNSUPPORTED"),
    ],
)
async def test_preflight_requires_one_way_empty_account(
    monkeypatch, tmp_path, client, code
):
    set_testnet_environment(monkeypatch)
    monkeypatch.setattr(roundtrip, "BinanceRestClient", lambda *args, **kwargs: client)
    pool_creation_attempted = False

    async def create_pool(*args, **kwargs):
        nonlocal pool_creation_attempted
        pool_creation_attempted = True

    monkeypatch.setattr(roundtrip, "create_connection_pool", create_pool)

    with pytest.raises(roundtrip.RoundtripFailure) as failure:
        await roundtrip.run_roundtrip(execution_args(tmp_path), {})

    assert failure.value.code == code
    assert pool_creation_attempted is False
    assert client.cancel_calls == []
    assert client.post_calls == []


@pytest.mark.asyncio
async def test_cleanup_cancels_only_this_campaign_entry_and_exits_reduce_only():
    own_entry = "tp_rt_campaign_001_entry"
    unrelated = "another_campaign_entry"
    client = AccountClient(
        position={
            "symbol": "BTCUSDT",
            "positionSide": "BOTH",
            "positionAmt": "-0.001",
            "markPrice": "60000",
        },
        open_orders=[
            {"symbol": "BTCUSDT", "clientOrderId": own_entry, "status": "NEW"},
            {"symbol": "BTCUSDT", "clientOrderId": unrelated, "status": "NEW"},
        ],
    )

    result = await roundtrip.cleanup_campaign_risk(
        client,
        symbol="BTCUSDT",
        entry_client_order_id=own_entry,
        exit_client_order_id="tp_rt_campaign_001_exit",
        quantity=Decimal("0.001"),
        query_attempts=1,
        query_interval_seconds=0,
    )

    assert client.cancel_calls == [own_entry]
    assert client.open_orders[0]["clientOrderId"] == unrelated
    assert len(client.post_calls) == 1
    assert client.post_calls[0]["side"] == "BUY"
    assert client.post_calls[0]["order_type"] == "MARKET"
    assert client.post_calls[0]["reduce_only"] is True
    assert result["flat"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client",
    [
        AccountClient(
            open_orders=[
                {"symbol": "BTCUSDT", "clientOrderId": "remaining", "status": "NEW"}
            ]
        ),
        AccountClient(
            position={
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "positionAmt": "-0.001",
            }
        ),
    ],
)
async def test_final_verification_fails_closed_on_remaining_risk(client):
    with pytest.raises(roundtrip.RoundtripFailure):
        await roundtrip.assert_account_flat(client, symbol="BTCUSDT")


@pytest.mark.asyncio
async def test_final_verification_accepts_flat_account():
    await roundtrip.assert_account_flat(AccountClient(), symbol="BTCUSDT")


@pytest.mark.asyncio
async def test_roundtrip_holds_lease_around_runtime_and_both_orders(
    monkeypatch, tmp_path
):
    set_testnet_environment(monkeypatch)
    events = []

    class Client(AccountClient):
        async def query_order(self, symbol, *, orig_client_order_id):
            return {
                "symbol": symbol,
                "clientOrderId": orig_client_order_id,
                "status": "FILLED",
            }

        async def close(self):
            events.append("client.close")

    client = Client()

    class Pool:
        async def close(self):
            events.append("pool.close")

    pool = Pool()

    async def create_pool(*args, **kwargs):
        events.append("pool.create")
        return pool

    class Lease:
        def __init__(self, actual_pool, account_id):
            assert actual_pool is pool
            assert account_id == roundtrip.ACCOUNT_ID

        async def acquire(self):
            events.append("lease.acquire")

        async def release(self):
            events.append("lease.release")

    class Runtime:
        async def start(self):
            events.append("runtime.start")

        async def stop(self):
            events.append("runtime.stop")

    submitted = []

    class Executor:
        def __init__(self, actual_client, wal, *, account_id, symbol_rules):
            assert actual_client is client
            assert account_id == roundtrip.ACCOUNT_ID

        async def submit(self, intent, *, reference_price):
            submitted.append(intent)
            events.append(f"submit.{intent.side}")
            client.position = (
                {
                    "symbol": intent.symbol,
                    "positionSide": "BOTH",
                    "positionAmt": "-0.001",
                }
                if intent.side == "SELL"
                else None
            )
            return SimpleNamespace(
                client_order_id=intent.client_order_id,
                exchange_order_id="123",
                status="FILLED",
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                quantity=intent.quantity,
            )

    class DB:
        def __init__(self, actual_pool):
            assert actual_pool is pool

        async def get_campaign_pnl(self, **kwargs):
            assert kwargs["account_id"] == roundtrip.ACCOUNT_ID
            assert kwargs["strategy_id"] == roundtrip.STRATEGY_ID
            return SimpleNamespace(
                trade_count=2,
                remaining_quantity=Decimal("0"),
                net_realized_pnl=Decimal("-0.02"),
            )

    monkeypatch.setattr(roundtrip, "BinanceRestClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(roundtrip, "create_connection_pool", create_pool)
    monkeypatch.setattr(roundtrip, "PostgresExecutionLease", Lease)
    monkeypatch.setattr(roundtrip, "LedgerDB", DB)
    monkeypatch.setattr(roundtrip, "BinanceOrderExecutor", Executor)
    monkeypatch.setattr(
        roundtrip, "create_binance_execution_runtime", lambda **kwargs: Runtime()
    )
    monkeypatch.setattr(roundtrip.time, "time", lambda: 1_777_777_777.0)
    report = {}

    await roundtrip.run_roundtrip(execution_args(tmp_path), report)

    assert report["result"] == "ROUNDTRIP_OK"
    assert events == [
        "pool.create",
        "lease.acquire",
        "runtime.start",
        "submit.SELL",
        "submit.BUY",
        "runtime.stop",
        "lease.release",
        "pool.close",
        "client.close",
    ]
    assert len(submitted) == 2
    assert submitted[0].order_type == "LIMIT"
    assert submitted[1].order_type == "MARKET"
    assert submitted[1].reduce_only is True
    assert submitted[0].campaign_id == submitted[1].campaign_id
