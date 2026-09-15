import io
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "long_breakout_runtime_guard.py"
SPEC = importlib.util.spec_from_file_location("long_breakout_runtime_guard", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
check_runtime_health = MODULE.check_runtime_health
validate_preflight = MODULE.validate_preflight
OPERATIONAL_GATES = tuple(MODULE.OPERATIONAL_GATES)


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "LONG_BREAKOUT_MODE": "testnet",
        "LONG_BREAKOUT_ACCOUNT_ID": "long_breakout_testnet",
        "LONG_BREAKOUT_SYMBOLS": "BTCUSDT",
        "LONG_BREAKOUT_TIMEFRAME": "4h",
        "LONG_BREAKOUT_LOOKBACK_BARS": "42",
        "LONG_BREAKOUT_ENTRY_NOTIONAL_USDT": "10",
        "LONG_BREAKOUT_LEVERAGE": "1",
        "LONG_BREAKOUT_BREAKOUT_BUFFER_PCT": "0",
        "LONG_BREAKOUT_TAKE_PROFIT_PCT": "0.02",
        "LONG_BREAKOUT_STOP_LOSS_PCT": "0.01",
        "LONG_BREAKOUT_ENTRY_ENABLED": "false",
        "LONG_BREAKOUT_SUBCATEGORY": "long_breakout",
        "LONG_BREAKOUT_WAL_PATH": "/app/data/wal/long_breakout.jsonl",
        "LONG_BREAKOUT_DEDICATED_STRATEGY_ACCOUNT": "true",
        "SPIKE_ACCOUNT_ID": "spike_testnet",
        "SPIKE_WAL_PATH": "/app/data/wal/spike_short.jsonl",
        "STRATEGY_ACCOUNT_ID": "long_breakout_testnet",
        "BINANCE_TESTNET": "true",
        "BINANCE_BASE_URL": "https://demo-fapi.binance.com",
        "BINANCE_WS_BASE_URL": "wss://stream.binancefuture.com",
        "BINANCE_API_KEY": "long-breakout-key",
        "BINANCE_API_SECRET": "long-breakout-secret",
    }
    values.update(overrides)
    return values


def _validate(values: dict[str, str]) -> None:
    with patch.dict(os.environ, values, clear=True):
        validate_preflight(values)


def test_preflight_accepts_default_legal_testnet_configuration():
    _validate(_environment())


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"STRATEGY_ACCOUNT_ID": "another_account"}, "must match"),
        ({"LONG_BREAKOUT_MODE": "live"}, "Input should be 'testnet'"),
        ({"BINANCE_API_KEY": ""}, "credentials"),
        ({"BINANCE_API_SECRET": ""}, "credentials"),
        ({"LONG_BREAKOUT_LOOKBACK_BARS": "7"}, "must be 42"),
        ({"LONG_BREAKOUT_WAL_PATH": "/app/data/wal/spike_short.jsonl"}, "Spike WAL"),
        ({"SPIKE_ACCOUNT_ID": "long_breakout_testnet"}, "different account IDs"),
        (
            {
                "LONG_BREAKOUT_WAL_PATH": "/app/data/wal/shared.jsonl",
                "SPIKE_WAL_PATH": "/app/data/wal/../wal/shared.jsonl",
            },
            "different WAL paths",
        ),
    ],
)
def test_preflight_rejects_unsafe_configuration(override, message):
    values = _environment(**override)
    with patch.dict(os.environ, values, clear=True):
        with pytest.raises(ValueError, match=message):
            validate_preflight(values)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _response(*, entry_enabled: bool = False, gates=None, **overrides):
    gate_conditions = {
        name: True for name in OPERATIONAL_GATES
    }
    if gates:
        gate_conditions.update(gates)
    item = {
        "account_id": "long_breakout_testnet",
        "strategy_id": "long_breakout",
        "mode": "testnet",
        "status": "running",
        "effective_status": "running",
        "entry_enabled": entry_enabled,
        "halted": False,
        "stopped_at": None,
        "gate_conditions": gate_conditions,
    }
    item.update(overrides)
    return _Response(json.dumps({"items": [item], "total": 1}).encode())


def test_runtime_health_allows_entry_disabled_when_operational_gates_are_ready():
    with patch("urllib.request.urlopen", return_value=_response()) as request:
        check_runtime_health(
            ledger_url="http://ledger:8001",
            account_id="long_breakout_testnet",
        )

    url = request.call_args.args[0]
    assert "account_id=long_breakout_testnet" in url
    assert "strategy_id=long_breakout" in url


@pytest.mark.parametrize("gate", OPERATIONAL_GATES)
def test_runtime_health_rejects_closed_operational_gate(gate):
    with patch(
        "urllib.request.urlopen",
        return_value=_response(gates={gate: False}),
    ):
        with pytest.raises(RuntimeError, match="operational gates"):
            check_runtime_health(
                ledger_url="http://ledger:8001",
                account_id="long_breakout_testnet",
            )
