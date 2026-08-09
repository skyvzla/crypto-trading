import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from trading_platform.backtest import run_spike_short, run_spike_sweep_symbol, runner


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


def _write_positive_spike_data(data_dir: Path) -> None:
    minute = 60_000
    start = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    warmup_start = start - 16 * 60 * minute
    data_dir.joinpath("aggtrades").mkdir(parents=True)
    data_dir.joinpath("klines").mkdir(parents=True)

    minute_rows = []
    for index in range(16 * 60):
        open_time = warmup_start + index * minute
        minute_rows.append({
            "open_time": open_time,
            "close_time": open_time + minute - 1,
            "open": 100.0,
            "high": 102.0,
            "low": 80.0,
            "close": 100.0,
            "volume": 10.0,
            "is_final": True,
        })
    pd.DataFrame(minute_rows).to_parquet(
        data_dir / "klines" / "BTCUSDT_1m.parquet", index=False
    )

    five_rows = []
    for index in range(15):
        open_time = start - (15 - index) * 5 * minute
        five_rows.append({
            "open_time": open_time,
            "close_time": open_time + 5 * minute - 1,
            "open": 100.0,
            "high": 102.0,
            "low": 98.0,
            "close": 100.0,
            "volume": 10.0,
            "is_final": True,
        })
    pd.DataFrame(five_rows).to_parquet(
        data_dir / "klines" / "BTCUSDT_5m.parquet", index=False
    )

    trades = []
    bar_start = start - minute
    closes = [100.0] * 56 + [100.0, 101.0, 102.0, 104.0]
    for index, close in enumerate(closes):
        trades.append({
            "trade_time": bar_start + index * 1_000,
            "price": close,
            "qty": 4.0 if index >= 56 else 1.0,
        })
    trades.extend([
        {"trade_time": start, "price": 120.0, "qty": 2.0},
        {"trade_time": start + 500, "price": 106.0, "qty": 2.0},
        {"trade_time": start + 1_000, "price": 110.0, "qty": 1.0},
        {"trade_time": start + 2_000, "price": 119.0, "qty": 0.5},
        {"trade_time": start + 2_500, "price": 110.0, "qty": 0.5},
    ])
    pd.DataFrame(trades).to_parquet(
        data_dir / "aggtrades" / "BTCUSDT.parquet", index=False
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
        argv = [
            "run_spike_short", "--symbol", "BTCUSDT",
            *common, "--prior-high-lookback-hours", "8",
        ]
        monkeypatch.setattr(sys, "argv", argv)
        run_spike_short.main()

    summary_path = output_dir / "summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text())
    run_meta = json.loads((output_dir / "run_meta.json").read_text())
    assert summary["orders"]["total"] == 0
    assert summary["pnl"]["total_profit"] == 0.0
    assert run_meta["total_events"] == 4
    assert run_meta["config"]["prior_high_lookback_minutes"] == (
        240 if entrypoint == "generic" else 480
    )
    assert (output_dir / "audit_events.parquet").exists()


def test_spike_loader_requires_explicit_notional():
    with pytest.raises(ValueError, match="total-notional"):
        runner.load_strategy("spike", "backtest", symbols=["BTCUSDT"])


def test_symbol_sweep_runner_writes_multiple_reports_from_one_market_stream(
    tmp_path,
):
    data_dir = tmp_path / "market"
    _write_sample_data(data_dir)
    common = [
        "--symbol", "BTCUSDT",
        "--start", "2026-06-01",
        "--end", "2026-06-02",
        "--data-dir", str(data_dir),
        "--total-notional", "1000",
    ]
    task = {"runs": [
        {
            "run_id": run_id,
            "arguments": [*common, "--output", str(tmp_path / run_id)],
        }
        for run_id in ("parameter-a", "parameter-b")
    ]}

    assert run_spike_sweep_symbol.run_symbol_task(task) == 0

    summaries = [
        json.loads((tmp_path / run_id / "summary.json").read_text())
        for run_id in ("parameter-a", "parameter-b")
    ]
    assert summaries[0] == summaries[1]
    assert (tmp_path / "parameter-a" / "trades.csv").exists()
    assert (tmp_path / "parameter-b" / "audit_events.parquet").exists()


def test_spike_cli_persists_positive_replay_audit(tmp_path, monkeypatch):
    data_dir = tmp_path / "market"
    output_dir = tmp_path / "positive"
    _write_positive_spike_data(data_dir)
    monkeypatch.setattr(sys, "argv", [
        "runner",
        "--strategy", "spike",
        "--symbols", "BTCUSDT",
        "--start", "2026-06-01",
        "--end", "2026-06-02",
        "--data-dir", str(data_dir),
        "--output", str(output_dir),
        "--total-notional", "1000",
    ])

    runner.main()

    summary = json.loads((output_dir / "summary.json").read_text())
    audit = pd.read_parquet(output_dir / "audit_events.parquet")
    positions = pd.read_parquet(output_dir / "positions.parquet")
    assert summary["orders"]["total"] == 3
    assert summary["orders"]["filled"] == 3
    assert summary["positions"]["open"] == 1
    assert set(audit["event_type"]) == {
        "signal_triggered",
        "entry_plan_created",
        "campaign_first_fill",
    }
    assert positions.loc[0, "status"] == "OPEN"


def test_generic_runner_dates_are_parsed_as_utc():
    assert runner.parse_date("2026-06-01") == 1780272000000
