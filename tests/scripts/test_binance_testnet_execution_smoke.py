import argparse
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "binance_testnet_execution_smoke.py"
SPEC = importlib.util.spec_from_file_location("binance_testnet_execution_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def args():
    return argparse.Namespace(
        symbol="BTCUSDT",
        client_order_id="tp_smoke_test_001",
        query_attempts=1,
        query_interval_seconds=0,
    )


class CleanupClient:
    def __init__(self, *, position=False):
        self.cancelled = False
        self.position = position
        self.exit_submitted = False
        self.post_calls = []

    async def query_order(self, symbol, *, orig_client_order_id):
        if orig_client_order_id.endswith("_exit"):
            if not self.exit_submitted:
                return None
            return {
                "symbol": symbol,
                "clientOrderId": orig_client_order_id,
                "status": "FILLED",
            }
        return {
            "symbol": symbol,
            "clientOrderId": orig_client_order_id,
            "status": "CANCELED" if self.cancelled else ("FILLED" if self.position else "NEW"),
        }

    async def cancel_order(self, symbol, *, orig_client_order_id):
        self.cancelled = True
        return {"status": "CANCELED"}

    async def get_position_risk(self, symbol):
        if not self.position:
            return []
        return [
            {
                "symbol": symbol,
                "positionSide": "BOTH",
                "positionAmt": "-0.051",
                "markPrice": "100",
            }
        ]

    async def post_order(self, **kwargs):
        self.post_calls.append(kwargs)
        self.exit_submitted = True
        self.position = False
        return {
            "symbol": kwargs["symbol"],
            "clientOrderId": kwargs["new_client_order_id"],
            "status": "FILLED",
        }


@pytest.mark.asyncio
async def test_emergency_cleanup_cancels_only_the_smoke_entry():
    client = CleanupClient()

    result = await smoke.emergency_cleanup(
        client,
        args=args(),
        rules=smoke.BinanceSymbolRuleBook.from_exchange_info(
            smoke.synthetic_exchange_info()
        ).get("BTCUSDT"),
        reference_price=Decimal("100"),
        exit_attempted=False,
    )

    assert client.cancelled is True
    assert client.post_calls == []
    assert result["flat"] is True


@pytest.mark.asyncio
async def test_emergency_cleanup_closes_attributable_position_once_reduce_only():
    client = CleanupClient(position=True)

    result = await smoke.emergency_cleanup(
        client,
        args=args(),
        rules=smoke.BinanceSymbolRuleBook.from_exchange_info(
            smoke.synthetic_exchange_info()
        ).get("BTCUSDT"),
        reference_price=Decimal("100"),
        exit_attempted=False,
    )

    assert result["flat"] is True
    assert len(client.post_calls) == 1
    assert client.post_calls[0]["reduce_only"] is True
    assert client.post_calls[0]["side"] == "BUY"


def test_testnet_endpoint_validation_rejects_production(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://fapi.binance.com")

    with pytest.raises(smoke.SmokeFailure, match="demo-fapi"):
        smoke.validate_testnet_environment(execute=False, confirmation=None)
