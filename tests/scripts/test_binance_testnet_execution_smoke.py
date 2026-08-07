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
    assert result["entry_resolved"] is True
    assert result["risk_resolved"] is True


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
    assert result["entry_resolved"] is True
    assert result["risk_resolved"] is True


class UnknownEntryClient(CleanupClient):
    def __init__(self):
        super().__init__()
        self.cancel_attempted = False

    async def query_order(self, symbol, *, orig_client_order_id):
        return None

    async def cancel_order(self, symbol, *, orig_client_order_id):
        self.cancel_attempted = True
        raise RuntimeError("unknown order")


@pytest.mark.asyncio
async def test_emergency_cleanup_does_not_claim_safety_for_unknown_entry():
    client = UnknownEntryClient()

    result = await smoke.emergency_cleanup(
        client,
        args=args(),
        rules=smoke.BinanceSymbolRuleBook.from_exchange_info(
            smoke.synthetic_exchange_info()
        ).get("BTCUSDT"),
        reference_price=Decimal("100"),
        exit_attempted=False,
    )

    assert client.cancel_attempted is True
    assert result["entry"] == "unknown"
    assert result["entry_resolved"] is False
    assert result["flat"] is True
    assert result["risk_resolved"] is False


def test_testnet_endpoint_validation_rejects_production(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://fapi.binance.com")

    with pytest.raises(smoke.SmokeFailure, match="demo-fapi"):
        smoke.validate_testnet_environment(execute=False, confirmation=None)


def test_parser_defaults_to_cancel_open_without_position_confirmation():
    parsed = smoke.build_parser().parse_args([])

    assert parsed.scenario == smoke.SCENARIO_CANCEL_OPEN
    assert parsed.confirm_position is None


def test_fill_and_exit_requires_explicit_position_confirmation():
    args = smoke.build_parser().parse_args(
        ["--execute", "--scenario", smoke.SCENARIO_FILL_AND_EXIT]
    )

    with pytest.raises(smoke.SmokeFailure, match="confirm-position"):
        smoke.validate_scenario_authorization(args)

    args.confirm_position = smoke.POSITION_CONFIRMATION
    smoke.validate_scenario_authorization(args)


class PositionLagClient:
    def __init__(self):
        self.snapshots = [
            [],
            [],
            [{"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-0.001"}],
        ]

    async def get_position_risk(self, symbol):
        return self.snapshots.pop(0)


@pytest.mark.asyncio
async def test_position_poll_waits_for_delayed_account_snapshot():
    client = PositionLagClient()

    position = await smoke.query_until_position(
        client,
        symbol="BTCUSDT",
        attempts=3,
        interval_seconds=0,
    )

    assert position is not None
    assert position["positionAmt"] == "-0.001"


@pytest.mark.asyncio
async def test_flat_poll_waits_for_delayed_exit_snapshot():
    client = PositionLagClient()
    client.snapshots = [
        [{"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-0.001"}],
        [],
    ]

    assert await smoke.query_until_flat(
        client,
        symbol="BTCUSDT",
        attempts=2,
        interval_seconds=0,
    ) is None


def test_canceled_partial_fill_is_treated_as_position_bearing():
    assert smoke.order_has_fill(
        {"status": "CANCELED", "executedQty": "0.001"}
    ) is True
    assert smoke.order_has_fill(
        {"status": "CANCELED", "executedQty": "0"}
    ) is False


class ScenarioClient:
    def __init__(self, *, fill_entry):
        self.fill_entry = fill_entry
        self.entry_submitted = False
        self.entry_canceled = False
        self.position_open = False
        self.post_calls = []
        self.closed = False

    async def get_exchange_info(self):
        return smoke.synthetic_exchange_info()

    async def get_position_mode(self):
        return {"dualSidePosition": False}

    async def get_klines(self, symbol, interval, *, limit):
        return [[0, "100", "100", "100", "100"]]

    async def get_open_orders(self, symbol):
        return []

    async def get_position_risk(self, symbol):
        if not self.position_open:
            return []
        return [
            {
                "symbol": symbol,
                "positionSide": "BOTH",
                "positionAmt": "-0.051",
                "markPrice": "100",
            }
        ]

    async def query_order(self, symbol, *, orig_client_order_id):
        if orig_client_order_id.endswith("_exit"):
            if len(self.post_calls) < 2:
                return None
            return self._order(symbol, orig_client_order_id, "FILLED")
        if not self.entry_submitted:
            return None
        if self.entry_canceled:
            status = "CANCELED"
        else:
            status = "FILLED" if self.fill_entry else "NEW"
        return self._order(symbol, orig_client_order_id, status)

    async def post_order(self, **kwargs):
        self.post_calls.append(kwargs)
        client_order_id = kwargs["new_client_order_id"]
        if kwargs.get("reduce_only"):
            self.position_open = False
            return self._order(kwargs["symbol"], client_order_id, "FILLED")
        self.entry_submitted = True
        self.position_open = self.fill_entry
        return self._order(
            kwargs["symbol"],
            client_order_id,
            "FILLED" if self.fill_entry else "NEW",
        )

    async def cancel_order(self, symbol, *, orig_client_order_id):
        self.entry_canceled = True
        return self._order(symbol, orig_client_order_id, "CANCELED")

    async def close(self):
        self.closed = True

    @staticmethod
    def _order(symbol, client_order_id, status):
        return {
            "symbol": symbol,
            "clientOrderId": client_order_id,
            "status": status,
        }


def scenario_args(*extra):
    return smoke.build_parser().parse_args(
        [
            "--execute",
            "--confirm",
            smoke.EXECUTE_CONFIRMATION,
            "--client-order-id",
            "tp_smoke_scenario_001",
            "--query-attempts",
            "2",
            "--query-interval-seconds",
            "0",
            *extra,
        ]
    )


def set_testnet_environment(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://demo-fapi.binance.com")
    monkeypatch.setenv("BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")


@pytest.mark.asyncio
async def test_cancel_open_scenario_only_prehangs_and_cancels(monkeypatch):
    set_testnet_environment(monkeypatch)
    client = ScenarioClient(fill_entry=False)
    monkeypatch.setattr(smoke, "BinanceRestClient", lambda **kwargs: client)
    report = {}

    await smoke.run_smoke(
        scenario_args("--limit-price", "102", "--quantity", "0.051"), report
    )

    assert report["result"] == "EXECUTION_OK"
    assert report["scenario"] == smoke.SCENARIO_CANCEL_OPEN
    assert report["cancel_result"]["status"] == "CANCELED"
    assert len(client.post_calls) == 1
    assert client.post_calls[0]["order_type"] == "LIMIT"
    assert client.post_calls[0].get("reduce_only") is None
    assert client.position_open is False
    assert client.closed is True


@pytest.mark.asyncio
async def test_cancel_open_accidental_fill_is_closed_reduce_only(monkeypatch):
    set_testnet_environment(monkeypatch)
    client = ScenarioClient(fill_entry=True)
    monkeypatch.setattr(smoke, "BinanceRestClient", lambda **kwargs: client)
    report = {}

    await smoke.run_smoke(
        scenario_args("--limit-price", "102", "--quantity", "0.051"), report
    )

    assert report["result"] == "EXECUTION_OK"
    assert len(client.post_calls) == 2
    assert client.post_calls[1]["order_type"] == "MARKET"
    assert client.post_calls[1]["reduce_only"] is True
    assert client.position_open is False


@pytest.mark.asyncio
async def test_fill_and_exit_uses_limit_entry_then_reduce_only_market(monkeypatch):
    set_testnet_environment(monkeypatch)
    client = ScenarioClient(fill_entry=True)
    monkeypatch.setattr(smoke, "BinanceRestClient", lambda **kwargs: client)
    report = {}
    args = scenario_args(
        "--scenario",
        smoke.SCENARIO_FILL_AND_EXIT,
        "--confirm-position",
        smoke.POSITION_CONFIRMATION,
        "--limit-price",
        "99.90",
        "--quantity",
        "0.051",
    )

    await smoke.run_smoke(args, report)

    assert report["result"] == "EXECUTION_OK"
    assert report["queried_order"]["status"] == "FILLED"
    assert report["reduce_only_exit"]["status"] == "FILLED"
    assert len(client.post_calls) == 2
    assert client.post_calls[0]["order_type"] == "LIMIT"
    assert client.post_calls[0].get("reduce_only") is None
    assert client.post_calls[1]["order_type"] == "MARKET"
    assert client.post_calls[1]["side"] == "BUY"
    assert client.post_calls[1]["reduce_only"] is True
    assert client.position_open is False
    assert client.closed is True
