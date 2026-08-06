"""
账本层 FastAPI 路由
提供查询接口、紧急控制、配置管理
"""
from typing import Optional, List, Dict, Any
from decimal import Decimal
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Body, Depends
from pydantic import BaseModel, ConfigDict, Field

from trading_platform.ledger.db.models import (
    LedgerDB,
    Order,
    Trade,
    Position,
    AccountControlState,
    StrategyConfig,
)


# ============ 请求/响应模型 ============

class OrderResponse(BaseModel):
    """订单响应"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: str
    strategy_id: str
    symbol: str
    order_id: str
    client_order_id: str
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
    """成交响应"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: str
    strategy_id: str
    symbol: str
    trade_id: str
    order_id: str
    client_order_id: str
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
    """持仓响应"""
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
    updated_at: datetime

class PnLSummary(BaseModel):
    """盈亏统计"""
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


class ControlStateRequest(BaseModel):
    """控制状态更新请求"""
    desired_state: str = Field(..., description="NORMAL, HALT_NEW, CANCEL_ORDERS, CLOSE_ALL")
    updated_by: Optional[str] = Field(None, description="操作者")
    reason: Optional[str] = Field(None, description="变更原因")


class ControlStateResponse(BaseModel):
    """控制状态响应"""
    model_config = ConfigDict(from_attributes=True)

    account_id: str
    desired_state: str
    state_version: int
    updated_at: datetime
    updated_by: Optional[str] = None
    reason: Optional[str] = None

class ConfigRequest(BaseModel):
    """配置更新请求"""
    config_value: str
    config_type: str = "string"
    description: Optional[str] = None
    updated_by: Optional[str] = None


class ConfigResponse(BaseModel):
    """配置响应"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: str
    strategy_id: str
    config_key: str
    config_value: str
    config_type: str
    description: Optional[str] = None
    updated_at: datetime
    updated_by: Optional[str] = None

# ============ 路由器 ============

router = APIRouter(prefix="/api/v1", tags=["ledger"])


# 依赖注入：获取数据库实例
async def get_db() -> LedgerDB:
    """获取数据库实例（需要在 main.py 中设置）"""
    from trading_platform.ledger.main import get_ledger_db
    return get_ledger_db()


# ============ 订单接口 ============

@router.get("/orders", response_model=List[OrderResponse])
async def get_orders(
    account_id: Optional[str] = Query(None, description="账户ID"),
    strategy_id: Optional[str] = Query(None, description="策略ID"),
    symbol: Optional[str] = Query(None, description="交易对"),
    status: Optional[str] = Query(None, description="订单状态"),
    limit: int = Query(100, ge=1, le=1000, description="返回数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: LedgerDB = Depends(get_db)
) -> List[OrderResponse]:
    """
    查询订单列表
    支持按账户、策略、交易对、状态筛选
    """
    orders = await db.get_orders(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        status=status,
        limit=limit,
        offset=offset
    )
    return [OrderResponse.model_validate(order) for order in orders]


# ============ 成交接口 ============

@router.get("/trades", response_model=List[TradeResponse])
async def get_trades(
    account_id: Optional[str] = Query(None, description="账户ID"),
    strategy_id: Optional[str] = Query(None, description="策略ID"),
    symbol: Optional[str] = Query(None, description="交易对"),
    limit: int = Query(100, ge=1, le=1000, description="返回数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: LedgerDB = Depends(get_db)
) -> List[TradeResponse]:
    """
    查询成交流水
    支持按账户、策略、交易对筛选
    """
    trades = await db.get_trades(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        limit=limit,
        offset=offset
    )
    return [TradeResponse.model_validate(trade) for trade in trades]


# ============ 持仓接口 ============

@router.get("/positions", response_model=List[PositionResponse])
async def get_positions(
    account_id: Optional[str] = Query(None, description="账户ID"),
    strategy_id: Optional[str] = Query(None, description="策略ID"),
    symbol: Optional[str] = Query(None, description="交易对"),
    db: LedgerDB = Depends(get_db)
) -> List[PositionResponse]:
    """
    查询当前持仓
    只返回有持仓数量 > 0 的记录
    """
    positions = await db.get_positions(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol
    )
    return [PositionResponse.model_validate(pos) for pos in positions]


# ============ 盈亏统计接口 ============

@router.get("/pnl", response_model=PnLSummary)
async def get_pnl_summary(
    account_id: str = Query(..., description="账户ID"),
    strategy_id: Optional[str] = Query(None, description="策略ID"),
    symbol: Optional[str] = Query(None, description="交易对"),
    db: LedgerDB = Depends(get_db)
) -> PnLSummary:
    """
    盈亏统计
    计算已实现盈亏、未实现盈亏、胜率等指标
    """
    # 查询成交记录计算已实现盈亏
    trades = await db.get_trades(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        limit=10000  # 取足够多的记录
    )

    # 查询当前持仓计算未实现盈亏
    positions = await db.get_positions(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol
    )

    # 计算统计指标
    total_commission = sum(t.commission for t in trades)
    total_realized_pnl = sum(t.realized_pnl or Decimal("0") for t in trades)
    total_unrealized_pnl = sum(p.unrealized_pnl or Decimal("0") for p in positions)
    net_pnl = total_realized_pnl + total_unrealized_pnl - total_commission

    # 胜率计算（按已实现盈亏的交易）
    realized_trades = [t for t in trades if t.realized_pnl is not None]
    win_count = len([t for t in realized_trades if t.realized_pnl > 0])
    loss_count = len([t for t in realized_trades if t.realized_pnl < 0])
    win_rate = win_count / len(realized_trades) if realized_trades else 0.0

    # 平均盈利/亏损
    wins = [t.realized_pnl for t in realized_trades if t.realized_pnl > 0]
    losses = [abs(t.realized_pnl) for t in realized_trades if t.realized_pnl < 0]
    avg_win = sum(wins) / len(wins) if wins else Decimal("0")
    avg_loss = sum(losses) / len(losses) if losses else Decimal("0")

    return PnLSummary(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        total_trades=len(trades),
        total_commission=total_commission,
        total_realized_pnl=total_realized_pnl,
        total_unrealized_pnl=total_unrealized_pnl,
        net_pnl=net_pnl,
        win_count=win_count,
        loss_count=loss_count,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
    )


# ============ 紧急控制接口 ============

@router.get("/account_control_state/{account_id}", response_model=ControlStateResponse)
async def get_control_state(
    account_id: str,
    db: LedgerDB = Depends(get_db)
) -> ControlStateResponse:
    """获取账户控制状态"""
    state = await db.get_account_control_state(account_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    return ControlStateResponse.model_validate(state)


@router.put("/account_control_state/{account_id}", response_model=ControlStateResponse)
async def update_control_state(
    account_id: str,
    request: ControlStateRequest,
    db: LedgerDB = Depends(get_db)
) -> ControlStateResponse:
    """
    更新账户控制状态

    状态说明：
    - NORMAL: 正常运行
    - HALT_NEW: 禁止新开仓信号（已有持仓继续运行，挂单不撤）
    - CANCEL_ORDERS: 撤销所有挂单 + 禁止新开仓（不平持仓）
    - CLOSE_ALL: 撤单 + 市价平所有持仓 + 禁止新开仓（全部清场）
    """
    valid_states = {"NORMAL", "HALT_NEW", "CANCEL_ORDERS", "CLOSE_ALL"}
    if request.desired_state not in valid_states:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid state. Must be one of {valid_states}"
        )

    new_version = await db.update_account_control_state(
        account_id=account_id,
        desired_state=request.desired_state,
        updated_by=request.updated_by,
        reason=request.reason
    )

    # 返回更新后的状态
    state = await db.get_account_control_state(account_id)
    if not state:
        raise HTTPException(status_code=500, detail="Failed to update state")

    return ControlStateResponse.model_validate(state)


# ============ 策略配置接口 ============

@router.get("/config/{account_id}/{strategy_id}", response_model=List[ConfigResponse])
async def get_strategy_configs(
    account_id: str,
    strategy_id: str,
    config_key: Optional[str] = Query(None, description="配置键（可选）"),
    db: LedgerDB = Depends(get_db)
) -> List[ConfigResponse]:
    """获取策略配置"""
    configs = await db.get_strategy_config(
        account_id=account_id,
        strategy_id=strategy_id,
        config_key=config_key
    )
    return [ConfigResponse.model_validate(cfg) for cfg in configs]


@router.put("/config/{account_id}/{strategy_id}/{config_key}", response_model=ConfigResponse)
async def update_strategy_config(
    account_id: str,
    strategy_id: str,
    config_key: str,
    request: ConfigRequest,
    db: LedgerDB = Depends(get_db)
) -> ConfigResponse:
    """更新策略配置（V1：需重启策略进程生效）"""
    await db.upsert_strategy_config(
        account_id=account_id,
        strategy_id=strategy_id,
        config_key=config_key,
        config_value=request.config_value,
        config_type=request.config_type,
        description=request.description,
        updated_by=request.updated_by
    )

    # 返回更新后的配置
    configs = await db.get_strategy_config(
        account_id=account_id,
        strategy_id=strategy_id,
        config_key=config_key
    )
    if not configs:
        raise HTTPException(status_code=500, detail="Failed to update config")

    return ConfigResponse.model_validate(configs[0])


# ============ 健康检查 ============

@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """健康检查接口"""
    return {
        "status": "healthy",
        "service": "ledger",
        "timestamp": datetime.utcnow().isoformat()
    }
