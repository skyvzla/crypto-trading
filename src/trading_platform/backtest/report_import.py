"""Parse a completed backtest report directory without persisting it."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Generic, Iterator, Mapping, TypeVar

import pandas as pd
import pyarrow.parquet as pq


class ReportValidationError(ValueError):
    """The report directory is incomplete or contains invalid data."""


@dataclass(frozen=True)
class ResearchMetadata:
    name: str
    strategy_id: str
    config: dict[str, Any]
    symbols: tuple[str, ...]
    run_count: int
    workers: int | None
    source_path: str
    extra: dict[str, Any]


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    strategy_id: str
    symbol: str | None
    status: str | None
    parameters: Any
    metrics: dict[str, Any]


TRADE_CORE_FIELDS = (
    "trade_id",
    "run_id",
    "symbol",
    "side",
    "campaign_id",
    "signal_time",
    "entry_time",
    "entry_price",
    "entry_quantity",
    "entry_notional",
    "entry_fill_count",
    "exit_time",
    "exit_price",
    "exit_quantity",
    "exit_fill_count",
    "exit_reason",
    "status",
    "gross_pnl",
    "commission",
    "net_pnl",
    "net_return",
    "winner",
    "parameters",
)


@dataclass(frozen=True)
class TradeRecord:
    strategy_id: str
    trade_id: Any
    run_id: Any
    symbol: Any
    side: Any
    campaign_id: Any
    signal_time: Any
    entry_time: Any
    entry_price: Any
    entry_quantity: Any
    entry_notional: Any
    entry_fill_count: Any
    exit_time: Any
    exit_price: Any
    exit_quantity: Any
    exit_fill_count: Any
    exit_reason: Any
    status: Any
    gross_pnl: Any
    commission: Any
    net_pnl: Any
    net_return: Any
    winner: Any
    parameters: Any
    strategy_data: dict[str, Any]


@dataclass(frozen=True)
class ReportColumn:
    key: str
    dtype: str


@dataclass(frozen=True)
class ReportRecord:
    name: str
    filename: str
    columns: tuple[ReportColumn, ...]
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class OrderRecord:
    run_id: str
    data: dict[str, Any]


@dataclass(frozen=True)
class FillRecord:
    run_id: str
    data: dict[str, Any]


@dataclass(frozen=True)
class EventRecord:
    run_id: str
    data: dict[str, Any]


RecordT = TypeVar("RecordT")


@dataclass(frozen=True)
class RecordBatch(Generic[RecordT]):
    records: tuple[RecordT, ...]


_REQUIRED_ROOT_FILES = ("experiment.json", "comparison.csv", "all_trades.csv")
_REQUIRED_RUN_FILES = ("orders.parquet", "fills.parquet", "audit_events.parquet")
_NON_REPORT_CSV_FILES = {"all_trades.csv"}
_JSON_FIELDS = {"parameters", "tier_prices", "tier_weights", "details"}


class ReportDirectoryParser:
    """Lazy, neutral representation of one ``reports/<experiment>`` directory."""

    def __init__(
        self,
        root: str | Path,
        *,
        csv_batch_size: int = 10_000,
        parquet_batch_size: int = 10_000,
    ) -> None:
        if csv_batch_size < 1 or parquet_batch_size < 1:
            raise ValueError("batch sizes must be positive")
        self.root = Path(root).resolve()
        self.csv_batch_size = csv_batch_size
        self.parquet_batch_size = parquet_batch_size
        self._run_directories: tuple[Path, ...] = ()
        self._validate()
        self.metadata = self._read_metadata()

    def _validate(self) -> None:
        if not self.root.is_dir():
            raise ReportValidationError(f"report directory does not exist: {self.root}")

        missing = [name for name in _REQUIRED_ROOT_FILES if not (self.root / name).is_file()]
        runs_root = self.root / "runs"
        if not runs_root.is_dir():
            missing.append("runs/")
        if missing:
            raise ReportValidationError(
                "missing required report files: " + ", ".join(missing)
            )

        comparison_path = self.root / "comparison.csv"
        try:
            run_ids = [
                str(run_id)
                for frame in pd.read_csv(
                    comparison_path,
                    usecols=["run_id"],
                    chunksize=self.csv_batch_size,
                )
                for run_id in frame["run_id"].tolist()
            ]
        except (pd.errors.EmptyDataError, ValueError) as error:
            raise ReportValidationError(
                f"comparison.csv must contain a run_id column: {error}"
            ) from error
        if not run_ids:
            raise ReportValidationError("comparison.csv contains no runs")
        if len(run_ids) != len(set(run_ids)):
            raise ReportValidationError("comparison.csv contains duplicate run_id values")

        invalid_run_ids = [
            run_id
            for run_id in run_ids
            if not run_id
            or run_id.casefold() in {"nan", "none"}
            or run_id in {".", ".."}
            or Path(run_id).name != run_id
            or Path(run_id).is_absolute()
        ]
        if invalid_run_ids:
            raise ReportValidationError(
                "comparison.csv contains invalid run_id values: "
                + ", ".join(invalid_run_ids[:5])
            )
        run_directories = tuple(runs_root / run_id for run_id in run_ids)
        missing_run_dirs = [
            str(path.relative_to(self.root))
            for path in run_directories
            if not path.is_dir()
        ]
        if missing_run_dirs:
            raise ReportValidationError(
                "missing required run directories: " + ", ".join(missing_run_dirs)
            )
        missing_run_files = [
            str(path.relative_to(self.root) / filename)
            for path in run_directories
            for filename in _REQUIRED_RUN_FILES
            if not (path / filename).is_file()
        ]
        if missing_run_files:
            raise ReportValidationError(
                "missing required run files: " + ", ".join(missing_run_files)
            )
        self._run_directories = run_directories

    def _read_metadata(self) -> ResearchMetadata:
        path = self.root / "experiment.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReportValidationError(f"invalid experiment.json: {error}") from error
        if not isinstance(raw, dict):
            raise ReportValidationError("experiment.json must contain an object")

        config = raw.get("config", {})
        if not isinstance(config, dict):
            raise ReportValidationError("experiment.json config must contain an object")
        universe = config.get("universe", {})
        strategy_id = (
            universe.get("strategy_id", "unknown")
            if isinstance(universe, dict)
            else "unknown"
        )
        symbols = raw.get("symbols", [])
        if not isinstance(symbols, list):
            raise ReportValidationError("experiment.json symbols must contain an array")
        known = {"config", "symbols", "runs", "workers"}
        return ResearchMetadata(
            name=str(config.get("name") or self.root.name),
            strategy_id=str(strategy_id or "unknown"),
            config=_normalise(config),
            symbols=tuple(str(symbol) for symbol in symbols),
            run_count=int(raw.get("runs", len(self._run_directories))),
            workers=_optional_int(raw.get("workers")),
            source_path=str(self.root),
            extra=_normalise({key: value for key, value in raw.items() if key not in known}),
        )

    def iter_run_batches(self) -> Iterator[RecordBatch[RunRecord]]:
        path = self.root / "comparison.csv"
        for frame in _read_csv_chunks(path, self.csv_batch_size):
            records = []
            for row in _frame_records(frame, path):
                records.append(RunRecord(
                    run_id=str(row.pop("run_id", "")),
                    strategy_id=self.metadata.strategy_id,
                    symbol=_optional_str(row.pop("symbol", None)),
                    status=_optional_str(row.pop("status", None)),
                    parameters=row.pop("parameters", None),
                    metrics=row,
                ))
            yield RecordBatch(tuple(records))

    def iter_runs(self) -> Iterator[RunRecord]:
        for batch in self.iter_run_batches():
            yield from batch.records

    def iter_trade_batches(self) -> Iterator[RecordBatch[TradeRecord]]:
        path = self.root / "all_trades.csv"
        for frame in _read_csv_chunks(path, self.csv_batch_size, allow_empty=True):
            records = []
            for row in _frame_records(frame, path):
                core = {field: row.pop(field, None) for field in TRADE_CORE_FIELDS}
                records.append(TradeRecord(
                    strategy_id=self.metadata.strategy_id,
                    **core,
                    strategy_data=row,
                ))
            yield RecordBatch(tuple(records))

    def iter_trades(self) -> Iterator[TradeRecord]:
        for batch in self.iter_trade_batches():
            yield from batch.records

    def iter_reports(self) -> Iterator[ReportRecord]:
        for path in sorted(self.root.glob("*.csv")):
            if path.name in _NON_REPORT_CSV_FILES:
                continue
            first = True
            for frame in _read_csv_chunks(path, self.csv_batch_size, allow_empty=True):
                columns = tuple(
                    ReportColumn(key=str(key), dtype=str(dtype))
                    for key, dtype in frame.dtypes.items()
                )
                first = False
                yield ReportRecord(
                    name=path.stem,
                    filename=path.name,
                    columns=columns,
                    rows=tuple(_frame_records(frame, path)),
                )
            if first:
                try:
                    header = pd.read_csv(path, nrows=0)
                except pd.errors.EmptyDataError:
                    header = pd.DataFrame()
                yield ReportRecord(
                    name=path.stem,
                    filename=path.name,
                    columns=tuple(
                        ReportColumn(key=str(key), dtype=str(dtype))
                        for key, dtype in header.dtypes.items()
                    ),
                    rows=(),
                )

    def iter_order_batches(self) -> Iterator[RecordBatch[OrderRecord]]:
        yield from self._iter_parquet_batches("orders.parquet", OrderRecord)

    def iter_orders(self) -> Iterator[OrderRecord]:
        for batch in self.iter_order_batches():
            yield from batch.records

    def iter_fill_batches(self) -> Iterator[RecordBatch[FillRecord]]:
        yield from self._iter_parquet_batches("fills.parquet", FillRecord)

    def iter_fills(self) -> Iterator[FillRecord]:
        for batch in self.iter_fill_batches():
            yield from batch.records

    def iter_event_batches(self) -> Iterator[RecordBatch[EventRecord]]:
        yield from self._iter_parquet_batches("audit_events.parquet", EventRecord)

    def iter_events(self) -> Iterator[EventRecord]:
        for batch in self.iter_event_batches():
            yield from batch.records

    def _iter_parquet_batches(
        self,
        filename: str,
        record_type: type[RecordT],
    ) -> Iterator[RecordBatch[RecordT]]:
        for run_dir in self._run_directories:
            path = run_dir / filename
            parquet = pq.ParquetFile(path)
            for arrow_batch in parquet.iter_batches(batch_size=self.parquet_batch_size):
                records = tuple(
                    record_type(run_id=run_dir.name, data=_normalise_json_fields(row, path))
                    for row in arrow_batch.to_pylist()
                )
                yield RecordBatch(records)


def _frame_records(frame: pd.DataFrame, path: Path) -> Iterator[dict[str, Any]]:
    for row in frame.to_dict(orient="records"):
        yield _normalise_json_fields(row, path)


def _read_csv_chunks(
    path: Path,
    batch_size: int,
    *,
    allow_empty: bool = False,
) -> Iterator[pd.DataFrame]:
    try:
        yield from pd.read_csv(path, chunksize=batch_size)
    except pd.errors.EmptyDataError:
        if not allow_empty:
            raise ReportValidationError(f"required CSV has no columns: {path}") from None


def _normalise_json_fields(row: Mapping[str, Any], path: Path) -> dict[str, Any]:
    result = {str(key): _normalise(value) for key, value in row.items()}
    for field in _JSON_FIELDS.intersection(result):
        value = result[field]
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            result[field] = _normalise(json.loads(value))
        except json.JSONDecodeError as error:
            raise ReportValidationError(
                f"invalid JSON field {field!r} in {path}: {error}"
            ) from error
    return result


def _normalise(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _normalise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
