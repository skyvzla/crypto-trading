"""Independent backtest research and chart review API."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import duckdb
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from trading_platform.backtest.candles import (
    fetch_binance_candles,
    load_archive_candles,
)
from trading_platform.ledger.db.backtest_repository import BacktestRepository


router = APIRouter(prefix="/api/v1", tags=["backtest-research"])


async def get_repository(request: Request) -> BacktestRepository:
    ledger_db = request.app.state.ledger_db
    if ledger_db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return BacktestRepository(ledger_db.pool)


def _page(items: list[dict[str, Any]], total: int, limit: int, offset: int) -> dict:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/backtest-researches")
async def list_researches(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repository: BacktestRepository = Depends(get_repository),
) -> dict:
    items, total = await repository.list_researches(limit=limit, offset=offset)
    return _page(items, total, limit, offset)


@router.get("/backtest-researches/{research_id}")
async def get_research(
    research_id: UUID,
    repository: BacktestRepository = Depends(get_repository),
) -> dict:
    item = await repository.get_research(research_id)
    if item is None:
        raise HTTPException(status_code=404, detail="backtest research not found")
    return item


@router.get("/backtest-researches/{research_id}/reports")
async def list_reports(
    research_id: UUID,
    repository: BacktestRepository = Depends(get_repository),
) -> dict:
    if await repository.get_research(research_id) is None:
        raise HTTPException(status_code=404, detail="backtest research not found")
    return {"items": await repository.list_reports(research_id)}


@router.get("/backtest-researches/{research_id}/reports/{report_type}")
async def get_report(
    research_id: UUID,
    report_type: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort_by: str | None = Query(None, max_length=128),
    sort_order: Literal["asc", "desc"] = "desc",
    repository: BacktestRepository = Depends(get_repository),
) -> dict:
    descriptor, rows = await repository.get_report(
        research_id, report_type, limit=limit, offset=offset,
        sort_by=sort_by, sort_order=sort_order,
    )
    if descriptor is None:
        raise HTTPException(status_code=404, detail="backtest report not found")
    return {
        "descriptor": descriptor,
        "columns": descriptor["columns"],
        "rows": rows,
        "total": descriptor["row_count"],
        "limit": limit,
        "offset": offset,
    }


@router.get("/backtest-researches/{research_id}/symbols")
async def list_symbols(
    research_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    repository: BacktestRepository = Depends(get_repository),
) -> dict:
    items, total = await repository.list_symbols(
        research_id, limit=limit, offset=offset
    )
    return _page(items, total, limit, offset)


@router.get("/backtest-researches/{research_id}/symbols/{symbol}/trades")
async def list_trades(
    research_id: UUID,
    symbol: str,
    winner: bool | None = None,
    exit_reason: str | None = Query(None, max_length=128),
    min_pnl: float | None = None,
    max_pnl: float | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    repository: BacktestRepository = Depends(get_repository),
) -> dict:
    items, total = await repository.list_trades(
        research_id,
        symbol.strip().upper(),
        winner=winner,
        exit_reason=exit_reason,
        min_pnl=min_pnl,
        max_pnl=max_pnl,
        limit=limit,
        offset=offset,
    )
    return _page(items, total, limit, offset)


@router.get("/backtest-researches/{research_id}/trades/{trade_id}")
async def get_trade(
    research_id: UUID,
    trade_id: UUID,
    repository: BacktestRepository = Depends(get_repository),
) -> dict:
    item = await repository.get_trade(research_id, trade_id)
    if item is None:
        raise HTTPException(status_code=404, detail="backtest trade not found")
    return item


@router.get("/backtest-researches/{research_id}/trades/{trade_id}/events")
async def get_trade_events(
    research_id: UUID,
    trade_id: UUID,
    repository: BacktestRepository = Depends(get_repository),
) -> dict:
    items = await repository.list_events(research_id, trade_id)
    if items is None:
        raise HTTPException(status_code=404, detail="backtest trade not found")
    return {"items": items}


@router.get("/backtest-strategies/{strategy_id}/schema")
async def get_strategy_schema(
    strategy_id: str,
    repository: BacktestRepository = Depends(get_repository),
) -> dict:
    item = await repository.get_strategy_schema(strategy_id)
    if item is None:
        raise HTTPException(status_code=404, detail="strategy schema not found")
    return {
        "strategy_id": item["strategy_id"],
        "schema_version": item["schema_version"],
        **item["descriptor"],
    }


@router.get("/backtest-candles")
async def get_backtest_candles(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    source: Literal["binance", "archive"] = "binance",
    research_id: UUID | None = None,
    repository: BacktestRepository = Depends(get_repository),
) -> dict:
    try:
        if source == "binance":
            candles = await fetch_binance_candles(
                symbol, interval, start_ms, end_ms
            )
        else:
            if research_id is None:
                raise HTTPException(
                    status_code=422,
                    detail="research_id is required for archive candles",
                )
            research = await repository.get_research(research_id)
            if research is None:
                raise HTTPException(
                    status_code=404, detail="backtest research not found"
                )
            normalized_symbol = symbol.strip().upper()
            if not await repository.has_symbol(research_id, normalized_symbol):
                raise HTTPException(
                    status_code=404, detail="symbol not found in backtest research"
                )
            config = research.get("config") or {}
            source_metadata = research.get("source_metadata") or {}
            index_path = (
                source_metadata.get("archive_index_path")
                or config.get("archive_index_path")
            )
            if index_path and not Path(str(index_path)).is_file():
                index_path = os.getenv("BACKTEST_ARCHIVE_INDEX_PATH", index_path)
            if not index_path:
                index_path = os.getenv("BACKTEST_ARCHIVE_INDEX_PATH")
            if not index_path:
                raise HTTPException(
                    status_code=409,
                    detail="research has no archive index reference",
                )
            candles = await run_in_threadpool(
                load_archive_candles,
                index_path,
                normalized_symbol,
                interval,
                start_ms,
                end_ms,
            )
    except HTTPException:
        raise
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502, detail=f"Binance market data unavailable: {error}"
        ) from error
    except (duckdb.Error, RuntimeError) as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    return {
        "symbol": symbol.strip().upper(),
        "interval": interval.strip().lower(),
        "source": source,
        "candles": candles,
    }
