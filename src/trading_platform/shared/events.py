"""
标准化事件类型定义
所有时间戳单位：毫秒级 Unix 时间
"""
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal


# 订阅类型
SubscriptionType = Literal['bar1s', 'kline']


def _decimal_to_string(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


@dataclass
class OHLCVBar:
    """所有 K 线事件共享的最小价格/成交量结构。"""

    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass
class Bar1s(OHLCVBar):
    """
    1秒 Bar（由行情层从 aggTrade 聚合）

    时间语义：
    - timestamp: 该秒开始时间（如 16:30:25.000）
    - available_time: 可用时间 = timestamp + 1000（该秒结束后）
    """
    timestamp: int  # 事件时间（秒开始）
    available_time: int  # 可用时间（秒结束后）
    trade_count: int
    vwap: Decimal  # 成交量加权平均价

    # aggTrade 可直接得到的订单流原始聚合。None 表示数据源没有提供该
    # 维度；0 表示该秒明确没有对应方向的成交。买/卖均指 taker 方向：
    # is_buyer_maker=false 为主动买，true 为主动卖。
    quote_volume: Decimal | None = None
    raw_trade_count: int | None = None
    taker_buy_volume: Decimal | None = None
    taker_sell_volume: Decimal | None = None
    taker_buy_quote_volume: Decimal | None = None
    taker_sell_quote_volume: Decimal | None = None
    taker_buy_trade_count: int | None = None
    taker_sell_trade_count: int | None = None
    taker_buy_agg_trade_count: int | None = None
    taker_sell_agg_trade_count: int | None = None
    max_agg_trade_quantity: Decimal | None = None
    max_taker_buy_agg_trade_quantity: Decimal | None = None
    max_taker_sell_agg_trade_quantity: Decimal | None = None
    first_trade_id: int | None = None
    last_trade_id: int | None = None

    # 用于稳定排序
    type_priority: int = 1  # Bar1s 优先级高于 Kline
    sequence: int = 0

    # 实时链路连续性水位；历史回测事件可以不提供。
    first_aggregate_trade_id: int | None = None
    last_aggregate_trade_id: int | None = None

    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            'symbol': self.symbol,
            'timestamp': self.timestamp,
            'available_time': self.available_time,
            'open': str(self.open),
            'high': str(self.high),
            'low': str(self.low),
            'close': str(self.close),
            'volume': str(self.volume),
            'trade_count': self.trade_count,
            'vwap': str(self.vwap),
            'quote_volume': _decimal_to_string(self.quote_volume),
            'raw_trade_count': self.raw_trade_count,
            'taker_buy_volume': _decimal_to_string(self.taker_buy_volume),
            'taker_sell_volume': _decimal_to_string(self.taker_sell_volume),
            'taker_buy_quote_volume': _decimal_to_string(self.taker_buy_quote_volume),
            'taker_sell_quote_volume': _decimal_to_string(self.taker_sell_quote_volume),
            'taker_buy_trade_count': self.taker_buy_trade_count,
            'taker_sell_trade_count': self.taker_sell_trade_count,
            'taker_buy_agg_trade_count': self.taker_buy_agg_trade_count,
            'taker_sell_agg_trade_count': self.taker_sell_agg_trade_count,
            'max_agg_trade_quantity': _decimal_to_string(self.max_agg_trade_quantity),
            'max_taker_buy_agg_trade_quantity': _decimal_to_string(
                self.max_taker_buy_agg_trade_quantity
            ),
            'max_taker_sell_agg_trade_quantity': _decimal_to_string(
                self.max_taker_sell_agg_trade_quantity
            ),
            'first_trade_id': self.first_trade_id,
            'last_trade_id': self.last_trade_id,
            'first_aggregate_trade_id': self.first_aggregate_trade_id,
            'last_aggregate_trade_id': self.last_aggregate_trade_id,
            'type_priority': self.type_priority,
            'sequence': self.sequence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Bar1s':
        """从字典反序列化"""
        return cls(
            symbol=data['symbol'],
            timestamp=data['timestamp'],
            available_time=data['available_time'],
            open=Decimal(data['open']),
            high=Decimal(data['high']),
            low=Decimal(data['low']),
            close=Decimal(data['close']),
            volume=Decimal(data['volume']),
            trade_count=data['trade_count'],
            vwap=Decimal(data['vwap']),
            quote_volume=_optional_decimal(data.get('quote_volume')),
            raw_trade_count=_optional_int(data.get('raw_trade_count')),
            taker_buy_volume=_optional_decimal(data.get('taker_buy_volume')),
            taker_sell_volume=_optional_decimal(data.get('taker_sell_volume')),
            taker_buy_quote_volume=_optional_decimal(
                data.get('taker_buy_quote_volume')
            ),
            taker_sell_quote_volume=_optional_decimal(
                data.get('taker_sell_quote_volume')
            ),
            taker_buy_trade_count=_optional_int(data.get('taker_buy_trade_count')),
            taker_sell_trade_count=_optional_int(data.get('taker_sell_trade_count')),
            taker_buy_agg_trade_count=_optional_int(
                data.get('taker_buy_agg_trade_count')
            ),
            taker_sell_agg_trade_count=_optional_int(
                data.get('taker_sell_agg_trade_count')
            ),
            max_agg_trade_quantity=_optional_decimal(
                data.get('max_agg_trade_quantity')
            ),
            max_taker_buy_agg_trade_quantity=_optional_decimal(
                data.get('max_taker_buy_agg_trade_quantity')
            ),
            max_taker_sell_agg_trade_quantity=_optional_decimal(
                data.get('max_taker_sell_agg_trade_quantity')
            ),
            first_trade_id=_optional_int(data.get('first_trade_id')),
            last_trade_id=_optional_int(data.get('last_trade_id')),
            type_priority=data.get('type_priority', 1),
            sequence=data.get('sequence', 0),
            first_aggregate_trade_id=(
                None
                if data.get('first_aggregate_trade_id') is None
                else int(data['first_aggregate_trade_id'])
            ),
            last_aggregate_trade_id=(
                None
                if data.get('last_aggregate_trade_id') is None
                else int(data['last_aggregate_trade_id'])
            ),
        )

    def to_json(self) -> str:
        """序列化为 JSON 字符串"""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, json_str: str) -> 'Bar1s':
        """从 JSON 字符串反序列化"""
        return cls.from_dict(json.loads(json_str))

    @property
    def orderflow_available(self) -> bool:
        return self.taker_buy_volume is not None and self.taker_sell_volume is not None

    @property
    def volume_delta(self) -> Decimal | None:
        """主动买量 - 主动卖量；它是构造滚动 CVD 的无损基础量。"""
        if not self.orderflow_available:
            return None
        return self.taker_buy_volume - self.taker_sell_volume

    @property
    def quote_volume_delta(self) -> Decimal | None:
        if self.taker_buy_quote_volume is None or self.taker_sell_quote_volume is None:
            return None
        return self.taker_buy_quote_volume - self.taker_sell_quote_volume

    @property
    def taker_buy_quote_ratio(self) -> Decimal | None:
        if self.taker_buy_quote_volume is None or self.quote_volume is None:
            return None
        if self.quote_volume <= 0:
            return None
        return self.taker_buy_quote_volume / self.quote_volume

    @property
    def quote_volume_imbalance(self) -> Decimal | None:
        delta = self.quote_volume_delta
        if delta is None or self.quote_volume is None or self.quote_volume <= 0:
            return None
        return delta / self.quote_volume

    @property
    def taker_buy_volume_ratio(self) -> Decimal | None:
        if not self.orderflow_available or self.volume <= 0:
            return None
        return self.taker_buy_volume / self.volume

    @property
    def volume_imbalance(self) -> Decimal | None:
        delta = self.volume_delta
        if delta is None or self.volume <= 0:
            return None
        return delta / self.volume

    @property
    def taker_buy_trade_ratio(self) -> Decimal | None:
        if (
            self.raw_trade_count is None
            or self.raw_trade_count <= 0
            or self.taker_buy_trade_count is None
        ):
            return None
        return Decimal(self.taker_buy_trade_count) / Decimal(self.raw_trade_count)

    @property
    def trade_imbalance(self) -> Decimal | None:
        ratio = self.taker_buy_trade_ratio
        return None if ratio is None else ratio * Decimal("2") - Decimal("1")

    @property
    def taker_buy_vwap(self) -> Decimal | None:
        if (
            self.taker_buy_volume is None
            or self.taker_buy_quote_volume is None
            or self.taker_buy_volume <= 0
        ):
            return None
        return self.taker_buy_quote_volume / self.taker_buy_volume

    @property
    def taker_sell_vwap(self) -> Decimal | None:
        if (
            self.taker_sell_volume is None
            or self.taker_sell_quote_volume is None
            or self.taker_sell_volume <= 0
        ):
            return None
        return self.taker_sell_quote_volume / self.taker_sell_volume

    @property
    def avg_agg_trade_quantity(self) -> Decimal | None:
        if self.trade_count <= 0:
            return None
        return self.volume / Decimal(self.trade_count)

    @property
    def avg_raw_trade_quantity(self) -> Decimal | None:
        if self.raw_trade_count is None or self.raw_trade_count <= 0:
            return None
        return self.volume / Decimal(self.raw_trade_count)

    @property
    def avg_taker_buy_agg_trade_quantity(self) -> Decimal | None:
        if (
            self.taker_buy_volume is None
            or self.taker_buy_agg_trade_count is None
            or self.taker_buy_agg_trade_count <= 0
        ):
            return None
        return self.taker_buy_volume / Decimal(self.taker_buy_agg_trade_count)

    @property
    def avg_taker_sell_agg_trade_quantity(self) -> Decimal | None:
        if (
            self.taker_sell_volume is None
            or self.taker_sell_agg_trade_count is None
            or self.taker_sell_agg_trade_count <= 0
        ):
            return None
        return self.taker_sell_volume / Decimal(self.taker_sell_agg_trade_count)

    @property
    def avg_taker_buy_raw_trade_quantity(self) -> Decimal | None:
        if (
            self.taker_buy_volume is None
            or self.taker_buy_trade_count is None
            or self.taker_buy_trade_count <= 0
        ):
            return None
        return self.taker_buy_volume / Decimal(self.taker_buy_trade_count)

    @property
    def avg_taker_sell_raw_trade_quantity(self) -> Decimal | None:
        if (
            self.taker_sell_volume is None
            or self.taker_sell_trade_count is None
            or self.taker_sell_trade_count <= 0
        ):
            return None
        return self.taker_sell_volume / Decimal(self.taker_sell_trade_count)


@dataclass
class Kline(OHLCVBar):
    """
    K 线（来自 Binance K 线 WS 流，只保留 isFinal=true）

    时间语义：
    - open_time: K 线开盘时间
    - close_time: K 线收盘时间
    - available_time: 可用时间 = close_time + 1（K 线完成后 1ms）
    """
    interval: str  # '1m', '5m', '15m', '1h', '4h', '1d'
    open_time: int
    close_time: int
    available_time: int

    # 用于稳定排序
    type_priority: int = 2  # Kline 优先级低于 Bar1s
    sequence: int = 0

    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            'symbol': self.symbol,
            'interval': self.interval,
            'open_time': self.open_time,
            'close_time': self.close_time,
            'available_time': self.available_time,
            'open': str(self.open),
            'high': str(self.high),
            'low': str(self.low),
            'close': str(self.close),
            'volume': str(self.volume),
            'type_priority': self.type_priority,
            'sequence': self.sequence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Kline':
        """从字典反序列化"""
        return cls(
            symbol=data['symbol'],
            interval=data['interval'],
            open_time=data['open_time'],
            close_time=data['close_time'],
            available_time=data['available_time'],
            open=Decimal(data['open']),
            high=Decimal(data['high']),
            low=Decimal(data['low']),
            close=Decimal(data['close']),
            volume=Decimal(data['volume']),
            type_priority=data.get('type_priority', 2),
            sequence=data.get('sequence', 0),
        )

    def to_json(self) -> str:
        """序列化为 JSON 字符串"""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, json_str: str) -> 'Kline':
        """从 JSON 字符串反序列化"""
        return cls.from_dict(json.loads(json_str))


@dataclass
class OrderIntent:
    """
    策略返回的下单意图（V1 同步策略核心模式）
    外围循环负责执行
    """
    symbol: str
    side: Literal['BUY', 'SELL']
    price: Decimal
    quantity: Decimal
    client_order_id: str
    ttl_ms: int | None = None  # 订单生存时间（毫秒），None=永久有效
    order_type: Literal['LIMIT', 'MARKET'] = 'LIMIT'
    reduce_only: bool = False

    # 元数据（用于日志和分析）
    strategy_id: str | None = None
    trigger_reason: str | None = None
    campaign_id: str | None = None


@dataclass
class Fill:
    """
    成交记录
    """
    fill_id: str
    order_id: str
    symbol: str
    side: Literal['BUY', 'SELL']
    price: Decimal
    quantity: Decimal
    commission: Decimal
    commission_asset: str
    fill_time: int  # 毫秒时间戳
    is_maker: bool


@dataclass
class Order:
    """
    订单状态（实盘和回测共用）
    """
    order_id: str
    client_order_id: str
    account_id: str
    symbol: str
    side: Literal['BUY', 'SELL']
    type: Literal['LIMIT', 'MARKET']
    price: Decimal
    quantity: Decimal
    status: Literal[
        'NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED',
        'SUBMIT_UNKNOWN'
    ]

    created_at: int  # 毫秒时间戳
    ttl_ms: int | None = None
    reduce_only: bool = False

    filled_quantity: Decimal = Decimal('0')
    fill_time: int | None = None
    cancel_time: int | None = None

    # 策略元数据
    strategy_id: str | None = None
    trigger_reason: str | None = None
    campaign_id: str | None = None


@dataclass
class Position:
    """
    持仓记录
    """
    symbol: str
    side: Literal['LONG', 'SHORT']
    entry_price: Decimal
    quantity: Decimal

    total_commission: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal

    opened_at: int  # 毫秒时间戳
    closed_at: int | None = None
    status: Literal['OPEN', 'CLOSED'] = 'OPEN'
    max_adverse_price: Decimal | None = None
    max_adverse_return: Decimal = Decimal('0')
    max_unrealized_loss: Decimal = Decimal('0')
    full_position_liquidation: bool = False
    full_position_liquidation_time: int | None = None
    liquidation_position_ratio: Decimal | None = None


@dataclass(frozen=True)
class StrategyAuditEvent:
    """策略决策审计事件，供 replay 和实时适配器统一记录。"""

    event_time: int
    event_type: str
    symbol: str
    strategy_id: str
    campaign_id: str | None
    details: dict[str, Any]
