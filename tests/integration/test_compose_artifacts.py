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
