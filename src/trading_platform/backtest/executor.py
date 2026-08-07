"""
回测执行层

模拟交易所订单接口，无网络调用，立即返回。
"""
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from trading_platform.shared.events import Order, OrderIntent
from trading_platform.shared.binance.symbol_rules import BinanceSymbolRuleBook

if TYPE_CHECKING:
    from .engine import BacktestEngine

logger = logging.getLogger(__name__)


class BacktestExecutor:
    """
    回测执行层

    职责：
    1. 模拟下单（内存订单簿）
    2. 模拟撤单（立即生效）
    3. 无网络调用

    与实盘差异：
    - 无网络延迟
    - 订单立即进入 NEW 状态
    - 撤单立即生效
    """

    def __init__(
        self,
        engine: 'BacktestEngine',
        account_id: str = 'backtest',
        *,
        symbol_rules: BinanceSymbolRuleBook | None = None,
    ):
        """
        Args:
            engine: 回测引擎实例
            account_id: 账户ID
        """
        self.engine = engine
        self.account_id = account_id
        self.symbol_rules = symbol_rules
        self._order_counter = 0

    def place_order(self, order_intent: OrderIntent) -> Order:
        """
        下单（立即返回，不调用真实API）

        Args:
            order_intent: 下单意图

        Returns:
            订单对象
        """
        if self.symbol_rules is not None:
            order_intent = self.symbol_rules.get(order_intent.symbol).normalize_intent(
                order_intent,
                reference_price=order_intent.price,
            )

        # clientOrderId 是执行幂等键：同一账户下重复提交时返回已有订单。
        for existing in self.engine.orders.values():
            if (
                existing.account_id == self.account_id
                and existing.client_order_id == order_intent.client_order_id
            ):
                return existing

        if order_intent.reduce_only:
            self._validate_reduce_only(order_intent)

        # 生成订单ID
        self._order_counter += 1
        order_id = f"order_{self._order_counter}_{self.engine.virtual_time_ms}"

        # 创建订单对象
        order = Order(
            order_id=order_id,
            client_order_id=order_intent.client_order_id,
            account_id=self.account_id,
            symbol=order_intent.symbol,
            side=order_intent.side,
            type=order_intent.order_type,
            price=order_intent.price,
            quantity=order_intent.quantity,
            status='NEW',
            created_at=self.engine.virtual_time_ms,
            ttl_ms=order_intent.ttl_ms,
            reduce_only=order_intent.reduce_only,
            strategy_id=order_intent.strategy_id,
            trigger_reason=order_intent.trigger_reason,
            filled_quantity=Decimal('0')
        )

        # 加入引擎订单簿
        self.engine.orders[order.order_id] = order
        self.engine.order_records.append(order)

        logger.debug(
            f"Order placed: {order.symbol} {order.side} "
            f"{order.quantity}@{order.price} ttl={order.ttl_ms}ms"
        )

        return order

    def _validate_reduce_only(self, intent: OrderIntent) -> None:
        position = self.engine.get_position(intent.symbol)
        if position is None:
            raise ValueError(f"reduce-only order requires an open position: {intent.symbol}")
        closing_side = "BUY" if position.side == "SHORT" else "SELL"
        if intent.side != closing_side:
            raise ValueError(
                f"reduce-only side {intent.side} would increase {position.side} position"
            )
        reserved = sum(
            (
                order.quantity - order.filled_quantity
                for order in self.engine.orders.values()
                if order.symbol == intent.symbol
                and order.reduce_only
                and order.status in {"NEW", "PARTIALLY_FILLED"}
            ),
            start=Decimal("0"),
        )
        if intent.quantity > position.quantity - reserved:
            raise ValueError(
                "reduce-only quantity exceeds unreserved position: "
                f"quantity={intent.quantity}, available={position.quantity - reserved}"
            )

    def cancel_order(self, order_id: str) -> bool:
        """
        撤单（立即生效）

        Args:
            order_id: 订单ID

        Returns:
            是否撤单成功
        """
        order = self.engine.orders.get(order_id)

        if not order:
            logger.warning(f"Order not found: {order_id}")
            return False

        if order.status not in {'NEW', 'PARTIALLY_FILLED'}:
            logger.warning(
                f"Order {order_id} cannot be cancelled, "
                f"status={order.status}"
            )
            return False

        # 立即生效
        order.status = 'CANCELLED'
        order.cancel_time = self.engine.virtual_time_ms

        logger.debug(f"Order cancelled: {order_id}")
        return True

    def cancel_symbol_orders(self, symbol: str) -> int:
        """
        撤销某币种的所有活跃订单

        Args:
            symbol: 币种符号

        Returns:
            撤销的订单数量
        """
        cancelled_count = 0

        for order in list(self.engine.orders.values()):
            if order.symbol == symbol and order.status in {'NEW', 'PARTIALLY_FILLED'}:
                if self.cancel_order(order.order_id):
                    cancelled_count += 1

        return cancelled_count

    def cancel_all_orders(self) -> int:
        """
        撤销所有活跃订单

        Returns:
            撤销的订单数量
        """
        cancelled_count = 0

        for order in list(self.engine.orders.values()):
            if order.status in {'NEW', 'PARTIALLY_FILLED'}:
                if self.cancel_order(order.order_id):
                    cancelled_count += 1

        return cancelled_count
