from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from trading_platform.backtest.funding import (
    FundingIncomeDataLoader,
    FundingIncomeEvent,
)


def _create_snapshot(path: Path) -> None:
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            """
            CREATE TABLE account_income_events (
                account_id VARCHAR,
                transaction_id BIGINT,
                income_type VARCHAR,
                symbol VARCHAR,
                asset VARCHAR,
                amount DECIMAL(30, 12),
                event_time TIMESTAMPTZ
            );
            CREATE TABLE account_income_coverage (
                account_id VARCHAR,
                income_type VARCHAR,
                symbol VARCHAR,
                start_time TIMESTAMPTZ,
                end_time TIMESTAMPTZ
            )
            """
        )
    finally:
        connection.close()


def _insert_coverage(
    path: Path,
    *,
    start_ms: int = 1_000,
    end_ms: int = 5_000,
    symbol: str = "BTCUSDT",
) -> None:
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            "INSERT INTO account_income_coverage VALUES "
            "('spike', 'FUNDING_FEE', ?, to_timestamp(? / 1000.0), "
            "to_timestamp(? / 1000.0))",
            [symbol, start_ms, end_ms],
        )
    finally:
        connection.close()


def _insert_event(
    path: Path,
    transaction_id: int,
    amount: str,
    event_time: int,
    *,
    account_id: str = "spike",
    income_type: str = "FUNDING_FEE",
    symbol: str = "BTCUSDT",
    asset: str = "USDT",
) -> None:
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            "INSERT INTO account_income_events VALUES "
            "(?, ?, ?, ?, ?, ?, to_timestamp(? / 1000.0))",
            [
                account_id,
                transaction_id,
                income_type,
                symbol,
                asset,
                amount,
                event_time,
            ],
        )
    finally:
        connection.close()


def test_loader_reads_signed_events_deduplicates_and_does_not_mutate_snapshot(
    tmp_path: Path,
):
    path = tmp_path / "funding.duckdb"
    _create_snapshot(path)
    _insert_coverage(path)
    _insert_event(path, 10, "0.25", 1_000)
    _insert_event(path, 11, "-1.25", 2_000)
    _insert_event(path, 11, "-1.25", 2_000)
    _insert_event(path, 12, "0.50", 4_000)
    _insert_event(path, 13, "99", 3_000, account_id="other")
    _insert_event(path, 14, "99", 3_000, symbol="ETHUSDT")
    _insert_event(path, 15, "99", 5_000)
    before = path.stat().st_mtime_ns

    events = FundingIncomeDataLoader(
        path,
        account_id="spike",
        symbols=["btcusdt"],
        start_ms=1_000,
        end_ms=5_000,
    ).load()

    assert events == [
        FundingIncomeEvent(10, "BTCUSDT", 1_000, amount=Decimal("0.25")),
        FundingIncomeEvent(11, "BTCUSDT", 2_000, amount=Decimal("-1.25")),
        FundingIncomeEvent(12, "BTCUSDT", 4_000, amount=Decimal("0.5")),
    ]
    assert path.stat().st_mtime_ns == before
    connection = duckdb.connect(str(path), read_only=True)
    try:
        assert connection.execute(
            "SELECT count(*) FROM account_income_events"
        ).fetchone() == (7,)
    finally:
        connection.close()


def test_loader_accepts_explicitly_covered_window_without_funding_events(
    tmp_path: Path,
):
    path = tmp_path / "funding.duckdb"
    _create_snapshot(path)
    _insert_coverage(path, start_ms=1_000, end_ms=3_000)
    _insert_coverage(path, start_ms=3_000, end_ms=5_000)

    assert FundingIncomeDataLoader(
        path,
        account_id="spike",
        symbols=["BTCUSDT"],
        start_ms=1_000,
        end_ms=5_000,
    ).load() == []


def test_loader_rejects_missing_or_gapped_coverage(tmp_path: Path):
    path = tmp_path / "funding.duckdb"
    _create_snapshot(path)
    _insert_coverage(path, start_ms=1_000, end_ms=2_000)
    _insert_coverage(path, start_ms=3_000, end_ms=5_000)

    with pytest.raises(ValueError, match="coverage is incomplete.*2000ms"):
        FundingIncomeDataLoader(
            path,
            account_id="spike",
            symbols=["BTCUSDT"],
            start_ms=1_000,
            end_ms=5_000,
        ).load()


def test_loader_rejects_conflicting_duplicate_transaction(tmp_path: Path):
    path = tmp_path / "funding.duckdb"
    _create_snapshot(path)
    _insert_coverage(path)
    _insert_event(path, 11, "-1.25", 2_000)
    _insert_event(path, 11, "-2.00", 2_000)

    with pytest.raises(ValueError, match="transaction 11 has conflicting facts"):
        FundingIncomeDataLoader(
            path,
            account_id="spike",
            symbols=["BTCUSDT"],
            start_ms=1_000,
            end_ms=5_000,
        ).load()


def test_loader_rejects_non_usdt_funding(tmp_path: Path):
    path = tmp_path / "funding.duckdb"
    _create_snapshot(path)
    _insert_coverage(path)
    _insert_event(path, 11, "-1.25", 2_000, asset="BNB")

    with pytest.raises(ValueError, match="not USDT-denominated"):
        FundingIncomeDataLoader(
            path,
            account_id="spike",
            symbols=["BTCUSDT"],
            start_ms=1_000,
            end_ms=5_000,
        ).load()


def test_loader_rejects_snapshot_without_required_tables(tmp_path: Path):
    path = tmp_path / "funding.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE unrelated (value INTEGER)")
    connection.close()

    with pytest.raises(ValueError, match="missing main.account_income_events"):
        FundingIncomeDataLoader(
            path,
            account_id="spike",
            symbols=["BTCUSDT"],
            start_ms=1_000,
            end_ms=5_000,
        ).load()
