"""账本查询与 subcategory 交易池准入 API。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from trading_platform.ledger.db.models import (
    CampaignPnLFactsError,
    ExchangeCategory,
    ExchangeSymbol,
    LedgerDB,
    StrategyCategoryAdmission,
    StrategyAuditRecord,
    SubcategoryAdmission,
    SymbolGlobalAdmission,
    VersionConflictError,
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
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: LedgerDB = Depends(get_db),
) -> Page:
    filters = _filter_kwargs(account_id, strategy_id, symbol)
    items = await db.get_orders(
        **filters, status=status, limit=limit, offset=offset
    )
    total = await db.count_orders(**filters, status=status)
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
    db: LedgerDB = Depends(get_db),
) -> Page:
    filters = _filter_kwargs(account_id, strategy_id, symbol)
    items = await db.get_trades(**filters, limit=limit, offset=offset)
    total = await db.count_trades(**filters)
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
    db: LedgerDB = Depends(get_db),
) -> Page:
    items, total = await db.list_exchange_symbols(limit, offset)
    return Page(
        items=[ExchangeSymbolResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
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


@router.put(
    "/strategy-category-admissions/{strategy_id}/{category_key}",
    response_model=StrategyCategoryAdmissionResponse,
)
async def set_strategy_category_admission(
    request: AdmissionRequest,
    strategy_id: str = Path(min_length=1, max_length=64),
    category_key: str = Path(min_length=1, max_length=192),
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
