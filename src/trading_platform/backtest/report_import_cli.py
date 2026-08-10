"""Import completed backtest reports into the research database."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from trading_platform.backtest.report_store import BacktestReportStore
from trading_platform.ledger.db.migrations import verify_current
from trading_platform.ledger.db.models import create_connection_pool
from trading_platform.shared.config import DatabaseConfig


async def import_report_directory(root: Path, dsn: str) -> str:
    pool = await create_connection_pool(dsn, min_size=1, max_size=2)
    try:
        await verify_current(pool)
        research_id = await BacktestReportStore(pool).import_directory(root)
        return str(research_id)
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import a completed backtest report into PostgreSQL"
    )
    parser.add_argument("report", type=Path)
    parser.add_argument("--dsn", help="defaults to DB_* environment settings")
    args = parser.parse_args(argv)
    research_id = asyncio.run(
        import_report_directory(args.report, args.dsn or DatabaseConfig().dsn)
    )
    print(f"回测研究已入库: id={research_id} report={args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
