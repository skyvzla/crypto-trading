import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from trading_platform.backtest import run_spike_short, runner


def _write_sample_data(data_dir: Path) -> None:
    data_dir.joinpath("aggtrades").mkdir(parents=True)
    data_dir.joinpath("klines").mkdir(parents=True)

    pd.DataFrame(
        {
            "trade_time": [1780272000000, 1780272000500],
            "price": [100.0, 100.1],
            "qty": [1.0, 1.0],
        }
    ).to_parquet(data_dir / "aggtrades" / "BTCUSDT.parquet", index=False)

    for interval, duration_ms in (("1m", 60_000), ("5m", 300_000), ("15m", 900_000)):
        pd.DataFrame(
            {
                "open_time": [1780272000000],
                "close_time": [1780272000000 + duration_ms - 1],
                "open": [100.0],
                "high": [101.0],
                "low": [99.0],
                "close": [100.0],
                "volume": [10.0],
                "is_final": [True],
            }
        ).to_parquet(
            data_dir / "klines" / f"BTCUSDT_{interval}.parquet", index=False
        )


@pytest.mark.parametrize("entrypoint", ["generic", "dedicated"])
def test_spike_cli_runs_on_sample_data(tmp_path, monkeypatch, entrypoint):
    data_dir = tmp_path / "market"
    output_dir = tmp_path / entrypoint
    _write_sample_data(data_dir)

    common = [
        "--start", "2026-06-01", "--end", "2026-06-02",
        "--data-dir", str(data_dir), "--output", str(output_dir),
        "--total-notional", "1000",
    ]
    if entrypoint == "generic":
        argv = ["runner", "--strategy", "spike", "--symbols", "BTCUSDT", *common]
        monkeypatch.setattr(sys, "argv", argv)
        runner.main()
    else:
        argv = ["run_spike_short", "--symbol", "BTCUSDT", *common]
        monkeypatch.setattr(sys, "argv", argv)
        run_spike_short.main()

    summary_path = output_dir / "summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text())
    assert summary["orders"]["total"] == 0
    assert summary["pnl"]["total_profit"] == 0.0


def test_spike_loader_requires_explicit_notional():
    with pytest.raises(ValueError, match="total-notional"):
        runner.load_strategy("spike", "backtest", symbols=["BTCUSDT"])
