import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parents[2]


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is unavailable")
def test_ledger_and_migration_runner_use_the_same_image_artifact():
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]

    assert services["ledger"]["image"] == services["ledger-migrate"]["image"]
    assert "build" in services["ledger"]
    assert "build" not in services["ledger-migrate"]


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is unavailable")
def test_spike_profile_runs_preflight_and_checks_persisted_runtime_health():
    result = subprocess.run(
        ["docker", "compose", "--profile", "spike", "config", "--format", "json"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    spike = json.loads(result.stdout)["services"]["spike"]

    assert "spike_runtime_guard.py preflight" in " ".join(spike["command"])
    assert spike["healthcheck"]["test"] == [
        "CMD",
        "python",
        "scripts/spike_runtime_guard.py",
        "health",
    ]
    assert spike["depends_on"]["ledger"]["condition"] == "service_healthy"
    assert spike["environment"]["STRATEGY_ACCOUNT_ID"] == spike["environment"][
        "SPIKE_ACCOUNT_ID"
    ]
    assert (
        spike["environment"]["SPIKE_STRATEGY_PATH"]
        == "trading_platform.strategies.spike.v2_2:V22"
    )
    assert spike["environment"]["SPIKE_WAL_PATH"] == "/app/data/wal/spike_short.jsonl"
    assert spike["environment"]["LONG_BREAKOUT_ACCOUNT_ID"] == (
        "long_breakout_testnet"
    )
    assert spike["environment"]["LONG_BREAKOUT_WAL_PATH"] == (
        "/app/data/wal/long_breakout.jsonl"
    )

    environment = os.environ.copy()
    environment.update(spike["environment"])
    environment.update(
        BINANCE_API_KEY="compose-test-key",
        BINANCE_API_SECRET="compose-test-secret",
    )
    subprocess.run(
        [sys.executable, "scripts/spike_runtime_guard.py", "preflight"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    override_environment = os.environ.copy()
    override_environment["SPIKE_WAL_PATH"] = "/app/data/wal/account-b.jsonl"
    overridden = subprocess.run(
        ["docker", "compose", "--profile", "spike", "config", "--format", "json"],
        cwd=PROJECT_ROOT,
        env=override_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(overridden.stdout)["services"]["spike"]["environment"][
        "SPIKE_WAL_PATH"
    ] == "/app/data/wal/account-b.jsonl"


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is unavailable")
def test_long_breakout_profile_isolated_from_spike_and_has_runtime_guards():
    environment = os.environ.copy()
    environment.update(
        {
            "BINANCE_API_KEY": "shared-key",
            "BINANCE_API_SECRET": "shared-secret",
            "LONG_BREAKOUT_BINANCE_API_KEY": "long-breakout-key",
            "LONG_BREAKOUT_BINANCE_API_SECRET": "long-breakout-secret",
            "LONG_BREAKOUT_ACCOUNT_ID": "long_breakout_artifact",
            "SPIKE_ACCOUNT_ID": "spike_artifact",
            "LONG_BREAKOUT_WAL_PATH": "/app/data/wal/long-breakout-artifact.jsonl",
            "SPIKE_WAL_PATH": "/app/data/wal/spike-artifact.jsonl",
        }
    )
    result = subprocess.run(
        ["docker", "compose", "--profile", "*", "config", "--format", "json"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]
    long_breakout = services["long_breakout"]
    spike = services["spike"]

    assert long_breakout["profiles"] == ["long_breakout"]
    assert long_breakout["labels"]["trading-platform.role"] == "strategy"
    command = " ".join(long_breakout["command"])
    assert "scripts/long_breakout_runtime_guard.py preflight" in command
    assert "trading_platform.strategies.long_breakout.main" in command
    assert long_breakout["healthcheck"]["test"] == [
        "CMD",
        "python",
        "scripts/long_breakout_runtime_guard.py",
        "health",
    ]
    assert long_breakout["depends_on"] == {
        "postgres": {"condition": "service_healthy", "required": True},
        "ledger-migrate": {
            "condition": "service_completed_successfully",
            "required": True,
        },
        "market": {"condition": "service_healthy", "required": True},
        "ledger": {"condition": "service_healthy", "required": True},
    }

    long_environment = long_breakout["environment"]
    spike_environment = spike["environment"]
    assert long_environment["STRATEGY_ACCOUNT_ID"] == "long_breakout_artifact"
    assert long_environment["LONG_BREAKOUT_ACCOUNT_ID"] == "long_breakout_artifact"
    assert long_environment["BINANCE_API_KEY"] == "long-breakout-key"
    assert long_environment["BINANCE_API_SECRET"] == "long-breakout-secret"
    assert long_environment["LONG_BREAKOUT_WAL_PATH"] == (
        "/app/data/wal/long-breakout-artifact.jsonl"
    )
    assert long_environment["SPIKE_ACCOUNT_ID"] == "spike_artifact"
    assert long_environment["SPIKE_WAL_PATH"] == (
        "/app/data/wal/spike-artifact.jsonl"
    )
    assert long_environment["STRATEGY_ACCOUNT_ID"] != spike_environment[
        "STRATEGY_ACCOUNT_ID"
    ]
    assert long_environment["BINANCE_API_KEY"] != spike_environment["BINANCE_API_KEY"]
    assert long_environment["BINANCE_API_SECRET"] != spike_environment[
        "BINANCE_API_SECRET"
    ]
    assert long_environment["LONG_BREAKOUT_WAL_PATH"] != spike_environment[
        "SPIKE_WAL_PATH"
    ]
    assert "ports" not in long_breakout
