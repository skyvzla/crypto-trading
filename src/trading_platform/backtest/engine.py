"""
回测引擎核心

虚拟时钟驱动的事件循环，确定性回测。
"""
import logging
from decimal import Decimal
from typing import Protocol, Union

from trading_platform.shared.events import Bar1s, Kline, Order, Fill, Position, OrderIntent
from trading_platform.shared.config import BacktestConfig
from .executor import BacktestExecutor
from .result import BacktestResult

logger = logging.getLogger(__name__)

# 事件类型
Event = Union[Bar1s, Kline]


class Strategy(Protocol):
    """
    策略接口协议

    V1 采用同步策略核心模式：
    - on_bar1s / on_kline 返回 OrderIntent 列表
    - on_fill 通知成交（可选实现）
    """

    def on_bar1s(self, bar: Bar1s) -> list[OrderIntent] | None:
        """处理 1s Bar"""
        ...

    def on_kline(self, kline: Kline) -> list[OrderIntent] | None:
        """处理 K 线"""
        ...

    def on_fill(self, fill: Fill) -> None:
        """处理成交通知（可选）"""
        ...


class BacktestEngine:
    """
    回测引擎

    职责：
    1. 维护虚拟时钟（使用 event.available_time）
    2. 逐事件推送给策略
    3. 检查订单成交（简化触价模型）
    4. 管理持仓
    5. 收集结果数据

    确定性保证：
    - 虚拟时钟严格使用 available_time
    - 事件按稳定排序键处理
    - 无随机数，无外部IO
    """

    def __init__(
        self,
        strategy: Strategy,
        events: list[Event],
        config: BacktestConfig,
        account_id: str = 'backtest'
    ):
        """
        Args:
            strategy: 策略实例
            events: 已排序的事件列表
            config: 回测配置
            account_id: 账户ID
        """
        self.strategy = strategy
        self.events = events
        self.config = config
        self.account_id = account_id

        # 虚拟时钟（使用 available_time，避免未来信息）
        self.virtual_time_ms = events[0].available_time if events else 0

        # 订单管理
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []

        # 持仓管理
        self.positions: dict[str, Position] = {}

        # 结果收集
        self.order_records: list[Order] = []
        self.fill_records: list[Fill] = []
        self.position_records: list[Position] = []

        # 执行层
        self.executor = BacktestExecutor(self, account_id)

    def run(self) -> BacktestResult:
        """
        主循环：逐事件推送

        Returns:
            回测结果
        """
        logger.info(f"Backtest starting: {len(self.events)} events")

        for i, event in enumerate(self.events):
            # 1. 更新虚拟时钟（使用 available_time，避免未来信息）
            self.virtual_time_ms = event.available_time

            # 2. 先检查订单成交（重要！成交判断在事件推送之前）
            self._check_fills(event)

            # 3. 再推送事件给策略（V1：同步调用，策略返回 OrderIntent 列表）
            order_intents: list[OrderIntent] | None = None

            if isinstance(event, Bar1s):
                order_intents = self.strategy.on_bar1s(event)
            elif isinstance(event, Kline):
                order_intents = self.strategy.on_kline(event)

            # 4. 执行策略返回的下单意图
            if order_intents:
                for intent in order_intents:
                    self.executor.place_order(intent)

            # 5. 进度打印（可选）
            if i % 10000 == 0 and i > 0:
                logger.info(f"Progress: {i}/{len(self.events)}")

        # 6. 生成结果报告
        logger.info("Backtest completed")
        return self._generate_result()

    def _check_fills(self, event: Event) -> None:
        """
        检查当前事件是否触发挂单成交

        简化触价模型：
        1. 只有 1s Bar 才能判断成交
        2. TTL 检查在价格检查之前
        3. 做空限价单：bar.high > order.price（严格穿透）
        4. 做多限价单：bar.low < order.price（严格穿透）
        5. 成交价 = 挂单价
        6. 全部成交，不模拟部分成交

        Args:
            event: 当前事件
        """
        if not isinstance(event, Bar1s):
            return  # 只有1s Bar才能判断成交

        symbol = event.symbol

        # 遍历该币种的所有活跃订单
        for order_id, order in list(self.orders.items()):
            if order.symbol != symbol or order.status != 'NEW':
                continue

            # 1. 先检查 TTL 是否过期（在价格检查之前）
            if order.ttl_ms and self.virtual_time_ms >= order.created_at + order.ttl_ms:
                self._expire_order(order)
                continue

            # 2. 再检查价格是否触发成交（简化触价模型）
            filled = False

            # 做空限价单成交条件：bar.high > order.price（严格穿透）
            if order.side == 'SELL' and event.high > order.price:
                filled = True

            # 做多限价单成交条件：bar.low < order.price（严格穿透）
            elif order.side == 'BUY' and event.low < order.price:
                filled = True

            if filled:
                fill = self._execute_fill(order, event)
                self.fills.append(fill)
                self.fill_records.append(fill)

                # 通知策略
                if hasattr(self.strategy, 'on_fill'):
                    self.strategy.on_fill(fill)

    def _execute_fill(self, order: Order, event: Bar1s) -> Fill:
        """
        执行成交，采用保守假设

        Args:
            order: 订单
            event: 触发成交的 Bar

        Returns:
            成交记录
        """
        # 保守假设：按挂单价成交（而非触发价）
        fill_price = order.price
        fill_qty = order.quantity

        # 计算手续费（Maker 费率）
        commission = fill_qty * fill_price * Decimal(str(self.config.maker_fee_rate))

        # 生成成交ID
        fill_id = f"fill_{order.order_id}_{self.virtual_time_ms}"

        fill = Fill(
            fill_id=fill_id,
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            price=fill_price,
            quantity=fill_qty,
            commission=commission,
            commission_asset='USDT',
            fill_time=self.virtual_time_ms,
            is_maker=True
        )

        # 更新订单状态
        order.status = 'FILLED'
        order.filled_quantity = fill_qty
        order.fill_time = self.virtual_time_ms

        # 更新持仓（支持多档累加）
        self._update_position(fill)

        logger.debug(
            f"Fill executed: {order.symbol} {order.side} "
            f"{fill_qty}@{fill_price} commission={commission:.4f}"
        )

        return fill

    def _expire_order(self, order: Order) -> None:
        """
        订单超时失效

        Args:
            order: 订单
        """
        order.status = 'EXPIRED'
        order.cancel_time = self.virtual_time_ms

        logger.debug(
            f"Order expired: {order.order_id} at {self.virtual_time_ms}"
        )

    def _update_position(self, fill: Fill) -> None:
        """
        根据成交更新持仓，支持多档同方向累加

        Args:
            fill: 成交记录
        """
        symbol = fill.symbol

        if symbol not in self.positions:
            # 新开仓
            self.positions[symbol] = Position(
                symbol=symbol,
                side='SHORT' if fill.side == 'SELL' else 'LONG',
                entry_price=fill.price,
                quantity=fill.quantity,
                total_commission=fill.commission,
                unrealized_pnl=Decimal('0'),
                realized_pnl=Decimal('0'),
                opened_at=self.virtual_time_ms,
                status='OPEN'
            )

            logger.debug(
                f"Position opened: {symbol} {self.positions[symbol].side} "
                f"{fill.quantity}@{fill.price}"
            )

        else:
            pos = self.positions[symbol]

            # 判断是平仓还是加仓
            is_closing = (
                (pos.side == 'SHORT' and fill.side == 'BUY') or
                (pos.side == 'LONG' and fill.side == 'SELL')
            )

            if is_closing:
                # 平仓
                close_qty = min(fill.quantity, pos.quantity)

                # 计算已实现盈亏
                if pos.side == 'SHORT':
                    pnl = (pos.entry_price - fill.price) * close_qty
                else:  # LONG
                    pnl = (fill.price - pos.entry_price) * close_qty

                pnl -= fill.commission  # 扣除手续费
                pos.realized_pnl += pnl
                pos.quantity -= close_qty
                pos.total_commission += fill.commission

                logger.debug(
                    f"Position closed (partial): {symbol} "
                    f"qty {close_qty}, pnl {pnl:.4f}"
                )

                if pos.quantity <= 0:
                    # 完全平仓
                    pos.status = 'CLOSED'
                    pos.closed_at = self.virtual_time_ms
                    self.position_records.append(pos)
                    del self.positions[symbol]

                    logger.debug(
                        f"Position fully closed: {symbol} "
                        f"total_pnl {pos.realized_pnl:.4f}"
                    )

                # V1 不支持反向开仓（平仓后立即反向）
                if fill.quantity > close_qty:
                    raise ValueError(
                        f"V1 does not support reverse opening: "
                        f"fill_qty={fill.quantity} > pos_qty={pos.quantity}"
                    )

            else:
                # 同方向加仓（多档挂单场景）
                old_qty = pos.quantity
                old_price = pos.entry_price
                add_qty = fill.quantity
                add_price = fill.price

                # 加权平均开仓价
                pos.entry_price = (
                    (old_qty * old_price + add_qty * add_price) /
                    (old_qty + add_qty)
                )
                pos.quantity += add_qty
                pos.total_commission += fill.commission

                logger.debug(
                    f"Position added: {symbol} "
                    f"qty {old_qty}->{pos.quantity}, "
                    f"entry {old_price:.2f}->{pos.entry_price:.2f}"
                )

    def _generate_result(self) -> BacktestResult:
        """
        生成回测结果

        Returns:
            回测结果对象
        """
        # 关闭所有未平仓持仓（用最后已知价格标记）
        for pos in list(self.positions.values()):
            pos.status = 'CLOSED'
            pos.closed_at = self.virtual_time_ms
            self.position_records.append(pos)

        return BacktestResult(
            virtual_time_start=self.events[0].available_time if self.events else 0,
            virtual_time_end=self.virtual_time_ms,
            orders=self.order_records,
            fills=self.fill_records,
            positions=self.position_records,
            config=self.config
        )
