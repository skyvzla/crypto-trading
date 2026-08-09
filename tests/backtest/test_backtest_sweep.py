from pathlib import Path
import subprocess
import sys

import duckdb
import pandas as pd
import pytest

from trading_platform.backtest.sweep import (
    _annotate_collisions,
    _attach_breakout_context,
    ChildProcessRegistry,
    _estimate_monthly_memory,
    _find_simultaneous_signals,
    _parameter_summary,
    _worker_memory_plan,
    expand_specs,
)


def test_expand_specs_is_deterministic_and_period_sensitive():
    config = {
        "start": "2026-07-01",
        "end": "2026-08-01",
        "duckdb_path": "history.duckdb",
        "fixed": {"total_notional": 1000},
        "matrix": {"prior_high_lookback_hours": [0, 4]},
    }
    first = expand_specs(config, ["AKEUSDT"])
    second = expand_specs(config, ["AKEUSDT"])
    changed = expand_specs({**config, "end": "2026-09-01"}, ["AKEUSDT"])

    assert first == second
    assert len(first) == 2
    assert {item.run_id for item in first}.isdisjoint(
        {item.run_id for item in changed}
    )


def test_worker_memory_budget_rejects_less_than_four_gb():
    with pytest.raises(ValueError, match="at least 4GB"):
        _worker_memory_plan(4, "3GB", 70)


def test_worker_memory_budget_raises_limit_with_available_memory():
    workers, memory_limit = _worker_memory_plan(
        6,
        "4GB",
        75,
        available_memory_bytes=48 * 1024**3,
    )

    assert workers == 6
    assert memory_limit == "5632MB"


def test_worker_memory_budget_rejects_explicit_unsafe_worker_count():
    with pytest.raises(
        RuntimeError,
        match=r"--workers 6 requires at least 27\.0 GiB.*maximum safe workers: 4",
    ):
        _worker_memory_plan(
            6,
            "4GB",
            75,
            available_memory_bytes=24 * 1024**3,
        )


def test_worker_memory_budget_auto_selects_workers_when_unspecified():
    workers, memory_limit = _worker_memory_plan(
        None,
        "4GB",
        80,
        available_memory_bytes=24 * 1024**3,
    )

    assert workers == 4
    assert int(memory_limit.removesuffix("MB")) >= 4096


def test_child_process_registry_terminates_running_subprocess():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    registry = ChildProcessRegistry()
    registry.add(process)

    registry.terminate_all()

    assert process.wait(timeout=2) != 0


def test_collision_summary_uses_lowest_trade_as_conservative_result():
    trades = pd.DataFrame([
        {"symbol": "AKEUSDT", "parameters": "p", "signal_time": 1_000,
         "entry_time": 2_000, "exit_time": 10_000, "net_pnl": 8.0},
        {"symbol": "BTCUSDT", "parameters": "p", "signal_time": 2_000,
         "entry_time": 3_000, "exit_time": 5_000, "net_pnl": -12.0},
    ])
    annotated, collisions = _annotate_collisions(trades, tolerance_ms=1_000)
    comparison = pd.DataFrame([
        {"run_id": "a", "symbol": "AKEUSDT", "parameters": "p", "status": "ok",
         "trades": 1, "wins": 1, "net_pnl": 8.0, "total_profit": 8.0,
         "total_loss": 0.0, "commission": 1.0},
        {"run_id": "b", "symbol": "BTCUSDT", "parameters": "p", "status": "ok",
         "trades": 1, "wins": 0, "net_pnl": -12.0, "total_profit": 0.0,
         "total_loss": 12.0, "commission": 1.0},
    ])

    summary = _parameter_summary(comparison, collisions)

    assert annotated["collision_size"].tolist() == [2, 2]
    assert collisions.iloc[0]["independent_pnl"] == -4.0
    assert collisions.iloc[0]["conservative_pnl"] == -12.0
    assert summary.iloc[0]["conservative_net_pnl"] == -12.0


def test_simultaneous_signal_groups_require_multiple_symbols():
    signals = pd.DataFrame([
        {"symbol": "AKEUSDT", "parameters": "p", "event_time": 1_000},
        {"symbol": "BTCUSDT", "parameters": "p", "event_time": 1_500},
        {"symbol": "AKEUSDT", "parameters": "p", "event_time": 20_000},
    ])

    groups = _find_simultaneous_signals(signals, tolerance_ms=1_000)

    assert len(groups) == 1
    assert groups.iloc[0]["signal_count"] == 2


def test_breakout_context_uses_only_completed_minutes(tmp_path: Path):
    archive = tmp_path / "history.duckdb"
    connection = duckdb.connect(str(archive))
    connection.execute(
        "CREATE TABLE candles (symbol VARCHAR, timeframe VARCHAR, "
        "open_time TIMESTAMPTZ, close_time TIMESTAMPTZ, "
        "low DOUBLE, high DOUBLE)"
    )
    entry_time = 8 * 3_600_000
    rows = []
    for minute in range(8 * 60):
        open_time = minute * 60_000
        rows.append(("AKEUSDT", "1m", open_time, open_time + 59_999,
                     80.0 if minute == 300 else 100.0, 120.0))
    connection.executemany(
        "INSERT INTO candles VALUES (?, ?, to_timestamp(? / 1000.0), "
        "to_timestamp(? / 1000.0), ?, ?)", rows
    )
    connection.close()
    trades = pd.DataFrame([{
        "symbol": "AKEUSDT", "entry_time": entry_time,
        "entry_price": 120.0, "net_pnl": 5.0, "parameters": "p",
    }])

    enriched = _attach_breakout_context(
        trades, duckdb_path=str(archive), windows_hours=[4, 6]
    )

    assert bool(enriched.iloc[0]["low_4h_valid"])
    assert enriched.iloc[0]["low_4h"] == 80.0
    assert enriched.iloc[0]["rise_from_4h_low"] == 0.5
    assert "low_4h_7d_position" in enriched.columns

    estimate = _estimate_monthly_memory(
        str(archive), symbols=["AKEUSDT"], start_ms=0,
        end_ms=entry_time, chunk_hours=720, fetch_batch_size=10_000,
    )
    assert estimate.iloc[0]["event_rows"] == 480
    assert estimate.iloc[0]["estimated_stream_peak_gb"] > 0
