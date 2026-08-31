from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Sequence

import pandas as pd

from trading_platform.market.archive.index import (
    load_archive_index,
    verify_archive_index_files,
)

from .event import SpikeEventConfig, detect_spike_events
from .factors import add_market_factors, add_orderflow_factors
from .labels import SpikeLabelConfig, attach_short_labels


BAR1S_COLUMNS = (
    "symbol",
    "timestamp_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "quote_volume",
    "trade_count",
    "raw_trade_count",
    "taker_buy_volume",
    "taker_sell_volume",
    "taker_buy_quote_volume",
    "taker_sell_quote_volume",
    "taker_buy_trade_count",
    "taker_sell_trade_count",
    "taker_buy_agg_trade_count",
    "taker_sell_agg_trade_count",
    "max_agg_trade_quantity",
    "max_taker_buy_agg_trade_quantity",
    "max_taker_sell_agg_trade_quantity",
)


@lru_cache(maxsize=4)
def _archive_source(catalog_path: str) -> tuple[Path, pd.DataFrame]:
    """Resolve and validate immutable archive routing once per research process."""
    import duckdb

    catalog = duckdb.connect(catalog_path, read_only=True)
    try:
        metadata = dict(
            catalog.execute(
                "SELECT key, value FROM archive_catalog_metadata"
            ).fetchall()
        )
    finally:
        catalog.close()
    return Path(metadata["archive_root"]), load_archive_index(
        Path(metadata["archive_index"])
    )


def load_bar1s_frame(
    catalog_path: str | Path,
    *,
    symbols: Sequence[str],
    start_ms: int,
    end_ms: int,
) -> pd.DataFrame:
    """只读加载研究窗口内的现有 1s 归档，不生成新的秒级存储。"""
    import duckdb

    if start_ms >= end_ms:
        raise ValueError("start_ms must be earlier than end_ms")
    normalized = tuple(dict.fromkeys(value.strip().upper() for value in symbols if value.strip()))
    if not normalized:
        raise ValueError("symbols must not be empty")
    path = Path(catalog_path)
    if not path.is_file():
        raise FileNotFoundError(f"DuckDB archive not found: {path}")

    archive_root, index = _archive_source(str(path.resolve()))
    selected = index[
        index["symbol"].isin(normalized)
        & index["timeframe"].eq("1s")
        & index["first_open_ms"].lt(end_ms)
        & index["last_close_ms"].ge(start_ms)
    ]
    files = [
        str(archive_root / relative_path)
        for relative_path in selected["relative_path"].drop_duplicates()
    ]
    if not files:
        return pd.DataFrame(columns=BAR1S_COLUMNS)
    verify_archive_index_files(selected, archive_root)

    placeholders = ", ".join("?" for _ in normalized)
    select = """
        symbol,
        epoch_ms(open_time)::BIGINT AS timestamp_ms,
        open, high, low, close, volume, vwap, quote_volume,
        trade_count, raw_trade_count,
        taker_buy_volume, taker_sell_volume,
        taker_buy_quote_volume, taker_sell_quote_volume,
        taker_buy_trade_count, taker_sell_trade_count,
        taker_buy_agg_trade_count, taker_sell_agg_trade_count,
        max_agg_trade_quantity,
        max_taker_buy_agg_trade_quantity,
        max_taker_sell_agg_trade_quantity
    """
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET memory_limit = '512MB'")
        connection.execute("SET preserve_insertion_order = false")
        frame = connection.execute(
            f"SELECT {select} FROM read_parquet(?, union_by_name=true) "
            "WHERE timeframe='1s' "
            f"AND symbol IN ({placeholders}) "
            "AND open_time >= to_timestamp(? / 1000.0) "
            "AND open_time < to_timestamp(? / 1000.0) "
            "ORDER BY symbol, open_time",
            [files, *normalized, int(start_ms), int(end_ms)],
        ).fetch_df()
    finally:
        connection.close()
    return frame


def build_factor_frame(bars: pd.DataFrame) -> pd.DataFrame:
    """由现有 1s 原始聚合计算 P1 第一批因子。"""
    return add_orderflow_factors(add_market_factors(bars))


def build_event_dataset(
    bars: pd.DataFrame,
    *,
    event_config: SpikeEventConfig = SpikeEventConfig(),
    label_config: SpikeLabelConfig = SpikeLabelConfig(),
    event_start_ms: int | None = None,
    event_end_ms: int | None = None,
) -> pd.DataFrame:
    """一次构建事件级研究数据集；默认不落盘。"""
    if event_start_ms is not None and event_end_ms is not None:
        if event_start_ms >= event_end_ms:
            raise ValueError("event_start_ms must be earlier than event_end_ms")
    factors = build_factor_frame(bars)
    candidates = factors
    if event_start_ms is not None:
        # 把 cooldown lookback 纳入检测，让跨研究分块的同一次连续 spike 不会
        # 在下一个 chunk 开头被重复记为新事件；最终再裁掉窗口外事件。
        detection_start = event_start_ms - event_config.cooldown_seconds * 1_000
        candidates = candidates[candidates["timestamp_ms"].ge(detection_start)]
    if event_end_ms is not None:
        candidates = candidates[candidates["timestamp_ms"].lt(event_end_ms)]
    events = detect_spike_events(candidates, config=event_config)
    if event_start_ms is not None:
        events = events[events["timestamp_ms"].ge(event_start_ms)]
    if event_end_ms is not None:
        events = events[events["timestamp_ms"].lt(event_end_ms)]
    return attach_short_labels(events, bars, config=label_config)
