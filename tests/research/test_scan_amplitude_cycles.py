from pathlib import Path
from runpy import run_path

import pandas as pd
import pytest

from trading_platform.research.amplitude_cycles import DAY_MS, ScanConfig


SCRIPT = run_path("scripts/scan_amplitude_cycles.py")
daily_candidates = SCRIPT["_daily_candidates"]


def _write_daily_archive(tmp_path: Path, day_prices: list[tuple[int, float]]) -> pd.DataFrame:
    rows = []
    for day, price in day_prices:
        open_ms = day * DAY_MS
        rows.append({
            "symbol": "ABCUSDT",
            "timeframe": "1d",
            "open_time": pd.to_datetime(open_ms, unit="ms", utc=True),
            "close_time": pd.to_datetime(open_ms + DAY_MS - 1, unit="ms", utc=True),
            "open": price,
            "high": price * 1.01,
            "low": price,
            "close": price,
        })
    path = tmp_path / "daily.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return pd.DataFrame([{
        "symbol": "ABCUSDT",
        "timeframe": "1d",
        "relative_path": path.name,
        "first_open_ms": min(day for day, _ in day_prices) * DAY_MS,
        "last_close_ms": (max(day for day, _ in day_prices) + 1) * DAY_MS - 1,
    }])


def test_daily_screen_rejects_non_contiguous_rolling_window(tmp_path: Path) -> None:
    index = _write_daily_archive(tmp_path, [(0, 100), (2, 130), (3, 150)])

    candidates = daily_candidates(
        index,
        tmp_path,
        3 * DAY_MS,
        4 * DAY_MS,
        None,
        ScanConfig(candidate_threshold_percent=15),
        1,
    )

    assert candidates == []


def test_daily_screen_separates_daily_amplitude_from_rolling_score(tmp_path: Path) -> None:
    index = _write_daily_archive(tmp_path, [(0, 100), (1, 105), (2, 130)])

    candidates = daily_candidates(
        index,
        tmp_path,
        2 * DAY_MS,
        3 * DAY_MS,
        None,
        ScanConfig(candidate_threshold_percent=15),
        1,
    )

    assert len(candidates) == 1
    assert candidates[0].amplitude == pytest.approx(1.0)
    assert candidates[0].candidate_score > 30
