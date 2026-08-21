from __future__ import annotations

import numpy as np
import pandas as pd


_REQUIRED_COLUMNS = {
    "symbol",
    "timestamp_ms",
    "volume",
    "trade_count",
    "taker_buy_volume",
    "taker_sell_volume",
}


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    values = numerator.div(denominator.where(denominator > 0))
    return values.replace([np.inf, -np.inf], np.nan)


def _rolling_sum(group: pd.DataFrame, values: pd.Series, seconds: int) -> pd.Series:
    result = values.rolling(seconds, min_periods=seconds).sum()
    continuous = group["timestamp_ms"].sub(
        group["timestamp_ms"].shift(seconds - 1)
    ).eq((seconds - 1) * 1_000)
    return result.where(continuous)


def _rolling_max(group: pd.DataFrame, values: pd.Series, seconds: int) -> pd.Series:
    result = values.rolling(seconds, min_periods=seconds).max()
    continuous = group["timestamp_ms"].sub(
        group["timestamp_ms"].shift(seconds - 1)
    ).eq((seconds - 1) * 1_000)
    return result.where(continuous)


def add_orderflow_factors(frame: pd.DataFrame) -> pd.DataFrame:
    """从 P0 已归档的订单流原始聚合按需计算滚动因子。"""
    missing = sorted(_REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"1s frame missing orderflow columns: {', '.join(missing)}")
    if frame.empty:
        return frame.copy()

    output: list[pd.DataFrame] = []
    ordered = frame.sort_values(["symbol", "timestamp_ms"], kind="stable")
    for _symbol, source in ordered.groupby("symbol", sort=False):
        group = source.copy().reset_index(drop=True)
        buy = pd.to_numeric(group["taker_buy_volume"], errors="coerce")
        sell = pd.to_numeric(group["taker_sell_volume"], errors="coerce")
        total = buy.add(sell)
        delta = buy.sub(sell)
        group["volume_delta_1s"] = delta
        group["taker_buy_ratio_1s"] = _safe_ratio(buy, total)
        group["volume_imbalance_1s"] = _safe_ratio(delta, total)

        quote_available = {
            "taker_buy_quote_volume",
            "taker_sell_quote_volume",
        }.issubset(group.columns)
        if quote_available:
            quote_buy = pd.to_numeric(
                group["taker_buy_quote_volume"], errors="coerce"
            )
            quote_sell = pd.to_numeric(
                group["taker_sell_quote_volume"], errors="coerce"
            )
            quote_total = quote_buy.add(quote_sell)
            quote_delta = quote_buy.sub(quote_sell)
            group["quote_volume_delta_1s"] = quote_delta
            group["quote_taker_buy_ratio_1s"] = _safe_ratio(quote_buy, quote_total)
            group["quote_volume_imbalance_1s"] = _safe_ratio(
                quote_delta, quote_total
            )
        else:
            quote_buy = quote_sell = quote_delta = None

        raw_count = (
            pd.to_numeric(group["raw_trade_count"], errors="coerce")
            if "raw_trade_count" in group.columns
            else None
        )
        agg_count = pd.to_numeric(group["trade_count"], errors="coerce")

        for seconds in (5, 60, 300):
            buy_roll = _rolling_sum(group, buy, seconds)
            sell_roll = _rolling_sum(group, sell, seconds)
            total_roll = buy_roll.add(sell_roll)
            cvd = buy_roll.sub(sell_roll)
            group[f"taker_buy_volume_{seconds}s"] = buy_roll
            group[f"taker_sell_volume_{seconds}s"] = sell_roll
            group[f"cvd_{seconds}s"] = cvd
            group[f"taker_buy_ratio_{seconds}s"] = _safe_ratio(
                buy_roll, total_roll
            )
            group[f"volume_imbalance_{seconds}s"] = _safe_ratio(cvd, total_roll)
            group[f"agg_trade_count_{seconds}s"] = _rolling_sum(
                group, agg_count, seconds
            )
            if raw_count is not None:
                raw_roll = _rolling_sum(group, raw_count, seconds)
                group[f"raw_trade_count_{seconds}s"] = raw_roll
                group[f"avg_raw_trade_quantity_{seconds}s"] = _safe_ratio(
                    total_roll, raw_roll
                )
            group[f"avg_agg_trade_quantity_{seconds}s"] = _safe_ratio(
                total_roll, group[f"agg_trade_count_{seconds}s"]
            )

            if quote_available and quote_buy is not None and quote_sell is not None:
                quote_buy_roll = _rolling_sum(group, quote_buy, seconds)
                quote_sell_roll = _rolling_sum(group, quote_sell, seconds)
                quote_total_roll = quote_buy_roll.add(quote_sell_roll)
                quote_cvd = quote_buy_roll.sub(quote_sell_roll)
                group[f"quote_cvd_{seconds}s"] = quote_cvd
                group[f"quote_taker_buy_ratio_{seconds}s"] = _safe_ratio(
                    quote_buy_roll, quote_total_roll
                )
                group[f"quote_volume_imbalance_{seconds}s"] = _safe_ratio(
                    quote_cvd, quote_total_roll
                )

            for direction in ("buy", "sell"):
                column = f"max_taker_{direction}_agg_trade_quantity"
                if column in group.columns:
                    values = pd.to_numeric(group[column], errors="coerce")
                    group[f"{column}_{seconds}s"] = _rolling_max(
                        group, values, seconds
                    )

        group["orderflow_exhaustion_5s_vs_60s"] = group[
            "taker_buy_ratio_60s"
        ].sub(group["taker_buy_ratio_5s"])
        if quote_available:
            group["quote_orderflow_exhaustion_5s_vs_60s"] = group[
                "quote_taker_buy_ratio_60s"
            ].sub(group["quote_taker_buy_ratio_5s"])

        output.append(group)

    return pd.concat(output, ignore_index=True)
