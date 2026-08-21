from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from .index import build_archive_index, load_archive_index
from .metrics import load_metrics_index, publish_metrics_archive
from .parquet import create_duckdb_catalog, repair_mixed_candle_partitions
from .cli import _validate_distinct_archive_roots


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild candles and metrics Parquet sidecar indexes from file footers."
    )
    parser.add_argument("archive", type=Path, help="candles Parquet archive root")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="candles DuckDB catalog (default: <archive>/candles.duckdb)",
    )
    parser.add_argument(
        "--metrics-archive",
        type=Path,
        default=None,
        help="metrics Parquet root (default: sibling metrics/ directory)",
    )
    parser.add_argument(
        "--without-metrics",
        action="store_true",
        help="rebuild only the candles index and catalog",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="parallel footer readers (default: min(8, CPU count))",
    )
    args = parser.parse_args(argv)
    if args.workers <= 0:
        parser.error("--workers must be positive")
    catalog = args.catalog or args.archive / "candles.duckdb"
    metrics_archive = (
        args.metrics_archive or args.archive.resolve().parent / "metrics"
    )
    if not args.without_metrics:
        _validate_distinct_archive_roots(args.archive, metrics_archive)
    try:
        print(
            f"正在并行扫描 Parquet footer：workers={args.workers}",
            flush=True,
        )
        removed_partitions = repair_mixed_candle_partitions(args.archive)
        if removed_partitions:
            print(
                f"已删除混合月份中的非活动分区：{len(removed_partitions)}",
                flush=True,
            )
        index_path = build_archive_index(args.archive, workers=args.workers)
        frame = load_archive_index(index_path)
        create_duckdb_catalog(args.archive, catalog)
        metrics_index_path = None
        metrics_frame = None
        metrics_catalog = None
        if not args.without_metrics:
            metrics_catalog = metrics_archive / "metrics.duckdb"
            metrics_index_path, _ = publish_metrics_archive(
                metrics_archive,
                metrics_catalog,
                workers=args.workers,
            )
            metrics_frame = load_metrics_index(metrics_index_path)
    except KeyboardInterrupt:
        print("索引重建已停止。", flush=True)
        return 130
    print(
        f"归档索引已更新：分区={len(frame)}，索引={index_path}，"
        f"catalog={catalog}",
        flush=True,
    )
    if (
        metrics_frame is not None
        and metrics_index_path is not None
        and metrics_catalog is not None
    ):
        print(
            f"metrics 索引已更新：分区={metrics_frame.num_rows}，"
            f"索引={metrics_index_path}，catalog={metrics_catalog}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
