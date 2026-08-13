import json
from io import StringIO
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import trading_platform.backtest.sweep as sweep
from trading_platform.backtest.sweep import (
    _annotate_collisions,
    _archive_coverage,
    _attach_breakout_context,
    ChildProcessRegistry,
    _configure_duckdb_connection,
    _estimate_monthly_memory,
    _find_simultaneous_signals,
    _parameter_summary,
    _run_symbol,
    _stream_process_output,
    _symbol_worker_memory_plan,
    _symbol_worker_resources,
    _worker_memory_plan,
    _write_tier3_only_projection_summary,
    _write_tier_fill_summary,
    expand_specs,
)
from trading_platform.market.archive.index import build_archive_index


def test_configure_duckdb_connection_limits_threads():
    connection = duckdb.connect(":memory:")
    try:
        _configure_duckdb_connection(connection, threads=2)

        assert int(connection.execute(
            "SELECT current_setting('threads')"
        ).fetchone()[0]) == 2
    finally:
        connection.close()


def test_archive_coverage_filters_symbols(monkeypatch):
    frame = pd.DataFrame([
        {"symbol": "AKEUSDT", "timeframe": "1s", "first_open_ms": 0,
         "last_close_ms": 999, "row_count": 1},
    ])
    monkeypatch.setattr(
        sweep, "_load_catalog_index", lambda *args, **kwargs: (frame, Path("index"))
    )

    coverage = _archive_coverage(
        "history.duckdb", start_ms=0, end_ms=2_000, symbols={"AKEUSDT"},
    )

    assert set(coverage) == {"AKEUSDT"}


def test_explicit_universe_only_scans_requested_symbols(monkeypatch):
    captured = {}
    monkeypatch.setattr(sweep, "_allowed_symbols", lambda *args, **kwargs: {
        "AKEUSDT", "BANKUSDT", "BTCUSDT"
    })

    def fake_coverage(*args, **kwargs):
        captured.update(kwargs)
        return {
            "AKEUSDT": {
                "1s": (0, 9_999_999, 1),
                "1m": (0, 9_999_999, 1),
                "5m": (0, 9_999_999, 1),
                "15m": (0, 9_999_999, 1),
            }
        }

    monkeypatch.setattr(sweep, "_archive_coverage", fake_coverage)
    config = {
        "start": "1970-01-01T00:00:00+00:00",
        "end": "1970-01-01T00:00:01+00:00",
        "duckdb_path": "history.duckdb",
        "database_dsn": "unused",
        "execution": {"duckdb_threads": 2},
        "universe": {"mode": "explicit", "symbols": ["AKEUSDT"]},
    }

    symbols, _ = sweep.resolve_universe(config)

    assert symbols == ["AKEUSDT"]
    assert captured["symbols"] == {"AKEUSDT"}


def test_anomaly_report_universe_intersects_database_and_archive(monkeypatch, tmp_path):
    report = tmp_path / "anomaly.csv"
    report.write_text("symbol,upper_wick_percent\nAKEUSDT,30\nZECUSDT,40\n", encoding="utf-8")
    monkeypatch.setattr(sweep, "_allowed_symbols", lambda *args, **kwargs: {"AKEUSDT", "ZECUSDT"})
    monkeypatch.setattr(
        sweep, "_archive_coverage",
        lambda *args, **kwargs: {
            "AKEUSDT": {"1s": (0, 9_999_999, 1), "1m": (0, 9_999_999, 1), "5m": (0, 9_999_999, 1), "15m": (0, 9_999_999, 1)},
            "ZECUSDT": {"1s": (0, 9_999_999, 1), "1m": (0, 9_999_999, 1), "5m": (0, 9_999_999, 1), "15m": (0, 9_999_999, 1)},
        },
    )
    symbols, rows = sweep.resolve_universe({
        "start": "1970-01-01T00:00:00+00:00", "end": "1970-01-01T00:00:01+00:00",
        "duckdb_path": "history.duckdb", "database_dsn": "unused",
        "universe": {"mode": "anomaly-report", "anomaly_report": str(report), "exclude_symbols": ["ZECUSDT"]},
    })
    assert symbols == ["AKEUSDT"]
    assert any(row["symbol"] == "ZECUSDT" and "explicitly_excluded" in row["exclude_reason"] for row in rows)


def test_main_handles_duckdb_query_interrupt_without_traceback(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        sweep, "_main", lambda argv=None: (_ for _ in ()).throw(
            RuntimeError("Query interrupted")
        )
    )

    assert sweep.main([]) == 130
    assert "回测已停止" in capsys.readouterr().out


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


def test_worker_memory_budget_does_not_expand_duckdb_limit():
    workers, memory_limit = _worker_memory_plan(
        6,
        "4GB",
        75,
        available_memory_bytes=48 * 1024**3,
    )

    assert workers == 6
    assert memory_limit == "3072MB"


def test_worker_memory_budget_rejects_explicit_unsafe_worker_count():
    with pytest.raises(
        RuntimeError,
        match=r"--workers 6 requires at least 24\.0 GiB.*maximum safe workers: 4",
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
    assert memory_limit == "3072MB"


def test_symbol_worker_count_never_exceeds_selected_symbols():
    workers, memory_limit = _symbol_worker_memory_plan(
        5,
        2,
        "4GB",
        75,
        available_memory_bytes=48 * 1024**3,
    )

    assert workers == 2
    assert memory_limit == "3072MB"


def test_eight_workers_fit_four_gb_budget_at_ninety_percent():
    workers, memory_limit = _worker_memory_plan(
        8,
        "4GB",
        90,
        available_memory_bytes=36 * 1024**3,
    )

    assert workers == 8
    assert memory_limit == "3072MB"


def test_memory_limit_switch_disables_budget_and_duckdb_cap():
    workers, worker_budget, duckdb_limit = _symbol_worker_resources(
        8,
        100,
        {"memory_limit_enabled": False},
        available_memory_bytes=1,
    )

    assert workers == 8
    assert worker_budget is None
    assert duckdb_limit is None


def test_disabled_memory_limit_requires_explicit_workers():
    with pytest.raises(ValueError, match="--workers"):
        _symbol_worker_resources(
            None,
            100,
            {"memory_limit_enabled": False},
        )


def test_symbol_task_uses_one_subprocess_for_multiple_parameters(
    tmp_path: Path, monkeypatch
):
    process_commands = []

    class FakeProcess:
        returncode = 0

        def __init__(self, command, **kwargs):
            process_commands.append(command)
            self.stdout = StringIO("shared stream\n")
            self.stderr = StringIO("")
            task = json.loads(Path(command[-1]).read_text())
            for run in task["runs"]:
                arguments = run["arguments"]
                output = Path(arguments[arguments.index("--output") + 1])
                output.mkdir(parents=True, exist_ok=True)
                (output / "summary.json").write_text(
                    '{"positions":{"total":0,"profitable":0},'
                    '"pnl":{"net_pnl":0,"total_profit":0,'
                    '"total_loss":0,"total_commission":0}}'
                )

        def wait(self):
            return self.returncode

    monkeypatch.setattr(sweep.subprocess, "Popen", FakeProcess)
    specs = [
        sweep.RunSpec(f"run-{lookback}", "AKEUSDT", {
            "total_notional": 1000,
            "prior_high_lookback_hours": lookback,
        })
        for lookback in (4, 8)
    ]
    config = {
        "start": "2026-07-01",
        "end": "2026-08-01",
        "duckdb_path": "history.duckdb",
        "execution": {"resume": False},
    }

    rows, elapsed = _run_symbol(specs, config, tmp_path)

    assert len(process_commands) == 1
    assert elapsed >= 0
    assert {row["run_id"] for row in rows} == {"run-4", "run-8"}
    assert all(row["status"] == "ok" for row in rows)
    assert "--prior-high-lookback-hours 4" in (
        tmp_path / "runs/run-4/command.txt"
    ).read_text()
    assert "run_spike_sweep_symbol" in (
        tmp_path / "runs/run-4/symbol_command.txt"
    ).read_text()


def test_child_process_registry_terminates_running_subprocess():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    registry = ChildProcessRegistry()
    registry.add(process)

    started = time.monotonic()
    registry.terminate_all()

    assert process.wait(timeout=2) != 0
    assert time.monotonic() - started < 2


def test_streamed_symbol_process_can_be_terminated_without_hanging():
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import time; print('started', flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    registry = ChildProcessRegistry()
    registry.add(process)
    with ThreadPoolExecutor(max_workers=1) as pool:
        output = pool.submit(
            _stream_process_output, process, symbol="AKEUSDT"
        )
        time.sleep(0.1)
        registry.terminate_all()
        stdout, stderr = output.result(timeout=2)

    assert "started" in stdout
    assert stderr == ""
    assert process.returncode != 0


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


def test_tier_fill_summary_uses_actual_filled_quantities(tmp_path: Path):
    trades = pd.DataFrame([
        {
            "parameters": "p", "tier1_fill_quantity": 1.0,
            "tier2_fill_quantity": 0.0, "tier3_fill_quantity": 0.0,
            "gross_pnl": 12.0, "commission": 1.0, "net_pnl": 11.0,
            "entry_notional": 300.0,
        },
        {
            "parameters": "p", "tier1_fill_quantity": 1.0,
            "tier2_fill_quantity": 1.0, "tier3_fill_quantity": 1.0,
            "gross_pnl": -8.0, "commission": 1.0, "net_pnl": -9.0,
            "entry_notional": 1_000.0,
        },
    ])

    _write_tier_fill_summary(trades, tmp_path)

    summary = pd.read_csv(tmp_path / "tier_fill_summary.csv")
    assert summary["filled_tier_label"].tolist() == ["一档成交", "三档全成交"]
    assert summary["trades"].tolist() == [1, 1]
    assert summary["net_pnl"].tolist() == [11.0, -9.0]


def test_tier3_only_projection_reprices_third_tier_and_scales_notional(
    tmp_path: Path,
):
    trades = pd.DataFrame([{
        "parameters": '{"total_notional": 1000}', "side": "SHORT",
        "tier3_fill_quantity": 3.0, "tier3_avg_fill_price": 100.0,
        "exit_price": 90.0, "entry_notional": 1_000.0,
        "entry_quantity": 10.0, "commission": 1.9,
    }])

    _write_tier3_only_projection_summary(trades, tmp_path)

    summary = pd.read_csv(tmp_path / "tier3_only_projection_summary.csv")
    assert summary.iloc[0]["trades"] == 1
    assert summary.iloc[0]["gross_pnl"] == 30.0
    assert summary.iloc[0]["commission"] == pytest.approx(0.57)
    assert summary.iloc[0]["net_pnl"] == pytest.approx(29.43)
    assert summary.iloc[0]["scaled_to_total_notional_net_pnl"] == pytest.approx(98.1)


def test_breakout_context_uses_only_completed_minutes(
    tmp_path: Path, monkeypatch
):
    archive = tmp_path / "history.duckdb"
    archive_root = tmp_path / "history-parquet"
    partition = archive_root / "AKEUSDT/1m/1970/01/00/candles.parquet"
    partition.parent.mkdir(parents=True)
    entry_time = 8 * 3_600_000
    rows = {
        "symbol": [], "timeframe": [], "open_time": [], "close_time": [],
        "low": [], "high": [],
    }
    for minute in range(8 * 60):
        open_time = minute * 60_000
        rows["symbol"].append("AKEUSDT")
        rows["timeframe"].append("1m")
        rows["open_time"].append(open_time)
        rows["close_time"].append(open_time + 59_999)
        rows["low"].append(80.0 if minute == 300 else 100.0)
        rows["high"].append(120.0)
    table = pa.table({
        "symbol": rows["symbol"], "timeframe": rows["timeframe"],
        "open_time": pa.array(rows["open_time"], type=pa.timestamp("ms", tz="UTC")),
        "close_time": pa.array(rows["close_time"], type=pa.timestamp("ms", tz="UTC")),
        "open": [100.0] * len(rows["low"]), "high": rows["high"],
        "low": rows["low"], "close": [100.0] * len(rows["low"]),
        "volume": [1.0] * len(rows["low"]),
    })
    pq.write_table(table, partition)
    index_path = build_archive_index(archive_root, workers=1)
    trades = pd.DataFrame([{
        "symbol": "AKEUSDT", "entry_time": entry_time,
        "entry_price": 120.0, "net_pnl": 5.0, "parameters": "p",
    }])

    enriched = _attach_breakout_context(
        trades, archive_index_path=str(index_path),
        windows_hours=[4, 6], duckdb_threads=1, workers=1,
    )

    assert bool(enriched.iloc[0]["low_4h_valid"])
    assert enriched.iloc[0]["low_4h"] == 80.0
    assert enriched.iloc[0]["rise_from_4h_low"] == 0.5
    assert "low_4h_7d_position" in enriched.columns

    index_frame = pd.DataFrame([{
        "symbol": "AKEUSDT", "timeframe": "1m", "year": 1970, "month": 1,
        "row_count": 480, "first_open_ms": 0, "last_close_ms": entry_time - 1,
    }])
    monkeypatch.setattr(
        sweep,
        "_load_catalog_index",
        lambda *args, **kwargs: (index_frame, Path("index")),
    )
    estimate = _estimate_monthly_memory(
        str(archive), symbols=["AKEUSDT"], start_ms=0,
        end_ms=entry_time, chunk_hours=720, fetch_batch_size=10_000,
    )
    assert estimate.iloc[0]["event_rows"] == 480
    assert estimate.iloc[0]["estimated_stream_peak_gb"] > 0
