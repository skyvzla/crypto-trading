import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading_platform.backtest.report_import import (
    ReportDirectoryParser,
    ReportValidationError,
)


def _write_report(root: Path, *, strategy_id: str | None = "spike_short") -> Path:
    root.mkdir()
    universe = {} if strategy_id is None else {"strategy_id": strategy_id}
    (root / "experiment.json").write_text(json.dumps({
        "config": {
            "name": "small-study",
            "universe": universe,
            "fixed": {"notional": Decimal("1000")},
        },
        "symbols": ["AKEUSDT"],
        "runs": 1,
        "workers": 2,
        "worker_memory_budget": "4GB",
    }, default=str), encoding="utf-8")
    pd.DataFrame([
        {
            "run_id": "run-1",
            "symbol": "AKEUSDT",
            "status": "ok",
            "parameters": '{"lookback": 6}',
            "trades": 2,
            "net_pnl": 12.5,
        }
    ]).to_csv(root / "comparison.csv", index=False)
    pd.DataFrame([
        {
            "trade_id": "trade-1",
            "run_id": "run-1",
            "symbol": "AKEUSDT",
            "side": "SELL",
            "entry_time": 1_000,
            "entry_price": 1.25,
            "exit_time": 2_000,
            "net_pnl": 12.5,
            "parameters": '{"lookback": 6}',
            "tier_prices": "[1.1, 1.2, 1.3]",
            "tier_weights": "[0.2, 0.3, 0.5]",
            "prior_high": 1.2,
            "optional_metric": None,
        },
        {
            "trade_id": "trade-2",
            "run_id": "run-1",
            "symbol": "AKEUSDT",
            "side": "SELL",
            "entry_time": 3_000,
            "entry_price": 2.0,
            "exit_time": 4_000,
            "net_pnl": -1.0,
            "parameters": "{}",
            "tier_prices": "[2.0]",
            "tier_weights": "[1]",
            "prior_high": 1.9,
            "optional_metric": 7.0,
        },
    ]).to_csv(root / "all_trades.csv", index=False)
    pd.DataFrame([
        {"bucket": "profit", "trades": 1, "net_pnl": 12.5},
        {"bucket": "loss", "trades": 1, "net_pnl": -1.0},
    ]).to_csv(root / "pnl_bucket_summary.csv", index=False)
    pd.DataFrame([{"rule": "local", "win_rate": 0.5}]).to_csv(
        root / "rise_duration_validation_ad_hoc.csv", index=False
    )

    run = root / "runs" / "run-1"
    run.mkdir(parents=True)
    pq.write_table(pa.table({
        "order_id": ["order-1"],
        "price": pa.array([Decimal("1.2500")], type=pa.decimal128(10, 4)),
        "fill_time": pa.array(
            [pd.Timestamp("2026-07-01T00:00:00Z")],
            type=pa.timestamp("ms", tz="UTC"),
        ),
        "cancel_time": pa.array([None], type=pa.int64()),
    }), run / "orders.parquet")
    pq.write_table(pa.table({
        "fill_id": ["fill-1"],
        "price": [1.25],
    }), run / "fills.parquet")
    pq.write_table(pa.table({
        "event_type": ["signal_triggered"],
        "details": ['{"spike_high": 1.3, "notes": null}'],
    }), run / "audit_events.parquet")
    return root


def test_parses_metadata_runs_and_chunked_trades(tmp_path: Path):
    parser = ReportDirectoryParser(
        _write_report(tmp_path / "study"),
        csv_batch_size=1,
        parquet_batch_size=1,
    )

    assert parser.metadata.name == "small-study"
    assert parser.metadata.strategy_id == "spike_short"
    assert parser.metadata.symbols == ("AKEUSDT",)
    assert parser.metadata.extra == {"worker_memory_budget": "4GB"}

    run = next(parser.iter_runs())
    assert run.run_id == "run-1"
    assert run.parameters == {"lookback": 6}
    assert run.metrics == {"trades": 2, "net_pnl": 12.5}

    batches = list(parser.iter_trade_batches())
    assert [len(batch.records) for batch in batches] == [1, 1]
    first = batches[0].records[0]
    assert first.trade_id == "trade-1"
    assert first.parameters == {"lookback": 6}
    assert first.strategy_data["tier_prices"] == [1.1, 1.2, 1.3]
    assert first.strategy_data["tier_weights"] == [0.2, 0.3, 0.5]
    assert first.strategy_data["optional_metric"] is None
    assert first.strategy_data["prior_high"] == 1.2
    json.dumps(asdict(first))


def test_discovers_summary_and_ad_hoc_reports_with_original_columns(tmp_path: Path):
    parser = ReportDirectoryParser(_write_report(tmp_path / "study"), csv_batch_size=1)

    reports = list(parser.iter_reports())
    assert {report.filename for report in reports} == {
        "comparison.csv",
        "pnl_bucket_summary.csv",
        "rise_duration_validation_ad_hoc.csv",
    }
    pnl_batches = [
        report for report in reports if report.filename == "pnl_bucket_summary.csv"
    ]
    assert len(pnl_batches) == 2
    assert [column.key for column in pnl_batches[0].columns] == [
        "bucket", "trades", "net_pnl"
    ]
    assert pnl_batches[0].rows == ({
        "bucket": "profit", "trades": 1, "net_pnl": 12.5
    },)


def test_streams_parquet_and_normalises_json_time_decimal_and_null(tmp_path: Path):
    parser = ReportDirectoryParser(
        _write_report(tmp_path / "study"), parquet_batch_size=1
    )

    order = next(parser.iter_orders())
    assert order.run_id == "run-1"
    assert order.data["price"] == "1.2500"
    assert order.data["fill_time"] == "2026-07-01T00:00:00+00:00"
    assert order.data["cancel_time"] is None
    assert next(parser.iter_fills()).data["fill_id"] == "fill-1"
    event = next(parser.iter_events())
    assert event.data["details"] == {"spike_high": 1.3, "notes": None}

    json.dumps(asdict(order))
    json.dumps(asdict(event))


def test_strategy_id_falls_back_to_unknown(tmp_path: Path):
    parser = ReportDirectoryParser(
        _write_report(tmp_path / "study", strategy_id=None)
    )

    assert parser.metadata.strategy_id == "unknown"
    assert next(parser.iter_trades()).strategy_id == "unknown"


def test_accepts_empty_trades_and_empty_optional_report(tmp_path: Path):
    root = _write_report(tmp_path / "study")
    (root / "all_trades.csv").write_text("\n", encoding="utf-8")
    (root / "empty_ad_hoc.csv").write_text("\n", encoding="utf-8")
    parser = ReportDirectoryParser(root)

    assert list(parser.iter_trades()) == []
    empty = next(
        report
        for report in parser.iter_reports()
        if report.filename == "empty_ad_hoc.csv"
    )
    assert empty.columns == ()
    assert empty.rows == ()


@pytest.mark.parametrize(
    "missing",
    ["experiment.json", "comparison.csv", "all_trades.csv"],
)
def test_rejects_missing_required_root_file(tmp_path: Path, missing: str):
    root = _write_report(tmp_path / "study")
    (root / missing).unlink()

    with pytest.raises(ReportValidationError, match=missing):
        ReportDirectoryParser(root)


def test_rejects_missing_required_run_parquet(tmp_path: Path):
    root = _write_report(tmp_path / "study")
    (root / "runs" / "run-1" / "fills.parquet").unlink()

    with pytest.raises(ReportValidationError, match="runs/run-1/fills.parquet"):
        ReportDirectoryParser(root)


def test_ignores_stale_run_directory_not_listed_in_comparison(tmp_path: Path):
    root = _write_report(tmp_path / "study")
    (root / "runs" / "stale-interrupted-run").mkdir()

    parser = ReportDirectoryParser(root)

    assert [run.run_id for run in parser.iter_runs()] == ["run-1"]
    assert {order.run_id for order in parser.iter_orders()} == {"run-1"}


def test_rejects_invalid_json_field_when_streamed(tmp_path: Path):
    root = _write_report(tmp_path / "study")
    comparison = pd.read_csv(root / "comparison.csv")
    comparison.loc[0, "parameters"] = "{broken"
    comparison.to_csv(root / "comparison.csv", index=False)
    parser = ReportDirectoryParser(root)

    with pytest.raises(ReportValidationError, match="parameters"):
        list(parser.iter_runs())
