import json
from decimal import Decimal
from io import StringIO
import os
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
from trading_platform.backtest import run_spike_short
from trading_platform.backtest.sweep import (
    _annotate_collisions,
    _archive_coverage,
    _attach_breakout_context,
    ChildProcessRegistry,
    _collect_signal_audit_events,
    _configure_duckdb_connection,
    _configured_worker_count,
    _estimate_monthly_memory,
    _find_simultaneous_signals,
    _parameter_summary,
    _run_symbol,
    _stream_process_output,
    _symbol_worker_memory_plan,
    _symbol_worker_resources,
    _worker_memory_plan,
    _write_report,
    _write_tier3_only_projection_summary,
    _write_tier_fill_summary,
    expand_specs,
)
from trading_platform.backtest.process_lock import (
    BacktestAlreadyRunning,
    BacktestProcessLock,
)
from trading_platform.market.archive.index import build_archive_index
from trading_platform.shared.config import BacktestConfig


def _write_worker_run_meta(output: Path) -> None:
    identity = json.loads(
        (output / ".sweep_identity.json").read_text(encoding="utf-8")
    )
    (output / "run_meta.json").write_text(
        json.dumps({
            "run_id": identity["run_id"],
            "virtual_time_start": 0,
            "virtual_time_end": 1,
        }),
        encoding="utf-8",
    )


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
    monkeypatch.setattr(
        sweep, "_symbol_onboard_times_ms", lambda *args, **kwargs: {"AKEUSDT": 0}
    )

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
        sweep,
        "_symbol_onboard_times_ms",
        lambda *args, **kwargs: {"AKEUSDT": 0, "ZECUSDT": 0},
    )
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


def test_universe_includes_in_period_listing_but_rejects_old_symbol_data_gap(
    monkeypatch,
):
    day_ms = 86_400_000
    start_ms = 10 * day_ms
    end_ms = 40 * day_ms
    listing_ms = 20 * day_ms
    monkeypatch.setattr(
        sweep,
        "_allowed_symbols",
        lambda *args, **kwargs: {"NEWUSDT", "BROKENNEWUSDT", "OLDUSDT"},
    )
    monkeypatch.setattr(
        sweep,
        "_symbol_onboard_times_ms",
        lambda *args, **kwargs: {
            "NEWUSDT": listing_ms,
            "BROKENNEWUSDT": listing_ms,
            "OLDUSDT": 0,
        },
    )
    complete_after_listing = {
        timeframe: (listing_ms + 1_000, end_ms, 1)
        for timeframe in ("1s", "1m", "5m", "15m")
    }
    monkeypatch.setattr(
        sweep,
        "_archive_coverage",
        lambda *args, **kwargs: {
            "NEWUSDT": complete_after_listing,
            "BROKENNEWUSDT": {
                timeframe: (listing_ms + 2 * day_ms, end_ms, 1)
                for timeframe in ("1s", "1m", "5m", "15m")
            },
            "OLDUSDT": complete_after_listing,
        },
    )

    symbols, rows = sweep.resolve_universe({
        "start": "1970-01-11T00:00:00+00:00",
        "end": "1970-02-10T00:00:00+00:00",
        "duckdb_path": "history.duckdb",
        "database_dsn": "unused",
        "universe": {
            "mode": "explicit",
            "symbols": ["NEWUSDT", "BROKENNEWUSDT", "OLDUSDT"],
            "exclude_symbols": [],
            "coverage_tolerance_hours": 1,
        },
    })

    assert symbols == ["NEWUSDT"]
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["NEWUSDT"]["listed_during_period"] is True
    assert by_symbol["NEWUSDT"]["onboard_age_days_at_period_start"] is None
    assert by_symbol["NEWUSDT"]["new_at_period_start"] is False
    assert by_symbol["NEWUSDT"]["period_new_listing"] is True
    assert by_symbol["NEWUSDT"]["effective_start_ms"] == listing_ms
    assert by_symbol["NEWUSDT"]["effective_start"] == (
        "1970-01-21T00:00:00+00:00"
    )
    assert by_symbol["NEWUSDT"]["data_incomplete"] is False
    assert by_symbol["BROKENNEWUSDT"]["listed_during_period"] is True
    assert by_symbol["BROKENNEWUSDT"]["data_incomplete"] is True
    assert "archive_starts_after_required_start" in (
        by_symbol["BROKENNEWUSDT"]["exclude_reason"]
    )
    assert by_symbol["OLDUSDT"]["listed_during_period"] is False
    assert by_symbol["OLDUSDT"]["data_incomplete"] is True
    assert "archive_starts_after_required_start" in by_symbol["OLDUSDT"]["exclude_reason"]


@pytest.mark.parametrize(
    ("new_listing_mark_days", "expected_new"),
    [(None, True), (4, False)],
)
def test_universe_marks_listing_shortly_before_period_start_without_changing_coverage(
    monkeypatch,
    new_listing_mark_days,
    expected_new,
):
    day_ms = 86_400_000
    start_ms = 10 * day_ms
    end_ms = 40 * day_ms
    bir_onboard_ms = start_ms - 5 * day_ms
    monkeypatch.setattr(
        sweep, "_allowed_symbols", lambda *args, **kwargs: {"BIRUSDT"}
    )
    monkeypatch.setattr(
        sweep,
        "_symbol_onboard_times_ms",
        lambda *args, **kwargs: {"BIRUSDT": bir_onboard_ms},
    )
    monkeypatch.setattr(
        sweep,
        "_archive_coverage",
        lambda *args, **kwargs: {
            "BIRUSDT": {
                timeframe: (bir_onboard_ms, end_ms, 1)
                for timeframe in ("1s", "1m", "5m", "15m")
            }
        },
    )
    universe = {
        "mode": "explicit",
        "symbols": ["BIRUSDT"],
        "exclude_symbols": [],
        "coverage_tolerance_hours": 1,
    }
    if new_listing_mark_days is not None:
        universe["new_listing_mark_days"] = new_listing_mark_days

    symbols, rows = sweep.resolve_universe({
        "start": "1970-01-11T00:00:00+00:00",
        "end": "1970-02-10T00:00:00+00:00",
        "duckdb_path": "history.duckdb",
        "database_dsn": "unused",
        "universe": universe,
    })

    assert symbols == ["BIRUSDT"]
    row = rows[0]
    assert row["listed_during_period"] is False
    assert row["effective_start_ms"] == start_ms
    assert row["onboard_age_days_at_period_start"] == 5
    assert row["new_at_period_start"] is expected_new
    assert row["period_new_listing"] is expected_new


def test_universe_rejects_negative_new_listing_mark_days(monkeypatch):
    monkeypatch.setattr(
        sweep, "_allowed_symbols", lambda *args, **kwargs: {"BIRUSDT"}
    )

    with pytest.raises(ValueError, match="new_listing_mark_days"):
        sweep.resolve_universe({
            "start": "1970-01-11T00:00:00+00:00",
            "end": "1970-02-10T00:00:00+00:00",
            "duckdb_path": "history.duckdb",
            "database_dsn": "unused",
            "universe": {
                "mode": "explicit",
                "symbols": ["BIRUSDT"],
                "exclude_symbols": [],
                "new_listing_mark_days": -1,
            },
        })


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


def test_backtest_process_lock_rejects_second_owner_and_releases(tmp_path):
    lock_path = tmp_path / "backtest.lock"
    first = BacktestProcessLock(lock_path)
    with first:
        with pytest.raises(BacktestAlreadyRunning, match="already running"):
            with BacktestProcessLock(lock_path):
                pass

    with BacktestProcessLock(lock_path):
        pass


def test_backtest_process_lock_is_released_when_owner_is_killed(tmp_path):
    lock_path = tmp_path / "backtest.lock"
    ready_path = tmp_path / "lock-acquired"
    source_root = Path(__file__).resolve().parents[2] / "src"
    child_code = (
        "from trading_platform.backtest.process_lock import BacktestProcessLock; "
        "from pathlib import Path; import time; "
        f"lock=BacktestProcessLock({str(lock_path)!r}); "
        f"lock.__enter__(); Path({str(ready_path)!r}).touch(); time.sleep(60)"
    )
    child_env = dict(os.environ, PYTHONPATH=str(source_root))
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        env=child_env,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready_path.exists():
            if child.poll() is not None:
                pytest.fail("child process exited before acquiring backtest lock")
            if time.monotonic() >= deadline:
                pytest.fail("child process did not acquire backtest lock")
            time.sleep(0.02)
    finally:
        child.kill()
        child.wait(timeout=5)

    with BacktestProcessLock(lock_path):
        pass


def test_main_reports_already_running(monkeypatch, tmp_path, capsys):
    lock_path = tmp_path / "backtest.lock"
    monkeypatch.setenv("BACKTEST_LOCK_FILE", str(lock_path))
    with BacktestProcessLock(lock_path):
        assert sweep.main([]) == 1

    assert "回测启动失败" in capsys.readouterr().err


def test_sweep_closes_dashboard_when_worker_submission_fails(
    tmp_path: Path, monkeypatch
):
    class RecordingDashboard:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = []

        def start(self, **kwargs):
            self.started = kwargs

        def close(self, *, status="ok", detail=None):
            self.closed.append((status, detail))

    class RaisingPool:
        def __init__(self, **kwargs):
            self.shutdown_calls = []

        def submit(self, *args, **kwargs):
            raise RuntimeError("submit failed")

        def shutdown(self, **kwargs):
            self.shutdown_calls.append(kwargs)

    config_path = tmp_path / "sweep.toml"
    config_path.write_text("name = 'test'", encoding="utf-8")
    dashboard = RecordingDashboard()
    pool = RaisingPool()
    specs = [
        sweep.RunSpec(f"run-{lookback}", "AKEUSDT", {
            "total_notional": 1000,
            "prior_high_lookback_hours": lookback,
        })
        for lookback in (4, 8)
    ]
    config = {
        "name": "test",
        "output": str(tmp_path / "output"),
        "duckdb_path": "history.duckdb",
        "start": "2026-07-01T00:00:00+00:00",
        "end": "2026-07-02T00:00:00+00:00",
        "execution": {"duckdb_threads": 1},
    }
    monkeypatch.setattr(sweep.tomllib, "loads", lambda _: config)
    monkeypatch.setattr(
        sweep,
        "resolve_universe",
        lambda _: (
            ["AKEUSDT"],
            [{
                "symbol": "AKEUSDT",
                "selected": True,
                "effective_start": "2026-07-01T00:00:00+00:00",
            }],
        ),
    )
    monkeypatch.setattr(sweep, "expand_specs", lambda *_: specs)
    monkeypatch.setattr(sweep, "archive_root_from_catalog", lambda _: tmp_path)
    monkeypatch.setattr(
        sweep, "_symbol_worker_resources", lambda *args: (1, None, None)
    )
    monkeypatch.setattr(sweep, "_estimate_monthly_memory", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        sweep,
        "TaskDashboard",
        lambda **kwargs: (dashboard.kwargs.update(kwargs) or dashboard),
    )
    monkeypatch.setattr(sweep, "ThreadPoolExecutor", lambda **kwargs: pool)

    with pytest.raises(RuntimeError, match="submit failed"):
        sweep._main(["--config", str(config_path)])

    assert pool.shutdown_calls == [{"wait": True, "cancel_futures": True}]
    assert dashboard.kwargs["total"] == 1
    assert dashboard.kwargs["workers"] == 1
    assert dashboard.started["detail"].startswith("pairs=1 runs=2 ")
    assert dashboard.closed == [("failed", None)]


def test_sweep_closes_dashboard_when_postprocessing_fails(
    tmp_path: Path, monkeypatch
):
    class RecordingDashboard:
        def __init__(self, **kwargs):
            self.closed = []

        def start(self, **kwargs):
            pass

        def close(self, *, status="ok", detail=None):
            self.closed.append((status, detail))

    class FakeFuture:
        def result(self):
            return ([{
                "run_id": "run-1", "symbol": "AKEUSDT", "status": "ok",
                "net_pnl": 0.0,
            }], 0.0)

        def cancel(self):
            pass

    class FakePool:
        def __init__(self, **kwargs):
            self.shutdown_calls = []

        def submit(self, *args, **kwargs):
            return future

        def shutdown(self, **kwargs):
            self.shutdown_calls.append(kwargs)

    config_path = tmp_path / "sweep.toml"
    config_path.write_text("name = 'test'", encoding="utf-8")
    dashboard = RecordingDashboard()
    future = FakeFuture()
    pool = FakePool()
    spec = sweep.RunSpec("run-1", "AKEUSDT", {"total_notional": 1000})
    config = {
        "name": "test",
        "output": str(tmp_path / "output"),
        "duckdb_path": "history.duckdb",
        "start": "2026-07-01T00:00:00+00:00",
        "end": "2026-07-02T00:00:00+00:00",
        "execution": {"duckdb_threads": 1},
    }
    monkeypatch.setattr(sweep.tomllib, "loads", lambda _: config)
    monkeypatch.setattr(
        sweep,
        "resolve_universe",
        lambda _: (
            ["AKEUSDT"],
            [{
                "symbol": "AKEUSDT",
                "selected": True,
                "effective_start": "2026-07-01T00:00:00+00:00",
            }],
        ),
    )
    monkeypatch.setattr(sweep, "expand_specs", lambda *_: [spec])
    monkeypatch.setattr(sweep, "archive_root_from_catalog", lambda _: tmp_path)
    monkeypatch.setattr(
        sweep, "_symbol_worker_resources", lambda *args: (1, None, None)
    )
    monkeypatch.setattr(
        sweep, "_estimate_monthly_memory", lambda *args, **kwargs: pd.DataFrame()
    )
    monkeypatch.setattr(sweep, "TaskDashboard", lambda **kwargs: dashboard)
    monkeypatch.setattr(sweep, "ThreadPoolExecutor", lambda **kwargs: pool)
    monkeypatch.setattr(sweep, "as_completed", lambda futures: list(futures))
    monkeypatch.setattr(
        sweep,
        "_attach_breakout_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("postprocessing failed")
        ),
    )

    with pytest.raises(RuntimeError, match="postprocessing failed"):
        sweep._main(["--config", str(config_path)])

    assert pool.shutdown_calls == [{"wait": True}]
    assert dashboard.closed == [("failed", None)]


def test_sweep_closes_dashboard_when_cleanup_is_interrupted_again(
    tmp_path: Path, monkeypatch
):
    class RecordingDashboard:
        def __init__(self, **kwargs):
            self.errors = []
            self.closed = []

        def start(self, **kwargs):
            pass

        def error(self, message):
            self.errors.append(message)

        def close(self, *, status="ok", detail=None):
            self.closed.append((status, detail))

    class FakeFuture:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class InterruptingPool:
        def __init__(self, **kwargs):
            self.shutdown_calls = []

        def submit(self, *args, **kwargs):
            return future

        def shutdown(self, **kwargs):
            self.shutdown_calls.append(kwargs)
            raise KeyboardInterrupt

    class RecordingRegistry:
        def __init__(self):
            self.terminated = False

        def terminate_all(self):
            self.terminated = True

    config_path = tmp_path / "sweep.toml"
    config_path.write_text("name = 'test'", encoding="utf-8")
    dashboard = RecordingDashboard()
    future = FakeFuture()
    pool = InterruptingPool()
    registry = RecordingRegistry()
    spec = sweep.RunSpec("run-1", "AKEUSDT", {"total_notional": 1000})
    config = {
        "name": "test",
        "output": str(tmp_path / "output"),
        "duckdb_path": "history.duckdb",
        "start": "2026-07-01T00:00:00+00:00",
        "end": "2026-07-02T00:00:00+00:00",
        "execution": {"duckdb_threads": 1},
    }
    monkeypatch.setattr(sweep.tomllib, "loads", lambda _: config)
    monkeypatch.setattr(
        sweep,
        "resolve_universe",
        lambda _: (
            ["AKEUSDT"],
            [{
                "symbol": "AKEUSDT",
                "selected": True,
                "effective_start": "2026-07-01T00:00:00+00:00",
            }],
        ),
    )
    monkeypatch.setattr(sweep, "expand_specs", lambda *_: [spec])
    monkeypatch.setattr(sweep, "archive_root_from_catalog", lambda _: tmp_path)
    monkeypatch.setattr(
        sweep, "_symbol_worker_resources", lambda *args: (1, None, None)
    )
    monkeypatch.setattr(
        sweep, "_estimate_monthly_memory", lambda *args, **kwargs: pd.DataFrame()
    )
    monkeypatch.setattr(sweep, "TaskDashboard", lambda **kwargs: dashboard)
    monkeypatch.setattr(sweep, "ThreadPoolExecutor", lambda **kwargs: pool)
    monkeypatch.setattr(sweep, "ChildProcessRegistry", lambda: registry)
    monkeypatch.setattr(
        sweep,
        "as_completed",
        lambda futures: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        sweep._main(["--config", str(config_path)])

    assert registry.terminated
    assert future.cancelled
    assert pool.shutdown_calls == [{"wait": True, "cancel_futures": True}]
    assert dashboard.closed == [("interrupted", None)]


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


def test_expand_specs_run_id_tracks_code_and_archive_content(monkeypatch, tmp_path):
    index_path = tmp_path / "archive_index.parquet"
    index_path.write_bytes(b"archive-v1")
    monkeypatch.setattr(sweep, "_backtest_code_fingerprint", lambda: "code-v1")
    config = {
        "start": "2026-07-01T00:00:00+00:00",
        "end": "2026-08-01T00:00:00+00:00",
        "duckdb_path": "history.duckdb",
        "archive_index_path": str(index_path),
        "fixed": {"total_notional": 1000},
    }

    first = expand_specs(config, ["AKEUSDT"])
    index_path.touch()
    same_content = expand_specs(config, ["AKEUSDT"])

    assert first == same_content

    monkeypatch.setattr(sweep, "_backtest_code_fingerprint", lambda: "code-v2")
    assert first[0].run_id != expand_specs(config, ["AKEUSDT"])[0].run_id
    index_path.write_bytes(b"archive-v2")
    assert first[0].run_id != expand_specs(config, ["AKEUSDT"])[0].run_id


def test_expand_specs_run_id_tracks_market_slippage_bps(monkeypatch, tmp_path):
    index_path = tmp_path / "archive_index.parquet"
    index_path.write_bytes(b"archive")
    monkeypatch.setattr(sweep, "_backtest_code_fingerprint", lambda: "code")
    base = {
        "start": "2026-07-01T00:00:00+00:00",
        "end": "2026-08-01T00:00:00+00:00",
        "duckdb_path": "history.duckdb",
        "archive_index_path": str(index_path),
        "fixed": {"total_notional": 1000, "market_slippage_bps": 0},
    }

    zero = expand_specs(base, ["AKEUSDT"])[0]
    changed = expand_specs({
        **base,
        "fixed": {"total_notional": 1000, "market_slippage_bps": 25},
    }, ["AKEUSDT"])[0]

    assert zero.run_id != changed.run_id


def test_expand_specs_run_id_normalizes_negative_zero_slippage(
    monkeypatch, tmp_path
):
    index_path = tmp_path / "archive_index.parquet"
    index_path.write_bytes(b"archive")
    monkeypatch.setattr(sweep, "_backtest_code_fingerprint", lambda: "code")
    base = {
        "start": "2026-07-01T00:00:00+00:00",
        "end": "2026-08-01T00:00:00+00:00",
        "duckdb_path": "history.duckdb",
        "archive_index_path": str(index_path),
        "fixed": {"total_notional": 1000},
    }

    negative_zero = expand_specs({
        **base,
        "fixed": {
            "total_notional": 1000,
            "entry_premium_mult": -0.0,
            "market_slippage_bps": -0.0,
        },
    }, ["AKEUSDT"])[0]
    positive_zero = expand_specs({
        **base,
        "fixed": {
            "total_notional": 1000,
            "entry_premium_mult": -0.0,
            "market_slippage_bps": 0.0,
        },
    }, ["AKEUSDT"])[0]

    assert negative_zero == positive_zero
    assert negative_zero.params["market_slippage_bps"] == 0.0
    assert str(negative_zero.params["market_slippage_bps"]) == "0.0"
    assert str(negative_zero.params["entry_premium_mult"]) == "-0.0"
    identity_kwargs = {
        "code_fingerprint": "code",
        "archive_index_fingerprint": sweep._archive_index_fingerprint(base),
    }
    assert sweep._run_identity(
        negative_zero, base, **identity_kwargs
    ) == sweep._run_identity(positive_zero, base, **identity_kwargs)

    unrelated_positive_zero = expand_specs({
        **base,
        "fixed": {
            "total_notional": 1000,
            "entry_premium_mult": 0.0,
            "market_slippage_bps": 0.0,
        },
    }, ["AKEUSDT"])[0]
    assert negative_zero.run_id != unrelated_positive_zero.run_id


@pytest.mark.parametrize(
    "zero_value",
    ["-0.0", "+0.0", Decimal("-0.0"), Decimal("+0.0")],
    ids=(
        "string-negative",
        "string-positive",
        "decimal-negative",
        "decimal-positive",
    ),
)
def test_expand_specs_normalizes_config_accepted_zero_representations(
    zero_value, monkeypatch, tmp_path
):
    assert BacktestConfig(
        market_slippage_bps=zero_value
    ).market_slippage_bps == 0.0
    index_path = tmp_path / "archive_index.parquet"
    index_path.write_bytes(b"archive")
    monkeypatch.setattr(sweep, "_backtest_code_fingerprint", lambda: "code")
    base = {
        "start": "2026-07-01T00:00:00+00:00",
        "end": "2026-08-01T00:00:00+00:00",
        "duckdb_path": "history.duckdb",
        "archive_index_path": str(index_path),
    }
    candidate = expand_specs({
        **base,
        "fixed": {"total_notional": 1000, "market_slippage_bps": zero_value},
    }, ["AKEUSDT"])[0]
    canonical = expand_specs({
        **base,
        "fixed": {"total_notional": 1000, "market_slippage_bps": 0.0},
    }, ["AKEUSDT"])[0]

    assert candidate == canonical
    assert candidate.params["market_slippage_bps"] == 0.0
    assert str(candidate.params["market_slippage_bps"]) == "0.0"
    identity_kwargs = {
        "code_fingerprint": "code",
        "archive_index_fingerprint": sweep._archive_index_fingerprint(base),
    }
    assert sweep._run_identity(
        candidate, base, **identity_kwargs
    ) == sweep._run_identity(canonical, base, **identity_kwargs)


@pytest.mark.parametrize(
    "value", ["12.5", Decimal("12.5"), "invalid", Decimal("NaN")]
)
def test_market_slippage_normalization_preserves_nonzero_and_invalid_values(value):
    params = {"total_notional": 1000, "market_slippage_bps": value}

    normalized = sweep._normalize_market_slippage(params)

    assert normalized["market_slippage_bps"] is value
    assert params["market_slippage_bps"] is value


def test_invalid_market_slippage_remains_visible_to_runner_validation(tmp_path):
    spec = sweep.RunSpec(
        "invalid-slippage",
        "AKEUSDT",
        {"total_notional": 1000, "market_slippage_bps": "invalid"},
    )
    config = {
        "start": "2026-07-01T00:00:00+00:00",
        "end": "2026-07-02T00:00:00+00:00",
        "duckdb_path": "history.duckdb",
    }
    arguments = sweep._run_arguments(spec, config, tmp_path)

    assert arguments[arguments.index("--market-slippage-bps") + 1] == "invalid"
    with pytest.raises(SystemExit):
        run_spike_short.parse_args(arguments)


def test_sweep_report_records_market_slippage_values(tmp_path):
    _write_report(
        tmp_path,
        pd.DataFrame(),
        run_count=2,
        workers=1,
        worker_memory_budget=None,
        duckdb_memory_limit=None,
        market_slippage_bps=[0, 25],
    )

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "MARKET 滑点（bps）：0, 25" in report


def test_backtest_code_fingerprint_tracks_shared_source_content(
    monkeypatch, tmp_path
):
    source_root = tmp_path / "src" / "trading_platform"
    fake_sweep = source_root / "backtest" / "sweep.py"
    shared_source = source_root / "shared" / "events.py"
    fake_sweep.parent.mkdir(parents=True)
    shared_source.parent.mkdir(parents=True)
    fake_sweep.write_text("# sweep\n", encoding="utf-8")
    shared_source.write_text("EVENT_VERSION = 1\n", encoding="utf-8")
    monkeypatch.setattr(sweep, "__file__", str(fake_sweep))

    first = sweep._backtest_code_fingerprint()
    shared_source.write_text("EVENT_VERSION = 2\n", encoding="utf-8")

    assert first is not None
    assert first != sweep._backtest_code_fingerprint()


def test_resume_is_disabled_when_metrics_index_is_unreliable(tmp_path: Path):
    archive_index_path = tmp_path / "archive_index.parquet"
    archive_index_path.write_bytes(b"archive")
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    spec = sweep.RunSpec("run-metrics", "AKEUSDT", {"total_notional": 1000})
    config = {
        "start": "2026-07-01",
        "end": "2026-08-01",
        "duckdb_path": "history.duckdb",
        "archive_index_path": str(archive_index_path),
        "metrics_root": str(metrics_root),
    }
    run_dir = tmp_path / "runs" / spec.run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        '{"positions":{"total":0,"profitable":0},'
        '"pnl":{"net_pnl":0,"total_profit":0,'
        '"total_loss":0,"total_commission":0}}'
    )
    identity = sweep._run_identity(
        spec,
        config,
        code_fingerprint=sweep._backtest_code_fingerprint(),
        archive_index_fingerprint=sweep._archive_index_fingerprint(config),
        metrics_fingerprint=None,
    )
    sweep._write_run_identity_marker(run_dir, identity)
    (run_dir / "run_meta.json").write_text(json.dumps({
        "run_id": spec.run_id,
        "virtual_time_start": 0,
        "virtual_time_end": 1,
    }))

    assert sweep._resume_summary(
        spec,
        config,
        run_dir,
        code_fingerprint=sweep._backtest_code_fingerprint(),
        archive_index_fingerprint=sweep._archive_index_fingerprint(config),
        metrics_fingerprint=sweep._metrics_input_fingerprint(config),
    ) is None


def test_run_symbol_rejects_summary_only_worker_output(
    tmp_path: Path, monkeypatch
):
    index_path = tmp_path / "archive_index.parquet"
    index_path.write_bytes(b"archive")
    monkeypatch.setattr(sweep, "_backtest_code_fingerprint", lambda: "code")
    spec = sweep.RunSpec("run-resume", "AKEUSDT", {"total_notional": 1000})
    run_dir = tmp_path / "runs" / spec.run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        '{"positions":{"total":0,"profitable":0},'
        '"pnl":{"net_pnl":0,"total_profit":0,'
        '"total_loss":0,"total_commission":0}}'
    )
    process_commands = []

    class FakeProcess:
        returncode = 0

        def __init__(self, command, **kwargs):
            process_commands.append(command)
            self.stdout = StringIO("")
            self.stderr = StringIO("")
            task = json.loads(Path(command[-1]).read_text())
            for run in task["runs"]:
                arguments = run["arguments"]
                output = Path(arguments[arguments.index("--output") + 1])
                (output / "summary.json").write_text(
                    '{"positions":{"total":0,"profitable":0},'
                    '"pnl":{"net_pnl":0,"total_profit":0,'
                    '"total_loss":0,"total_commission":0}}'
                )

        def wait(self):
            return self.returncode

    monkeypatch.setattr(sweep.subprocess, "Popen", FakeProcess)

    rows, _ = _run_symbol(
        [spec],
        {
            "start": "2026-07-01",
            "end": "2026-08-01",
            "duckdb_path": "history.duckdb",
            "archive_index_path": str(index_path),
            "execution": {"resume": True},
        },
        tmp_path,
    )

    assert process_commands
    assert rows[0]["status"] == "failed"
    assert "summary.json missing" in rows[0]["error"]
    assert not (run_dir / "run_meta.json").exists()


def test_run_symbol_accepts_real_worker_run_meta_without_sweep_identity(
    tmp_path: Path, monkeypatch
):
    index_path = tmp_path / "archive_index.parquet"
    index_path.write_bytes(b"archive")
    monkeypatch.setattr(sweep, "_backtest_code_fingerprint", lambda: "code")
    spec = sweep.RunSpec("run-worker-meta", "AKEUSDT", {"total_notional": 1000})

    class RealWorkerProcess:
        returncode = 0

        def __init__(self, command, **kwargs):
            self.stdout = StringIO("")
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
                _write_worker_run_meta(output)

        def wait(self):
            return self.returncode

    monkeypatch.setattr(sweep.subprocess, "Popen", RealWorkerProcess)

    rows, _ = _run_symbol(
        [spec],
        {
            "start": "2026-07-01",
            "end": "2026-08-01",
            "duckdb_path": "history.duckdb",
            "archive_index_path": str(index_path),
            "execution": {"resume": True},
        },
        tmp_path,
    )

    assert rows[0]["status"] == "ok"
    metadata = json.loads(
        (tmp_path / "runs" / spec.run_id / "run_meta.json").read_text()
    )
    assert metadata["run_id"] == spec.run_id
    assert "sweep_identity" not in metadata


def test_resume_rejects_mismatched_attempt_identity_marker(
    tmp_path: Path, monkeypatch
):
    index_path = tmp_path / "archive_index.parquet"
    index_path.write_bytes(b"archive")
    monkeypatch.setattr(sweep, "_backtest_code_fingerprint", lambda: "code")
    spec = sweep.RunSpec("run-marker-mismatch", "AKEUSDT", {"total_notional": 1000})
    config = {
        "start": "2026-07-01",
        "end": "2026-08-01",
        "duckdb_path": "history.duckdb",
        "archive_index_path": str(index_path),
    }
    run_dir = tmp_path / "runs" / spec.run_id
    run_dir.mkdir(parents=True)
    identity = sweep._run_identity(
        spec,
        config,
        code_fingerprint="code",
        archive_index_fingerprint=sweep._archive_index_fingerprint(config),
    )
    sweep._write_run_identity_marker(
        run_dir,
        {**identity, "end": "2026-08-02"},
    )
    (run_dir / "summary.json").write_text(
        '{"positions":{"total":0,"profitable":0},'
        '"pnl":{"net_pnl":0,"total_profit":0,'
        '"total_loss":0,"total_commission":0}}'
    )
    (run_dir / "run_meta.json").write_text(
        json.dumps({"run_id": spec.run_id, "virtual_time_start": 0})
    )

    assert sweep._resume_summary(
        spec,
        config,
        run_dir,
        code_fingerprint="code",
        archive_index_fingerprint=sweep._archive_index_fingerprint(config),
    ) is None


@pytest.mark.parametrize("unsafe_target", ["run_id", "runs", "run_dir"])
def test_reset_run_dir_rejects_unsafe_paths_and_preserves_files(
    tmp_path: Path, unsafe_target: str
):
    output_root = tmp_path / "output"
    output_root.mkdir()
    parent_file = output_root / "parent.keep"
    parent_file.write_text("parent", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    external_file = outside / "external.keep"
    external_file.write_text("external", encoding="utf-8")
    runs_dir = output_root / "runs"

    if unsafe_target == "run_id":
        runs_dir.mkdir()
        run_id = ".."
        run_dir = runs_dir / run_id
    elif unsafe_target == "runs":
        runs_dir.symlink_to(outside, target_is_directory=True)
        run_id = "run-safe"
        run_dir = runs_dir / run_id
    else:
        runs_dir.mkdir()
        linked_run = outside / "linked-run"
        linked_run.mkdir()
        run_id = "run-safe"
        run_dir = runs_dir / run_id
        run_dir.symlink_to(linked_run, target_is_directory=True)

    with pytest.raises(ValueError):
        sweep._reset_run_dir(
            run_dir,
            output_root=output_root,
            run_id=run_id,
        )

    assert parent_file.read_text(encoding="utf-8") == "parent"
    assert external_file.read_text(encoding="utf-8") == "external"


def test_run_symbol_partial_success_ignores_leftover_outputs(
    tmp_path: Path, monkeypatch
):
    process_commands = []

    class PartiallyFailedProcess:
        returncode = 7

        def __init__(self, command, **kwargs):
            process_commands.append(command)
            self.stdout = StringIO("")
            self.stderr = StringIO("worker failed")
            task = json.loads(Path(command[-1]).read_text())
            for run in task["runs"][:1]:
                arguments = run["arguments"]
                output = Path(arguments[arguments.index("--output") + 1])
                output.mkdir(parents=True, exist_ok=True)
                (output / "summary.json").write_text(
                    '{"positions":{"total":1,"profitable":1},'
                    '"pnl":{"net_pnl":99,"total_profit":99,'
                    '"total_loss":0,"total_commission":0}}'
                )
                _write_worker_run_meta(output)

        def wait(self):
            return self.returncode

    monkeypatch.setattr(sweep.subprocess, "Popen", PartiallyFailedProcess)
    specs = [
        sweep.RunSpec(f"run-{name}", "AKEUSDT", {"total_notional": 1000})
        for name in ("ok", "failed")
    ]
    stale_dir = tmp_path / "runs" / specs[1].run_id
    stale_dir.mkdir(parents=True)
    (stale_dir / "trades.csv").write_text("stale\n")
    (stale_dir / "audit_events.parquet").write_bytes(b"stale")
    (stale_dir / "summary.json").write_text(
        '{"positions":{"total":1,"profitable":1},'
        '"pnl":{"net_pnl":999,"total_profit":999,'
        '"total_loss":0,"total_commission":0}}'
    )

    rows, _ = _run_symbol(
        specs,
        {
            "start": "2026-07-01",
            "end": "2026-08-01",
            "duckdb_path": "history.duckdb",
            "execution": {"resume": False},
        },
        tmp_path,
    )

    assert process_commands
    by_id = {row["run_id"]: row for row in rows}
    assert by_id[specs[0].run_id]["status"] == "ok"
    assert by_id[specs[1].run_id]["status"] == "failed"
    assert by_id[specs[1].run_id]["returncode"] == 7
    assert not (stale_dir / "summary.json").exists()
    assert not (stale_dir / "trades.csv").exists()
    assert not (stale_dir / "audit_events.parquet").exists()


def test_run_arguments_use_symbol_effective_start(tmp_path: Path):
    spec = sweep.RunSpec("run-new", "NEWUSDT", {"total_notional": 1000})
    config = {
        "start": "2025-08-01T00:00:00+00:00",
        "end": "2026-08-01T00:00:00+00:00",
        "duckdb_path": "history.duckdb",
        "symbol_start_times": {
            "NEWUSDT": "2025-09-26T12:30:00+00:00",
        },
    }

    arguments = sweep._run_arguments(spec, config, tmp_path)

    assert arguments[arguments.index("--start") + 1] == (
        "2025-09-26T12:30:00+00:00"
    )


def test_run_arguments_map_pullback_moving_rise_threshold(tmp_path: Path):
    spec = sweep.RunSpec(
        "run-pullback-rise",
        "AKEUSDT",
        {
            "strategy": "trading_platform.strategies.spike.pullback:PullbackV3",
            "total_notional": 1000,
            "rise_60s_threshold": 0.4,
        },
    )
    config = {
        "start": "2025-08-01T00:00:00+00:00",
        "end": "2026-08-01T00:00:00+00:00",
        "duckdb_path": "history.duckdb",
    }

    arguments = sweep._run_arguments(spec, config, tmp_path)

    assert arguments[arguments.index("--rise-60s-threshold") + 1] == "0.4"


def test_worker_memory_budget_rejects_less_than_two_gb():
    with pytest.raises(ValueError, match="at least 2GB"):
        _worker_memory_plan(4, "1GB", 70)


def test_backtest_workers_are_read_from_environment(monkeypatch):
    monkeypatch.setenv("BACKTEST_WORKERS", "7")
    assert _configured_worker_count() == 7


@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_backtest_workers_environment_must_be_positive_integer(monkeypatch, value):
    monkeypatch.setenv("BACKTEST_WORKERS", value)
    with pytest.raises(ValueError, match="BACKTEST_WORKERS"):
        _configured_worker_count()


def test_fixed_worker_count_is_not_reduced_to_symbol_count():
    workers, worker_budget, duckdb_limit = _symbol_worker_resources(
        13,
        2,
        {"memory_limit_enabled": False},
    )
    assert workers == 13
    assert worker_budget is None
    assert duckdb_limit is None


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


def test_twelve_workers_fit_two_gb_budget_at_ninety_five_percent():
    workers, memory_limit = _worker_memory_plan(
        12,
        "2GB",
        95,
        available_memory_bytes=26 * 1024**3,
    )

    assert workers == 12
    assert memory_limit == "1024MB"


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
                _write_worker_run_meta(output)

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


def test_run_symbol_marks_fully_resumed_specs_complete_in_dashboard(tmp_path: Path):
    class RecordingDashboard:
        def __init__(self):
            self.started = []
            self.skipped = []

        def task_start(self, name):
            self.started.append(name)

        def task_skip(self, name, status, *, increment):
            self.skipped.append((name, status, increment))

    specs = [
        sweep.RunSpec(f"run-{lookback}", "AKEUSDT", {
            "total_notional": 1000,
            "prior_high_lookback_hours": lookback,
        })
        for lookback in (4, 8)
    ]
    archive_index_path = tmp_path / "archive_index.parquet"
    archive_index_path.write_bytes(b"archive")
    config = {
        "start": "2026-07-01",
        "end": "2026-08-01",
        "duckdb_path": "history.duckdb",
        "archive_index_path": str(archive_index_path),
        "execution": {"resume": True},
    }
    for spec in specs:
        run_dir = tmp_path / "runs" / spec.run_id
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            '{"positions":{"total":0,"profitable":0},'
            '"pnl":{"net_pnl":0,"total_profit":0,'
            '"total_loss":0,"total_commission":0}}'
        )
        identity = sweep._run_identity(
            spec,
            config,
            code_fingerprint=sweep._backtest_code_fingerprint(),
            archive_index_fingerprint=sweep._archive_index_fingerprint(config),
        )
        sweep._write_run_identity_marker(run_dir, identity)
        (run_dir / "run_meta.json").write_text(json.dumps({
            "run_id": spec.run_id,
            "virtual_time_start": 0,
            "virtual_time_end": 1,
        }))
    dashboard = RecordingDashboard()

    rows, _ = _run_symbol(
        specs,
        config,
        tmp_path,
        dashboard=dashboard,
    )

    assert [row["status"] for row in rows] == ["resumed", "resumed"]
    assert dashboard.started == ["AKEUSDT"]
    assert dashboard.skipped == [("AKEUSDT", "Resumed", 1)]


def test_run_symbol_counts_resumed_and_new_specs_in_dashboard(
    tmp_path: Path, monkeypatch
):
    class RecordingDashboard:
        def __init__(self):
            self.started = []
            self.done = []

        def task_start(self, name):
            self.started.append(name)

        def task_done(self, name, status, *, count_as_sample, increment):
            self.done.append((name, status, increment))

    class FakeProcess:
        returncode = 0

        def __init__(self, command, **kwargs):
            self.stdout = StringIO("")
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
                _write_worker_run_meta(output)

        def wait(self):
            return self.returncode

    monkeypatch.setattr(sweep.subprocess, "Popen", FakeProcess)
    resumed = sweep.RunSpec("run-resumed", "AKEUSDT", {
        "total_notional": 1000,
        "prior_high_lookback_hours": 4,
    })
    new = sweep.RunSpec("run-new", "AKEUSDT", {
        "total_notional": 1000,
        "prior_high_lookback_hours": 8,
    })
    archive_index_path = tmp_path / "archive_index.parquet"
    archive_index_path.write_bytes(b"archive")
    config = {
        "start": "2026-07-01",
        "end": "2026-08-01",
        "duckdb_path": "history.duckdb",
        "archive_index_path": str(archive_index_path),
        "execution": {"resume": True},
    }
    resumed_dir = tmp_path / "runs" / resumed.run_id
    resumed_dir.mkdir(parents=True)
    (resumed_dir / "summary.json").write_text(
        '{"positions":{"total":0,"profitable":0},'
        '"pnl":{"net_pnl":0,"total_profit":0,'
        '"total_loss":0,"total_commission":0}}'
    )
    identity = sweep._run_identity(
        resumed,
        config,
        code_fingerprint=sweep._backtest_code_fingerprint(),
        archive_index_fingerprint=sweep._archive_index_fingerprint(config),
    )
    sweep._write_run_identity_marker(resumed_dir, identity)
    (resumed_dir / "run_meta.json").write_text(json.dumps({
        "run_id": resumed.run_id,
        "virtual_time_start": 0,
        "virtual_time_end": 1,
    }))
    dashboard = RecordingDashboard()

    rows, _ = _run_symbol(
        [resumed, new],
        config,
        tmp_path,
        dashboard=dashboard,
    )

    assert {row["status"] for row in rows} == {"resumed", "ok"}
    assert dashboard.started == ["AKEUSDT"]
    assert dashboard.done == [("AKEUSDT", "OK", 1)]


def test_run_symbol_reruns_when_resume_metadata_is_invalid(
    tmp_path: Path, monkeypatch
):
    class RecordingDashboard:
        def __init__(self):
            self.started = []
            self.failed = []

        def task_start(self, name):
            self.started.append(name)

        def task_done(self, name, status, *, count_as_sample, increment):
            self.done = (name, status, increment)

    specs = [
        sweep.RunSpec(f"run-{lookback}", "AKEUSDT", {
            "total_notional": 1000,
            "prior_high_lookback_hours": lookback,
        })
        for lookback in (4, 8)
    ]
    archive_index_path = tmp_path / "archive_index.parquet"
    archive_index_path.write_bytes(b"archive")
    run_dir = tmp_path / "runs" / specs[0].run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text("not json")
    dashboard = RecordingDashboard()

    class FakeProcess:
        returncode = 0

        def __init__(self, command, **kwargs):
            self.stdout = StringIO("")
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
                _write_worker_run_meta(output)

        def wait(self):
            return self.returncode

    monkeypatch.setattr(sweep.subprocess, "Popen", FakeProcess)

    rows, _ = _run_symbol(
        specs,
        {
            "start": "2026-07-01",
            "end": "2026-08-01",
            "duckdb_path": "history.duckdb",
            "archive_index_path": str(archive_index_path),
            "execution": {"resume": True},
        },
        tmp_path,
        dashboard=dashboard,
    )

    assert dashboard.started == ["AKEUSDT"]
    assert [row["status"] for row in rows] == ["ok", "ok"]
    assert dashboard.done == ("AKEUSDT", "OK", 1)


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


def test_streamed_symbol_process_can_be_terminated_without_hanging(tmp_path):
    ready_path = tmp_path / "child-started"
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "from pathlib import Path; import time; "
            f"print('started', flush=True); Path({str(ready_path)!r}).touch(); "
            "time.sleep(60)",
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
        deadline = time.monotonic() + 10
        while not ready_path.exists():
            if process.poll() is not None:
                pytest.fail("child process exited before signaling readiness")
            if time.monotonic() >= deadline:
                pytest.fail("child process did not signal readiness")
            time.sleep(0.02)
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


def test_parameter_summary_win_rate_uses_closed_trades_and_names_open_counts():
    spec = sweep.RunSpec("run-open", "AKEUSDT", {"total_notional": 1000})
    comparison = pd.DataFrame([
        sweep._summary_row(spec, {
            "positions": {
                "total": 2,
                "closed": 1,
                "open": 1,
                "profitable": 1,
                "win_rate": 1.0,
            },
            "pnl": {
                "net_pnl": 5.0,
                "total_profit": 5.0,
                "total_loss": 0.0,
                "total_commission": 0.0,
            },
        }, "ok")
    ])

    summary = _parameter_summary(comparison, pd.DataFrame())

    assert comparison.iloc[0]["total_trades"] == 2
    assert comparison.iloc[0]["closed_trades"] == 1
    assert comparison.iloc[0]["open_trades"] == 1
    assert comparison.iloc[0]["win_rate"] == 1.0
    assert summary.iloc[0]["total_trades"] == 2
    assert summary.iloc[0]["closed_trades"] == 1
    assert summary.iloc[0]["open_trades"] == 1
    assert summary.iloc[0]["win_rate"] == 1.0


def test_parameter_summary_win_rate_is_zero_when_no_positions_closed():
    spec = sweep.RunSpec("run-open-only", "AKEUSDT", {"total_notional": 1000})
    comparison = pd.DataFrame([
        sweep._summary_row(spec, {
            "positions": {"total": 2, "closed": 0, "open": 2, "profitable": 0},
            "pnl": {},
        }, "ok")
    ])

    summary = _parameter_summary(comparison, pd.DataFrame())

    assert summary.iloc[0]["total_trades"] == 2
    assert summary.iloc[0]["closed_trades"] == 0
    assert summary.iloc[0]["open_trades"] == 2
    assert summary.iloc[0]["win_rate"] == 0.0


@pytest.mark.parametrize(
    ("stop_5m_high", "expected_flag"),
    [(False, "--no-stop-5m-high"), (True, None)],
)
def test_run_arguments_maps_stop_5m_high_to_opt_out_flag(
    tmp_path: Path, stop_5m_high, expected_flag
):
    spec = sweep.RunSpec(
        "run-stop-5m-high", "AKEUSDT",
        {"total_notional": 1000, "stop_5m_high": stop_5m_high},
    )

    arguments = sweep._run_arguments(spec, {
        "start": "2026-07-01T00:00:00+00:00",
        "end": "2026-07-02T00:00:00+00:00",
        "duckdb_path": "history.duckdb",
    }, tmp_path)

    assert (expected_flag in arguments) is (expected_flag is not None)
    if expected_flag is None:
        assert "--no-stop-5m-high" not in arguments


def test_expand_specs_accepts_stop_5m_high_matrix_values(tmp_path: Path):
    specs = sweep.expand_specs({
        "start": "2026-07-01T00:00:00+00:00",
        "end": "2026-07-02T00:00:00+00:00",
        "duckdb_path": "history.duckdb",
        "fixed": {"total_notional": 1000},
        "matrix": {"stop_5m_high": [False, True]},
    }, ["AKEUSDT"])

    arguments = {
        spec.params["stop_5m_high"]: sweep._run_arguments(spec, {
            "start": "2026-07-01T00:00:00+00:00",
            "end": "2026-07-02T00:00:00+00:00",
            "duckdb_path": "history.duckdb",
        }, tmp_path)
        for spec in specs
    }

    assert set(arguments) == {False, True}
    assert "--no-stop-5m-high" in arguments[False]
    assert "--no-stop-5m-high" not in arguments[True]


def test_sweep_does_not_abort_on_high_memory_estimate(
    tmp_path: Path, monkeypatch
):
    class RecordingDashboard:
        def __init__(self, **kwargs):
            self.closed = []

        def start(self, **kwargs):
            pass

        def close(self, **kwargs):
            self.closed.append(kwargs)

        def error(self, message):
            pass

    class FakeFuture:
        def __init__(self, result):
            self._result = result

        def result(self):
            return self._result

        def cancel(self):
            pass

    class FakePool:
        def __init__(self, **kwargs):
            pass

        def submit(self, function, *args):
            return FakeFuture(function(*args))

        def shutdown(self, **kwargs):
            pass

    config_path = tmp_path / "sweep.toml"
    config_path.write_text("name = 'test'", encoding="utf-8")
    spec = sweep.RunSpec("run-memory", "AKEUSDT", {"total_notional": 1000})
    config = {
        "name": "test",
        "output": str(tmp_path / "output"),
        "duckdb_path": "history.duckdb",
        "start": "2026-07-01T00:00:00+00:00",
        "end": "2026-07-02T00:00:00+00:00",
        "execution": {"duckdb_threads": 1, "worker_memory_budget": "2GB"},
    }
    seen_config = {}
    monkeypatch.setattr(sweep.tomllib, "loads", lambda _: config)
    monkeypatch.setattr(
        sweep, "resolve_universe", lambda _: (["AKEUSDT"], [{
            "symbol": "AKEUSDT", "selected": True,
            "effective_start": config["start"],
        }])
    )
    monkeypatch.setattr(sweep, "expand_specs", lambda *_: [spec])
    monkeypatch.setattr(sweep, "archive_root_from_catalog", lambda _: tmp_path)
    monkeypatch.setattr(
        sweep, "_symbol_worker_resources", lambda *args: (1, "2GB", "1024MB")
    )
    monkeypatch.setattr(
        sweep, "_estimate_monthly_memory",
        lambda *args, **kwargs: pd.DataFrame({"estimated_stream_peak_gb": [3.0]}),
    )

    def fake_run_symbol(specs, run_config, *_args):
        seen_config.update(run_config)
        return ([sweep._summary_row(specs[0], {"positions": {}, "pnl": {}}, "ok")], 0.0)

    monkeypatch.setattr(sweep, "_run_symbol", fake_run_symbol)
    monkeypatch.setattr(sweep, "TaskDashboard", RecordingDashboard)
    monkeypatch.setattr(sweep, "ThreadPoolExecutor", FakePool)
    monkeypatch.setattr(sweep, "as_completed", lambda futures: list(futures))

    assert sweep._main(["--config", str(config_path)]) == 0
    assert seen_config["execution"]["duckdb_memory_limit"] == "1024MB"


def test_simultaneous_signal_groups_require_multiple_symbols():
    signals = pd.DataFrame([
        {"symbol": "AKEUSDT", "parameters": "p", "event_time": 1_000},
        {"symbol": "BTCUSDT", "parameters": "p", "event_time": 1_500},
        {"symbol": "AKEUSDT", "parameters": "p", "event_time": 20_000},
    ])

    groups = _find_simultaneous_signals(signals, tolerance_ms=1_000)

    assert len(groups) == 1
    assert groups.iloc[0]["signal_count"] == 2


def test_collect_signal_audit_events_keeps_triggered_and_rejected(tmp_path: Path):
    spec = sweep.RunSpec("run-1", "AKEUSDT", {"total_notional": 1000})
    run_root = tmp_path / "runs" / spec.run_id
    run_root.mkdir(parents=True)
    pd.DataFrame([
        {
            "event_time": 1_000,
            "event_type": "signal_triggered",
            "symbol": "AKEUSDT",
            "strategy_id": "spike_short",
            "campaign_id": "spike_short:AKEUSDT:1000",
            "details": '{"rise_5s":"0.06"}',
        },
        {
            "event_time": 2_000,
            "event_type": "signal_rejected",
            "symbol": "AKEUSDT",
            "strategy_id": "spike_short",
            "campaign_id": "spike_short:AKEUSDT:2000",
            "details": '{"rejection_reasons":["max_rise_5s"]}',
        },
        {
            "event_time": 3_000,
            "event_type": "entry_plan_created",
            "symbol": "AKEUSDT",
            "strategy_id": "spike_short",
            "campaign_id": "spike_short:AKEUSDT:1000",
            "details": '{}',
        },
    ]).to_parquet(run_root / "audit_events.parquet", index=False)

    signals = _collect_signal_audit_events(tmp_path, [spec])

    assert signals["event_type"].tolist() == ["signal_triggered", "signal_rejected"]
    assert signals["run_id"].tolist() == [spec.run_id, spec.run_id]
    assert signals["parameters"].tolist() == [
        '{"total_notional": 1000}',
        '{"total_notional": 1000}',
    ]


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
