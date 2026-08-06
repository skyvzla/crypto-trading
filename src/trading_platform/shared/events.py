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


@dataclass
class Bar1s:
    """
    1秒 Bar（由行情层从 aggTrade 聚合）

    时间语义：
    - timestamp: 该秒开始时间（如 16:30:25.000）
    - available_time: 可用时间 = timestamp + 1000（该秒结束后）
    """
    symbol: str
    timestamp: int  # 事件时间（秒开始）
    available_time: int  # 可用时间（秒结束后）

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int
    vwap: Decimal  # 成交量加权平均价

    # 用于稳定排序
    type_priority: int = 1  # Bar1s 优先级高于 Kline
    sequence: int = 0

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
            type_priority=data.get('type_priority', 1),
            sequence=data.get('sequence', 0),
        )

    def to_json(self) -> str:
        """序列化为 JSON 字符串"""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, json_str: str) -> 'Bar1s':
        """从 JSON 字符串反序列化"""
        return cls.from_dict(json.loads(json_str))


@dataclass
class Kline:
    """
    K 线（来自 Binance K 线 WS 流，只保留 isFinal=true）

    时间语义：
    - open_time: K 线开盘时间
    - close_time: K 线收盘时间
    - available_time: 可用时间 = close_time + 1（K 线完成后 1ms）
    """
    symbol: str
    interval: str  # '1m', '5m', '15m', '1h', '4h', '1d'
    open_time: int
    close_time: int
    available_time: int

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

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

    # 元数据（用于日志和分析）
    strategy_id: str | None = None
    trigger_reason: str | None = None


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
    status: Literal['NEW', 'FILLED', 'CANCELLED', 'EXPIRED', 'SUBMIT_UNKNOWN']

    created_at: int  # 毫秒时间戳
    ttl_ms: int | None = None

    filled_quantity: Decimal = Decimal('0')
    fill_time: int | None = None
    cancel_time: int | None = None

    # 策略元数据
    strategy_id: str | None = None
    trigger_reason: str | None = None


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


@dataclass(frozen=True)
class StrategyAuditEvent:
    """策略决策审计事件，供 replay 和实时适配器统一记录。"""

    event_time: int
    event_type: str
    symbol: str
    strategy_id: str
    campaign_id: str | None
    details: dict[str, Any]
