import json
import os
from pathlib import Path
import sys

import pytest

from trading_platform.backtest import benchmark
from trading_platform.backtest import run_spike_sweep_symbol as symbol_runner
from trading_platform.backtest.benchmark import (
    CommandRun,
    BenchmarkError,
    _reproducibility_metadata,
    _events_per_second,
    _read_event_count,
    _sweep_factory,
    choose_symbol,
    measure_command,
    run_cli,
    run_repeated,
    summarize_measurements,
)


def test_event_count_requires_all_run_meta_files(tmp_path: Path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps({"total_events": 7}), encoding="utf-8")
    second.write_text(json.dumps({"total_events": 11}), encoding="utf-8")

    assert _read_event_count([first, second]) == 18
    second.unlink()
    assert _read_event_count([first, second]) is None
    assert _read_event_count([]) is None


def test_events_per_second_does_not_invent_value_for_missing_or_zero_wall():
    assert _events_per_second(None, 1.0) is None
    assert _events_per_second(10, 0.0) is None
    assert _events_per_second(10, 2.0) == 5.0


def test_measure_command_reports_nonzero_exit_without_raising(tmp_path: Path):
    measurement = measure_command(
        [sys.executable, "-c", "raise SystemExit(7)"],
        workload={"kind": "failure-fixture"},
        artifact_root=tmp_path / "failure-artifacts",
    )

    assert measurement.exit_code == 7
    assert measurement.wall_seconds >= 0
    assert measurement.events is None
    assert measurement.events_per_second is None
    assert measurement.market_events is None
    assert measurement.strategy_events is None
    assert measurement.stdout_path is not None
    assert measurement.stderr_path is not None
    assert measurement.stdout_path.is_file()
    assert measurement.stderr_path.is_file()


def test_measure_command_without_artifact_root_does_not_return_dead_paths():
    measurement = measure_command(
        [sys.executable, "-c", "pass"],
        workload={"kind": "temporary-fixture"},
    )

    assert measurement.exit_code == 0
    assert measurement.stdout_path is None
    assert measurement.stderr_path is None
    assert measurement.measurement_path is None


def test_successful_single_command_without_run_meta_is_invalid(tmp_path: Path):
    measurement = measure_command(
        [sys.executable, "-c", "pass"],
        workload={"kind": "missing-meta-fixture"},
        event_paths=(tmp_path / "missing-run-meta.json",),
        event_mode="single",
        artifact_root=tmp_path / "artifacts",
    )

    assert measurement.exit_code == 0
    assert measurement.valid is False
    assert "run_meta.total_events" in measurement.invalid_reason
    report = summarize_measurements([measurement], workload={"kind": "missing-meta-fixture"})
    assert report["status"] == "invalid"
    assert report["successful_count"] == 0
    assert report["failed_count"] == 0
    assert report["invalid_count"] == 1


def test_measure_command_timeout_does_not_read_old_event_meta(tmp_path: Path):
    meta = tmp_path / "run_meta.json"
    meta.write_text(json.dumps({"total_events": 999}), encoding="utf-8")
    measurement = measure_command(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        workload={"kind": "timeout-fixture"},
        event_paths=(meta,),
        event_mode="single",
        artifact_root=tmp_path / "artifacts",
        timeout_seconds=0.05,
    )

    assert measurement.timed_out is True
    assert measurement.exit_code == 124
    assert measurement.strategy_events is None
    assert measurement.market_events is None
    assert measurement.stderr_path is not None
    assert measurement.stderr_path.is_file()


def test_repeated_report_contains_median_and_run_meta_events(tmp_path: Path):
    script = (
        "import json,sys; "
        "json.dump({'total_events': int(sys.argv[1])}, open(sys.argv[2], 'w'))"
    )

    def factory(repeat: int, run_root: Path) -> CommandRun:
        meta = run_root / "run_meta.json"
        return CommandRun(
            command=(sys.executable, "-c", script, str(repeat + 1), str(meta)),
            event_paths=(meta,),
            event_mode="single",
        )

    report = run_repeated(
        factory,
        workload={"kind": "calculation-fixture"},
        repeats=3,
        output_root=tmp_path / "artifacts",
    )

    assert report["repeats"] == 3
    assert report["exit_code"] == 0
    assert report["events"] == 2
    assert report["market_events"] == 2
    assert report["strategy_events"] == 2
    assert report["events_per_second"] is not None
    assert len(report["runs"]) == 3
    assert all(row["exit_code"] == 0 for row in report["runs"])
    assert report["successful_repeats"] == [1, 2, 3]
    assert report["failed_repeats"] == []
    assert report["phases"] is None
    assert "未对现有入口内部" in report["phase_note"]


def test_summary_uses_common_exit_code_and_none_for_mixed_exit_codes():
    success = measure_command(
        [sys.executable, "-c", "pass"], workload="success"
    )
    failed = measure_command(
        [sys.executable, "-c", "raise SystemExit(3)"], workload="failure"
    )
    report = summarize_measurements([success, failed], workload="mixed")

    assert report["exit_code"] is None
    assert report["exit_codes"] == [0, 3]
    assert report["events"] is None


def test_mixed_repeats_use_only_successful_denominator_and_clean_old_output(
    tmp_path: Path,
):
    root = tmp_path / "artifacts"
    old = root / "repeat-002" / "run_meta.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"total_events": 999}), encoding="utf-8")

    script = (
        "import json,sys; "
        "(json.dump({'total_events': int(sys.argv[2])}, open(sys.argv[1], 'w')) "
        "if sys.argv[3] == 'ok' else sys.exit(7))"
    )

    def factory(repeat: int, run_root: Path) -> CommandRun:
        meta = run_root / "run_meta.json"
        status = "fail" if repeat == 1 else "ok"
        return CommandRun(
            command=(sys.executable, "-c", script, str(meta), str(repeat + 1), status),
            event_paths=(meta,),
            event_mode="single",
        )

    report = run_repeated(
        factory,
        workload={"kind": "mixed-fixture"},
        repeats=3,
        output_root=root,
    )

    assert report["status"] == "failed"
    assert report["successful_repeats"] == [1, 3]
    assert report["failed_repeats"] == [2]
    assert report["successful_count"] == 2
    assert report["failed_count"] == 1
    assert report["strategy_events"] == 2
    failed_row = report["runs"][1]
    assert failed_row["strategy_events"] is None
    assert Path(failed_row["stderr_path"]).is_file()


def test_sweep_event_split_uses_shared_market_count_and_summed_strategy_count(
    tmp_path: Path,
):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    script = (
        "import json,sys; "
        "json.dump({'total_events': 7}, open(sys.argv[1], 'w')); "
        "json.dump({'total_events': 11}, open(sys.argv[2], 'w')); "
        "print('行情读取完成：共享事件=23，参数实例=2，失败=0', flush=True)"
    )
    measurement = measure_command(
        [sys.executable, "-c", script, str(first), str(second)],
        workload={"kind": "sweep-fixture"},
        event_paths=(first, second),
        event_mode="shared",
        artifact_root=tmp_path / "artifacts",
    )

    assert measurement.exit_code == 0
    assert measurement.market_events == 23
    assert measurement.strategy_events == 18
    assert measurement.market_events_per_second is not None
    assert measurement.strategy_events_per_second is not None


def test_successful_sweep_without_shared_event_log_is_invalid(tmp_path: Path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    script = (
        "import json,sys; "
        "json.dump({'total_events': 7}, open(sys.argv[1], 'w')); "
        "json.dump({'total_events': 11}, open(sys.argv[2], 'w')); "
        "print('行情读取完成，但日志字段缺失', flush=True)"
    )
    measurement = measure_command(
        [sys.executable, "-c", script, str(first), str(second)],
        workload={"kind": "invalid-sweep-fixture"},
        event_paths=(first, second),
        event_mode="shared",
        artifact_root=tmp_path / "artifacts",
    )

    assert measurement.exit_code == 0
    assert measurement.strategy_events == 18
    assert measurement.market_events is None
    assert measurement.valid is False
    assert "共享事件" in measurement.invalid_reason


def test_cli_single_missing_run_meta_is_nonzero(tmp_path: Path):
    code, payload = run_cli([
        "--workload", "command",
        "--repeats", "1",
        "--output-root", str(tmp_path / "cli"),
        "--events-from", str(tmp_path / "missing-run-meta.json"),
        "--command", sys.executable, "-c", "pass",
    ])

    assert code == 1
    assert payload is not None
    assert payload["status"] == "invalid"
    assert payload["cli_exit_code"] == 1
    assert payload["failed_count"] == 0
    assert payload["invalid_count"] == 1


def test_choose_symbol_requires_full_interval_coverage(tmp_path: Path, monkeypatch):
    import pandas as pd
    from trading_platform.backtest import benchmark

    frame = pd.DataFrame([
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "first_open_ms": 0,
            "last_close_ms": 99 if symbol == "PARTIAL" else 199,
        }
        for symbol in ("PARTIAL", "COMPLETE")
        for timeframe in ("1s", "1m", "5m")
    ])
    gapped = pd.DataFrame([
        {
            "symbol": "GAPPED",
            "timeframe": timeframe,
            "first_open_ms": first,
            "last_close_ms": last,
        }
        for timeframe in ("1s", "1m", "5m")
        for first, last in ((0, 99), (200, 299))
    ])
    frame = pd.concat([frame, gapped], ignore_index=True)
    monkeypatch.setattr(benchmark, "load_archive_index", lambda _path: frame)

    assert choose_symbol(tmp_path / "index.parquet", start_ms=0, end_ms=200) == "COMPLETE"


def test_choose_symbol_rejects_middle_partition_gap(tmp_path: Path, monkeypatch):
    import pandas as pd

    frame = pd.DataFrame([
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "first_open_ms": first,
            "last_close_ms": last,
        }
        for symbol, ranges in (
            ("GAPPED", ((0, 99), (200, 299))),
            ("CONTIGUOUS", ((0, 99), (100, 299))),
        )
        for timeframe in ("1s", "1m", "5m")
        for first, last in ranges
    ])
    monkeypatch.setattr(benchmark, "load_archive_index", lambda _path: frame)

    assert choose_symbol(tmp_path / "index.parquet", start_ms=0, end_ms=300) == "CONTIGUOUS"


def _benchmark_archive_frame(
    *,
    symbol: str,
    timeframes: tuple[str, ...],
    first_open_ms: int,
    last_close_ms: int,
):
    import pandas as pd

    return pd.DataFrame([
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "first_open_ms": first_open_ms,
            "last_close_ms": last_close_ms,
        }
        for timeframe in timeframes
    ])


def test_prepare_args_auto_symbol_checks_strategy_declared_timeframes(
    tmp_path: Path, monkeypatch
):
    start_ms = benchmark._iso_to_ms("2026-07-08T00:00:00+00:00")
    end_ms = benchmark._iso_to_ms("2026-07-09T00:00:00+00:00")
    frame = _benchmark_archive_frame(
        symbol="V2ONLY",
        timeframes=("1s", "1m", "5m"),
        first_open_ms=start_ms - 168 * 3_600_000,
        last_close_ms=end_ms - 1,
    )
    duckdb_path = tmp_path / "candles.duckdb"
    archive_index = tmp_path / "archive_index.parquet"
    monkeypatch.setattr(benchmark, "discover_duckdb_path", lambda *_args: duckdb_path)
    monkeypatch.setattr(
        benchmark, "discover_archive_index", lambda *_args: archive_index
    )
    monkeypatch.setattr(benchmark, "load_archive_index", lambda _path: frame)
    args = benchmark._build_parser().parse_args([
        "--workload", "single",
        "--strategy", "trading_platform.strategies.spike.v2:V2",
        "--start", "2026-07-08T00:00:00+00:00",
        "--end", "2026-07-09T00:00:00+00:00",
    ])

    with pytest.raises(BenchmarkError, match="15m"):
        benchmark._prepare_args(args)


def test_prepare_args_explicit_symbol_checks_effective_strategy_warmup(
    tmp_path: Path, monkeypatch
):
    start_ms = benchmark._iso_to_ms("2026-07-08T00:00:00+00:00")
    end_ms = benchmark._iso_to_ms("2026-07-09T00:00:00+00:00")
    frame = _benchmark_archive_frame(
        symbol="V2ONLY",
        timeframes=("1s", "1m", "5m", "15m"),
        first_open_ms=start_ms - 16 * 3_600_000,
        last_close_ms=end_ms - 1,
    )
    duckdb_path = tmp_path / "candles.duckdb"
    archive_index = tmp_path / "archive_index.parquet"
    monkeypatch.setattr(benchmark, "discover_duckdb_path", lambda *_args: duckdb_path)
    monkeypatch.setattr(
        benchmark, "discover_archive_index", lambda *_args: archive_index
    )
    monkeypatch.setattr(benchmark, "load_archive_index", lambda _path: frame)
    args = benchmark._build_parser().parse_args([
        "--workload", "single",
        "--symbol", "V2ONLY",
        "--strategy", "trading_platform.strategies.spike.v2:V2",
        "--start", "2026-07-08T00:00:00+00:00",
        "--end", "2026-07-09T00:00:00+00:00",
    ])

    with pytest.raises(BenchmarkError, match="coverage"):
        benchmark._prepare_args(args)


def test_cli_mixed_failure_is_nonzero_and_writes_report(tmp_path: Path):
    code, payload = run_cli([
        "--workload", "command",
        "--repeats", "1",
        "--output-root", str(tmp_path / "cli"),
        "--command", sys.executable, "-c", "raise SystemExit(9)",
    ])

    assert code == 1
    assert payload is not None
    assert payload["cli_exit_code"] == 1
    assert payload["failed_count"] == 1
    assert (tmp_path / "cli" / "benchmark.json").is_file()


def test_reproducibility_metadata_contains_fingerprints_and_runtime(tmp_path: Path):
    index = tmp_path / "archive_index.parquet"
    index.write_bytes(b"stable-index")
    workload = {
        "kind": "metadata-fixture",
        "archive_index": str(index),
        "repeats": 3,
        "sweep_values": ["0", "4"],
    }

    metadata = _reproducibility_metadata(workload, archive_index=index)

    if metadata["git_available"]:
        assert metadata["git_revision"]
        assert isinstance(metadata["git_dirty"], bool)
    else:
        assert metadata["git_revision"] is None
        assert metadata["git_dirty"] is None
    assert len(metadata["source_content_sha256"]) == 64
    assert metadata["archive_index"]["content_sha256"]
    assert metadata["python"]["version"]
    assert metadata["platform"]["cpu_count"]
    assert metadata["workload_parameters"] == workload


def test_reproducibility_metadata_contains_metrics_and_dependency_fingerprints(
    tmp_path: Path,
):
    index = tmp_path / "archive_index.parquet"
    index.write_bytes(b"stable-index")
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    (metrics_root / "metrics_index.parquet").write_bytes(b"metrics-index-v1")
    (metrics_root / "metrics_index.meta.json").write_text(
        json.dumps({"completed": True}), encoding="utf-8"
    )
    workload = {
        "kind": "metadata-fixture",
        "archive_index": str(index),
        "metrics_root": str(metrics_root),
    }

    first = _reproducibility_metadata(workload, archive_index=index)
    (metrics_root / "metrics_index.parquet").write_bytes(b"metrics-index-v2")
    second = _reproducibility_metadata(workload, archive_index=index)

    assert first["metrics_root"]["path"] == str(metrics_root)
    assert first["metrics_root"]["status"] == "present"
    assert first["metrics_root"]["files"]["index"]["sha256"]
    assert first["metrics_root"]["combined_sha256"]
    assert (
        first["metrics_root"]["combined_sha256"]
        != second["metrics_root"]["combined_sha256"]
    )
    assert first["dependency_lock"]["path"].endswith("uv.lock")
    assert len(first["dependency_lock"]["content_sha256"]) == 64


def test_metrics_root_fingerprint_only_reads_canonical_files(
    tmp_path: Path, monkeypatch
):
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    index_path = metrics_root / "metrics_index.parquet"
    meta_path = metrics_root / "metrics_index.meta.json"
    index_path.write_bytes(b"metrics-index-v1")
    meta_path.write_bytes(b"metrics-meta-v1")

    monkeypatch.setattr(
        Path,
        "rglob",
        lambda *_args, **_kwargs: pytest.fail("metrics fingerprint must not scan partitions"),
    )
    first = benchmark._metrics_root_fingerprint(metrics_root)

    index_stat = index_path.stat()
    index_path.write_bytes(b"metrics-index-v2")
    os.utime(index_path, ns=(index_stat.st_atime_ns, index_stat.st_mtime_ns))
    second = benchmark._metrics_root_fingerprint(metrics_root)

    assert first["status"] == "present"
    assert first["files"]["index"]["size"] == second["files"]["index"]["size"]
    assert first["files"]["index"]["mtime_ns"] == second["files"]["index"]["mtime_ns"]
    assert first["files"]["index"]["sha256"] != second["files"]["index"]["sha256"]
    assert first["combined_sha256"] != second["combined_sha256"]


def test_metrics_root_fingerprint_marks_missing_canonical_file(tmp_path: Path):
    metrics_root = tmp_path / "metrics"
    metrics_root.mkdir()
    (metrics_root / "metrics_index.meta.json").write_text("{}", encoding="utf-8")

    fingerprint = benchmark._metrics_root_fingerprint(metrics_root)

    assert fingerprint["status"] == "missing"
    assert fingerprint["files"]["index"]["exists"] is False


def test_reproducibility_metadata_marks_git_unavailable(tmp_path: Path, monkeypatch):
    index = tmp_path / "archive_index.parquet"
    index.write_bytes(b"stable-index")
    monkeypatch.setattr(benchmark, "_git_value", lambda *_arguments: None)

    metadata = _reproducibility_metadata(
        {"kind": "no-git-fixture"}, archive_index=index
    )

    assert metadata["git_available"] is False
    assert metadata["git_revision"] is None
    assert metadata["git_dirty"] is None


def test_symbol_sweep_forwards_numeric_parameter_and_allows_one_value(
    tmp_path: Path,
):
    args = benchmark._build_parser().parse_args([
        "--workload", "symbol-sweep",
        "--symbol", "AKEUSDT",
        "--strategy", "trading_platform.strategies.spike.v1_1:V11",
        "--sweep-parameter", "rise_5s_threshold_percent",
        "--sweep-values", "5",
    ])

    factory = _sweep_factory(
        args,
        {"kind": "symbol-sweep"},
        "test-invocation",
    )
    spec = factory(0, tmp_path)
    task = json.loads((tmp_path / "task.json").read_text(encoding="utf-8"))

    assert len(task["runs"]) == 1
    run_arguments = task["runs"][0]["arguments"]
    assert "--rise-5s-threshold-percent" in run_arguments
    assert run_arguments[run_arguments.index("--rise-5s-threshold-percent") + 1] == "5"
    assert "--prior-high-lookback-hours" not in run_arguments
    assert "trading_platform.strategies.spike.v1_1:V11" in run_arguments
    assert spec.event_mode == "shared"


def test_symbol_sweep_forwards_market_slippage_and_records_workload_identity(
    tmp_path: Path,
):
    args = benchmark._build_parser().parse_args([
        "--workload", "symbol-sweep",
        "--symbol", "AKEUSDT",
        "--market-slippage-bps", "25",
        "--strategy", "trading_platform.strategies.spike.v1:V1",
        "--sweep-parameter", "market_slippage_bps",
        "--sweep-values", "25,50",
    ])

    factory = _sweep_factory(args, {"kind": "symbol-sweep"}, "test-invocation")
    factory(0, tmp_path)
    task = json.loads((tmp_path / "task.json").read_text(encoding="utf-8"))

    assert [
        run["arguments"][run["arguments"].index("--market-slippage-bps") + 1]
        for run in task["runs"]
    ] == ["25", "50"]


def test_benchmark_normalizes_negative_zero_slippage_in_workload_identity(
    tmp_path: Path, monkeypatch
):
    start_ms = benchmark._iso_to_ms("2026-07-01T00:00:00+00:00")
    end_ms = benchmark._iso_to_ms("2026-07-02T00:00:00+00:00")
    frame = _benchmark_archive_frame(
        symbol="V1ONLY",
        timeframes=("1s", "1m", "5m", "15m"),
        first_open_ms=start_ms - 16 * 3_600_000,
        last_close_ms=end_ms - 1,
    )
    monkeypatch.setattr(
        benchmark, "discover_duckdb_path", lambda *_args: tmp_path / "candles.duckdb"
    )
    monkeypatch.setattr(
        benchmark, "discover_archive_index", lambda *_args: tmp_path / "index.parquet"
    )
    monkeypatch.setattr(benchmark, "load_archive_index", lambda _path: frame)
    monkeypatch.setattr(
        benchmark,
        "_reproducibility_metadata",
        lambda workload, **_kwargs: {"workload_parameters": workload},
    )
    seen: list[dict[str, object]] = []

    def fake_run_repeated(_factory, *, workload, **_kwargs):
        seen.append(workload)
        return {"failed_count": 0, "invalid_count": 0}

    monkeypatch.setattr(benchmark, "run_repeated", fake_run_repeated)

    for value in ("-0.0", "0.0"):
        code, _payload = run_cli([
            "--workload", "single",
            "--symbol", "V1ONLY",
            "--market-slippage-bps", value,
            "--output-root", str(tmp_path / "same-output"),
        ])
        assert code == 0

    assert seen[0] == seen[1]
    assert seen[0]["market_slippage_bps"] == 0.0
    assert str(seen[0]["market_slippage_bps"]) == "0.0"


def test_symbol_sweep_normalizes_negative_zero_across_reproducibility_inputs(
    tmp_path: Path, monkeypatch
):
    start_ms = benchmark._iso_to_ms("2026-07-01T00:00:00+00:00")
    end_ms = benchmark._iso_to_ms("2026-07-02T00:00:00+00:00")
    frame = _benchmark_archive_frame(
        symbol="V1ONLY",
        timeframes=("1s", "1m", "5m", "15m"),
        first_open_ms=start_ms - 16 * 3_600_000,
        last_close_ms=end_ms - 1,
    )
    monkeypatch.setattr(
        benchmark, "discover_duckdb_path", lambda *_args: tmp_path / "candles.duckdb"
    )
    monkeypatch.setattr(
        benchmark,
        "discover_archive_index",
        lambda *_args: tmp_path / "index.parquet",
    )
    monkeypatch.setattr(benchmark, "load_archive_index", lambda _path: frame)
    monkeypatch.setattr(
        benchmark,
        "_reproducibility_metadata",
        lambda workload, **_kwargs: {"workload_parameters": workload},
    )
    seen: list[dict[str, object]] = []

    def fake_run_repeated(factory, *, workload, metadata, **_kwargs):
        run_root = tmp_path / f"run-{len(seen)}"
        run_root.mkdir()
        factory(0, run_root)
        task = json.loads((run_root / "task.json").read_text(encoding="utf-8"))
        arguments = task["runs"][0]["arguments"]
        seen.append({
            "workload": workload,
            "metadata": metadata,
            "command_slippage": arguments[
                arguments.index("--market-slippage-bps") + 1
            ],
        })
        return {"failed_count": 0, "invalid_count": 0}

    monkeypatch.setattr(benchmark, "run_repeated", fake_run_repeated)

    for value in ("-0.0", "0.0"):
        code, _payload = run_cli([
            "--workload", "symbol-sweep",
            "--symbol", "V1ONLY",
            "--sweep-parameter", "market_slippage_bps",
            f"--sweep-values={value}",
            "--output-root", str(tmp_path / "same-output"),
        ])
        assert code == 0

    assert seen[0] == seen[1]
    workload = seen[0]["workload"]
    assert isinstance(workload, dict)
    assert workload["sweep_values"] == ["0.0"]
    assert workload["effective_settings"][0]["market_slippage_bps"] == 0.0
    metadata = seen[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["workload_parameters"] == workload
    assert seen[0]["command_slippage"] == "0.0"


def test_normalized_sweep_values_do_not_reuse_stale_namespace_state():
    args = benchmark._build_parser().parse_args([
        "--workload", "symbol-sweep",
        "--sweep-parameter", "market_slippage_bps",
        "--sweep-values=-0.0",
    ])

    assert benchmark._normalized_sweep_values(
        args.sweep_parameter, args.sweep_values
    ) == ("0.0",)
    args.sweep_values = "25"
    assert benchmark._normalized_sweep_values(
        args.sweep_parameter, args.sweep_values
    ) == ("25",)


def test_symbol_sweep_keeps_prior_high_parameter(tmp_path: Path):
    args = benchmark._build_parser().parse_args([
        "--workload", "symbol-sweep",
        "--symbol", "AKEUSDT",
        "--strategy", "trading_platform.strategies.spike.v1:V1",
        "--sweep-parameter", "prior_high_lookback_hours",
        "--sweep-values", "4",
    ])

    factory = _sweep_factory(args, {"kind": "symbol-sweep"}, "test-invocation")
    factory(0, tmp_path)
    task = json.loads((tmp_path / "task.json").read_text(encoding="utf-8"))
    run_arguments = task["runs"][0]["arguments"]

    assert run_arguments[
        run_arguments.index("--prior-high-lookback-hours") + 1
    ] == "4"


def test_v2_sweep_forwards_profit_unlock_parameter(tmp_path: Path):
    args = benchmark._build_parser().parse_args([
        "--workload", "symbol-sweep",
        "--symbol", "AKEUSDT",
        "--strategy", "trading_platform.strategies.spike.v2:V2",
        "--exit-policy", "candidate-v1",
        "--sweep-parameter", "profit_unlock_percent",
        "--sweep-values", "1.5,3",
    ])

    factory = _sweep_factory(args, {"kind": "symbol-sweep"}, "test-invocation")
    factory(0, tmp_path)
    task = json.loads((tmp_path / "task.json").read_text(encoding="utf-8"))

    assert [
        run["arguments"][run["arguments"].index("--profit-unlock-percent") + 1]
        for run in task["runs"]
    ] == ["1.5", "3"]
    assert all(
        run["arguments"][run["arguments"].index("--exit-policy") + 1]
        == "candidate-v1"
        for run in task["runs"]
    )


def test_benchmark_leaves_strategy_exit_policy_default_unset(tmp_path: Path):
    args = benchmark._build_parser().parse_args([
        "--workload", "symbol-sweep",
        "--symbol", "AKEUSDT",
        "--strategy", "trading_platform.strategies.spike.v2:V2",
        "--sweep-parameter", "prior_high_lookback_hours",
        "--sweep-values", "4",
    ])

    assert args.exit_policy is None
    factory = _sweep_factory(args, {"kind": "symbol-sweep"}, "test-invocation")
    factory(0, tmp_path)
    task = json.loads((tmp_path / "task.json").read_text(encoding="utf-8"))

    assert all("--exit-policy" not in run["arguments"] for run in task["runs"])


def test_benchmark_workload_records_only_explicit_exit_policy(
    tmp_path: Path, monkeypatch
):
    start_ms = benchmark._iso_to_ms("2026-07-01T00:00:00+00:00")
    end_ms = benchmark._iso_to_ms("2026-07-02T00:00:00+00:00")
    frame = _benchmark_archive_frame(
        symbol="V1ONLY",
        timeframes=("1s", "1m", "5m", "15m"),
        first_open_ms=start_ms - 16 * 3_600_000,
        last_close_ms=end_ms - 1,
    )
    monkeypatch.setattr(
        benchmark, "discover_duckdb_path", lambda *_args: tmp_path / "candles.duckdb"
    )
    monkeypatch.setattr(
        benchmark, "discover_archive_index", lambda *_args: tmp_path / "index.parquet"
    )
    monkeypatch.setattr(benchmark, "load_archive_index", lambda _path: frame)
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        benchmark,
        "_reproducibility_metadata",
        lambda workload, **_kwargs: {"workload_parameters": workload},
    )

    def fake_run_repeated(_factory, *, workload, **_kwargs):
        seen.append(workload)
        return {"failed_count": 0, "invalid_count": 0}

    monkeypatch.setattr(benchmark, "run_repeated", fake_run_repeated)
    code, _payload = run_cli([
        "--workload", "single",
        "--symbol", "V1ONLY",
        "--output-root", str(tmp_path / "default"),
    ])
    assert code == 0
    assert "exit_policy" not in seen[-1]

    code, _payload = run_cli([
        "--workload", "single",
        "--symbol", "V1ONLY",
        "--exit-policy", "candidate-v1",
        "--output-root", str(tmp_path / "explicit"),
    ])
    assert code == 0
    assert seen[-1]["exit_policy"] == "candidate-v1"


def test_run_cli_preflights_v2_and_records_effective_workload(
    tmp_path: Path, monkeypatch
):
    start_ms = benchmark._iso_to_ms("2026-07-08T00:00:00+00:00")
    end_ms = benchmark._iso_to_ms("2026-07-09T00:00:00+00:00")
    frame = _benchmark_archive_frame(
        symbol="V2ONLY",
        timeframes=("1s", "1m", "5m", "15m"),
        first_open_ms=start_ms - 168 * 3_600_000,
        last_close_ms=end_ms - 1,
    )
    monkeypatch.setattr(
        benchmark, "discover_duckdb_path", lambda *_args: tmp_path / "candles.duckdb"
    )
    monkeypatch.setattr(
        benchmark, "discover_archive_index", lambda *_args: tmp_path / "index.parquet"
    )
    monkeypatch.setattr(benchmark, "load_archive_index", lambda _path: frame)
    monkeypatch.setattr(
        benchmark,
        "_reproducibility_metadata",
        lambda workload, **_kwargs: {"workload_parameters": workload},
    )
    seen: list[dict[str, object]] = []

    def fake_run_repeated(_factory, *, workload, **_kwargs):
        seen.append(workload)
        return {"failed_count": 0, "invalid_count": 0}

    monkeypatch.setattr(benchmark, "run_repeated", fake_run_repeated)

    code, payload = run_cli([
        "--workload", "single",
        "--strategy", "trading_platform.strategies.spike.v2:V2",
        "--start", "2026-07-08T00:00:00+00:00",
        "--end", "2026-07-09T00:00:00+00:00",
        "--output-root", str(tmp_path / "output"),
    ])

    assert code == 0
    assert payload is not None
    workload = seen[-1]
    assert workload["symbol"] == "V2ONLY"
    assert workload["effective_exit_policy"] == "candidate-v1"
    assert workload["effective_load_start_ms"] == start_ms - 168 * 3_600_000
    assert workload["required_timeframes"] == ["1s", "1m", "5m", "15m"]
    assert workload["effective_settings"] == [{
        "load_start_ms": start_ms - 168 * 3_600_000,
        "required_timeframes": ["1s", "1m", "5m", "15m"],
        "exit_policy": "candidate-v1",
        "market_slippage_bps": 0.0,
    }]
    assert "exit_policy" not in workload


def test_prepare_args_required_metrics_checks_canonical_index_only(
    tmp_path: Path, monkeypatch
):
    start_ms = benchmark._iso_to_ms("2026-07-08T00:00:00+00:00")
    end_ms = benchmark._iso_to_ms("2026-07-09T00:00:00+00:00")
    frame = _benchmark_archive_frame(
        symbol="V11ONLY",
        timeframes=("1s", "1m", "5m", "15m"),
        first_open_ms=start_ms - 16 * 3_600_000,
        last_close_ms=end_ms - 1,
    )
    metrics_root = tmp_path / "metrics"
    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        benchmark, "discover_duckdb_path", lambda *_args: tmp_path / "candles.duckdb"
    )
    monkeypatch.setattr(
        benchmark, "discover_archive_index", lambda *_args: tmp_path / "index.parquet"
    )
    monkeypatch.setattr(benchmark, "load_archive_index", lambda _path: frame)

    def fake_load_metrics_index(root, *, verify_files=False):
        calls.append((Path(root), verify_files))
        return object()

    monkeypatch.setattr(
        "trading_platform.market.archive.metrics.load_metrics_index",
        fake_load_metrics_index,
    )
    args = benchmark._build_parser().parse_args([
        "--workload", "single",
        "--symbol", "V11ONLY",
        "--strategy", "trading_platform.strategies.spike.v1_1:V11",
        "--metrics-root", str(metrics_root),
        "--start", "2026-07-08T00:00:00+00:00",
        "--end", "2026-07-09T00:00:00+00:00",
    ])

    prepared = benchmark._prepare_args(args)

    assert prepared.symbol == "V11ONLY"
    assert calls == [(metrics_root, False)]


def test_prepare_args_rejects_blank_symbol_before_auto_selection(monkeypatch):
    args = benchmark._build_parser().parse_args([
        "--workload", "single",
        "--symbol", "   ",
    ])
    monkeypatch.setattr(
        benchmark,
        "choose_symbol",
        lambda *_args, **_kwargs: pytest.fail("blank symbol must not auto-select"),
    )

    with pytest.raises(BenchmarkError, match="must not be blank"):
        benchmark._prepare_args(args)


def test_single_value_sweep_reaches_symbol_runner_task_chain(
    tmp_path: Path, monkeypatch
):
    args = benchmark._build_parser().parse_args([
        "--workload", "symbol-sweep",
        "--symbol", "AKEUSDT",
        "--strategy", "trading_platform.strategies.spike.v1_1:V11",
        "--metrics-root", str(tmp_path / "metrics"),
        "--sweep-parameter", "rise_5s_threshold_percent",
        "--sweep-values", "5",
    ])
    factory = _sweep_factory(args, {"kind": "symbol-sweep"}, "test-invocation")
    factory(0, tmp_path)
    task = json.loads((tmp_path / "task.json").read_text(encoding="utf-8"))

    plans = []
    monkeypatch.setattr(
        symbol_runner,
        "load_metrics_series",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        symbol_runner,
        "create_spike_engine",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        symbol_runner,
        "_run_shift_group",
        lambda group: plans.append(group) or set(),
    )

    assert symbol_runner.run_symbol_task(task) == 0
    assert len(plans) == 1
    assert len(plans[0]) == 1
    assert plans[0][0].args.rise_5s_threshold_percent == 5
    assert plans[0][0].args.metrics_root == tmp_path / "metrics"


@pytest.mark.parametrize(
    "parameter",
    [
        "rise_5s_threshold",
        "rise_5s_threshold_percen",
        "entry_tier_mode",
        "chunk_hours",
        "stop_5m_high",
    ],
)
def test_sweep_rejects_unsupported_parameters_before_archive_lookup(
    parameter: str, tmp_path: Path, monkeypatch
):
    args = benchmark._build_parser().parse_args([
        "--workload", "symbol-sweep",
        "--strategy", "trading_platform.strategies.spike.v1:V1",
        "--sweep-parameter", parameter,
        "--sweep-values", "5",
    ])
    monkeypatch.setattr(
        benchmark,
        "discover_duckdb_path",
        lambda *_args: pytest.fail("archive lookup must not run"),
    )

    with pytest.raises(BenchmarkError, match="--sweep-parameter"):
        benchmark._prepare_args(args)


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("rise_5s_threshold_percent", "not-a-number"),
        ("prior_high_lookback_hours", "4.5"),
    ],
)
def test_sweep_rejects_non_numeric_values_before_archive_lookup(
    parameter: str, value: str, monkeypatch
):
    args = benchmark._build_parser().parse_args([
        "--workload", "symbol-sweep",
        "--strategy", "trading_platform.strategies.spike.v1:V1",
        "--sweep-parameter", parameter,
        "--sweep-values", value,
    ])
    monkeypatch.setattr(
        benchmark,
        "discover_duckdb_path",
        lambda *_args: pytest.fail("archive lookup must not run"),
    )

    with pytest.raises(BenchmarkError, match="--sweep-values"):
        benchmark._prepare_args(args)


def test_run_cli_rejects_invalid_strategy_before_archive_lookup(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        benchmark,
        "discover_duckdb_path",
        lambda *_args: pytest.fail("archive lookup must not run"),
    )

    with pytest.raises(SystemExit) as error:
        benchmark.run_cli([
            "--workload", "symbol-sweep",
            "--strategy", " : V11 ",
            "--sweep-parameter", "prior_high_lookback_hours",
        ])

    assert error.value.code == 2
    assert "module:attribute" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" trading_platform.strategies.spike.v1:V1 ",
         "trading_platform.strategies.spike.v1:V1"),
        ("trading_platform.strategies.spike.v1 : V1",
         "trading_platform.strategies.spike.v1:V1"),
    ],
)
def test_strategy_path_strips_outer_and_colon_whitespace(raw, expected):
    assert benchmark._validate_strategy_path(raw) == expected


@pytest.mark.parametrize("raw", ["v1_1", ":V1", "module:", " : "])
def test_strategy_path_rejects_empty_module_or_attribute(raw):
    with pytest.raises(BenchmarkError, match="module:attribute"):
        benchmark._validate_strategy_path(raw)
