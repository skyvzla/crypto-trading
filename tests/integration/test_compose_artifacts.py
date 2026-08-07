import json
import shutil
import subprocess
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
