"""可重复的回测性能基准工具。

这个模块只编排已有的回测 CLI，并在独立子进程中测量资源使用，不参与
策略、数据加载器、引擎或 sweep 的生产执行逻辑。默认 workload 使用
仓库中的只读 DuckDB 归档；``symbol-sweep`` workload 直接调用已有的
``run_spike_sweep_symbol`` worker，以免基准依赖 PostgreSQL universe。

示例::

    python -m trading_platform.backtest.benchmark \
        --workload single --repeats 3 \
        --duckdb-path data/market/candles/candles.duckdb \
        --symbol AKEUSDT --start 2026-07-01 --end 2026-07-02

    python -m trading_platform.backtest.benchmark \
        --workload symbol-sweep --repeats 3 \
        --duckdb-path data/market/candles/candles.duckdb \
        --symbol AKEUSDT --start 2026-07-01 --end 2026-07-02
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import resource
import signal
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Callable, Literal, Sequence, TypeAlias, TypedDict

from trading_platform.market.archive.index import load_archive_index


_MEASURE_ONCE = "--_measure-once"
_DEFAULT_START = "2026-07-01T00:00:00+00:00"
_DEFAULT_END = "2026-07-02T00:00:00+00:00"
_DEFAULT_STRATEGY = "trading_platform.strategies.spike.v1:V1"
_DEFAULT_SWEEP_VALUES = ("0", "4")
_REQUIRED_TIMEFRAMES = ("1s", "1m", "5m")

EventMode: TypeAlias = Literal["single", "shared", "none"]


class Workload(TypedDict, total=False):
    """JSON-compatible workload description recorded in the report."""

    kind: str
    symbol: str
    start: str
    end: str
    duckdb_path: str
    archive_index: str
    strategy: str
    total_notional: str
    warmup_hours: float
    exit_policy: str
    chunk_hours: float
    fetch_batch_size: int
    duckdb_memory_limit: str | None
    duckdb_threads: int
    sweep_parameter: str
    sweep_values: list[str]
    repeats: int
    timeout_seconds: float | None
    output_root: str
    report: str | None
    command: list[str]
    event_paths: list[str]
    event_mode: EventMode


class BenchmarkError(ValueError):
    """基准输入无效，或无法发现可用归档。"""


@dataclass(frozen=True)
class CommandRun:
    """一次 workload 执行所需的命令和可选事件元数据路径。"""

    command: tuple[str, ...]
    event_paths: tuple[Path, ...] = ()
    event_mode: EventMode = "none"


@dataclass(frozen=True)
class RunMeasurement:
    """一次子进程执行的资源和结果。"""

    command: tuple[str, ...]
    workload: Workload
    wall_seconds: float
    child_cpu_seconds: float | None
    max_rss_mb: float | None
    market_events: int | None
    strategy_events: int | None
    market_events_per_second: float | None
    strategy_events_per_second: float | None
    exit_code: int
    timed_out: bool = False
    valid: bool = True
    invalid_reason: str | None = None
    error: str | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    measurement_path: Path | None = None
    repeat: int | None = None

    @property
    def events(self) -> int | None:
        """向后兼容：events 表示策略处理事件数。"""

        return self.strategy_events

    @property
    def events_per_second(self) -> float | None:
        """向后兼容：吞吐表示策略处理事件数。"""

        return self.strategy_events_per_second

    def as_dict(self) -> dict[str, object]:
        return {
            "command": list(self.command),
            "workload": self.workload,
            "wall_seconds": self.wall_seconds,
            "child_cpu_seconds": self.child_cpu_seconds,
            "max_rss_mb": self.max_rss_mb,
            "market_events": self.market_events,
            "strategy_events": self.strategy_events,
            "market_events_per_second": self.market_events_per_second,
            "strategy_events_per_second": self.strategy_events_per_second,
            "events": self.events,
            "events_per_second": self.events_per_second,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "valid": self.valid,
            "invalid_reason": self.invalid_reason,
            "stdout_path": str(self.stdout_path) if self.stdout_path else None,
            "stderr_path": str(self.stderr_path) if self.stderr_path else None,
            "measurement_path": (
                str(self.measurement_path) if self.measurement_path else None
            ),
            "repeat": self.repeat,
            **({"error": self.error} if self.error else {}),
        }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _environment() -> dict[str, str]:
    """让从源码 checkout 启动的子进程也能导入 ``src`` 包。"""

    env = os.environ.copy()
    source_root = str(_project_root() / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        source_root if not existing else source_root + os.pathsep + existing
    )
    return env


def discover_duckdb_path(explicit: str | Path | None = None) -> Path:
    """发现仓库内可用的只读 candles DuckDB catalog。"""

    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    configured = os.environ.get("TRADING_PLATFORM_DUCKDB")
    if configured:
        candidates.append(Path(configured))
    root = _project_root()
    candidates.extend((
        root / "data/market/candles/candles.duckdb",
        root / "data/market/history.duckdb",
    ))
    for path in candidates:
        if path.is_file():
            return path.resolve()
    rendered = ", ".join(str(path) for path in candidates)
    raise BenchmarkError(f"没有找到 candles DuckDB 归档；已检查: {rendered}")


def discover_archive_index(
    duckdb_path: str | Path,
    explicit: str | Path | None = None,
) -> Path:
    """发现与 DuckDB 同根的 archive index。"""

    candidates = []
    if explicit is not None:
        candidates.append(Path(explicit))
    root = Path(duckdb_path).resolve().parent
    candidates.append(root / "archive_index.parquet")
    for path in candidates:
        if path.is_file():
            return path.resolve()
    rendered = ", ".join(str(path) for path in candidates)
    raise BenchmarkError(f"没有找到 archive index；已检查: {rendered}")


def choose_symbol(
    archive_index: str | Path,
    *,
    preferred: str | None = "AKEUSDT",
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> str:
    """从 index 选择覆盖所需 timeframe 的稳定 symbol，优先 AKEUSDT。"""

    frame = load_archive_index(Path(archive_index))
    if start_ms is None or end_ms is None or start_ms >= end_ms:
        raise BenchmarkError("choose_symbol requires a valid start_ms/end_ms interval")
    if start_ms is not None and end_ms is not None:
        frame = frame[
            (frame["first_open_ms"] < end_ms)
            & (frame["last_close_ms"] >= start_ms)
        ]

    def has_contiguous_coverage(parts) -> bool:
        if parts.empty:
            return False
        cursor = start_ms
        started = False
        for row in parts.sort_values("first_open_ms").itertuples(index=False):
            first_open_ms = int(row.first_open_ms)
            last_close_ms = int(row.last_close_ms)
            if last_close_ms < cursor:
                continue
            if first_open_ms > cursor:
                return False
            started = True
            cursor = max(cursor, last_close_ms + 1)
            if cursor >= end_ms:
                return True
        return started and cursor >= end_ms

    covered_symbols: list[str] = []
    for symbol, group in frame.groupby("symbol"):
        if not set(_REQUIRED_TIMEFRAMES).issubset(set(group["timeframe"])):
            continue
        complete = True
        for timeframe in _REQUIRED_TIMEFRAMES:
            parts = group[group["timeframe"] == timeframe]
            complete = complete and has_contiguous_coverage(parts)
        if complete:
            covered_symbols.append(str(symbol))
    symbols = sorted(covered_symbols)
    ordered = []
    if preferred:
        normalized = preferred.strip().upper()
        if normalized in symbols:
            ordered.append(normalized)
    ordered.extend(symbol for symbol in symbols if symbol not in ordered)
    for symbol in ordered:
        if symbol in covered_symbols:
            return symbol
    raise BenchmarkError(
        "archive index 中没有同时覆盖 "
        f"{', '.join(_REQUIRED_TIMEFRAMES)} 的可用 symbol"
    )


def _usage_rss_mb(value: int | float) -> float:
    # Linux ru_maxrss 是 KiB；macOS 返回 bytes。项目测试/部署环境为 Linux，
    # 仍保留 macOS 分支让该工具可在本地复现。
    if sys.platform == "darwin":
        return float(value) / (1024 * 1024)
    return float(value) / 1024


def _read_event_count(paths: Sequence[str | Path]) -> int | None:
    """从所有 run_meta 读取 total_events；缺任一文件则不返回部分总数。"""

    normalized = [Path(path) for path in paths]
    if not normalized:
        return None
    counts: list[int] = []
    for path in normalized:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        value = payload.get("total_events") if isinstance(payload, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if int(value) != value or value < 0:
            return None
        counts.append(int(value))
    return sum(counts)


def _read_shared_event_count(stdout_path: str | Path) -> int | None:
    """读取 sweep worker 的共享事件计数，而不是把它乘以参数实例数。"""

    try:
        text = Path(stdout_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    matches = re.findall(r"共享事件=(\d+)", text)
    if not matches:
        return None
    return int(matches[-1])


def _events_per_second(events: int | None, wall_seconds: float) -> float | None:
    if events is None or wall_seconds <= 0:
        return None
    return float(events) / wall_seconds


def _run_once_child(arguments: Sequence[str]) -> int:
    """内部 wrapper：在独立进程中获取目标命令的 RUSAGE_CHILDREN。"""

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--stdout-path", type=Path, required=True)
    parser.add_argument("--stderr-path", type=Path, required=True)
    parser.add_argument("--", dest="separator", nargs="?")
    parsed, command = parser.parse_known_args(list(arguments))
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parsed.result.write_text(
            json.dumps({"exit_code": 127, "error": "empty command"}),
            encoding="utf-8",
        )
        return 0

    started = time.perf_counter()
    exit_code = 127
    error: str | None = None
    try:
        parsed.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        parsed.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with parsed.stdout_path.open("w", encoding="utf-8") as stdout, parsed.stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            completed = subprocess.run(
                command,
                stdout=stdout,
                stderr=stderr,
                check=False,
                env=_environment(),
            )
            exit_code = int(completed.returncode)
    except OSError as exc:
        error = str(exc)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    payload = {
        "wall_seconds": time.perf_counter() - started,
        "child_cpu_seconds": float(usage.ru_utime + usage.ru_stime),
        "max_rss_mb": _usage_rss_mb(usage.ru_maxrss),
        "exit_code": exit_code,
    }
    if error:
        payload["error"] = error
    parsed.result.parent.mkdir(parents=True, exist_ok=True)
    parsed.result.write_text(
        json.dumps(payload, ensure_ascii=True), encoding="utf-8"
    )
    return 0


def measure_command(
    command: Sequence[str],
    *,
    workload: Workload,
    event_paths: Sequence[str | Path] = (),
    event_mode: EventMode = "none",
    artifact_root: str | Path | None = None,
    timeout_seconds: float | None = None,
    environment: dict[str, str] | None = None,
) -> RunMeasurement:
    """测量一次命令；目标命令失败时返回非零 ``exit_code`` 而非抛异常。"""

    normalized = tuple(str(part) for part in command)
    if not normalized:
        raise BenchmarkError("benchmark command must not be empty")
    temporary_root: tempfile.TemporaryDirectory[str] | None = None
    if artifact_root is None:
        temporary_root = tempfile.TemporaryDirectory(prefix="trading-platform-benchmark-")
        artifact_dir = Path(temporary_root.name)
    else:
        artifact_dir = Path(artifact_root).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)
    try:
        result_path = artifact_dir / "measurement.json"
        stdout_path = artifact_dir / "stdout.log"
        stderr_path = artifact_dir / "stderr.log"
        stdout_path.touch()
        stderr_path.touch()
        wrapper = [
            sys.executable,
            "-m",
            "trading_platform.backtest.benchmark",
            _MEASURE_ONCE,
            "--result",
            str(result_path),
            "--stdout-path",
            str(stdout_path),
            "--stderr-path",
            str(stderr_path),
            "--",
            *normalized,
        ]
        env = _environment()
        if environment:
            env.update(environment)
        started = time.perf_counter()
        process = subprocess.Popen(
            wrapper,
            cwd=_project_root(),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        timed_out = False
        error: str | None = None
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            error = f"command timed out after {timeout_seconds:g}s"
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
            process.wait()
        wall_fallback = time.perf_counter() - started
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        exit_code = 124 if timed_out else int(payload.get("exit_code", 127))
        wall_seconds = float(payload.get("wall_seconds", wall_fallback))
        child_cpu = payload.get("child_cpu_seconds")
        max_rss = payload.get("max_rss_mb")
        if child_cpu is not None:
            child_cpu = float(child_cpu)
        if max_rss is not None:
            max_rss = float(max_rss)
        error = error or payload.get("error")

        successful = exit_code == 0 and not timed_out
        invalid_reasons: list[str] = []
        strategy_events = (
            _read_event_count(event_paths)
            if successful and event_mode != "none"
            else None
        )
        if successful and event_mode != "none" and strategy_events is None:
            invalid_reasons.append("缺少或无效的 run_meta.total_events")
        market_events = None
        if successful and event_mode == "single":
            market_events = strategy_events
        elif successful and event_mode == "shared":
            market_events = _read_shared_event_count(stdout_path)
            if market_events is None:
                invalid_reasons.append("缺少或无法解析共享事件计数")
        persistent_artifacts = temporary_root is None
        return RunMeasurement(
            command=normalized,
            workload=workload,
            wall_seconds=wall_seconds,
            child_cpu_seconds=child_cpu,
            max_rss_mb=max_rss,
            market_events=market_events,
            strategy_events=strategy_events,
            market_events_per_second=_events_per_second(market_events, wall_seconds),
            strategy_events_per_second=_events_per_second(strategy_events, wall_seconds),
            exit_code=exit_code,
            timed_out=timed_out,
            valid=not invalid_reasons,
            invalid_reason="；".join(invalid_reasons) if invalid_reasons else None,
            error=error,
            stdout_path=stdout_path if persistent_artifacts else None,
            stderr_path=stderr_path if persistent_artifacts else None,
            measurement_path=result_path if persistent_artifacts else None,
        )
    finally:
        if temporary_root is not None:
            temporary_root.cleanup()


def _median_value(values: Sequence[int | float | None]) -> int | float | None:
    available = [value for value in values if value is not None]
    if not available:
        return None
    value = statistics.median(available)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def summarize_measurements(
    measurements: Sequence[RunMeasurement],
    *,
    workload: Workload,
) -> dict[str, object]:
    """生成重复运行报告；数值字段取中位数，不设置抖动阈值。"""

    if not measurements:
        raise BenchmarkError("at least one measurement is required")
    rows = [measurement.as_dict() for measurement in measurements]
    exit_codes = [measurement.exit_code for measurement in measurements]
    successful = [
        measurement
        for measurement in measurements
        if measurement.exit_code == 0 and measurement.valid
    ]
    invalid = [
        measurement
        for measurement in measurements
        if measurement.exit_code == 0 and not measurement.valid
    ]
    failed = [measurement for measurement in measurements if measurement.exit_code != 0]
    common_exit = exit_codes[0] if len(set(exit_codes)) == 1 else None
    successful_repeats = [
        measurement.repeat for measurement in successful if measurement.repeat is not None
    ]
    failed_repeats = [
        measurement.repeat for measurement in failed if measurement.repeat is not None
    ]
    invalid_repeats = [
        measurement.repeat for measurement in invalid if measurement.repeat is not None
    ]
    sample = successful or []

    def median(field: str) -> int | float | None:
        return _median_value([getattr(measurement, field) for measurement in sample])

    summary = {
        "schema_version": 1,
        "command": rows[0]["command"],
        "workload": workload,
        "repeats": len(rows),
        "runs": rows,
        "exit_codes": exit_codes,
        "exit_code": common_exit,
        "status": (
            "failed"
            if failed
            else ("invalid" if invalid else "ok")
        ),
        "successful_repeats": successful_repeats,
        "failed_repeats": failed_repeats,
        "invalid_repeats": invalid_repeats,
        "successful_count": len(successful),
        "failed_count": len(failed),
        "invalid_count": len(invalid),
        "wall_seconds": _median_value(
            [measurement.wall_seconds for measurement in sample]
        ),
        "child_cpu_seconds": median("child_cpu_seconds"),
        "max_rss_mb": median("max_rss_mb"),
        "market_events": median("market_events"),
        "strategy_events": median("strategy_events"),
        "market_events_per_second": median("market_events_per_second"),
        "strategy_events_per_second": median("strategy_events_per_second"),
        # 保留旧字段，但明确它等于 strategy_events。
        "events": median("strategy_events"),
        "events_per_second": median("strategy_events_per_second"),
        # 现有入口没有输出可独立验证的 load/replay/save 时间戳，因此不猜测。
        "phases": None,
        "phase_note": (
            "未对现有入口内部 load/replay/save 分段；仅报告命令级 wall_seconds"
        ),
    }
    return summary


def run_repeated(
    factory: Callable[[int, Path], CommandRun],
    *,
    workload: Workload,
    repeats: int,
    output_root: str | Path,
    timeout_seconds: float | None = None,
    environment: dict[str, str] | None = None,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    """按相同 workload 重复运行并返回 JSON-compatible 中位数报告。"""

    if repeats <= 0:
        raise BenchmarkError("repeats must be positive")
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    measurements = []
    for repeat in range(repeats):
        run_root = root / f"repeat-{repeat + 1:03d}"
        if run_root.exists():
            if run_root.is_symlink() or not run_root.is_dir():
                raise BenchmarkError(f"repeat output is not a real directory: {run_root}")
            shutil.rmtree(run_root)
        run_root.mkdir(parents=True, exist_ok=True)
        spec = factory(repeat, run_root)
        measurement = measure_command(
            spec.command,
            workload=workload,
            event_paths=spec.event_paths,
            event_mode=spec.event_mode,
            artifact_root=run_root / "benchmark",
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
        measurements.append(replace(measurement, repeat=repeat + 1))
    report = summarize_measurements(measurements, workload=workload)
    if metadata:
        report["reproducibility"] = metadata
    return report


def _iso_to_ms(value: str) -> int:
    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _sha256_file(path: str | Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _source_content_fingerprint() -> str | None:
    source_root = _project_root() / "src" / "trading_platform"
    files = sorted(source_root.rglob("*.py"))
    if not files:
        return None
    digest = hashlib.sha256()
    try:
        for path in files:
            digest.update(path.relative_to(source_root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError:
        return None
    return digest.hexdigest()


def _git_value(*arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=_project_root(),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def _reproducibility_metadata(
    workload: Workload,
    *,
    archive_index: str | Path | None = None,
) -> dict[str, object]:
    """记录足以解释一次 benchmark 结果的环境和输入指纹。"""

    uname = platform.uname()
    cpu_model = platform.processor() or None
    if not cpu_model and Path("/proc/cpuinfo").is_file():
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith("model name"):
                    cpu_model = line.split(":", 1)[-1].strip() or None
                    break
        except (OSError, UnicodeDecodeError):
            pass
    git_revision = _git_value("rev-parse", "HEAD")
    git_available = git_revision is not None
    git_status = _git_value("status", "--porcelain") if git_available else None
    metadata: dict[str, object] = {
        "git_available": git_available,
        "git_revision": git_revision,
        "git_dirty": None if git_status is None else git_status != "",
        "source_content_sha256": _source_content_fingerprint(),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "model": cpu_model,
            "uname": {
                "system": uname.system,
                "release": uname.release,
                "version": uname.version,
                "machine": uname.machine,
            },
            "cpu_count": os.cpu_count(),
        },
        "workload_parameters": workload,
    }
    index_path = archive_index or workload.get("archive_index")
    metadata["archive_index"] = {
        "path": str(index_path) if index_path else None,
        "content_sha256": _sha256_file(index_path) if index_path else None,
    }
    return metadata


def _base_spike_arguments(args: argparse.Namespace) -> list[str]:
    values = [
        "--symbol", args.symbol,
        "--start", args.start,
        "--end", args.end,
        "--duckdb-path", str(args.duckdb_path),
        "--archive-index", str(args.archive_index),
        "--strategy", args.strategy,
        "--total-notional", str(args.total_notional),
        "--warmup-hours", str(args.warmup_hours),
        "--exit-policy", args.exit_policy,
        "--chunk-hours", str(args.chunk_hours),
        "--fetch-batch-size", str(args.fetch_batch_size),
        "--duckdb-threads", str(args.duckdb_threads),
    ]
    if args.duckdb_memory_limit:
        values.extend(("--duckdb-memory-limit", args.duckdb_memory_limit))
    return values


def _single_factory(args: argparse.Namespace, workload: Workload, invocation: str):
    base = _base_spike_arguments(args)

    def factory(repeat: int, run_root: Path) -> CommandRun:
        output = run_root / "run"
        command = (
            sys.executable,
            "-m",
            "trading_platform.backtest.run_spike_short",
            *base,
            "--output",
            str(output),
        )
        return CommandRun(
            command=command,
            event_paths=(output / "run_meta.json",),
            event_mode="single",
        )

    return factory


def _sweep_factory(args: argparse.Namespace, workload: Workload, invocation: str):
    base = _base_spike_arguments(args)
    values = tuple(value.strip() for value in args.sweep_values.split(",") if value.strip())
    if len(values) < 2:
        raise BenchmarkError("symbol-sweep requires at least two --sweep-values")
    if args.sweep_parameter != "prior_high_lookback_hours":
        raise BenchmarkError(
            "当前 benchmark 仅支持 sweep 参数 prior_high_lookback_hours"
        )

    def factory(repeat: int, run_root: Path) -> CommandRun:
        task_path = run_root / "task.json"
        task_runs = []
        event_paths = []
        for index, value in enumerate(values):
            output = run_root / f"run-{index + 1:02d}"
            event_paths.append(output / "run_meta.json")
            task_runs.append({
                "run_id": f"{invocation}-{repeat + 1:03d}-{index + 1:02d}",
                "arguments": [
                    *base,
                    "--prior-high-lookback-hours", value,
                    "--output", str(output),
                ],
            })
        task_path.write_text(
            json.dumps({"symbol": args.symbol, "runs": task_runs}, indent=2),
            encoding="utf-8",
        )
        command = (
            sys.executable,
            "-m",
            "trading_platform.backtest.run_spike_sweep_symbol",
            "--task",
            str(task_path),
        )
        return CommandRun(
            command=command,
            event_paths=tuple(event_paths),
            event_mode="shared",
        )

    return factory


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workload", choices=("single", "symbol-sweep", "command"), default="single"
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--output-root", type=Path, default=Path("reports/benchmarks/p0"))
    parser.add_argument("--report", type=Path, default=None, help="可选 JSON 报告路径")
    parser.add_argument("--duckdb-path", type=Path, default=None)
    parser.add_argument("--archive-index", type=Path, default=None)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--start", default=_DEFAULT_START)
    parser.add_argument("--end", default=_DEFAULT_END)
    parser.add_argument("--strategy", default=_DEFAULT_STRATEGY)
    parser.add_argument("--total-notional", default="1000")
    parser.add_argument("--warmup-hours", type=float, default=16.0)
    parser.add_argument("--exit-policy", default="confirmed")
    parser.add_argument("--chunk-hours", type=float, default=4320.0)
    parser.add_argument("--fetch-batch-size", type=int, default=10_000)
    parser.add_argument("--duckdb-memory-limit", default=None)
    parser.add_argument("--duckdb-threads", type=int, default=1)
    parser.add_argument("--sweep-parameter", default="prior_high_lookback_hours")
    parser.add_argument("--sweep-values", default=','.join(_DEFAULT_SWEEP_VALUES))
    parser.add_argument(
        "--events-from", action="append", type=Path, default=[],
        help="command workload 的 run_meta.json 路径，可重复传入",
    )
    parser.add_argument(
        "--command", nargs=argparse.REMAINDER,
        help="command workload 的目标命令；必须放在其他选项之后",
    )
    return parser


def _prepare_args(args: argparse.Namespace) -> argparse.Namespace:
    if args.repeats <= 0:
        raise BenchmarkError("--repeats must be positive")
    if args.fetch_batch_size <= 0 or args.chunk_hours <= 0:
        raise BenchmarkError("chunk/fetch batch values must be positive")
    try:
        start_ms = _iso_to_ms(args.start)
        end_ms = _iso_to_ms(args.end)
    except ValueError as exc:
        raise BenchmarkError(f"invalid start/end: {exc}") from exc
    if start_ms >= end_ms:
        raise BenchmarkError("--start must be earlier than --end")
    if args.workload == "command":
        if not args.command:
            raise BenchmarkError("--workload command requires --command")
        if args.command[0] == "--":
            args.command = args.command[1:]
        if not args.command:
            raise BenchmarkError("--command must not be empty")
        return args
    args.duckdb_path = discover_duckdb_path(args.duckdb_path)
    args.archive_index = discover_archive_index(args.duckdb_path, args.archive_index)
    args.symbol = args.symbol or choose_symbol(
        args.archive_index, start_ms=start_ms - int(args.warmup_hours * 3_600_000), end_ms=end_ms
    )
    args.symbol = args.symbol.strip().upper()
    return args


def run_cli(argv: Sequence[str] | None = None) -> tuple[int, dict[str, object] | None]:
    """执行公开 CLI；返回 ``(process_exit_code, payload)`` 便于测试。"""

    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == _MEASURE_ONCE:
        return _run_once_child(raw[1:]), None
    parser = _build_parser()
    try:
        args = _prepare_args(parser.parse_args(raw))
        if args.workload == "command":
            workload: Workload = {
                "kind": "command",
                "command": list(args.command),
                "event_paths": [str(path) for path in args.events_from],
                "event_mode": "single" if args.events_from else "none",
                "repeats": args.repeats,
                "timeout_seconds": args.timeout_seconds,
                "output_root": str(args.output_root.resolve()),
                "report": str(args.report.resolve()) if args.report else None,
            }

            def factory(_repeat: int, _run_root: Path) -> CommandRun:
                return CommandRun(
                    tuple(args.command),
                    tuple(args.events_from),
                    "single" if args.events_from else "none",
                )

        else:
            workload: Workload = {
                "kind": args.workload,
                "symbol": args.symbol,
                "start": args.start,
                "end": args.end,
                "duckdb_path": str(args.duckdb_path),
                "archive_index": str(args.archive_index),
                "strategy": args.strategy,
                "total_notional": str(args.total_notional),
                "warmup_hours": args.warmup_hours,
                "exit_policy": args.exit_policy,
                "chunk_hours": args.chunk_hours,
                "fetch_batch_size": args.fetch_batch_size,
                "duckdb_memory_limit": args.duckdb_memory_limit,
                "duckdb_threads": args.duckdb_threads,
                "sweep_parameter": args.sweep_parameter,
                "sweep_values": [
                    value.strip()
                    for value in args.sweep_values.split(",")
                    if value.strip()
                ],
                "repeats": args.repeats,
                "timeout_seconds": args.timeout_seconds,
                "output_root": str(args.output_root.resolve()),
                "report": str(args.report.resolve()) if args.report else None,
                "event_mode": "single" if args.workload == "single" else "shared",
            }
            invocation = f"{int(time.time())}-{os.getpid()}"
            factory = (
                _single_factory(args, workload, invocation)
                if args.workload == "single"
                else _sweep_factory(args, workload, invocation)
            )
        metadata = _reproducibility_metadata(
            workload,
            archive_index=workload.get("archive_index"),
        )
        payload = run_repeated(
            factory,
            workload=workload,
            repeats=args.repeats,
            output_root=args.output_root,
            timeout_seconds=args.timeout_seconds,
            metadata=metadata,
        )
        report_path = (
            args.report.resolve()
            if args.report is not None
            else args.output_root.resolve() / "benchmark.json"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        cli_exit_code = (
            0
            if payload["failed_count"] == 0 and payload["invalid_count"] == 0
            else 1
        )
        payload["cli_exit_code"] = cli_exit_code
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return cli_exit_code, payload
    except (BenchmarkError, OSError, ValueError) as exc:
        parser.error(str(exc))
        return 2, None


def main(argv: Sequence[str] | None = None) -> int:
    code, payload = run_cli(argv)
    if payload is not None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
