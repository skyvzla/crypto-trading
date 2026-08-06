from decimal import Decimal

import pandas as pd
import pytest

from trading_platform.backtest.loader import BacktestDataLoader


def test_aggtrades_are_stably_sorted_before_aggregation(tmp_path):
    aggtrades = tmp_path / "aggtrades"
    aggtrades.mkdir()
    pd.DataFrame(
        {
            "trade_time": [1_900, 1_100, 1_500],
            "price": [Decimal("103"), Decimal("100"), Decimal("101")],
            "qty": [Decimal("1"), Decimal("2"), Decimal("1")],
        }
    ).to_parquet(aggtrades / "BTCUSDT.parquet", index=False)

    loader = BacktestDataLoader(str(tmp_path), ["btcusdt"], 1_000, 2_000)
    bars = loader._load_bars("BTCUSDT")

    assert len(bars) == 1
    assert bars[0].open == Decimal("100")
    assert bars[0].close == Decimal("103")
    assert bars[0].high == Decimal("103")
    assert bars[0].low == Decimal("100")
    assert bars[0].volume == Decimal("4")


def test_loader_rejects_invalid_time_range(tmp_path):
    with pytest.raises(ValueError, match="start_ms"):
        BacktestDataLoader(str(tmp_path), ["BTCUSDT"], 2_000, 2_000)


def test_loader_reports_missing_columns(tmp_path):
    aggtrades = tmp_path / "aggtrades"
    aggtrades.mkdir()
    pd.DataFrame({"trade_time": [1_100], "price": [100]}).to_parquet(
        aggtrades / "BTCUSDT.parquet", index=False
    )
    loader = BacktestDataLoader(str(tmp_path), ["BTCUSDT"], 1_000, 2_000)

    with pytest.raises(ValueError, match="missing required columns: qty"):
        loader._load_bars("BTCUSDT")


def test_spike_loader_rejects_missing_required_dataset(tmp_path):
    loader = BacktestDataLoader(
        str(tmp_path), ["BTCUSDT"], 1_000, 2_000,
        require_aggtrades=True,
        required_kline_intervals=["1m", "5m"],
    )

    with pytest.raises(ValueError, match="required aggTrade"):
        loader.load_all()
