#!/usr/bin/env python3
"""可复现的 Dynamic Spike Short 参数实验编排器。"""

from __future__ import annotations

import argparse
import asyncio
from bisect import bisect_left
import csv
import hashlib
import itertools
import json
import logging
import math
import multiprocessing
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection

import duckdb
import pandas as pd
import psycopg

from trading_platform.shared.symbol_universe_query import (
    EFFECTIVE_SYMBOL_UNIVERSE_SQL,
)
from trading_platform.market.archive.index import (
    ARCHIVE_INDEX_FILENAME,
    load_archive_index,
    verify_archive_index_files,
)
from trading_platform.market.archive.parquet import archive_root_from_catalog
from trading_platform.backtest.loader import DEFAULT_CHUNK_HOURS
from trading_platform.shared.progress import TaskDashboard
from trading_platform.shared.config import BacktestConfig
from trading_platform.backtest.process_lock import (
    BacktestAlreadyRunning,
    BacktestProcessLock,
)

logger = logging.getLogger(__name__)

PARAMETER_FLAGS = {
    "strategy": "--strategy",
    "research": "--research",
    "th_v": "--th-v",
    "th_e": "--th-e",
    "th_r": "--th-r",
    "atr_min": "--atr-min",
    "premium_mult": "--premium-mult",
    "premium_floor": "--premium-floor",
    "premium_cap": "--premium-cap",
    "stop_pct": "--stop-pct",
    "stop_check_ms": "--stop-check-ms",
    "model_json": "--model-json",
    "total_notional": "--total-notional",
    "exit_policy": "--exit-policy",
    "prior_high_lookback_hours": "--prior-high-lookback-hours",
    "rise_low_lookback_hours": "--rise-low-lookback-hours",
    "min_rise_duration_hours": "--min-rise-duration-hours",
    "box_duration_min_hours": "--box-duration-min-hours",
    "spike_avg_deviation_max_pct": "--spike-avg-deviation-max-pct",
    "spike_range_max_pct": "--spike-range-max-pct",
    "spike_vwap_deviation_max_pct": "--spike-vwap-deviation-max-pct",
    "entry_tier_mode": "--entry-tier-mode",
    "profit_unlock_percent": "--profit-unlock-percent",
    "max_consecutive_up_minutes": "--max-consecutive-up-minutes",
    "group_rise_12h_threshold": "--group-rise-12h-threshold",
    "loose_consecutive_up_minutes": "--loose-consecutive-up-minutes",
    "loose_max_ls_ratio": "--loose-max-ls-ratio",
    "strong_tier_atr_shift": "--strong-tier-atr-shift",
    "exit_strict_age_ms": "--exit-strict-age-ms",
    "exit_flat_agreement": "--exit-flat-agreement",
    "time_risk_grace_ms": "--time-risk-grace-ms",
    "time_risk_grace_loss_ratio": "--time-risk-grace-loss-ratio",
    "strong_strict_age_ms": "--strong-strict-age-ms",
    "weak_strict_age_ms": "--weak-strict-age-ms",
    "strong_bucket_strict_age_ms": "--strong-bucket-strict-age-ms",
    "weak_bucket_strict_age_ms": "--weak-bucket-strict-age-ms",
    "profit_unlock_ratio": "--profit-unlock-ratio",
    "profit_drawdown_ratio": "--profit-drawdown-ratio",
    "profit_drawdown_peak_ratio": "--profit-drawdown-peak-ratio",
    "max_oi_change_pct": "--max-oi-change-pct",
    "max_ls_ratio": "--max-ls-ratio",
    "rise_5s_threshold_percent": "--rise-5s-threshold-percent",
    "accel_rise_5s_min_percent": "--accel-rise-5s-min-percent",
    "accel_ratio": "--accel-ratio",
    "accel_prev_minutes": "--accel-prev-minutes",
    "reject_below_current": "--reject-below-current",
    "entry_premium_mult": "--entry-premium-mult",
    "entry_premium_floor": "--entry-premium-floor",
    "entry_premium_cap": "--entry-premium-cap",
    "entry_premium_model": "--entry-premium-model",
    "entry_scoring_enabled": "--entry-scoring-enabled",
    "entry_scoring_threshold": "--entry-scoring-threshold",
    "entry_scoring_config": "--entry-scoring-config",
    "entry_premium_base_pct": "--entry-premium-base-pct",
    "oi_stop_enabled": "--oi-stop-enabled",
    "oi_stop_oi_rise_pct": "--oi-stop-oi-rise-pct",
    "oi_stop_loss_pct": "--oi-stop-loss-pct",
    "max_rise_5s_percent": "--max-rise-5s-percent",
    "max_rise_window_seconds": "--max-rise-window-seconds",
    "max_rise_window_percent": "--max-rise-window-percent",
    "max_volume_multiple_5s": "--max-volume-multiple-5s",
    "min_td_sell_setup_5m": "--min-td-sell-setup-5m",
    "min_volume_multiple_5m": "--min-volume-multiple-5m",
    "prior_high_tolerance_percent": "--prior-high-tolerance-percent",
    "limit_fill_fraction": "--limit-fill-fraction",
    "warmup_hours": "--warmup-hours",
    "bar1s_time_shift_hours": "--bar1s-time-shift-hours",
}
SUPPORTED_MATRIX_KEYS = set(PARAMETER_FLAGS)
EXECUTION_FLAGS = {
    "chunk_hours": "--chunk-hours",
    "fetch_batch_size": "--fetch-batch-size",
    "duckdb_memory_limit": "--duckdb-memory-limit",
    "duckdb_threads": "--duckdb-threads",
}
ESTIMATED_PYTHON_EVENT_BYTES = 1_024
ESTIMATED_DUCKDB_ROW_BYTES = 96
WORKER_NON_DUCKDB_RESERVE_BYTES = 1024**3
MIN_WORKER_MEMORY_BYTES = 2 * 1024**3
BACKTEST_WORKERS_ENV = "BACKTEST_WORKERS"
_SIGNAL_AUDIT_EVENT_TYPES = frozenset({"signal_triggered", "signal_rejected"})


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    symbol: str
    params: dict[str, Any]


class ChildProcessRegistry:
    """让调度器能在中断时统一终止活动回测子进程。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[str]] = set()
        self._stopping = False

    def add(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            stopping = self._stopping
            if not stopping:
                self._processes.add(process)
        if stopping:
            self._terminate(process, signal.SIGTERM)

    def remove(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.discard(process)

    def terminate_all(self) -> None:
        with self._lock:
            self._stopping = True
            processes = list(self._processes)
        for process in processes:
            self._terminate(process, signal.SIGTERM)

        # 给正常退出一个很短的窗口，避免 Ctrl+C 后逐个等待 worker。
        deadline = time.monotonic() + 0.5
        try:
            while time.monotonic() < deadline and any(
                process.poll() is None for process in processes
            ):
                time.sleep(0.02)
        except KeyboardInterrupt:
            pass
        for process in processes:
            if process.poll() is None:
                self._terminate(process, signal.SIGKILL)
        for process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    pass

    @staticmethod
    def _terminate(process: subprocess.Popen[str], sig: signal.Signals) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            elif sig == signal.SIGKILL:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            return


def _timestamp_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _dsn_from_environment() -> str:
    return (
        f"postgresql://{os.getenv('DB_USER', 'postgres')}:{os.getenv('DB_PASSWORD', 'postgres')}"
        f"@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}"
        f"/{os.getenv('DB_DATABASE', 'trading_platform')}"
    )


def _allowed_symbols(dsn: str, *, freeze_days: int, strategy_id: str) -> set[str]:
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            # 交易对筛选只允许读取主库；即使后续误加 SQL，也不能写入。
            connection.execute("SET TRANSACTION READ ONLY")
            with connection.cursor() as cursor:
                cursor.execute(
                    EFFECTIVE_SYMBOL_UNIVERSE_SQL,
                    (timedelta(days=freeze_days), strategy_id),
                )
                return {str(row[0]).strip().upper() for row in cursor.fetchall()}
    except psycopg.OperationalError as error:
        raise RuntimeError(
            "无法连接 PostgreSQL 主库；请检查 DB_HOST、DB_PORT、DB_USER、"
            "DB_PASSWORD、DB_DATABASE，以及 postgres 服务是否已启动"
        ) from error


def _symbol_onboard_times_ms(
    dsn: str, symbols: Collection[str]
) -> dict[str, int]:
    normalized = sorted({str(symbol).strip().upper() for symbol in symbols})
    if not normalized:
        return {}
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                "SELECT symbol, onboard_date FROM exchange_symbols "
                "WHERE symbol = ANY(%s)",
                (normalized,),
            ).fetchall()
    except psycopg.OperationalError as error:
        raise RuntimeError(
            "无法读取 PostgreSQL 交易对上市时间；请检查数据库连接"
        ) from error
    return {
        str(symbol).strip().upper(): int(onboard_date.timestamp() * 1000)
        for symbol, onboard_date in rows
        if onboard_date is not None
    }


def _timestamp_iso(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).isoformat()


def _configure_duckdb_connection(
    connection: duckdb.DuckDBPyConnection, *, threads: int
) -> None:
    if threads <= 0:
        raise ValueError("execution.duckdb_threads must be positive")
    connection.execute(f"SET threads = {int(threads)}")
    connection.execute("SET enable_progress_bar = false")


def _load_catalog_index(
    duckdb_path: str,
    *,
    start_ms: int,
    end_ms: int,
    symbols: Collection[str] | None,
) -> tuple[pd.DataFrame, Path]:
    archive_root = archive_root_from_catalog(duckdb_path)
    index_path = archive_root / ARCHIVE_INDEX_FILENAME
    frame = load_archive_index(index_path)
    selected = frame[
        (frame["first_open_ms"] < end_ms)
        & (frame["last_close_ms"] >= start_ms)
    ].copy()
    if symbols is not None:
        normalized = {str(symbol).strip().upper() for symbol in symbols}
        selected = selected[selected["symbol"].isin(normalized)].copy()
    verify_archive_index_files(selected, archive_root)
    return selected, index_path


def _archive_coverage(
    duckdb_path: str,
    *,
    start_ms: int,
    end_ms: int,
    symbols: Collection[str] | None,
) -> dict[str, dict[str, tuple[int, int, int]]]:
    if symbols is not None and not symbols:
        return {}
    frame, _index_path = _load_catalog_index(
        duckdb_path, start_ms=start_ms, end_ms=end_ms, symbols=symbols
    )
    frame = frame[frame["timeframe"].isin(["1s", "1m", "5m", "15m"])]
    coverage: dict[str, dict[str, tuple[int, int, int]]] = {}
    for (symbol, timeframe), group in frame.groupby(["symbol", "timeframe"]):
        coverage.setdefault(str(symbol), {})[str(timeframe)] = (
            int(group["first_open_ms"].min()),
            int(group["last_close_ms"].max()),
            int(group["row_count"].sum()),
        )
    return coverage


def _estimate_monthly_memory(
    duckdb_path: str,
    *,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    chunk_hours: float,
    fetch_batch_size: int,
) -> pd.DataFrame:
    """按币种月份估算全量物化和流式窗口的内存量级。"""
    frame, _index_path = _load_catalog_index(
        duckdb_path, start_ms=start_ms, end_ms=end_ms, symbols=symbols
    )
    frame = frame[frame["timeframe"].isin(["1s", "1m", "5m", "15m"])]
    grouped = frame.groupby(["symbol", "year", "month"], sort=True)
    records = []
    for (symbol, year, month), group in grouped:
        event_rows = int(group["row_count"].sum())
        rows_1s = int(group.loc[group["timeframe"] == "1s", "row_count"].sum())
        chunk_fraction = max(float(chunk_hours) / (24 * 30), 1 / (24 * 30))
        chunk_rows = min(event_rows, max(1, math.ceil(event_rows * chunk_fraction)))
        materialized = event_rows * ESTIMATED_PYTHON_EVENT_BYTES
        stream_peak = (
            chunk_rows * ESTIMATED_DUCKDB_ROW_BYTES
            + fetch_batch_size * ESTIMATED_PYTHON_EVENT_BYTES
            + WORKER_NON_DUCKDB_RESERVE_BYTES
        )
        records.append({
            "symbol": str(symbol), "month": f"{int(year):04d}-{int(month):02d}-01",
            "rows_1s": int(rows_1s), "event_rows": event_rows,
            "estimated_materialized_gb": materialized / 1024**3,
            "estimated_stream_peak_gb": stream_peak / 1024**3,
            "chunk_hours": float(chunk_hours),
            "estimate_note": "96B/stream event estimate calibrated against annual 1s replay; +1GiB non-DuckDB reserve",
        })
    return pd.DataFrame(records)


def resolve_universe(config: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    universe = config.get("universe", {})
    requested = {
        str(symbol).strip().upper()
        for symbol in universe.get("symbols", [])
        if str(symbol).strip()
    }
    excluded = {
        str(symbol).strip().upper()
        for symbol in universe.get("exclude_symbols", ["ZECUSDT"])
    }
    mode = universe.get("mode", "database")
    if mode not in {"database", "explicit", "all-archived", "anomaly-report"}:
        raise ValueError(
            "universe.mode must be database, explicit, all-archived, or anomaly-report"
        )

    anomaly_symbols: set[str] = set()
    if mode == "anomaly-report":
        report_path = Path(universe.get("anomaly_report", ""))
        if not report_path.is_file():
            raise ValueError(f"universe.anomaly_report does not exist: {report_path}")
        with report_path.open(newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                symbol = str(row.get("symbol", "")).strip().upper()
                if symbol:
                    anomaly_symbols.add(symbol)
        if not anomaly_symbols:
            raise ValueError("universe.anomaly_report contains no symbols")

    start_ms = _timestamp_ms(config["start"])
    end_ms = _timestamp_ms(config["end"])
    new_listing_mark_days = float(universe.get("new_listing_mark_days", 30))
    if new_listing_mark_days < 0:
        raise ValueError("universe.new_listing_mark_days must be non-negative")
    if universe.get("require_database_allowed", True):
        allowed = _allowed_symbols(
            config.get("database_dsn") or _dsn_from_environment(),
            freeze_days=int(universe.get("freeze_days", 15)),
            strategy_id=str(universe.get("strategy_id", "spike_short")),
        )
    else:
        allowed = None
        logger.warning(
            "universe.require_database_allowed=false: 跳过主库 allowed 过滤，"
            "仅用于离线回测"
        )
    archive_scan_symbols = (
        requested if mode == "explicit"
        else anomaly_symbols if mode == "anomaly-report"
        else allowed if mode == "database" else None
    )
    coverage = _archive_coverage(
        config["duckdb_path"],
        start_ms=start_ms,
        end_ms=end_ms,
        symbols=archive_scan_symbols,
    )
    archived = {
        symbol for symbol, timeframes in coverage.items() if "1s" in timeframes
    }
    tolerance_ms = int(float(universe.get("coverage_tolerance_hours", 24)) * 3_600_000)
    candidates = (
        requested if mode == "explicit"
        else anomaly_symbols if mode == "anomaly-report"
        else archived if mode == "all-archived" else allowed
    )
    onboard_times_ms = _symbol_onboard_times_ms(
        config.get("database_dsn") or _dsn_from_environment(), candidates
    )
    required_timeframes = ("1s", "1m", "5m", "15m")

    def coverage_status(symbol: str) -> tuple[bool, bool, int]:
        timeframes = coverage.get(symbol, {})
        onboard_ms = onboard_times_ms.get(symbol)
        listed_during_period = (
            onboard_ms is not None and start_ms < onboard_ms < end_ms
        )
        required_start_ms = onboard_ms if listed_during_period else start_ms
        complete = all(
            timeframe in timeframes
            and timeframes[timeframe][0] <= required_start_ms + tolerance_ms
            and timeframes[timeframe][1] >= end_ms - tolerance_ms
            for timeframe in required_timeframes
        )
        return complete, listed_during_period, required_start_ms

    complete_archived = {
        symbol for symbol in coverage if coverage_status(symbol)[0]
    }
    selected = sorted(
        (candidates & (allowed if allowed is not None else complete_archived) & complete_archived) - excluded
    )

    rows = []
    report_symbols = candidates | excluded
    if mode == "all-archived" and allowed is not None:
        report_symbols |= allowed
    if mode == "anomaly-report" and allowed is not None:
        report_symbols |= allowed
    for symbol in sorted(report_symbols):
        timeframes = coverage.get(symbol, {})
        reasons = []
        if allowed is not None and symbol not in allowed:
            reasons.append("database_disabled_or_not_tradeable")
        if symbol not in archived:
            reasons.append("missing_1s_archive")
        if symbol in excluded:
            reasons.append("explicitly_excluded")
        if mode == "anomaly-report" and symbol not in anomaly_symbols:
            reasons.append("not_in_anomaly_report")
        one_second = timeframes.get("1s")
        missing_required = [
            timeframe for timeframe in required_timeframes
            if timeframe not in timeframes
        ]
        complete, listed_during_period, effective_start_ms = coverage_status(symbol)
        onboard_ms = onboard_times_ms.get(symbol)
        onboard_age_days_at_period_start = (
            None
            if onboard_ms is None or onboard_ms > start_ms
            else (start_ms - onboard_ms) / 86_400_000
        )
        new_at_period_start = (
            onboard_age_days_at_period_start is not None
            and onboard_age_days_at_period_start <= new_listing_mark_days
        )
        period_new_listing = listed_during_period or new_at_period_start
        starts_late = any(
            timeframe in timeframes
            and timeframes[timeframe][0] > effective_start_ms + tolerance_ms
            for timeframe in required_timeframes
        )
        ends_early = any(
            timeframe in timeframes
            and timeframes[timeframe][1] < end_ms - tolerance_ms
            for timeframe in required_timeframes
        )
        if missing_required:
            reasons.append("missing_required_timeframes")
        if starts_late:
            reasons.append("archive_starts_after_required_start")
        if ends_early:
            reasons.append("archive_ends_before_period_end")
        data_incomplete = not complete
        rows.append({
            "symbol": symbol,
            "database_allowed": (
                True if allowed is None else symbol in allowed
            ),
            "has_1s_archive": symbol in archived,
            "selected": symbol in selected,
            "exclude_reason": ";".join(reasons),
            "has_1m": "1m" in timeframes,
            "has_5m": "5m" in timeframes,
            "has_15m": "15m" in timeframes,
            "first_1s_ms": None if one_second is None else one_second[0],
            "last_1s_ms": None if one_second is None else one_second[1],
            "onboard_ms": onboard_ms,
            "onboard_at": (
                None
                if onboard_ms is None
                else _timestamp_iso(onboard_ms)
            ),
            "listed_during_period": listed_during_period,
            "onboard_age_days_at_period_start": onboard_age_days_at_period_start,
            "new_at_period_start": new_at_period_start,
            "period_new_listing": period_new_listing,
            "effective_start_ms": effective_start_ms,
            "effective_start": _timestamp_iso(effective_start_ms),
            "data_incomplete": data_incomplete,
        })
    if not selected:
        raise RuntimeError("no symbols remain after database/archive universe filtering")
    return selected, rows


def _memory_bytes(value: str) -> int:
    text = str(value).strip().upper()
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
    for unit in ("GB", "MB", "KB", "B"):
        multiplier = units[unit]
        if text.endswith(unit):
            return int(float(text[:-len(unit)]) * multiplier)
    raise ValueError(f"unsupported memory size: {value}")


def _available_memory_bytes() -> int | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def _configured_worker_count() -> int:
    """Read the fixed sweep worker count from the process environment."""

    try:
        return BacktestConfig().workers
    except ValueError as error:
        raise ValueError(
            f"{BACKTEST_WORKERS_ENV} must be a positive integer"
        ) from error


def _worker_memory_plan(
    requested: int | None,
    worker_memory_budget: str,
    budget_percent: int,
    *,
    available_memory_bytes: int | None = None,
) -> tuple[int, str]:
    if requested is not None and requested <= 0:
        raise ValueError("execution.workers must be positive")
    if not 1 <= budget_percent <= 95:
        raise ValueError("execution.memory_budget_percent must be 1..95")
    per_worker_budget = _memory_bytes(worker_memory_budget)
    if per_worker_budget < MIN_WORKER_MEMORY_BYTES:
        raise ValueError("execution.worker_memory_budget must be at least 2GB")
    available = available_memory_bytes
    if available is None:
        available = _available_memory_bytes()
    if available is None:
        duckdb_bytes = per_worker_budget - WORKER_NON_DUCKDB_RESERVE_BYTES
        return requested or 1, f"{duckdb_bytes // 1024**2}MB"
    budget = available * budget_percent // 100
    max_workers = budget // per_worker_budget
    if max_workers < 1:
        raise RuntimeError(
            "available memory cannot provide the minimum 2GB worker budget"
        )
    if requested is not None and requested > max_workers:
        required_gib = requested * per_worker_budget / 1024**3
        budget_gib = budget / 1024**3
        raise RuntimeError(
            f"--workers {requested} requires at least {required_gib:.1f} GiB "
            f"within the worker budget, but only {budget_gib:.1f} GiB is "
            f"available; maximum safe workers: {max_workers}"
        )
    workers = max_workers if requested is None else requested
    duckdb_bytes = per_worker_budget - WORKER_NON_DUCKDB_RESERVE_BYTES
    return workers, f"{duckdb_bytes // 1024**2}MB"


def _symbol_worker_memory_plan(
    requested: int | None,
    symbol_count: int,
    worker_memory_budget: str,
    budget_percent: int,
    *,
    available_memory_bytes: int | None = None,
) -> tuple[int, str]:
    if symbol_count <= 0:
        raise ValueError("symbol_count must be positive")
    effective_requested = (
        min(requested, symbol_count) if requested is not None else None
    )
    workers, memory_limit = _worker_memory_plan(
        effective_requested,
        worker_memory_budget,
        budget_percent,
        available_memory_bytes=available_memory_bytes,
    )
    if workers <= symbol_count:
        return workers, memory_limit
    return _worker_memory_plan(
        symbol_count,
        worker_memory_budget,
        budget_percent,
        available_memory_bytes=available_memory_bytes,
    )


def _symbol_worker_resources(
    requested: int | None,
    symbol_count: int,
    execution: dict[str, Any],
    *,
    available_memory_bytes: int | None = None,
) -> tuple[int, str | None, str | None]:
    enabled = execution.get("memory_limit_enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("execution.memory_limit_enabled must be true or false")
    if not enabled:
        if requested is None:
            raise ValueError(
                f"关闭内存限制时必须通过 {BACKTEST_WORKERS_ENV} 固定 worker 数，"
                "不能使用 --workers"
            )
        if requested <= 0:
            raise ValueError(f"{BACKTEST_WORKERS_ENV} must be positive")
        return requested, None, None

    worker_memory_budget = str(execution.get("worker_memory_budget", "2GB"))
    # Keep the configured count even when there are fewer symbol tasks. The
    # executor creates threads lazily, so this does not waste resources and
    # makes the reported/scheduled worker count deterministic.
    workers, duckdb_memory_limit = _worker_memory_plan(
        requested,
        worker_memory_budget,
        int(execution.get("memory_budget_percent", 95)),
        available_memory_bytes=available_memory_bytes,
    )
    return workers, worker_memory_budget, duckdb_memory_limit


def expand_specs(config: dict[str, Any], symbols: list[str]) -> list[RunSpec]:
    fixed = dict(config.get("fixed", {}))
    matrix = dict(config.get("matrix", {}))
    unknown = set(matrix) - SUPPORTED_MATRIX_KEYS
    if unknown:
        raise ValueError(f"unsupported matrix parameter(s): {', '.join(sorted(unknown))}")
    if "total_notional" not in fixed and "total_notional" not in matrix:
        raise ValueError("fixed or matrix must define total_notional")
    keys = sorted(matrix)
    values = []
    for key in keys:
        value = matrix[key]
        expanded = value if isinstance(value, list) else [value]
        if not expanded:
            raise ValueError(f"matrix parameter {key} must not be empty")
        values.append(expanded)
    specs = []
    for symbol, combination in itertools.product(symbols, itertools.product(*values) if values else [()]):
        params = {**fixed, **dict(zip(keys, combination))}
        identity = json.dumps({
            "symbol": symbol,
            "params": params,
            "start": config.get("symbol_start_times", {}).get(
                symbol, config.get("start")
            ),
            "end": config.get("end"),
            "duckdb_path": config.get("duckdb_path"),
        }, sort_keys=True, default=str)
        digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
        specs.append(RunSpec(f"{digest}_{symbol}", symbol, params))
    return specs


def _run_arguments(
    spec: RunSpec,
    config: dict[str, Any],
    run_dir: Path,
) -> list[str]:
    effective_start = config.get("symbol_start_times", {}).get(
        spec.symbol, config["start"]
    )
    arguments = [
        "--symbol", spec.symbol,
        "--start", effective_start,
        "--end", config["end"],
        "--duckdb-path", config["duckdb_path"],
        "--output", str(run_dir),
    ]
    archive_index_path = config.get("archive_index_path")
    if archive_index_path:
        arguments.extend(["--archive-index", str(archive_index_path)])
    metrics_root = config.get("metrics_root")
    if metrics_root:
        arguments.extend(["--metrics-root", str(metrics_root)])
    params = {**spec.params, **config.get("execution", {})}
    for key, flag in {**PARAMETER_FLAGS, **EXECUTION_FLAGS}.items():
        if key not in params or params[key] is None:
            continue
        if isinstance(params[key], bool):
            # 布尔开关：仅 True 时传 flag（如 --reject-below-current）
            if params[key]:
                arguments.append(flag)
            continue
        arguments.extend([flag, str(params[key])])
    return arguments


def _failed_summary_row(
    spec: RunSpec,
    *,
    returncode: int,
    error: str | None = None,
) -> dict[str, Any]:
    row = {
        "run_id": spec.run_id, "symbol": spec.symbol, "status": "failed",
        "returncode": returncode,
        "parameters": json.dumps(spec.params, sort_keys=True, default=str),
        "trades": 0, "wins": 0, "win_rate": 0.0, "net_pnl": 0.0,
        "total_profit": 0.0, "total_loss": 0.0, "commission": 0.0,
        "max_drawdown": 0.0, "profit_factor": 0.0,
    }
    if error:
        row["error"] = error
    return row


def _stream_process_output(
    process: subprocess.Popen[str],
    *,
    symbol: str,
) -> tuple[str, str]:
    """捕获子进程输出（不转发到终端，由父进程面板汇总进度）。"""
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def drain(stream, target: list[str]) -> None:
        if stream is None:
            return
        for line in stream:
            target.append(line)

    stderr_thread = threading.Thread(
        target=drain,
        args=(process.stderr, stderr_lines),
        daemon=True,
    )
    stderr_thread.start()
    drain(process.stdout, stdout_lines)
    stderr_thread.join()
    process.wait()
    return "".join(stdout_lines), "".join(stderr_lines)


def _run_symbol(
    specs: list[RunSpec],
    config: dict[str, Any],
    output_root: Path,
    processes: ChildProcessRegistry | None = None,
    dashboard: TaskDashboard | None = None,
) -> tuple[list[dict[str, Any]], float]:
    started_at = time.monotonic()
    rows = []
    active: list[tuple[RunSpec, Path, list[str]]] = []
    symbol = specs[0].symbol
    if dashboard is not None:
        dashboard.task_start(symbol)
    try:
        resume = config.get("execution", {}).get("resume", True)
        for spec in specs:
            run_dir = output_root / "runs" / spec.run_id
            summary_path = run_dir / "summary.json"
            if resume and summary_path.exists():
                rows.append(_summary_row(
                    spec, json.loads(summary_path.read_text()), "resumed"
                ))
                continue
            run_dir.mkdir(parents=True, exist_ok=True)
            active.append((spec, run_dir, _run_arguments(spec, config, run_dir)))
        if not active:
            if dashboard is not None:
                dashboard.task_skip(symbol, "Resumed", increment=1)
            return rows, time.monotonic() - started_at

        task_dir = output_root / "symbol_tasks"
        task_dir.mkdir(parents=True, exist_ok=True)
        task_path = task_dir / f"{symbol}.json"
        task_path.write_text(json.dumps({
            "symbol": symbol,
            "runs": [
                {"run_id": spec.run_id, "arguments": arguments}
                for spec, _run_dir, arguments in active
            ],
        }, indent=2, ensure_ascii=False))
        command = [
            sys.executable,
            "-m",
            "trading_platform.backtest.run_spike_sweep_symbol",
            "--task",
            str(task_path),
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        if processes is not None:
            processes.add(process)
        try:
            stdout, stderr = _stream_process_output(process, symbol=symbol)
        finally:
            if processes is not None:
                processes.remove(process)

        for spec, run_dir, arguments in active:
            standalone_command = [
                sys.executable,
                "-m",
                "trading_platform.backtest.run_spike_short",
                *arguments,
            ]
            (run_dir / "command.txt").write_text(
                shlex.join(standalone_command) + "\n"
            )
            (run_dir / "symbol_command.txt").write_text(
                shlex.join(command) + "\n"
            )
            (run_dir / "stdout.log").write_text(stdout)
            (run_dir / "stderr.log").write_text(stderr)
            summary_path = run_dir / "summary.json"
            if summary_path.exists():
                rows.append(_summary_row(
                    spec, json.loads(summary_path.read_text()), "ok"
                ))
            else:
                rows.append(_failed_summary_row(
                    spec,
                    returncode=process.returncode,
                    error=stderr.strip() or None,
                ))
        failed_runs = sum(
            row["status"] not in {"ok", "resumed"} for row in rows
        )
        if dashboard is not None:
            dashboard.task_done(
                symbol,
                "OK" if failed_runs == 0 else "Failed",
                count_as_sample=failed_runs == 0 and not any(
                    row["status"] == "resumed" for row in rows
                ),
                increment=1,
            )
        return rows, time.monotonic() - started_at
    except BaseException:
        if dashboard is not None:
            dashboard.task_failed(symbol, increment=1)
        raise


def _summary_row(spec: RunSpec, summary: dict[str, Any], status: str) -> dict[str, Any]:
    positions = summary.get("positions", {})
    pnl = summary.get("pnl", {})
    return {
        "run_id": spec.run_id, "symbol": spec.symbol, "status": status,
        "parameters": json.dumps(spec.params, sort_keys=True, default=str),
        "trades": positions.get("total", 0), "wins": positions.get("profitable", 0),
        "win_rate": positions.get("win_rate", 0),
        "net_pnl": pnl.get("net_pnl", 0), "total_profit": pnl.get("total_profit", 0),
        "total_loss": pnl.get("total_loss", 0),
        "commission": pnl.get("total_commission", 0),
        "max_drawdown": pnl.get("max_drawdown", 0),
        "profit_factor": pnl.get("profit_factor", 0),
    }


def _annotate_collisions(trades: pd.DataFrame, *, tolerance_ms: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    if trades.empty:
        return trades, pd.DataFrame()
    trades = trades.copy()
    for column in ("signal_time", "entry_time", "exit_time"):
        trades[column] = pd.to_numeric(trades.get(column), errors="coerce")
    trades["collision_group_id"] = ""
    trades["collision_size"] = 1
    trades["collision_independent_pnl"] = trades["net_pnl"]
    trades["collision_conservative_pnl"] = trades["net_pnl"]
    trades["collision_status"] = "独立"
    records = []
    for params, group in trades.groupby("parameters", dropna=False):
        ordered = group.sort_values(["signal_time", "entry_time", "symbol"])
        active: list[tuple[int, int, int]] = []
        groups: list[list[int]] = []
        for index, row in ordered.iterrows():
            start = int(row["signal_time"] if pd.notna(row["signal_time"]) else row["entry_time"])
            end = int(row["exit_time"] if pd.notna(row["exit_time"]) else start)
            overlaps = [item for item in active if item[1] + tolerance_ms >= start]
            if overlaps:
                collision = [item[2] for item in overlaps] + [index]
                merged = next((item for item in groups if any(i in item for i in collision)), None)
                if merged is None:
                    groups.append(collision)
                else:
                    merged.extend(i for i in collision if i not in merged)
            active = [item for item in active if item[1] + tolerance_ms >= start]
            active.append((start, end, index))
        for number, indexes in enumerate(groups, 1):
            subset = trades.loc[indexes]
            if subset["symbol"].nunique() < 2:
                continue
            group_id = f"collision_{hashlib.sha1(str((params, indexes)).encode()).hexdigest()[:10]}"
            conservative = float(subset["net_pnl"].min())
            independent = float(subset["net_pnl"].sum())
            trades.loc[indexes, "collision_group_id"] = group_id
            trades.loc[indexes, "collision_size"] = len(subset)
            trades.loc[indexes, "collision_independent_pnl"] = independent
            trades.loc[indexes, "collision_conservative_pnl"] = conservative
            trades.loc[indexes, "collision_status"] = "多币种竞争"
            records.append({
                "collision_group_id": group_id, "parameters": params,
                "symbols": ",".join(sorted(subset["symbol"].astype(str))),
                "trade_count": len(subset), "independent_pnl": independent,
                "conservative_pnl": conservative,
            })
    return trades, pd.DataFrame(records)


def _find_simultaneous_signals(
    signals: pd.DataFrame, *, tolerance_ms: int
) -> pd.DataFrame:
    if signals.empty:
        return pd.DataFrame()
    records = []
    for params, group in signals.groupby("parameters", dropna=False):
        ordered = group.sort_values(["event_time", "symbol"])
        current: list[int] = []
        group_end = -1
        for index, row in ordered.iterrows():
            event_time = int(row["event_time"])
            if current and event_time > group_end + tolerance_ms:
                subset = ordered.loc[current]
                if subset["symbol"].nunique() > 1:
                    records.append({
                        "parameters": params,
                        "start_time": int(subset["event_time"].min()),
                        "end_time": int(subset["event_time"].max()),
                        "signal_count": len(subset),
                        "symbols": ",".join(sorted(subset["symbol"].astype(str).unique())),
                    })
                current = []
            current.append(index)
            group_end = max(group_end, event_time)
        if current:
            subset = ordered.loc[current]
            if subset["symbol"].nunique() > 1:
                records.append({
                    "parameters": params,
                    "start_time": int(subset["event_time"].min()),
                    "end_time": int(subset["event_time"].max()),
                    "signal_count": len(subset),
                    "symbols": ",".join(sorted(subset["symbol"].astype(str).unique())),
                })
    return pd.DataFrame(records)


def _collect_signal_audit_events(
    output_root: Path, specs: Collection[RunSpec]
) -> pd.DataFrame:
    """收集可复测的候选信号及通过核心条件后的入口过滤拒绝。"""
    all_signals = []
    for spec in specs:
        audit_path = output_root / "runs" / spec.run_id / "audit_events.parquet"
        if not audit_path.exists():
            continue
        audit = pd.read_parquet(audit_path)
        audit = audit[audit["event_type"].isin(_SIGNAL_AUDIT_EVENT_TYPES)].copy()
        if audit.empty:
            continue
        audit["run_id"] = spec.run_id
        audit["parameters"] = json.dumps(
            spec.params, sort_keys=True, default=str
        )
        all_signals.append(audit)
    return pd.concat(all_signals, ignore_index=True) if all_signals else pd.DataFrame()


def _write_trade_breakdowns(
    trades: pd.DataFrame, output_root: Path, *, pnl_split_usdt: float
) -> None:
    """统一输出持仓时间和盈亏金额分档，避免每次实验手工统计。"""
    if trades.empty:
        pd.DataFrame(columns=["bucket", "trades", "wins", "win_rate", "net_pnl"]).to_csv(
            output_root / "holding_bucket_summary.csv", index=False
        )
        pd.DataFrame(columns=["bucket", "trades", "net_pnl"]).to_csv(
            output_root / "pnl_bucket_summary.csv", index=False
        )
        return
    frame = trades.copy()
    holding_seconds = (
        pd.to_numeric(frame["exit_time"], errors="coerce")
        - pd.to_numeric(frame["entry_time"], errors="coerce")
    ) / 1000
    frame["holding_seconds"] = holding_seconds
    frame["holding_bucket"] = pd.cut(
        holding_seconds,
        bins=[-1, 60, 300, 900, 3600, 14400, float("inf")],
        labels=["<1m", "1-5m", "5-15m", "15-60m", "1-4h", ">4h"],
    ).astype("string")
    frame["net_pnl"] = pd.to_numeric(frame["net_pnl"], errors="coerce").fillna(0)
    threshold = float(pnl_split_usdt)
    frame["pnl_bucket"] = f"亏损绝对值<{threshold:g}U"
    frame.loc[frame["net_pnl"] >= 0, "pnl_bucket"] = f"盈利0-{threshold:g}U"
    frame.loc[frame["net_pnl"] > threshold, "pnl_bucket"] = f"盈利>{threshold:g}U"
    frame.loc[frame["net_pnl"] <= -threshold, "pnl_bucket"] = f"亏损绝对值>={threshold:g}U"

    def grouped(column: str) -> pd.DataFrame:
        result = frame.groupby(["parameters", column], dropna=False).agg(
            trades=("net_pnl", "size"),
            wins=("net_pnl", lambda values: int((values > 0).sum())),
            net_pnl=("net_pnl", "sum"),
        ).reset_index().rename(columns={column: "bucket"})
        result["win_rate"] = result["wins"] / result["trades"]
        return result[["parameters", "bucket", "trades", "wins", "win_rate", "net_pnl"]]

    grouped("holding_bucket").to_csv(output_root / "holding_bucket_summary.csv", index=False)
    grouped("pnl_bucket").to_csv(output_root / "pnl_bucket_summary.csv", index=False)


def _write_tier_fill_summary(trades: pd.DataFrame, output_root: Path) -> None:
    """按实际有成交量的开仓档位数汇总，供比较分批挂单的结果。"""
    columns = [
        "parameters", "filled_tier_count", "filled_tier_label", "trades",
        "wins", "win_rate", "gross_pnl", "commission", "net_pnl",
        "avg_entry_notional",
    ]
    if trades.empty:
        pd.DataFrame(columns=columns).to_csv(
            output_root / "tier_fill_summary.csv", index=False
        )
        return

    frame = trades.copy()
    tier_quantity_columns = [
        f"tier{tier}_fill_quantity" for tier in (1, 2, 3)
        if f"tier{tier}_fill_quantity" in frame
    ]
    if not tier_quantity_columns:
        pd.DataFrame(columns=columns).to_csv(
            output_root / "tier_fill_summary.csv", index=False
        )
        return
    quantities = frame[tier_quantity_columns].apply(
        pd.to_numeric, errors="coerce"
    ).fillna(0)
    frame["filled_tier_count"] = (quantities > 0).sum(axis=1)
    frame["filled_tier_label"] = frame["filled_tier_count"].map({
        0: "未成交", 1: "一档成交", 2: "两档成交", 3: "三档全成交",
    }).fillna("未知")
    for column in ("net_pnl", "gross_pnl", "commission", "entry_notional"):
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce").fillna(0)
    summary = frame.groupby(
        ["parameters", "filled_tier_count", "filled_tier_label"], dropna=False
    ).agg(
        trades=("net_pnl", "size"),
        wins=("net_pnl", lambda values: int((values > 0).sum())),
        gross_pnl=("gross_pnl", "sum"),
        commission=("commission", "sum"),
        net_pnl=("net_pnl", "sum"),
        avg_entry_notional=("entry_notional", "mean"),
    ).reset_index()
    summary["win_rate"] = summary["wins"] / summary["trades"]
    summary = summary[columns].sort_values(
        ["parameters", "filled_tier_count"]
    )
    summary.to_csv(output_root / "tier_fill_summary.csv", index=False)


def _write_tier3_only_projection_summary(
    trades: pd.DataFrame, output_root: Path
) -> None:
    """用原退出价格推算仅挂第三档的结果，不能替代实际单档回测。"""
    columns = [
        "parameters", "trades", "wins", "win_rate", "gross_pnl",
        "commission", "net_pnl", "avg_tier3_entry_notional",
        "scaled_to_total_notional_net_pnl",
    ]
    required = {
        "tier3_fill_quantity", "tier3_avg_fill_price", "exit_price",
        "entry_notional", "entry_quantity", "commission", "parameters",
    }
    if trades.empty or not required.issubset(trades.columns):
        pd.DataFrame(columns=columns).to_csv(
            output_root / "tier3_only_projection_summary.csv", index=False
        )
        return

    frame = trades.copy()
    numeric_columns = [
        "tier3_fill_quantity", "tier3_avg_fill_price", "exit_price",
        "entry_notional", "entry_quantity", "commission",
    ]
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[
        (frame["tier3_fill_quantity"] > 0)
        & frame["tier3_avg_fill_price"].notna()
        & frame["exit_price"].notna()
    ].copy()
    if frame.empty:
        pd.DataFrame(columns=columns).to_csv(
            output_root / "tier3_only_projection_summary.csv", index=False
        )
        return

    frame["tier3_entry_notional"] = (
        frame["tier3_fill_quantity"] * frame["tier3_avg_fill_price"]
    )
    short_gross = (
        frame["tier3_avg_fill_price"] - frame["exit_price"]
    ) * frame["tier3_fill_quantity"]
    frame["gross_pnl"] = short_gross
    if "side" in frame:
        frame.loc[frame["side"] != "SHORT", "gross_pnl"] = -short_gross
    original_turnover = (
        frame["entry_notional"]
        + frame["exit_price"] * frame["entry_quantity"]
    )
    tier3_turnover = frame["tier3_entry_notional"] + (
        frame["exit_price"] * frame["tier3_fill_quantity"]
    )
    frame["commission"] = (
        frame["commission"] * tier3_turnover / original_turnover
    ).where(original_turnover > 0, 0)
    frame["net_pnl"] = frame["gross_pnl"] - frame["commission"]

    def total_notional(parameters: str) -> float:
        try:
            return float(json.loads(parameters).get("total_notional", 0))
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0.0

    frame["configured_total_notional"] = frame["parameters"].map(total_notional)
    frame["scaled_net_pnl"] = frame["net_pnl"] * (
        frame["configured_total_notional"] / frame["tier3_entry_notional"]
    ).where(frame["tier3_entry_notional"] > 0, 0)
    summary = frame.groupby("parameters", dropna=False).agg(
        trades=("net_pnl", "size"),
        wins=("net_pnl", lambda values: int((values > 0).sum())),
        gross_pnl=("gross_pnl", "sum"),
        commission=("commission", "sum"),
        net_pnl=("net_pnl", "sum"),
        avg_tier3_entry_notional=("tier3_entry_notional", "mean"),
        scaled_to_total_notional_net_pnl=("scaled_net_pnl", "sum"),
    ).reset_index()
    summary["win_rate"] = summary["wins"] / summary["trades"]
    summary[columns].to_csv(
        output_root / "tier3_only_projection_summary.csv", index=False
    )


def _attach_breakout_context(
    trades: pd.DataFrame,
    *,
    archive_index_path: str,
    windows_hours: list[int],
    duckdb_threads: int,
    workers: int,
) -> pd.DataFrame:
    """并行按交易对批量提取入场前1m K线的上涨周期与箱体指标。"""
    if trades.empty:
        return trades
    calculation_windows = sorted({4, *windows_hours})
    max_hours = max([168, *calculation_windows])
    frame = trades.copy()
    entry_times = pd.to_numeric(frame["entry_time"], errors="coerce")
    entry_prices = pd.to_numeric(frame["entry_price"], errors="coerce")
    valid = frame[entry_times.notna() & entry_prices.notna()]
    if valid.empty:
        return frame
    index = load_archive_index(archive_index_path)
    archive_root = Path(archive_index_path).resolve().parent
    tasks = []
    for symbol, group in frame.groupby(frame["symbol"].astype(str), sort=True):
        group_times = pd.to_numeric(group["entry_time"], errors="coerce").dropna()
        if group_times.empty:
            tasks.append((symbol, group, [], calculation_windows, duckdb_threads))
            continue
        start_ms = int(group_times.min()) - max_hours * 3_600_000
        end_ms = int(group_times.max())
        selected = index[
            (index["symbol"] == symbol)
            & (index["timeframe"] == "1m")
            & (index["first_open_ms"] < end_ms)
            & (index["last_close_ms"] >= start_ms)
        ].drop_duplicates("relative_path")
        verify_archive_index_files(selected, archive_root)
        source_files = [
            str(archive_root / path)
            for path in selected["relative_path"].tolist()
        ]
        tasks.append((
            symbol, group, source_files, calculation_windows, duckdb_threads
        ))

    print(
        f"开始后计算：交易对={len(tasks)}，"
        f"worker={min(max(1, workers), len(tasks))}",
        flush=True,
    )
    context = multiprocessing.get_context("spawn")
    pool = context.Pool(processes=min(max(1, workers), len(tasks)))
    completed = 0
    enriched = []
    try:
        for symbol, result in pool.imap_unordered(
            _attach_breakout_context_symbol, tasks, chunksize=1
        ):
            enriched.append(result)
            completed += 1
            print(f"后计算进度：{completed}/{len(tasks)}，当前={symbol}", flush=True)
    except BaseException:
        pool.terminate()
        pool.join()
        raise
    else:
        pool.close()
        pool.join()
    return pd.concat(enriched, axis=0).sort_index() if enriched else frame


def _attach_breakout_context_symbol(
    task: tuple[str, pd.DataFrame, list[str], list[int], int]
) -> tuple[str, pd.DataFrame]:
    symbol, frame, source_files, calculation_windows, duckdb_threads = task
    entry_times = pd.to_numeric(frame["entry_time"], errors="coerce")
    entry_prices = pd.to_numeric(frame["entry_price"], errors="coerce")
    max_hours = max([168, *calculation_windows])
    candles = []
    if source_files:
        connection = duckdb.connect()
        try:
            _configure_duckdb_connection(connection, threads=duckdb_threads)
            candles = [
                (int(open_time), int(close_time), float(low), float(high))
                for open_time, close_time, low, high in connection.execute(
                    "SELECT epoch_ms(open_time), epoch_ms(close_time), low, high "
                    "FROM read_parquet(?, union_by_name=true) "
                    "WHERE symbol = ? AND timeframe = '1m' "
                    "ORDER BY close_time",
                    [source_files, symbol],
                ).fetchall()
            ]
        finally:
            connection.close()
    candle_cache: dict[int, list[tuple[int, int, float, float]]] = {}
    candle_close_times = [item[1] for item in candles]
    for index in frame.index:
        if pd.isna(entry_times.loc[index]) or pd.isna(entry_prices.loc[index]):
            continue
        entry_time = int(entry_times.loc[index])
        entry_price = float(entry_prices.loc[index])
        window_candles = candle_cache.get(entry_time)
        if window_candles is None:
            left = bisect_left(
                candle_close_times,
                entry_time - max_hours * 3_600_000,
            )
            right = bisect_left(candle_close_times, entry_time)
            window_candles = candles[left:right]
            candle_cache[entry_time] = window_candles
        for hours in calculation_windows:
            values = [
                item for item in window_candles
                if item[1] >= entry_time - hours * 3_600_000
            ]
            frame.at[index, f"low_{hours}h_valid"] = (
                len(values) >= math.floor(hours * 60 * 0.95)
            )
            if not values:
                continue
            low_candle = min(values, key=lambda item: (item[2], item[0]))
            frame.at[index, f"low_{hours}h"] = low_candle[2]
            frame.at[index, f"low_{hours}h_time"] = low_candle[0]
            frame.at[index, f"low_{hours}h_age_hours"] = (
                entry_time - low_candle[0]
            ) / 3_600_000
            frame.at[index, f"rise_from_{hours}h_low"] = (
                entry_price / low_candle[2] - 1
                if low_candle[2] > 0 else math.nan
            )
        low_4h = frame.at[index, "low_4h"] if "low_4h" in frame else math.nan
        for days in (3, 7):
            values = [
                item for item in window_candles
                if item[1] >= entry_time - days * 86_400_000
            ]
            frame.at[index, f"box_{days}d_valid"] = (
                len(values) >= math.floor(days * 24 * 60 * 0.95)
            )
            if not values:
                continue
            box_low = min(item[2] for item in values)
            box_high = max(item[3] for item in values)
            frame.at[index, f"box_{days}d_low"] = box_low
            frame.at[index, f"box_{days}d_high"] = box_high
            if pd.notna(low_4h) and box_low > 0:
                frame.at[index, f"low_4h_to_{days}d_low"] = (
                    low_4h / box_low - 1
                )
                span = box_high - box_low
                frame.at[index, f"low_4h_{days}d_position"] = (
                    (low_4h - box_low) / span if span > 0 else 0.0
                )
    return symbol, frame


def _write_breakout_summaries(
    trades: pd.DataFrame,
    output_root: Path,
    *,
    windows_hours: list[int],
    proximity_percentages: list[float],
) -> None:
    if trades.empty:
        pd.DataFrame().to_csv(output_root / "breakout_window_summary.csv", index=False)
        pd.DataFrame().to_csv(output_root / "box_position_summary.csv", index=False)
        pd.DataFrame().to_csv(output_root / "box_proximity_summary.csv", index=False)
        return
    window_rows = []
    for params, group in trades.groupby("parameters", dropna=False):
        for hours in windows_hours:
            valid = group[group.get(f"low_{hours}h_valid", False) == True]  # noqa: E712
            window_rows.append({
                "parameters": params, "window_hours": hours,
                "valid_trades": len(valid),
                "wins": int((valid["net_pnl"] > 0).sum()) if not valid.empty else 0,
                "win_rate": float((valid["net_pnl"] > 0).mean()) if not valid.empty else math.nan,
                "net_pnl": float(valid["net_pnl"].sum()) if not valid.empty else 0.0,
                "median_low_age_hours": float(valid[f"low_{hours}h_age_hours"].median()) if not valid.empty else math.nan,
                "median_rise_from_low": float(valid[f"rise_from_{hours}h_low"].median()) if not valid.empty else math.nan,
            })
    pd.DataFrame(window_rows).to_csv(output_root / "breakout_window_summary.csv", index=False)

    position_rows = []
    proximity_rows = []
    for params, group in trades.groupby("parameters", dropna=False):
        for days in (3, 7):
            position_column = f"low_4h_{days}d_position"
            distance_column = f"low_4h_to_{days}d_low"
            valid = group[group.get(f"box_{days}d_valid", False) == True].copy()  # noqa: E712
            if position_column in valid:
                valid["box_position"] = pd.cut(
                    valid[position_column],
                    bins=[-math.inf, 0.2, 0.5, 0.8, math.inf],
                    labels=["bottom_20", "20_50", "50_80", "top_20"],
                ).astype("string")
                for bucket, subset in valid.groupby("box_position", dropna=False):
                    position_rows.append({
                        "parameters": params, "box_days": days, "position": bucket,
                        "trades": len(subset), "wins": int((subset["net_pnl"] > 0).sum()),
                        "win_rate": float((subset["net_pnl"] > 0).mean()),
                        "net_pnl": float(subset["net_pnl"].sum()),
                    })
            if distance_column in valid:
                for threshold in proximity_percentages:
                    subset = valid[valid[distance_column] <= threshold / 100]
                    proximity_rows.append({
                        "parameters": params, "box_days": days,
                        "threshold_percent": threshold, "trades": len(subset),
                        "wins": int((subset["net_pnl"] > 0).sum()),
                        "win_rate": float((subset["net_pnl"] > 0).mean()) if not subset.empty else math.nan,
                        "net_pnl": float(subset["net_pnl"].sum()),
                    })
    pd.DataFrame(position_rows).to_csv(output_root / "box_position_summary.csv", index=False)
    pd.DataFrame(proximity_rows).to_csv(output_root / "box_proximity_summary.csv", index=False)


def _parameter_summary(
    comparison: pd.DataFrame,
    collisions: pd.DataFrame,
    signal_collisions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    successful = comparison[comparison["status"].isin(["ok", "resumed"])].copy()
    if successful.empty:
        return pd.DataFrame()
    summary = successful.groupby("parameters", dropna=False).agg(
        runs=("run_id", "size"),
        symbols=("symbol", "nunique"),
        trades=("trades", "sum"),
        wins=("wins", "sum"),
        net_pnl=("net_pnl", "sum"),
        total_profit=("total_profit", "sum"),
        total_loss=("total_loss", "sum"),
        commission=("commission", "sum"),
    ).reset_index()
    summary["win_rate"] = summary["wins"] / summary["trades"].replace(0, math.nan)
    summary["profit_factor"] = summary["total_profit"] / summary["total_loss"].replace(0, math.nan)
    summary["collision_groups"] = 0
    summary["collision_trades"] = 0
    summary["simultaneous_signal_groups"] = 0
    summary["conservative_net_pnl"] = summary["net_pnl"]
    if not collisions.empty:
        collision_totals = collisions.groupby("parameters").agg(
            collision_groups=("collision_group_id", "nunique"),
            collision_trades=("trade_count", "sum"),
            collision_independent_pnl=("independent_pnl", "sum"),
            collision_conservative_pnl=("conservative_pnl", "sum"),
        ).reset_index()
        summary = summary.drop(columns=["collision_groups", "collision_trades"]).merge(
            collision_totals, on="parameters", how="left"
        )
        summary[["collision_groups", "collision_trades"]] = summary[
            ["collision_groups", "collision_trades"]
        ].fillna(0).astype(int)
        summary["conservative_net_pnl"] = (
            summary["net_pnl"]
            - summary["collision_independent_pnl"].fillna(0)
            + summary["collision_conservative_pnl"].fillna(0)
        )
    if signal_collisions is not None and not signal_collisions.empty:
        counts = signal_collisions.groupby("parameters").size().rename(
            "simultaneous_signal_groups"
        ).reset_index()
        summary = summary.drop(columns=["simultaneous_signal_groups"]).merge(
            counts, on="parameters", how="left"
        )
        summary["simultaneous_signal_groups"] = summary[
            "simultaneous_signal_groups"
        ].fillna(0).astype(int)
    return summary.sort_values("net_pnl", ascending=False)


def _write_report(
    output_root: Path,
    summary: pd.DataFrame,
    *,
    run_count: int,
    workers: int,
    worker_memory_budget: str | None,
    duckdb_memory_limit: str | None,
) -> None:
    worker_budget_label = worker_memory_budget or "关闭"
    duckdb_limit_label = duckdb_memory_limit or "关闭"
    lines = [
        "# Spike 参数对比回测",
        "",
        f"- 回测任务：{run_count}",
        f"- 实际 worker：{workers}",
        f"- 每 worker 总内存预算：{worker_budget_label}",
        f"- 每 worker DuckDB 内存上限：{duckdb_limit_label}",
        "- 行情：DuckDB 只读流式窗口；同交易对且相同 1s 时间偏移的参数实例共享一次读取",
        "- 预检：Parquet sidecar 索引（仅用于覆盖校验，不作为行情源）",
        "- 交易对：PostgreSQL 主库只读有效集合与历史归档集合的交集",
        "- conservative_net_pnl：每个多币种冲突组仅保留最低盈亏后的保守结果",
        "",
    ]
    if not summary.empty:
        display_columns = [
            "parameters", "symbols", "trades", "win_rate", "net_pnl",
            "collision_groups", "conservative_net_pnl",
        ]
        lines.extend(["## 参数汇总", "", "| " + " | ".join(display_columns) + " |"])
        lines.append("|" + "|".join(["---"] * len(display_columns)) + "|")
        for _, row in summary.iterrows():
            lines.append("| " + " | ".join(str(row[column]) for column in display_columns) + " |")
        lines.append("")
    lines.extend([
        "## 复核文件", "",
        "- `parameter_summary.csv`：参数组合总体结果。",
        "- `comparison.csv`：参数组合按币种的执行结果。",
        "- `all_signals.csv`：完整核心入场检查通过后的信号决策，含已触发和扩展入口过滤拒绝（连阳、OI、LS、涨幅/成交量上限）；同一拒绝原因在信号冷却窗口内合并，可用于后续复测选币。",
        "- `all_trades.csv`：逐笔买卖点、盈亏及冲突标记。",
        "- `collisions.csv`：多币种同时或重叠交易组。",
        "- `signal_collisions.csv`：同一秒附近触发的多币种信号组。",
        "- `holding_bucket_summary.csv`：持仓周期分档。",
        "- `pnl_bucket_summary.csv`：输赢金额分档。",
        "- `tier_fill_summary.csv`：实际成交一档、两档、三档全成交的收益分档。",
        "- `tier3_only_projection_summary.csv`：仅挂第三档的逐笔重算汇总；复用原退出价，非实际单档回测。",
        "- `breakout_window_summary.csv`：入场前各上涨窗口的低点距离与整体表现。",
        "- `box_position_summary.csv`：4 小时低点在 3/7 天箱体的位置分档。",
        "- `box_proximity_summary.csv`：4 小时低点贴近 3/7 天箱体底部的阈值分档。",
        "- `universe.csv`：交易对纳入和排除原因。",
        "- `memory_estimate.csv`：按币种月份估算的全量与流式内存。",
    ])
    (output_root / "report.md").write_text("\n".join(lines) + "\n")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dynamic Spike 参数矩阵流式回测")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--persist-results",
        action="store_true",
        help="完成报告后将研究、报表和逐笔交易导入 PostgreSQL",
    )
    args = parser.parse_args(argv)
    config = tomllib.loads(args.config.read_text())
    config["duckdb_path"] = str(
        config.get("duckdb_path", "data/market/candles/candles.duckdb")
    )
    execution = config.setdefault("execution", {})
    if "workers" in execution:
        raise ValueError(
            f"worker 数请通过环境变量 {BACKTEST_WORKERS_ENV} 配置，"
            "不要放在回测配置文件中"
        )
    configured_workers = _configured_worker_count()
    duckdb_threads = int(execution.get("duckdb_threads", 1))
    if duckdb_threads <= 0:
        raise ValueError("execution.duckdb_threads must be positive")
    output_root = Path(config.get("output", f"reports/{config.get('name', 'spike_sweep')}"))
    print("正在筛选交易对并检查历史归档覆盖...", flush=True)
    symbols, universe_rows = resolve_universe(config)
    config["symbol_start_times"] = {
        row["symbol"]: row["effective_start"]
        for row in universe_rows
        if row["selected"]
    }
    config["archive_index_path"] = str(
        archive_root_from_catalog(config["duckdb_path"]) / ARCHIVE_INDEX_FILENAME
    )
    specs = expand_specs(config, symbols)
    output_root.mkdir(parents=True, exist_ok=True)
    with (output_root / "universe.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=universe_rows[0].keys())
        writer.writeheader(); writer.writerows(universe_rows)
    (output_root / "universe_snapshot.json").write_text(
        json.dumps({
            "source": "primary_postgresql_read_only",
            "duckdb_path": config["duckdb_path"],
            "start": config["start"],
            "end": config["end"],
            "selected_symbols": symbols,
            "symbol_start_times": config["symbol_start_times"],
        }, indent=2, ensure_ascii=False)
    )
    if "duckdb_memory_limit" in execution:
        raise ValueError(
            "execution.duckdb_memory_limit 已移除；请使用表示进程总预算的 "
            "execution.worker_memory_budget"
        )
    specs_by_symbol: dict[str, list[RunSpec]] = {}
    for spec in specs:
        specs_by_symbol.setdefault(spec.symbol, []).append(spec)
    workers, worker_memory_budget, actual_memory_limit = _symbol_worker_resources(
        configured_workers,
        len(specs_by_symbol),
        execution,
    )
    if actual_memory_limit is not None:
        execution["duckdb_memory_limit"] = actual_memory_limit
    else:
        execution.pop("duckdb_memory_limit", None)
    print("正在估算所选交易对的流式回测内存...", flush=True)
    memory_estimate = _estimate_monthly_memory(
        config["duckdb_path"],
        symbols=symbols,
        start_ms=_timestamp_ms(config["start"]),
        end_ms=_timestamp_ms(config["end"]),
        chunk_hours=float(execution.get("chunk_hours", DEFAULT_CHUNK_HOURS)),
        fetch_batch_size=int(execution.get("fetch_batch_size", 10_000)),
    )
    memory_estimate.to_csv(output_root / "memory_estimate.csv", index=False)
    memory_limit_bytes = (
        _memory_bytes(worker_memory_budget)
        if worker_memory_budget is not None
        else None
    )
    if (
        memory_limit_bytes is not None
        and not memory_estimate.empty
        and memory_estimate["estimated_stream_peak_gb"].max() * 1024**3
        > memory_limit_bytes
    ):
        raise RuntimeError(
            "estimated stream peak exceeds execution.worker_memory_budget; "
            "reduce chunk_hours or raise the worker budget"
        )
    rows = []
    processes = ChildProcessRegistry()
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {}
    completed_symbol_count = 0
    succeeded_count = 0
    failed_count = 0
    dashboard = TaskDashboard(
        title="backtest",
        total=len(specs_by_symbol),
        workers=workers,
        stream=sys.stdout,
    )
    dashboard.start(
        detail=(
            f"pairs={len(specs_by_symbol)} runs={len(specs)} "
            f"workers={workers} output={output_root}"
        )
    )
    try:
        futures = {
            pool.submit(
                _run_symbol, symbol_specs, config, output_root, processes,
                dashboard,
            ): (symbol, symbol_specs)
            for symbol, symbol_specs in specs_by_symbol.items()
        }
        for future in as_completed(futures):
            symbol, symbol_specs = futures[future]
            completed_symbol_count += 1
            try:
                symbol_rows, symbol_elapsed = future.result()
            except Exception as error:
                symbol_rows = [
                    _failed_summary_row(
                        spec, returncode=-1, error=str(error)
                    )
                    for spec in symbol_specs
                ]
                symbol_elapsed = 0.0
            rows.extend(symbol_rows)
            succeeded_count += sum(
                row["status"] in {"ok", "resumed"} for row in symbol_rows
            )
            failed_count += sum(
                row["status"] not in {"ok", "resumed"} for row in symbol_rows
            )
            symbol_failed = sum(
                row["status"] not in {"ok", "resumed"} for row in symbol_rows
            )
            if symbol_failed:
                dashboard.error(f"{symbol} 失败 {symbol_failed} 个参数实例")
        pool.shutdown(wait=True)
    except KeyboardInterrupt:
        dashboard.error("收到 Ctrl+C，正在终止活动回测子进程...")
        print("收到 Ctrl+C，正在终止活动回测子进程...", flush=True)
        try:
            processes.terminate_all()
            for future in futures:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
            print(
                "回测已停止："
                f"已完成交易对={completed_symbol_count}/{len(specs_by_symbol)}；"
                "已完成任务可在下次运行时通过 resume 复用。",
                flush=True,
            )
        finally:
            dashboard.close(status="interrupted")
        return 130
    except Exception:
        try:
            processes.terminate_all()
            for future in futures:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
        finally:
            dashboard.close(status="failed")
        raise
    try:
        comparison = pd.DataFrame(rows).sort_values(
            ["status", "net_pnl"], ascending=[True, False]
        )
        comparison.to_csv(output_root / "comparison.csv", index=False)
        print("回测任务已结束，正在合并逐笔交易和信号...", flush=True)
        all_trades = []
        for spec in specs:
            path = output_root / "runs" / spec.run_id / "trades.csv"
            if path.exists():
                frame = pd.read_csv(path)
                frame["run_id"] = spec.run_id
                frame["parameters"] = json.dumps(
                    spec.params, sort_keys=True, default=str
                )
                all_trades.append(frame)
        trades = (
            pd.concat(all_trades, ignore_index=True)
            if all_trades else pd.DataFrame()
        )
        analysis = config.get("analysis", {})
        windows_hours = [
            int(value)
            for value in analysis.get(
                "breakout_windows_hours", [4, 6, 8, 12, 24]
            )
        ]
        trades = _attach_breakout_context(
            trades,
            archive_index_path=config["archive_index_path"],
            windows_hours=windows_hours,
            duckdb_threads=duckdb_threads,
            workers=workers,
        )
        print("后计算完成，正在生成冲突、分档和参数汇总报表...", flush=True)
        trades, collisions = _annotate_collisions(
            trades,
            tolerance_ms=int(
                config.get("analysis", {}).get(
                    "collision_tolerance_seconds", 1
                ) * 1000
            ),
        )
        trades.to_csv(output_root / "all_trades.csv", index=False)
        collisions.to_csv(output_root / "collisions.csv", index=False)
        signals = _collect_signal_audit_events(output_root, specs)
        signals.to_csv(output_root / "all_signals.csv", index=False)
        triggered_signals = (
            signals[signals["event_type"] == "signal_triggered"]
            if "event_type" in signals
            else signals
        )
        signal_collisions = _find_simultaneous_signals(
            triggered_signals,
            tolerance_ms=int(
                analysis.get("collision_tolerance_seconds", 1) * 1000
            ),
        )
        signal_collisions.to_csv(output_root / "signal_collisions.csv", index=False)
        _write_trade_breakdowns(
            trades,
            output_root,
            pnl_split_usdt=float(
                config.get("analysis", {}).get("pnl_split_usdt", 10)
            ),
        )
        _write_tier_fill_summary(trades, output_root)
        _write_tier3_only_projection_summary(trades, output_root)
        _write_breakout_summaries(
            trades,
            output_root,
            windows_hours=windows_hours,
            proximity_percentages=[
                float(value)
                for value in analysis.get(
                    "box_proximity_percentages", [1, 3, 5, 10]
                )
            ],
        )
        parameter_summary = _parameter_summary(
            comparison, collisions, signal_collisions
        )
        parameter_summary.to_csv(output_root / "parameter_summary.csv", index=False)
        _write_report(
            output_root,
            parameter_summary,
            run_count=len(specs),
            workers=workers,
            worker_memory_budget=worker_memory_budget,
            duckdb_memory_limit=actual_memory_limit,
        )
        public_config = {
            key: value for key, value in config.items() if key != "database_dsn"
        }
        (output_root / "experiment.json").write_text(json.dumps({
            "config": public_config, "symbols": symbols,
            "runs": len(specs), "workers": workers,
            "worker_memory_budget": worker_memory_budget,
            "duckdb_memory_limit_per_worker": actual_memory_limit,
        }, indent=2, default=str))
        if args.persist_results:
            from trading_platform.backtest.report_import_cli import (
                import_report_directory,
            )

            print("正在将回测研究导入 PostgreSQL...", flush=True)
            research_id = asyncio.run(
                import_report_directory(
                    output_root,
                    config.get("database_dsn") or _dsn_from_environment(),
                )
            )
            print(f"回测研究入库完成: {research_id}", flush=True)
    except KeyboardInterrupt:
        dashboard.close(status="interrupted")
        raise
    except RuntimeError as error:
        dashboard.close(
            status="interrupted" if "Query interrupted" in str(error) else "failed"
        )
        raise
    except Exception:
        dashboard.close(status="failed")
        raise
    dashboard.close(
        status="ok" if failed_count == 0 else "partial",
        detail=f"success={succeeded_count} failed={failed_count} "
        f"output={output_root}",
    )
    print(
        f"实验完成: {len(specs)} runs, workers={workers}, output={output_root}",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        with BacktestProcessLock():
            return _main(argv)
    except KeyboardInterrupt:
        print("回测已停止。", flush=True)
        return 130
    except BacktestAlreadyRunning as error:
        print(f"回测启动失败：{error}", file=sys.stderr, flush=True)
        return 1
    except RuntimeError as error:
        if "Query interrupted" not in str(error):
            raise
        print("回测已停止。", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
