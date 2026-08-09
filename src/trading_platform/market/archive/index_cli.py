from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from .index import build_archive_index, load_archive_index
from .parquet import create_duckdb_catalog


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild the Parquet archive sidecar index from file footers."
    )
    parser.add_argument("archive", type=Path, help="Parquet archive root")
    parser.add_argument(
        "--catalog",
        type=Path,
        required=True,
        help="DuckDB catalog to register with the rebuilt archive index",
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
    try:
        print(
            f"正在并行扫描 Parquet footer：workers={args.workers}",
            flush=True,
        )
        index_path = build_archive_index(args.archive, workers=args.workers)
        frame = load_archive_index(index_path)
        create_duckdb_catalog(args.archive, args.catalog)
    except KeyboardInterrupt:
        print("索引重建已停止。", flush=True)
        return 130
    print(
        f"归档索引已更新：分区={len(frame)}，索引={index_path}，"
        f"catalog={args.catalog}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
