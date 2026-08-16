"""账本查询与 subcategory 交易池准入 API。"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from trading_platform.ledger.db.models import (
    CampaignPnLFactsError,
    ExchangeCategory,
    ExchangeSymbol,
    ExchangeSymbolSyncState,
    LedgerDB,
    PerformanceCampaignDimension,
    PerformanceCampaignFact,
    StrategyCategoryAdmission,
    StrategyAuditRecord,
    SubcategoryAdmission,
    SymbolGlobalAdmission,
    SymbolUniverseDecision,
    VersionConflictError,
)
from trading_platform.shared.symbol_universe_query import (
    SYMBOL_UNIVERSE_MAX_SYNC_AGE_HOURS,
)


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: str
    strategy_id: str
    symbol: str
    order_id: str
    client_order_id: str
    campaign_id: Optional[str] = None
    side: str
    order_type: str
    position_side: Optional[str] = None
    quantity: Decimal
    price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    status: str
    filled_quantity: Decimal
    avg_fill_price: Optional[Decimal] = None
    commission: Optional[Decimal] = None
    commission_asset: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    exchange_created_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None


class TradeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: str
    strategy_id: str
    symbol: str
    trade_id: str
    order_id: str
    client_order_id: str
    campaign_id: Optional[str] = None
    side: str
    position_side: Optional[str] = None
    quantity: Decimal
    price: Decimal
    quote_quantity: Decimal
    commission: Decimal
    commission_asset: str
    realized_pnl: Optional[Decimal] = None
    is_maker: bool
    created_at: datetime
    exchange_time: datetime


class PositionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: str
    strategy_id: str
    symbol: str
    position_side: str
    quantity: Decimal
    entry_price: Decimal
    mark_price: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    liquidation_price: Optional[Decimal] = None
    leverage: Optional[int] = None
    margin_type: Optional[str] = None
    isolated_margin: Optional[Decimal] = None
    exchange_time: Optional[datetime] = None
    updated_at: datetime


class Page(BaseModel):
    items: list[Any]
    total: int
    limit: int
    offset: int


class AdmissionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    subcategory: str
    enabled: bool
    version: int
    updated_at: datetime
    updated_by: str
    reason: Optional[str] = None


class AuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subcategory: str
    previous_enabled: Optional[bool]
    enabled: bool
    version: int
    changed_at: datetime
    changed_by: str
    reason: Optional[str] = None


class ExchangeSymbolResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    pair: str
    contract_type: str
    status: str
    onboard_date: Optional[datetime]
    delivery_date: Optional[datetime]
    base_asset: Optional[str]
    quote_asset: Optional[str]
    margin_asset: Optional[str]
    underlying_type: Optional[str]
    active: bool
    synced_at: datetime
    global_enabled: bool
    global_admission_version: int


class ExchangeCategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category_key: str
    source: str
    category_type: str
    code: str
    name: str
    parent_key: Optional[str]
    active: bool
    synced_at: datetime
    symbol_count: int = 0


class ExchangeSymbolSyncStatusResponse(BaseModel):
    initialized: bool
    status: str
    last_attempt_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    synced_symbols: int
    last_error: Optional[str] = None
    stale: bool
    effective_universe_ready: bool
    max_age_hours: int


class SymbolUniversePreviewItem(BaseModel):
    symbol: str
    effective: bool
    exclusion_reasons: list[str]
    blocked_category_keys: list[str]


class StrategyUniversePreviewResponse(BaseModel):
    strategy_id: str
    freeze_days: int
    total_symbols: int
    effective_symbols: int
    excluded_symbols: int
    items: list[SymbolUniversePreviewItem]
    limit: int
    offset: int


class SymbolGlobalAdmissionResponse(BaseModel):
    symbol: str
    enabled: bool
    version: int
    explicit: bool
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    reason: Optional[str] = None


class StrategyCategoryAdmissionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    strategy_id: str
    category_key: str
    enabled: bool
    version: int
    updated_at: datetime
    updated_by: str
    reason: Optional[str] = None


class SymbolGlobalAdmissionAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    previous_enabled: Optional[bool]
    enabled: bool
    version: int
    changed_at: datetime
    changed_by: str
    reason: Optional[str] = None


class StrategyCategoryAdmissionAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: str
    category_key: str
    previous_enabled: Optional[bool]
    enabled: bool
    version: int
    changed_at: datetime
    changed_by: str
    reason: Optional[str] = None


class StrategyAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_key: str
    account_id: str
    event_time: int
    event_type: str
    symbol: str
    strategy_id: str
    campaign_id: Optional[str] = None
    details: dict[str, Any]
    created_at: datetime


class StrategyRuntimeStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: str
    strategy_id: str
    instance_id: str
    mode: str
    status: str
    effective_status: str
    entry_enabled: bool
    halted: bool
    halt_reason: Optional[str] = None
    gate_conditions: dict[str, Any]
    started_at: datetime
    heartbeat_at: datetime
    stopped_at: Optional[datetime] = None


class AdmissionRequest(BaseModel):
    enabled: bool
    expected_version: int = Field(ge=0)
    updated_by: str = Field(min_length=1, max_length=128)
    reason: Optional[str] = None


class PnLResponse(BaseModel):
    account_id: str
    strategy_id: Optional[str] = None
    symbol: Optional[str] = None
    total_trades: int
    total_commission: Decimal
    total_realized_pnl: Decimal
    total_unrealized_pnl: Decimal
    net_pnl: Decimal
    win_count: int
    loss_count: int
    win_rate: float
    avg_win: Decimal
    avg_loss: Decimal


class DailyPnLResponse(BaseModel):
    """Named-timezone natural-day realized ledger PnL for the calendar view."""

    date: date
    account_id: str
    strategy_id: Optional[str] = None
    symbol: Optional[str] = None
    timezone: str
    trade_count: int
    realized_trade_count: int
    gross_realized_pnl: Decimal
    total_commission: Decimal
    commission_asset: Optional[str] = None
    net_pnl: Optional[Decimal] = None


class PerformanceResponse(BaseModel):
    """Campaign-level performance within the requested close-date window."""

    account_id: str
    strategy_id: Optional[str] = None
    symbol: Optional[str] = None
    start_date: date
    end_date: date
    timezone: str = "Asia/Shanghai"
    total_trades: int
    total_fills: int
    win_count: int
    loss_count: int
    flat_count: int
    win_rate: float
    avg_win: Decimal
    avg_loss: Decimal
    payoff_ratio: Optional[Decimal] = None
    expectancy: Decimal
    profit_factor: Optional[Decimal] = None
    total_commission: Decimal
    total_realized_pnl: Decimal
    net_pnl: Decimal
    max_drawdown: Decimal
    candidate_campaigns: int
    excluded_campaigns: int
    unattributed_fills: int
    metric_scope: str = (
        "closed campaigns with complete USDT PnL facts; "
        "closed_at within the requested calendar date range"
    )


PerformanceDimension = Literal[
    "symbol", "category", "subcategory", "side", "exit_reason"
]


class PerformanceBreakdownItem(BaseModel):
    dimension_key: Optional[str] = None
    dimension_label: Optional[str] = None
    total_trades: int
    total_fills: int
    win_count: int
    loss_count: int
    flat_count: int
    win_rate: float
    avg_win: Decimal
    avg_loss: Decimal
    payoff_ratio: Optional[Decimal] = None
    expectancy: Decimal
    profit_factor: Optional[Decimal] = None
    total_commission: Decimal
    total_realized_pnl: Decimal
    net_pnl: Decimal
    max_drawdown: Decimal
    candidate_campaigns: int
    excluded_campaigns: int


class PerformanceBreakdownResponse(BaseModel):
    """Campaign-level performance grouped by one authoritative dimension."""

    account_id: str
    strategy_id: Optional[str] = None
    symbol: Optional[str] = None
    category_key: Optional[str] = None
    subcategory_key: Optional[str] = None
    side: Optional[str] = None
    start_date: date
    end_date: date
    timezone: str = "Asia/Shanghai"
    group_by: PerformanceDimension
    dimension_available: bool
    dimension_note: Optional[str] = None
    available_dimensions: list[str]
    items: list[PerformanceBreakdownItem]
    metric_scope: str = (
        "closed campaigns with complete USDT PnL facts; "
        "closed_at within the requested calendar date range"
    )


class CampaignPnLResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: str
    strategy_id: str
    symbol: str
    campaign_id: str
    trade_count: int
    sell_quantity: Decimal
    sell_avg_price: Optional[Decimal] = None
    buy_quantity: Decimal
    buy_avg_price: Optional[Decimal] = None
    total_commission: Decimal
    commission_asset: Optional[str] = None
    gross_realized_pnl: Decimal
    net_realized_pnl: Decimal
    remaining_quantity: Decimal
    has_open_quantity: bool
    acquired_at: Optional[datetime] = None
    first_fill_at: datetime
    last_fill_at: datetime
    closed_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
    lifecycle_duration_ms: Optional[int] = None


router = APIRouter(prefix="/api/v1", tags=["ledger"])


async def get_db(request: Request) -> LedgerDB:
    db = getattr(request.app.state, "ledger_db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    return db


def _filter_kwargs(
    account_id: Optional[str],
    strategy_id: Optional[str],
    symbol: Optional[str],
) -> dict[str, Optional[str]]:
    return {
        "account_id": account_id,
        "strategy_id": strategy_id,
        "symbol": symbol,
    }


@router.get("/orders", response_model=Page)
async def get_orders(
    account_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    symbol: Optional[str] = None,
    status: Optional[str] = None,
    active_only: bool = Query(False, description="only NEW or PARTIALLY_FILLED orders"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: LedgerDB = Depends(get_db),
) -> Page:
    if active_only and status is not None:
        raise HTTPException(status_code=422, detail="status and active_only cannot be combined")
    filters = _filter_kwargs(account_id, strategy_id, symbol)
    items = await db.get_orders(
        **filters, status=status, active_only=active_only, limit=limit, offset=offset
    )
    total = await db.count_orders(**filters, status=status, active_only=active_only)
    return Page(
        items=[OrderResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/trades", response_model=Page)
async def get_trades(
    account_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    start_date: Optional[date] = Query(None, description="inclusive calendar date"),
    end_date: Optional[date] = Query(None, description="inclusive calendar date"),
    timezone_name: Literal["UTC", "Asia/Shanghai"] = Query(
        "Asia/Shanghai", alias="timezone", description="calendar date timezone"
    ),
    db: LedgerDB = Depends(get_db),
) -> Page:
    filters = _filter_kwargs(account_id, strategy_id, symbol)
    if (start_date is None) != (end_date is None):
        raise HTTPException(status_code=422, detail="start_date and end_date must be provided together")
    date_filters: dict[str, object] = {}
    if start_date is not None and end_date is not None:
        start_at, end_at = _date_bounds_in_timezone(start_date, end_date, timezone_name)
        date_filters = {"start_at": start_at, "end_at": end_at}
    items = await db.get_trades(**filters, **date_filters, limit=limit, offset=offset)
    total = await db.count_trades(**filters, **date_filters)
    return Page(
        items=[TradeResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/positions", response_model=Page)
async def get_positions(
    account_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: LedgerDB = Depends(get_db),
) -> Page:
    filters = _filter_kwargs(account_id, strategy_id, symbol)
    items = await db.get_positions(**filters, limit=limit, offset=offset)
    total = await db.count_positions(**filters)
    return Page(
        items=[PositionResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/pnl", response_model=PnLResponse)
async def get_pnl(
    account_id: str,
    strategy_id: Optional[str] = None,
    symbol: Optional[str] = None,
    db: LedgerDB = Depends(get_db),
) -> PnLResponse:
    values = await db.get_pnl_summary(account_id, strategy_id, symbol)
    realized = values["total_realized_pnl"]
    unrealized = values["total_unrealized_pnl"]
    commission = values["total_commission"]
    decided = values["win_count"] + values["loss_count"]
    return PnLResponse(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        net_pnl=realized + unrealized - commission,
        win_rate=values["win_count"] / decided if decided else 0.0,
        **values,
    )


def _date_bounds_in_timezone(
    start_date: date,
    end_date: date,
    timezone_name: str,
) -> tuple[datetime, datetime]:
    """Convert inclusive calendar dates to UTC half-open boundaries."""
    if end_date < start_date:
        raise HTTPException(
            status_code=422, detail="end_date must not be before start_date"
        )
    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:  # pragma: no cover - timezone is Literal constrained
        raise HTTPException(status_code=422, detail="unsupported timezone") from exc
    return (
        datetime.combine(start_date, time.min, tzinfo=zone).astimezone(timezone.utc),
        datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(
            timezone.utc
        ),
    )


@router.get("/pnl/daily", response_model=list[DailyPnLResponse])
async def get_daily_pnl(
    account_id: str = Query(min_length=1, max_length=32),
    start_date: date = Query(..., description="inclusive calendar date"),
    end_date: date = Query(..., description="inclusive calendar date"),
    timezone_name: Literal["UTC", "Asia/Shanghai"] = Query(
        "Asia/Shanghai", alias="timezone", description="calendar date timezone"
    ),
    strategy_id: Optional[str] = Query(None, max_length=64),
    symbol: Optional[str] = Query(None, max_length=32),
    db: LedgerDB = Depends(get_db),
) -> list[DailyPnLResponse]:
    start_at, end_at = _date_bounds_in_timezone(
        start_date, end_date, timezone_name
    )
    items = await db.list_daily_realized_pnl(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        start_at=start_at,
        end_at=end_at,
        timezone_name=timezone_name,
    )
    return [
        DailyPnLResponse(
            date=item.day,
            account_id=account_id,
            strategy_id=strategy_id,
            symbol=symbol,
            timezone=timezone_name,
            trade_count=item.trade_count,
            realized_trade_count=item.realized_trade_count,
            gross_realized_pnl=item.gross_realized_pnl,
            total_commission=item.total_commission,
            commission_asset=item.commission_asset,
            net_pnl=item.net_pnl,
        )
        for item in items
    ]


def _performance_values(
    facts: list[PerformanceCampaignFact],
    *,
    start_at: datetime,
    end_at: datetime,
) -> dict[str, Any]:
    """Calculate metrics from complete Campaign rows, never individual fills."""
    selected = [
        fact
        for fact in facts
        if (
            fact.closed_at is None
            or fact.closed_at >= start_at
        )
        and (fact.closed_at is None or fact.closed_at < end_at)
    ]
    eligible = [
        fact
        for fact in selected
        if fact.closed_at is not None
        and fact.realized_pnl_complete
        and fact.commission_asset == "USDT"
        and fact.unique_symbols == 1
        and fact.sell_quantity >= fact.buy_quantity
    ]
    net_values = [fact.gross_realized_pnl - fact.total_commission for fact in eligible]
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value < 0]
    flats = [value for value in net_values if value == 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))
    avg_win = gross_profit / len(wins) if wins else Decimal("0")
    avg_loss = gross_loss / len(losses) if losses else Decimal("0")

    running = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for value, _fact in sorted(
        zip(net_values, eligible), key=lambda pair: pair[1].closed_at  # type: ignore[arg-type]
    ):
        running += value
        peak = max(peak, running)
        max_drawdown = min(max_drawdown, running - peak)

    total_net = sum(net_values, Decimal("0"))
    decided = len(wins) + len(losses)
    return {
        "total_trades": len(eligible),
        "total_fills": sum(fact.trade_count for fact in eligible),
        "win_count": len(wins),
        "loss_count": len(losses),
        "flat_count": len(flats),
        "win_rate": len(wins) / decided if decided else 0.0,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": avg_win / avg_loss if avg_loss else None,
        "expectancy": total_net / len(eligible) if eligible else Decimal("0"),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "total_commission": sum(
            (fact.total_commission for fact in eligible), Decimal("0")
        ),
        "total_realized_pnl": sum(
            (fact.gross_realized_pnl for fact in eligible), Decimal("0")
        ),
        "net_pnl": total_net,
        "max_drawdown": max_drawdown,
        "candidate_campaigns": len(selected),
        "excluded_campaigns": len(selected) - len(eligible),
    }


def _performance_breakdown_values(
    dimensions: list[PerformanceCampaignDimension],
    *,
    start_at: datetime,
    end_at: datetime,
) -> list[PerformanceBreakdownItem]:
    grouped: dict[tuple[Optional[str], Optional[str]], list[PerformanceCampaignFact]] = {}
    for item in dimensions:
        grouped.setdefault(
            (item.dimension_key, item.dimension_label), []
        ).append(item.campaign)

    rows: list[PerformanceBreakdownItem] = []
    for (dimension_key, dimension_label), facts in sorted(
        grouped.items(), key=lambda pair: (pair[0][0] is None, pair[0][0] or "")
    ):
        rows.append(
            PerformanceBreakdownItem(
                dimension_key=dimension_key,
                dimension_label=dimension_label,
                **_performance_values(facts, start_at=start_at, end_at=end_at),
            )
        )
    return rows


@router.get("/performance", response_model=PerformanceResponse)
async def get_performance(
    account_id: str = Query(min_length=1, max_length=32),
    strategy_id: Optional[str] = Query(None, max_length=64),
    symbol: Optional[str] = Query(None, max_length=32),
    start_date: date = Query(..., description="inclusive close date"),
    end_date: date = Query(..., description="inclusive close date"),
    timezone_name: Literal["UTC", "Asia/Shanghai"] = Query(
        "Asia/Shanghai", alias="timezone", description="close date timezone"
    ),
    db: LedgerDB = Depends(get_db),
) -> PerformanceResponse:
    start_at, end_at = _date_bounds_in_timezone(
        start_date, end_date, timezone_name
    )
    facts = await db.list_performance_campaign_facts(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        start_at=start_at,
        end_at=end_at,
    )
    unattributed = await db.count_unattributed_trades(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        start_at=start_at,
        end_at=end_at,
    )
    values = _performance_values(facts, start_at=start_at, end_at=end_at)
    return PerformanceResponse(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        timezone=timezone_name,
        unattributed_fills=unattributed,
        **values,
    )


@router.get(
    "/performance/breakdown",
    response_model=PerformanceBreakdownResponse,
)
async def get_performance_breakdown(
    account_id: str = Query(min_length=1, max_length=32),
    strategy_id: Optional[str] = Query(None, max_length=64),
    symbol: Optional[str] = Query(None, max_length=32),
    category_key: Optional[str] = Query(None, max_length=256),
    subcategory_key: Optional[str] = Query(None, max_length=256),
    side: Optional[Literal["LONG", "SHORT"]] = Query(None),
    exit_reason: Optional[str] = Query(None, max_length=128),
    start_date: date = Query(..., description="inclusive close date"),
    end_date: date = Query(..., description="inclusive close date"),
    timezone_name: Literal["UTC", "Asia/Shanghai"] = Query(
        "Asia/Shanghai", alias="timezone", description="close date timezone"
    ),
    group_by: PerformanceDimension = Query("symbol"),
    db: LedgerDB = Depends(get_db),
) -> PerformanceBreakdownResponse:
    start_at, end_at = _date_bounds_in_timezone(
        start_date, end_date, timezone_name
    )
    available_dimensions = ["symbol", "category", "subcategory", "side"]
    if group_by == "exit_reason" or exit_reason is not None:
        return PerformanceBreakdownResponse(
            account_id=account_id,
            strategy_id=strategy_id,
            symbol=symbol,
            category_key=category_key,
            subcategory_key=subcategory_key,
            side=side,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone_name,
            group_by=group_by,
            dimension_available=False,
            dimension_note=(
                "账本 trades 未持久化规范化 exit_reason；当前不能可靠按退出原因聚合。"
            ),
            available_dimensions=available_dimensions,
            items=[],
        )
    try:
        dimensions = await db.list_performance_campaign_dimensions(
            account_id=account_id,
            strategy_id=strategy_id,
            symbol=symbol,
            category_key=category_key,
            subcategory_key=subcategory_key,
            side=side,
            start_at=start_at,
            end_at=end_at,
            group_by=group_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PerformanceBreakdownResponse(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        category_key=category_key,
        subcategory_key=subcategory_key,
        side=side,
        start_date=start_date,
        end_date=end_date,
        timezone=timezone_name,
        group_by=group_by,
        dimension_available=True,
        available_dimensions=available_dimensions,
        items=_performance_breakdown_values(
            dimensions, start_at=start_at, end_at=end_at
        ),
    )


@router.get(
    "/campaigns/{campaign_id}/pnl",
    response_model=CampaignPnLResponse,
)
async def get_campaign_pnl(
    campaign_id: str = Path(min_length=1, max_length=128),
    account_id: str = Query(min_length=1, max_length=32),
    strategy_id: str = Query(min_length=1, max_length=64),
    db: LedgerDB = Depends(get_db),
) -> CampaignPnLResponse:
    try:
        item = await db.get_campaign_pnl(
            account_id=account_id,
            strategy_id=strategy_id,
            campaign_id=campaign_id,
        )
    except CampaignPnLFactsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Campaign trades not found")
    return CampaignPnLResponse.model_validate(item)


@router.get("/exchange-symbols", response_model=Page)
async def list_exchange_symbols(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    unclassified: bool = Query(
        False,
        description="only symbols without an active Category/Subcategory assignment",
    ),
    db: LedgerDB = Depends(get_db),
) -> Page:
    items, total = await db.list_exchange_symbols(
        limit, offset, unclassified=unclassified
    )
    return Page(
        items=[ExchangeSymbolResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/exchange-symbol-sync/status",
    response_model=ExchangeSymbolSyncStatusResponse,
)
async def get_exchange_symbol_sync_status(
    db: LedgerDB = Depends(get_db),
) -> ExchangeSymbolSyncStatusResponse:
    state: Optional[
        ExchangeSymbolSyncState
    ] = await db.get_exchange_symbol_sync_state()
    if state is None:
        return ExchangeSymbolSyncStatusResponse(
            initialized=False,
            status="NEVER",
            synced_symbols=0,
            stale=True,
            effective_universe_ready=False,
            max_age_hours=SYMBOL_UNIVERSE_MAX_SYNC_AGE_HOURS,
        )
    return ExchangeSymbolSyncStatusResponse(
        initialized=True,
        max_age_hours=SYMBOL_UNIVERSE_MAX_SYNC_AGE_HOURS,
        **state.__dict__,
    )


@router.get(
    "/exchange-symbols/{symbol}/categories",
    response_model=list[ExchangeCategoryResponse],
)
async def list_exchange_symbol_categories(
    symbol: str = Path(min_length=1, max_length=32),
    db: LedgerDB = Depends(get_db),
) -> list[ExchangeCategoryResponse]:
    if await db.get_exchange_symbol(symbol) is None:
        raise HTTPException(status_code=404, detail="exchange symbol not found")
    items = await db.list_exchange_symbol_categories(symbol)
    return [ExchangeCategoryResponse.model_validate(item) for item in items]


@router.get(
    "/exchange-symbols/{symbol}/admission",
    response_model=SymbolGlobalAdmissionResponse,
)
async def get_symbol_global_admission(
    symbol: str = Path(min_length=1, max_length=32),
    db: LedgerDB = Depends(get_db),
) -> SymbolGlobalAdmissionResponse:
    exchange_symbol = await db.get_exchange_symbol(symbol)
    if exchange_symbol is None:
        raise HTTPException(status_code=404, detail="exchange symbol not found")
    item = await db.get_symbol_global_admission(symbol)
    if item is None:
        return SymbolGlobalAdmissionResponse(
            symbol=exchange_symbol.symbol,
            enabled=True,
            version=0,
            explicit=False,
        )
    return SymbolGlobalAdmissionResponse(**item.__dict__, explicit=True)


@router.put(
    "/exchange-symbols/{symbol}/admission",
    response_model=SymbolGlobalAdmissionResponse,
)
async def set_symbol_global_admission(
    request: AdmissionRequest,
    symbol: str = Path(min_length=1, max_length=32),
    db: LedgerDB = Depends(get_db),
) -> SymbolGlobalAdmissionResponse:
    if await db.get_exchange_symbol(symbol) is None:
        raise HTTPException(status_code=404, detail="exchange symbol not found")
    try:
        item: SymbolGlobalAdmission = await db.set_symbol_global_admission(
            symbol,
            request.enabled,
            request.expected_version,
            request.updated_by,
            request.reason,
        )
    except VersionConflictError as exc:
        raise HTTPException(
            status_code=409, detail="symbol admission version conflict"
        ) from exc
    return SymbolGlobalAdmissionResponse(**item.__dict__, explicit=True)


@router.get(
    "/exchange-categories",
    response_model=list[ExchangeCategoryResponse],
)
async def list_exchange_categories(
    active_only: bool = True,
    db: LedgerDB = Depends(get_db),
) -> list[ExchangeCategoryResponse]:
    items = await db.list_exchange_categories(active_only=active_only)
    return [ExchangeCategoryResponse.model_validate(item) for item in items]


@router.get(
    "/exchange-categories/{category_key}/symbols",
    response_model=Page,
)
async def list_exchange_category_symbols(
    category_key: str = Path(min_length=1, max_length=256),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: LedgerDB = Depends(get_db),
) -> Page:
    if await db.get_exchange_category(category_key) is None:
        raise HTTPException(status_code=404, detail="exchange category not found")
    items, total = await db.list_exchange_category_symbols(
        category_key, limit=limit, offset=offset
    )
    return Page(
        items=[ExchangeSymbolResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/symbol-global-admission-audit", response_model=Page)
async def list_symbol_global_admission_audit(
    symbol: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: LedgerDB = Depends(get_db),
) -> Page:
    items, total = await db.list_symbol_global_admission_audit(
        symbol, limit, offset
    )
    return Page(
        items=[
            SymbolGlobalAdmissionAuditResponse.model_validate(item)
            for item in items
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/strategy-category-admissions/{strategy_id}",
    response_model=list[StrategyCategoryAdmissionResponse],
)
async def list_strategy_category_admissions(
    strategy_id: str = Path(min_length=1, max_length=64),
    db: LedgerDB = Depends(get_db),
) -> list[StrategyCategoryAdmissionResponse]:
    items = await db.list_strategy_category_admissions(strategy_id)
    return [StrategyCategoryAdmissionResponse.model_validate(item) for item in items]


def _universe_exclusion_reasons(
    decision: SymbolUniverseDecision,
) -> list[str]:
    reasons: list[str] = []
    if not decision.sync_ready:
        reasons.append("SYNC_UNAVAILABLE_OR_STALE")
    if not decision.symbol_active:
        reasons.append("SYMBOL_INACTIVE")
    if not decision.perpetual_contract:
        reasons.append("NOT_PERPETUAL")
    if not decision.trading_status:
        reasons.append("NOT_TRADING")
    if not decision.onboarded:
        reasons.append("NOT_ONBOARDED")
    if not decision.delivery_window_open:
        reasons.append("DELIVERY_FREEZE_WINDOW")
    if not decision.global_enabled:
        reasons.append("GLOBAL_DISABLED")
    if decision.blocked_category_keys:
        reasons.append("STRATEGY_CATEGORY_DISABLED")
    return reasons


@router.get(
    "/strategy-category-admissions/{strategy_id}/universe-preview",
    response_model=StrategyUniversePreviewResponse,
)
async def get_strategy_universe_preview(
    strategy_id: str = Path(min_length=1, max_length=64),
    freeze_days: int = Query(15, ge=0, le=3650),
    effective: Optional[bool] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: LedgerDB = Depends(get_db),
) -> StrategyUniversePreviewResponse:
    items, total_symbols, effective_symbols = (
        await db.list_strategy_symbol_universe_preview(
            strategy_id=strategy_id,
            freeze_days=freeze_days,
            effective=effective,
            limit=limit,
            offset=offset,
        )
    )
    return StrategyUniversePreviewResponse(
        strategy_id=strategy_id,
        freeze_days=freeze_days,
        total_symbols=total_symbols,
        effective_symbols=effective_symbols,
        excluded_symbols=total_symbols - effective_symbols,
        items=[
            SymbolUniversePreviewItem(
                symbol=item.symbol,
                effective=item.effective,
                exclusion_reasons=_universe_exclusion_reasons(item),
                blocked_category_keys=item.blocked_category_keys,
            )
            for item in items
        ],
        limit=limit,
        offset=offset,
    )


@router.put(
    "/strategy-category-admissions/{strategy_id}/{category_key}",
    response_model=StrategyCategoryAdmissionResponse,
)
async def set_strategy_category_admission(
    request: AdmissionRequest,
    strategy_id: str = Path(min_length=1, max_length=64),
    category_key: str = Path(min_length=1, max_length=256),
    db: LedgerDB = Depends(get_db),
) -> StrategyCategoryAdmissionResponse:
    category = await db.get_exchange_category(category_key)
    if category is None:
        raise HTTPException(status_code=404, detail="exchange category not found")
    try:
        item: StrategyCategoryAdmission = (
            await db.set_strategy_category_admission(
                strategy_id,
                category_key,
                request.enabled,
                request.expected_version,
                request.updated_by,
                request.reason,
            )
        )
    except VersionConflictError as exc:
        raise HTTPException(
            status_code=409, detail="strategy category admission version conflict"
        ) from exc
    return StrategyCategoryAdmissionResponse.model_validate(item)


@router.get("/strategy-category-admission-audit", response_model=Page)
async def list_strategy_category_admission_audit(
    strategy_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: LedgerDB = Depends(get_db),
) -> Page:
    items, total = await db.list_strategy_category_admission_audit(
        strategy_id, limit, offset
    )
    return Page(
        items=[
            StrategyCategoryAdmissionAuditResponse.model_validate(item)
            for item in items
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/subcategory-admissions", response_model=Page)
async def list_admissions(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: LedgerDB = Depends(get_db),
) -> Page:
    items, total = await db.list_subcategory_admissions(limit, offset)
    return Page(
        items=[AdmissionResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/subcategory-admissions/{subcategory}",
    response_model=AdmissionResponse,
)
async def get_admission(
    subcategory: str = Path(min_length=1, max_length=64),
    db: LedgerDB = Depends(get_db),
) -> AdmissionResponse:
    item = await db.get_subcategory_admission(subcategory)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail="subcategory admission not found",
        )
    return AdmissionResponse.model_validate(item)


@router.put(
    "/subcategory-admissions/{subcategory}",
    response_model=AdmissionResponse,
)
async def set_admission(
    request: AdmissionRequest,
    subcategory: str = Path(min_length=1, max_length=64),
    db: LedgerDB = Depends(get_db),
) -> AdmissionResponse:
    try:
        item: SubcategoryAdmission = await db.set_subcategory_admission(
            subcategory,
            request.enabled,
            request.expected_version,
            request.updated_by,
            request.reason,
        )
    except VersionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="subcategory admission version conflict",
        ) from exc
    return AdmissionResponse.model_validate(item)


@router.get("/subcategory-admission-audit", response_model=Page)
async def list_audit(
    subcategory: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: LedgerDB = Depends(get_db),
) -> Page:
    items, total = await db.list_subcategory_audit(subcategory, limit, offset)
    return Page(
        items=[AuditResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/strategy-audit-events", response_model=Page)
async def list_strategy_audit_events(
    account_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    symbol: Optional[str] = None,
    event_type: Optional[str] = None,
    campaign_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: LedgerDB = Depends(get_db),
) -> Page:
    items, total = await db.list_strategy_audit_events(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        event_type=event_type,
        campaign_id=campaign_id,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[StrategyAuditResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/strategy-runtime-status", response_model=Page)
async def list_strategy_runtime_status(
    account_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: LedgerDB = Depends(get_db),
) -> Page:
    items, total = await db.list_strategy_runtime_statuses(
        account_id=account_id,
        strategy_id=strategy_id,
        limit=limit,
        offset=offset,
    )
    stale_before = datetime.now(timezone.utc) - timedelta(seconds=15)
    responses = []
    for item in items:
        effective_status = item.status
        if (
            item.status in {"running", "degraded"}
            and item.heartbeat_at is not None
            and item.heartbeat_at < stale_before
        ):
            effective_status = "stale"
        values = item.__dict__.copy()
        values["effective_status"] = effective_status
        responses.append(StrategyRuntimeStatusResponse.model_validate(values))
    return Page(items=responses, total=total, limit=limit, offset=offset)


@router.get("/health")
async def health_check(db: LedgerDB = Depends(get_db)) -> dict[str, str]:
    try:
        await db.is_healthy()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {
        "status": "healthy",
        "service": "ledger",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
