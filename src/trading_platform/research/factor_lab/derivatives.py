from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from trading_platform.market.archive.metrics import (
    MetricsArchiveIndexError,
    load_metrics_index,
)


METRICS_COLUMNS = (
    "symbol",
    "snapshot_time_ms",
    "available_time_ms",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)


@lru_cache(maxsize=4)
def _metrics_source(catalog_path: str) -> tuple[Path, pd.DataFrame]:
    """Resolve and validate immutable metrics routing once per research process."""
    import duckdb

    catalog = duckdb.connect(catalog_path, read_only=True)
    try:
        metadata = dict(
            catalog.execute(
                "SELECT key, value FROM metrics_catalog_metadata"
            ).fetchall()
        )
    finally:
        catalog.close()
    return Path(metadata["metrics_root"]), load_metrics_index(
        Path(metadata["metrics_index"])
    ).to_pandas()


def load_metrics_frame(
    catalog_path: str | Path,
    *,
    symbols: Sequence[str],
    start_ms: int,
    end_ms: int,
) -> pd.DataFrame:
    """读取现有 5m metrics 归档；不创建新的衍生品历史存储。"""
    import duckdb

    if start_ms >= end_ms:
        raise ValueError("start_ms must be earlier than end_ms")
    normalized = tuple(dict.fromkeys(value.strip().upper() for value in symbols if value.strip()))
    if not normalized:
        raise ValueError("symbols must not be empty")
    path = Path(catalog_path)
    if not path.is_file():
        raise FileNotFoundError(f"metrics DuckDB catalog not found: {path}")

    metrics_root, index = _metrics_source(str(path.resolve()))
    selected = index[
        index["symbol"].isin(normalized)
        & index["period"].eq("5m")
        & index["first_snapshot_ms"].lt(end_ms)
        & index["last_snapshot_ms"].ge(start_ms - 300_000)
    ]
    files = [
        str(metrics_root / relative_path)
        for relative_path in selected["relative_path"].drop_duplicates()
    ]
    if not files:
        return pd.DataFrame(columns=METRICS_COLUMNS)
    for row in selected.itertuples(index=False):
        partition = metrics_root / row.relative_path
        try:
            stat = partition.stat()
        except FileNotFoundError as error:
            raise MetricsArchiveIndexError(
                f"metrics index is stale: missing {row.relative_path}"
            ) from error
        if stat.st_size != row.file_size or stat.st_mtime_ns != row.file_mtime_ns:
            raise MetricsArchiveIndexError(
                f"metrics index is stale: changed {row.relative_path}"
            )

    placeholders = ", ".join("?" for _ in normalized)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET memory_limit = '512MB'")
        connection.execute("SET preserve_insertion_order = false")
        frame = connection.execute(
            "SELECT symbol, epoch_ms(snapshot_time)::BIGINT AS snapshot_time_ms, "
            "epoch_ms(available_time)::BIGINT AS available_time_ms, "
            "sum_open_interest, sum_open_interest_value, "
            "count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, "
            "count_long_short_ratio, sum_taker_long_short_vol_ratio "
            "FROM read_parquet(?, union_by_name=true) WHERE period='5m' "
            f"AND symbol IN ({placeholders}) "
            "AND available_time >= to_timestamp(? / 1000.0) "
            "AND available_time < to_timestamp(? / 1000.0) "
            "ORDER BY symbol, available_time",
            [files, *normalized, int(start_ms), int(end_ms)],
        ).fetch_df()
    finally:
        connection.close()
    return frame


def _rolling_zscore(values: pd.Series, window: int, min_periods: int) -> pd.Series:
    history = values.shift(1)
    mean = history.rolling(window, min_periods=min_periods).mean()
    std = history.rolling(window, min_periods=min_periods).std(ddof=0)
    result = values.sub(mean).div(std.where(std > 0))
    return result.replace([np.inf, -np.inf], np.nan)


def add_derivative_factors(metrics: pd.DataFrame) -> pd.DataFrame:
    """从 Binance 5m metrics 派生慢因子，严格按 available_time 使用。"""
    required = set(METRICS_COLUMNS)
    missing = sorted(required - set(metrics.columns))
    if missing:
        raise ValueError(f"metrics frame missing columns: {', '.join(missing)}")
    if metrics.empty:
        return metrics.copy()

    output: list[pd.DataFrame] = []
    ordered = metrics.sort_values(["symbol", "available_time_ms"], kind="stable")
    for _symbol, source in ordered.groupby("symbol", sort=False):
        group = source.copy().reset_index(drop=True)
        oi = pd.to_numeric(group["sum_open_interest"], errors="coerce")
        oi_value = pd.to_numeric(group["sum_open_interest_value"], errors="coerce")
        step_5m = group["snapshot_time_ms"].sub(group["snapshot_time_ms"].shift(1)).eq(
            300_000
        )
        step_15m = group["snapshot_time_ms"].sub(group["snapshot_time_ms"].shift(3)).eq(
            900_000
        )
        group["oi_change_5m"] = oi.pct_change(fill_method=None).where(step_5m)
        group["oi_change_15m"] = oi.div(oi.shift(3)).sub(1.0).where(step_15m)
        group["oi_value_change_5m"] = oi_value.pct_change(fill_method=None).where(step_5m)
        group["oi_change_zscore_1h"] = _rolling_zscore(
            group["oi_change_5m"], window=12, min_periods=6
        )

        for column in (
            "count_long_short_ratio",
            "count_toptrader_long_short_ratio",
            "sum_toptrader_long_short_ratio",
            "sum_taker_long_short_vol_ratio",
        ):
            values = pd.to_numeric(group[column], errors="coerce")
            group[f"{column}_zscore_24h"] = _rolling_zscore(
                values, window=288, min_periods=24
            )

        output.append(group)
    return pd.concat(output, ignore_index=True)


def attach_derivative_factors(
    events: pd.DataFrame,
    metrics: pd.DataFrame,
    *,
    max_age_ms: int = 10 * 60_000,
) -> pd.DataFrame:
    """把事件时点之前已经可见的最近一份 5m metrics 因果拼接到事件。"""
    if max_age_ms <= 0:
        raise ValueError("max_age_ms must be positive")
    if events.empty:
        return events.copy()
    if "symbol" not in events.columns or "timestamp_ms" not in events.columns:
        raise ValueError("events must contain symbol and timestamp_ms")
    derived = add_derivative_factors(metrics)
    if derived.empty:
        result = events.copy()
        result["metrics_available_time_ms"] = pd.NA
        return result

    parts: list[pd.DataFrame] = []
    factor_columns = [column for column in derived.columns if column != "symbol"]
    for symbol, event_group in events.groupby("symbol", sort=False):
        metric_group = derived[derived["symbol"] == symbol]
        if metric_group.empty:
            part = event_group.copy()
            for column in factor_columns:
                if column not in part.columns:
                    part[column] = np.nan
            parts.append(part)
            continue
        right = metric_group.drop(columns=["symbol"]).sort_values(
            "available_time_ms", kind="stable"
        )
        part = pd.merge_asof(
            event_group.sort_values("timestamp_ms", kind="stable"),
            right,
            left_on="timestamp_ms",
            right_on="available_time_ms",
            direction="backward",
            allow_exact_matches=True,
        )
        parts.append(part)

    result = pd.concat(parts, ignore_index=True).sort_values(
        ["timestamp_ms", "symbol"], kind="stable"
    )
    result = result.rename(columns={"available_time_ms": "metrics_available_time_ms"})
    result["metrics_age_ms"] = result["timestamp_ms"].sub(
        result["metrics_available_time_ms"]
    )
    stale = result["metrics_age_ms"].gt(max_age_ms)
    if stale.any():
        protected = {
            "event_id",
            "symbol",
            "timestamp_ms",
            "metrics_available_time_ms",
            "metrics_age_ms",
        }
        metric_columns = [
            column
            for column in derived.columns
            if column != "symbol" and column in result.columns and column not in protected
        ]
        result.loc[stale, metric_columns] = np.nan
    if {"return_300s", "oi_change_5m"}.issubset(result.columns):
        result["price_oi_joint_5m"] = result["return_300s"].mul(
            result["oi_change_5m"]
        )
        result["price_up_oi_up_5m"] = (
            result["return_300s"].gt(0) & result["oi_change_5m"].gt(0)
        ).astype("boolean")
    return result.reset_index(drop=True)
