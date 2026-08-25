import json
from pathlib import Path
import sys

from trading_platform.backtest import benchmark
from trading_platform.backtest.benchmark import (
    CommandRun,
    _reproducibility_metadata,
    _events_per_second,
    _read_event_count,
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
