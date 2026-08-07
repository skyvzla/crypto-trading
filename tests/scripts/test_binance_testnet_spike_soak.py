import argparse
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "binance_testnet_spike_soak.py"
SPEC = importlib.util.spec_from_file_location("binance_testnet_spike_soak", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
soak = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(soak)


def set_testnet_environment(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_BASE_URL", "https://demo-fapi.binance.com")
    monkeypatch.setenv("BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test-secret")


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("BINANCE_TESTNET", "false", "TESTNET_FLAG_REQUIRED"),
        ("BINANCE_BASE_URL", "https://fapi.binance.com", "TESTNET_ENDPOINT_REQUIRED"),
        ("BINANCE_BASE_URL", "http://demo-fapi.binance.com", "TESTNET_ENDPOINT_REQUIRED"),
        (
            "BINANCE_BASE_URL",
            "https://demo-fapi.binance.com/fapi",
            "TESTNET_ENDPOINT_REQUIRED",
        ),
    ],
)
def test_environment_requires_strict_testnet(monkeypatch, name, value, code):
    set_testnet_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(soak.SoakFailure) as failure:
        soak.validate_environment(soak.CONFIRMATION)

    assert failure.value.code == code


@pytest.mark.parametrize("confirmation", [None, "", "wrong"])
def test_environment_requires_exact_confirmation(monkeypatch, confirmation):
    set_testnet_environment(monkeypatch)

    with pytest.raises(soak.SoakFailure) as failure:
        soak.validate_environment(confirmation)

    assert failure.value.code == "CONFIRMATION_REQUIRED"


def runtime_item(**changes):
    now = datetime.now(timezone.utc)
    item = {
        "instance_id": "instance-1",
        "mode": "testnet",
        "effective_status": "running",
        "entry_enabled": False,
        "halted": False,
        "heartbeat_at": now.isoformat(),
        "gate_conditions": {
            "execution": True,
            "market": True,
            "bar_stream": True,
            "subcategory": False,
        },
    }
    item.update(changes)
    return item


def test_runtime_sample_accepts_healthy_pinned_instance():
    now = datetime.now(timezone.utc)
    instance_id, heartbeat = soak.validate_runtime_sample(
        runtime_item(heartbeat_at=(now - timedelta(seconds=3)).isoformat()),
        expected_instance_id="instance-1",
        expected_entry_enabled="false",
        now=now,
        heartbeat_max_age_seconds=15,
    )

    assert instance_id == "instance-1"
    assert heartbeat == now - timedelta(seconds=3)


@pytest.mark.parametrize(
    ("changes", "expected_instance", "entry_state", "code"),
    [
        ({"instance_id": "instance-2"}, "instance-1", "false", "INSTANCE_CHANGED"),
        ({"mode": "live"}, None, "false", "RUNTIME_MODE_INVALID"),
        ({"effective_status": "stale"}, None, "false", "RUNTIME_NOT_RUNNING"),
        ({"effective_status": "fatal"}, None, "false", "RUNTIME_NOT_RUNNING"),
        ({"effective_status": "degraded"}, None, "false", "RUNTIME_DEGRADED"),
        ({"halted": True}, None, "false", "RUNTIME_HALTED"),
        ({"entry_enabled": True}, None, "false", "ENTRY_STATE_MISMATCH"),
        (
            {"gate_conditions": {"execution": False, "market": True, "bar_stream": True}},
            None,
            "false",
            "SAFETY_GATE_CLOSED",
        ),
    ],
)
def test_runtime_sample_fails_closed(
    changes, expected_instance, entry_state, code
):
    with pytest.raises(soak.SoakFailure) as failure:
        soak.validate_runtime_sample(
            runtime_item(**changes),
            expected_instance_id=expected_instance,
            expected_entry_enabled=entry_state,
            now=datetime.now(timezone.utc),
            heartbeat_max_age_seconds=15,
        )

    assert failure.value.code == code


def test_runtime_sample_rejects_stale_heartbeat():
    now = datetime.now(timezone.utc)
    with pytest.raises(soak.SoakFailure) as failure:
        soak.validate_runtime_sample(
            runtime_item(heartbeat_at=(now - timedelta(seconds=16)).isoformat()),
            expected_instance_id=None,
            expected_entry_enabled="any",
            now=now,
            heartbeat_max_age_seconds=15,
        )

    assert failure.value.code == "HEARTBEAT_STALE"


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class Http:
    def __init__(
        self,
        runtime,
        *,
        market_ready=True,
        ledger_status=200,
        market_status=200,
    ):
        self.runtime = runtime
        self.market_ready = market_ready
        self.ledger_status = ledger_status
        self.market_status = market_status

    async def get(self, url):
        if url.endswith("/api/v1/health"):
            return Response({"status": "healthy"}, self.ledger_status)
        if "/strategy-runtime-status?" in url:
            return Response(
                {"total": 1, "items": [self.runtime]}, self.ledger_status
            )
        if url.endswith("/health"):
            return Response(
                {"status": "ready", "binance_testnet": True}
                if self.market_ready
                else {"status": "degraded", "binance_testnet": True},
                self.market_status,
            )
        if url.endswith("/quality"):
            return Response({"ready": self.market_ready}, self.market_status)
        raise AssertionError(url)


class Rest:
    def __init__(self, *, dual_side=False, orders=None, positions=None):
        self.dual_side = dual_side
        self.orders = list(orders or [])
        self.positions = list(positions or [])

    async def get_position_mode(self):
        return {"dualSidePosition": self.dual_side}

    async def get_open_orders(self):
        return self.orders

    async def get_position_risk(self):
        return self.positions


@pytest.mark.asyncio
async def test_collect_sample_reports_only_safe_account_summary():
    now = datetime.now(timezone.utc)
    result = await soak.collect_sample(
        http=Http(runtime_item(heartbeat_at=now.isoformat())),
        rest=Rest(),
        ledger_url="http://ledger",
        market_url="http://market",
        account_id="spike_testnet",
        strategy_id="spike_short",
        expected_instance_id=None,
        expected_entry_enabled="false",
        heartbeat_max_age_seconds=15,
        require_flat=True,
        now=now,
    )

    assert result["instance_id"] == "instance-1"
    assert result["open_order_count"] == 0
    assert result["nonzero_position_count"] == 0
    assert "api" not in str(result).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rest", "code"),
    [
        (Rest(dual_side=True), "HEDGE_MODE_UNSUPPORTED"),
        (Rest(orders=[{"symbol": "BTCUSDT"}]), "ACCOUNT_NOT_FLAT"),
        (Rest(positions=[{"symbol": "BTCUSDT", "positionAmt": "-0.001"}]), "ACCOUNT_NOT_FLAT"),
    ],
)
async def test_collect_sample_rejects_unsafe_account(rest, code):
    now = datetime.now(timezone.utc)
    with pytest.raises(soak.SoakFailure) as failure:
        await soak.collect_sample(
            http=Http(runtime_item(heartbeat_at=now.isoformat())),
            rest=rest,
            ledger_url="http://ledger",
            market_url="http://market",
            account_id="spike_testnet",
            strategy_id="spike_short",
            expected_instance_id=None,
            expected_entry_enabled="false",
            heartbeat_max_age_seconds=15,
            require_flat=True,
            now=now,
        )

    assert failure.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http", "code"),
    [
        (Http(runtime_item(), ledger_status=503), "LEDGER_UNHEALTHY"),
        (Http(runtime_item(), market_status=503), "MARKET_UNHEALTHY"),
    ],
)
async def test_collect_sample_classifies_service_503(http, code):
    now = datetime.now(timezone.utc)
    with pytest.raises(soak.SoakFailure) as failure:
        await soak.collect_sample(
            http=http,
            rest=Rest(),
            ledger_url="http://ledger",
            market_url="http://market",
            account_id="spike_testnet",
            strategy_id="spike_short",
            expected_instance_id=None,
            expected_entry_enabled="false",
            heartbeat_max_age_seconds=15,
            require_flat=True,
            now=now,
        )

    assert failure.value.code == code


class Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds


def arguments(**changes):
    values = {
        "duration_seconds": 10.0,
        "sample_seconds": 5.0,
        "heartbeat_max_age_seconds": 15.0,
        "runtime_recovery_seconds": 0.0,
        "max_consecutive_errors": 1,
        "account_id": "spike_testnet",
        "strategy_id": "spike_short",
        "ledger_url": "http://ledger",
        "market_url": "http://market",
        "expect_entry_enabled": "false",
        "require_flat": True,
    }
    values.update(changes)
    return argparse.Namespace(**values)


@pytest.mark.asyncio
async def test_run_soak_pins_instance_and_summarizes_samples(monkeypatch):
    clock = Clock()

    async def sample(**kwargs):
        heartbeat = datetime.now(timezone.utc).isoformat()
        return {
            "observed_at": heartbeat,
            "instance_id": "instance-1",
            "heartbeat_at": heartbeat,
            "heartbeat_age_seconds": 0.0,
            "entry_enabled": False,
            "gates": {name: True for name in soak.REQUIRED_GATES},
            "open_order_count": 0,
            "open_order_symbols": [],
            "nonzero_position_count": 0,
            "nonzero_position_symbols": [],
        }

    monkeypatch.setattr(soak, "collect_sample", sample)
    report = {}
    await soak.run_soak(
        arguments(),
        report,
        rest=object(),
        http=object(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert report["result"] == "SOAK_OK"
    assert report["instance_id"] == "instance-1"
    assert report["sample_count"] == 3
    assert report["actual_duration_seconds"] == 10.0


@pytest.mark.asyncio
async def test_run_soak_fails_after_consecutive_dependency_errors(monkeypatch):
    clock = Clock()

    async def broken_sample(**kwargs):
        raise httpx.ConnectError("secret signed URL")

    monkeypatch.setattr(soak, "collect_sample", broken_sample)
    report = {}
    with pytest.raises(soak.SoakFailure) as failure:
        await soak.run_soak(
            arguments(max_consecutive_errors=1),
            report,
            rest=object(),
            http=object(),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert failure.value.code == "DEPENDENCY_UNREACHABLE"
    assert "secret" not in str(failure.value)
    assert report["sample_count"] == 0
    assert report["instance_id"] is None
    assert report["actual_duration_seconds"] == 5.0
    assert report["last_sample"] is None
    assert [item["type"] for item in report["transient_errors"]] == [
        "ConnectError",
        "ConnectError",
    ]
    assert "secret" not in str(report)


@pytest.mark.asyncio
async def test_run_soak_preserves_last_valid_sample_on_failure(monkeypatch):
    clock = Clock()
    calls = 0

    async def sample_then_timeout(**kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise httpx.ReadTimeout("secret response")
        heartbeat = datetime.now(timezone.utc).isoformat()
        return {
            "observed_at": heartbeat,
            "instance_id": "instance-1",
            "heartbeat_at": heartbeat,
            "heartbeat_age_seconds": 0.0,
            "entry_enabled": False,
            "gates": {name: True for name in soak.REQUIRED_GATES},
            "open_order_count": 0,
            "open_order_symbols": [],
            "nonzero_position_count": 0,
            "nonzero_position_symbols": [],
        }

    monkeypatch.setattr(soak, "collect_sample", sample_then_timeout)
    report = {}
    with pytest.raises(soak.SoakFailure) as failure:
        await soak.run_soak(
            arguments(duration_seconds=15, max_consecutive_errors=1),
            report,
            rest=object(),
            http=object(),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert failure.value.code == "DEPENDENCY_UNREACHABLE"
    assert report["sample_count"] == 1
    assert report["instance_id"] == "instance-1"
    assert report["actual_duration_seconds"] == 10.0
    assert report["last_sample"]["instance_id"] == "instance-1"
    assert [item["type"] for item in report["transient_errors"]] == [
        "ReadTimeout",
        "ReadTimeout",
    ]
    assert "secret" not in str(report)


@pytest.mark.asyncio
async def test_run_soak_does_not_treat_application_error_as_transient(monkeypatch):
    clock = Clock()

    async def broken_sample(**kwargs):
        raise ValueError("invalid response")

    monkeypatch.setattr(soak, "collect_sample", broken_sample)
    report = {}
    with pytest.raises(ValueError):
        await soak.run_soak(
            arguments(),
            report,
            rest=object(),
            http=object(),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert report["transient_errors"] == []


@pytest.mark.asyncio
async def test_run_soak_records_bounded_runtime_recovery(monkeypatch):
    clock = Clock()
    calls = 0

    async def sample_with_recovery(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise soak.SoakFailure("RUNTIME_DEGRADED", "runtime degraded")
        heartbeat = datetime.now(timezone.utc).isoformat()
        return {
            "observed_at": heartbeat,
            "instance_id": "instance-1",
            "heartbeat_at": heartbeat,
            "heartbeat_age_seconds": 0.0,
            "entry_enabled": False,
            "gates": {name: True for name in soak.REQUIRED_GATES},
            "open_order_count": 0,
            "open_order_symbols": [],
            "nonzero_position_count": 0,
            "nonzero_position_symbols": [],
        }

    monkeypatch.setattr(soak, "collect_sample", sample_with_recovery)
    report = {}
    await soak.run_soak(
        arguments(runtime_recovery_seconds=10),
        report,
        rest=object(),
        http=object(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert report["result"] == "SOAK_OK"
    assert report["sample_count"] == 2
    assert report["runtime_recoveries"][0]["duration_seconds"] == 5.0
    assert report["runtime_recoveries"][0]["recovered_at"] is not None


@pytest.mark.asyncio
async def test_run_soak_fails_when_runtime_recovery_exceeds_window(monkeypatch):
    clock = Clock()

    async def degraded_sample(**kwargs):
        raise soak.SoakFailure("RUNTIME_DEGRADED", "runtime degraded")

    monkeypatch.setattr(soak, "collect_sample", degraded_sample)
    report = {}
    with pytest.raises(soak.SoakFailure) as failure:
        await soak.run_soak(
            arguments(duration_seconds=20, runtime_recovery_seconds=6),
            report,
            rest=object(),
            http=object(),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert failure.value.code == "RUNTIME_RECOVERY_TIMEOUT"
    assert report["sample_count"] == 0
    assert report["runtime_recoveries"][0]["recovered_at"] is None


def test_runtime_halt_takes_priority_over_degraded_status():
    with pytest.raises(soak.SoakFailure) as failure:
        soak.validate_runtime_sample(
            runtime_item(effective_status="degraded", halted=True),
            expected_instance_id=None,
            expected_entry_enabled="false",
            now=datetime.now(timezone.utc),
            heartbeat_max_age_seconds=15,
        )

    assert failure.value.code == "RUNTIME_HALTED"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"duration_seconds": 0}, "DURATION_INVALID"),
        ({"sample_seconds": 0}, "SAMPLE_INTERVAL_INVALID"),
        ({"heartbeat_max_age_seconds": 0}, "HEARTBEAT_LIMIT_INVALID"),
        ({"max_consecutive_errors": -1}, "ERROR_LIMIT_INVALID"),
        ({"runtime_recovery_seconds": -1}, "RUNTIME_RECOVERY_LIMIT_INVALID"),
    ],
)
def test_cli_numeric_arguments_fail_closed(changes, code):
    with pytest.raises(soak.SoakFailure) as failure:
        soak._validate_args(arguments(**changes))

    assert failure.value.code == code
