import argparse
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "binance_testnet_flatten.py"
SPEC = importlib.util.spec_from_file_location("binance_testnet_flatten", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
flatten = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(flatten)


def _args(*, execute=False):
    return argparse.Namespace(
        symbols=("BTCUSDT",),
        execute=execute,
        confirm=flatten.CONFIRMATION if execute else None,
        report=None,
        query_attempts=2,
        query_interval_seconds=0,
    )


class FakeClient:
    def __init__(self):
        self.cancel_calls = []
        self.post_calls = []
        self.flat = False
        self.orders_open = True

    async def get_position_mode(self):
        return {"dualSidePosition": False}

    async def get_exchange_info(self):
        return {
            "symbols": [{
                "symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "0", "maxPrice": "1000000"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }, {
                "symbol": "UNRELATEDUSDT", "contractType": "PERPETUAL", "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.1", "minPrice": "0", "maxPrice": "1000"},
                    {"filterType": "LOT_SIZE", "stepSize": "0", "minQty": "0", "maxQty": "0"},
                ],
            }]
        }

    async def get_position_risk(self, symbol):
        if self.flat:
            return []
        return [{"symbol": symbol, "positionSide": "BOTH", "positionAmt": "-0.051", "markPrice": "100"}]

    async def get_open_orders(self, symbol):
        if not self.orders_open:
            return []
        return [{"symbol": symbol, "orderId": 42, "clientOrderId": "entry_42", "status": "NEW"}]

    async def cancel_order(self, symbol, **kwargs):
        self.cancel_calls.append((symbol, kwargs))
        self.orders_open = False
        return {"symbol": symbol, "orderId": 42, "status": "CANCELED"}

    async def post_order(self, **kwargs):
        self.post_calls.append(kwargs)
        self.flat = True
        return {"symbol": kwargs["symbol"], "clientOrderId": kwargs["new_client_order_id"], "status": "FILLED", "reduceOnly": True}

    async def query_order(self, symbol, *, orig_client_order_id):
        return {"symbol": symbol, "clientOrderId": orig_client_order_id, "status": "FILLED", "executedQty": "0.051", "reduceOnly": True}

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_flatten_cancels_orders_and_closes_short_reduce_only(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(flatten, "BinanceRestClient", lambda **kwargs: client)
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://demo-fapi.binance.com")
    monkeypatch.setenv("BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")

    result = await flatten.flatten(_args(execute=True))

    assert result["result"] == "FLATTEN_OK"
    assert len(client.cancel_calls) == 1
    assert len(client.post_calls) == 1
    assert client.post_calls[0]["side"] == "BUY"
    assert client.post_calls[0]["reduce_only"] is True
    assert result["positions"][0]["position_after"] is None


@pytest.mark.asyncio
async def test_resolve_exit_waits_for_filled_order_fact():
    client = FakeClient()
    resolved = await flatten.resolve_exit_order(
        client,
        symbol="BTCUSDT",
        client_order_id="flatten-delayed",
        initial={"symbol": "BTCUSDT", "clientOrderId": "flatten-delayed", "status": "NEW"},
        attempts=2,
        interval_seconds=0,
    )
    assert resolved["status"] == "FILLED"


@pytest.mark.asyncio
async def test_flatten_refuses_exit_while_an_order_remains_open(monkeypatch):
    client = FakeClient()

    async def ineffective_cancel(symbol, **kwargs):
        return {"symbol": symbol, "orderId": 42, "status": "PENDING_CANCEL"}

    client.cancel_order = ineffective_cancel
    monkeypatch.setattr(flatten, "BinanceRestClient", lambda **kwargs: client)
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://demo-fapi.binance.com")
    monkeypatch.setenv("BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")

    with pytest.raises(flatten.FlattenFailure, match="open orders remain"):
        await flatten.flatten(_args(execute=True))

    assert client.post_calls == []


def test_environment_rejects_non_testnet(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "false")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://fapi.binance.com")
    with pytest.raises(flatten.FlattenFailure, match="BINANCE_TESTNET"):
        flatten.validate_environment(execute=False, confirmation=None)


def test_position_rejects_hedge_mode():
    with pytest.raises(flatten.FlattenFailure, match="one-way"):
        flatten.position_for_symbol([{"symbol": "BTCUSDT", "positionAmt": "1", "positionSide": "LONG"}], "BTCUSDT")
