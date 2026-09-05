import io
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "spike_runtime_guard.py"
SPEC = importlib.util.spec_from_file_location("spike_runtime_guard", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
check_runtime_health = MODULE.check_runtime_health
validate_preflight = MODULE.validate_preflight


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "SPIKE_MODE": "testnet",
        "SPIKE_ACCOUNT_ID": "spike_testnet",
        "SPIKE_SYMBOLS": "AKEUSDT",
        "SPIKE_INITIAL_ACCOUNT_CAPITAL": "100",
        "SPIKE_INITIAL_TRADING_CAPITAL": "50",
        "SPIKE_PROFIT_REINVEST_RATIO": "0.5",
        "SPIKE_MINIMUM_TRADING_CAPITAL": "10",
        "SPIKE_ENTRY_TIER_MODE": "single-entry",
        "SPIKE_STRATEGY_PATH": "trading_platform.strategies.spike.v2:V2",
        "STRATEGY_ACCOUNT_ID": "spike_testnet",
        "BINANCE_TESTNET": "true",
        "BINANCE_API_KEY": "test-key",
        "BINANCE_API_SECRET": "test-secret",
    }
    values.update(overrides)
    return values


def test_preflight_accepts_compose_compatible_testnet_configuration():
    validate_preflight(_environment())


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"STRATEGY_ACCOUNT_ID": "other"}, "must match"),
        ({"BINANCE_TESTNET": "false"}, "same environment"),
        ({"BINANCE_API_SECRET": ""}, "credentials"),
    ],
)
def test_preflight_rejects_incompatible_configuration(override, message):
    with pytest.raises(ValueError, match=message):
        validate_preflight(_environment(**override))


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _response(**overrides):
    item = {
        "mode": "testnet",
        "status": "running",
        "effective_status": "running",
        "halted": False,
        "stopped_at": None,
        "gate_conditions": {
            "execution": True,
            "market": True,
            "bar_stream": True,
            "bar_continuity": True,
            "exchange_symbols": True,
            "event_queue": True,
            "capital": True,
            "metrics_5m": True,
            "subcategory": False,
            "campaign": False,
        },
    }
    item.update(overrides)
    return _Response(json.dumps({"items": [item], "total": 1}).encode())


def test_runtime_health_accepts_current_running_status():
    with patch("urllib.request.urlopen", return_value=_response()) as request:
        check_runtime_health(
            ledger_url="http://ledger:8001",
            account_id="spike_testnet",
            expected_mode="testnet",
        )

    assert "account_id=spike_testnet" in request.call_args.args[0]
    assert "strategy_id=spike_short" in request.call_args.args[0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"effective_status": "stale"},
        {"status": "degraded", "effective_status": "degraded"},
        {"halted": True},
        {"stopped_at": "2026-09-05T00:00:00Z"},
        {"mode": "live"},
    ],
)
def test_runtime_health_rejects_unsafe_status(overrides):
    with patch("urllib.request.urlopen", return_value=_response(**overrides)):
        with pytest.raises(RuntimeError):
            check_runtime_health(
                ledger_url="http://ledger:8001",
                account_id="spike_testnet",
                expected_mode="testnet",
            )


@pytest.mark.parametrize("gate", ["bar_continuity", "metrics_5m"])
def test_runtime_health_rejects_a_closed_required_safety_gate(gate):
    response = _response()
    payload = json.loads(response.getvalue())
    payload["items"][0]["gate_conditions"][gate] = False
    with patch(
        "urllib.request.urlopen",
        return_value=_Response(json.dumps(payload).encode()),
    ):
        with pytest.raises(RuntimeError, match="safety gates"):
            check_runtime_health(
                ledger_url="http://ledger:8001",
                account_id="spike_testnet",
                expected_mode="testnet",
            )
