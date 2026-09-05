import argparse
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "binance_testnet_preflight.py"
SPEC = importlib.util.spec_from_file_location("binance_testnet_preflight", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def set_testnet_environment(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://demo-fapi.binance.com")
    monkeypatch.setenv("BINANCE_WS_BASE_URL", "wss://stream.binancefuture.com")
    monkeypatch.setenv("BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("BINANCE_TESTNET", "false", "TESTNET_FLAG_REQUIRED"),
        ("BINANCE_BASE_URL", "https://fapi.binance.com", "TESTNET_ENDPOINT_REQUIRED"),
        (
            "BINANCE_WS_BASE_URL",
            "wss://fstream.binance.com",
            "TESTNET_ENDPOINT_REQUIRED",
        ),
    ],
)
def test_environment_is_strictly_testnet(monkeypatch, name, value, code):
    set_testnet_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(preflight.PreflightFailure) as failure:
        preflight.validate_environment()

    assert failure.value.code == code


def test_environment_rejects_malformed_endpoint_port(monkeypatch):
    set_testnet_environment(monkeypatch)
    monkeypatch.setenv("BINANCE_BASE_URL", "https://demo-fapi.binance.com:not-a-port")

    with pytest.raises(preflight.PreflightFailure) as failure:
        preflight.validate_environment()

    assert failure.value.code == "TESTNET_ENDPOINT_REQUIRED"


class Rest:
    def __init__(self, *, can_trade=True, dual_side=False, orders=None, positions=None):
        self.can_trade = can_trade
        self.dual_side = dual_side
        self.orders = orders or []
        self.positions = positions or []
        self.test_orders = []

    async def get_account(self):
        return {
            "canTrade": self.can_trade,
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "5000",
                    "availableBalance": "4999",
                }
            ],
        }

    async def get_position_mode(self):
        return {"dualSidePosition": self.dual_side}

    async def get_open_orders(self):
        return self.orders

    async def get_position_risk(self):
        return self.positions

    async def get_exchange_info(self):
        return preflight_exchange_info()

    async def get_klines(self, symbol, interval, *, limit):
        assert (symbol, interval, limit) == ("BTCUSDT", "1m", 1)
        return [[0, "99", "101", "98", "100"]]

    async def test_order(self, **kwargs):
        self.test_orders.append(kwargs)
        return {}

    async def close(self):
        return None


def preflight_exchange_info():
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
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


def args(**changes):
    values = {
        "symbol": "BTCUSDT",
        "minimum_available_usdt": Decimal("100"),
        "require_flat": True,
    }
    values.update(changes)
    return argparse.Namespace(**values)


def test_invalid_capital_environment_writes_fail_closed_report(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SPIKE_INITIAL_ACCOUNT_CAPITAL", "not-a-decimal")
    report_path = tmp_path / "preflight.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["binance_testnet_preflight.py", "--report", str(report_path)],
    )

    assert preflight.main() == 2

    stdout_report = json.loads(capsys.readouterr().out)
    file_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert stdout_report == file_report
    assert file_report["result"] == "FAIL_CLOSED"
    assert file_report["error"]["code"] == "BALANCE_THRESHOLD_INVALID"


@pytest.mark.asyncio
async def test_preflight_validates_signed_reads_and_non_matching_order(monkeypatch):
    set_testnet_environment(monkeypatch)
    rest = Rest()
    monkeypatch.setattr(preflight, "BinanceRestClient", lambda **kwargs: rest)
    report = {}

    await preflight.run_preflight(args(), report)

    assert report["result"] == "PREFLIGHT_OK"
    assert report["writes_exchange_state"] is False
    assert report["account"]["signed_read"] is True
    assert report["account"]["open_order_count"] == 0
    assert report["account"]["nonzero_position_count"] == 0
    assert report["order_test"]["matching_engine_submission"] is False
    assert len(rest.test_orders) == 1
    assert rest.test_orders[0]["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_preflight_fails_before_order_test_for_nonflat_account(monkeypatch):
    set_testnet_environment(monkeypatch)
    rest = Rest(orders=[{"symbol": "BTCUSDT"}])
    monkeypatch.setattr(preflight, "BinanceRestClient", lambda **kwargs: rest)

    with pytest.raises(preflight.PreflightFailure) as failure:
        await preflight.run_preflight(args(), {})

    assert failure.value.code == "ACCOUNT_NOT_FLAT"
    assert rest.test_orders == []


@pytest.mark.asyncio
async def test_preflight_rejects_hedge_mode(monkeypatch):
    set_testnet_environment(monkeypatch)
    rest = Rest(dual_side=True)
    monkeypatch.setattr(preflight, "BinanceRestClient", lambda **kwargs: rest)

    with pytest.raises(preflight.PreflightFailure) as failure:
        await preflight.run_preflight(args(), {})

    assert failure.value.code == "HEDGE_MODE_UNSUPPORTED"


@pytest.mark.asyncio
async def test_preflight_rejects_malformed_exchange_rows(monkeypatch):
    set_testnet_environment(monkeypatch)
    rest = Rest(orders=[None])
    monkeypatch.setattr(preflight, "BinanceRestClient", lambda **kwargs: rest)

    with pytest.raises(preflight.PreflightFailure) as failure:
        await preflight.run_preflight(args(), {})

    assert failure.value.code == "EXCHANGE_RESPONSE_INVALID"
    assert rest.test_orders == []
