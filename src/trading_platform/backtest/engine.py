"""
回测引擎核心

虚拟时钟驱动的事件循环，确定性回测。
"""
import logging
from collections.abc import Iterable, Sized
from decimal import Decimal
from typing import Protocol, Union

from trading_platform.shared.events import (
    Bar1s,
    Kline,
    Order,
    Fill,
    Position,
    OrderIntent,
    StrategyAuditEvent,
)
from trading_platform.shared.config import BacktestConfig
from trading_platform.shared.binance.symbol_rules import BinanceSymbolRuleBook
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
        events: Iterable[Event],
        config: BacktestConfig,
        account_id: str = 'backtest',
        symbol_rules: BinanceSymbolRuleBook | None = None,
        execution_timeframe: str = "1s",
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
        self._event_count_hint = len(events) if isinstance(events, Sized) else None
        self._events_processed = 0
        self._first_event_time: int | None = None
        self._finished_result: BacktestResult | None = None
        self.config = config
        self.account_id = account_id
        self.execution_timeframe = execution_timeframe

        # 虚拟时钟（使用 available_time，避免未来信息）
        self.virtual_time_ms = (
            events[0].available_time
            if isinstance(events, list) and events
            else 0
        )

        # 订单管理
        self.orders: dict[str, Order] = {}
        self._active_orders_by_symbol: dict[str, dict[str, Order]] = {}
        self.fills: list[Fill] = []

        # 持仓管理
        self.positions: dict[str, Position] = {}
        self.last_prices: dict[str, Decimal] = {}

        # 结果收集
        self.order_records: list[Order] = []
        self.fill_records: list[Fill] = []
        self.position_records: list[Position] = []
        self.audit_records: list[StrategyAuditEvent] = []

        # 执行层
        self.executor = BacktestExecutor(self, account_id, symbol_rules=symbol_rules)

        self._trading_enabled = (
            config.trading_start_ms is None
            or self.virtual_time_ms >= config.trading_start_ms
        )

        bind_account = getattr(self.strategy, 'bind_account', None)
        if callable(bind_account):
            bind_account(self)
        else:
            # 保留演示/第三方策略的旧适配钩子。
            bind_engine = getattr(self.strategy, 'bind_engine', None)
            if callable(bind_engine):
                bind_engine(self)
        drain_audit_events = getattr(self.strategy, 'drain_audit_events', None)
        self._drain_audit_events = (
            drain_audit_events if callable(drain_audit_events) else None
        )
        on_fill = getattr(self.strategy, 'on_fill', None)
        self._on_fill = on_fill if callable(on_fill) else None
        self._set_strategy_trading_enabled(self._trading_enabled)

    def run(self) -> BacktestResult:
        """
        主循环：逐事件推送

        Returns:
            回测结果
        """
        event_label = (
            str(self._event_count_hint)
            if self._event_count_hint is not None
            else "streaming"
        )
        logger.info(f"Backtest starting: {event_label} events")

        for i, event in enumerate(self.events):
            self.process_event(event)
            if i % 10000 == 0 and i > 0:
                if self._event_count_hint is None:
                    logger.debug(f"Progress: {i} events")
                else:
                    logger.debug(f"Progress: {i}/{self._event_count_hint}")

        logger.info("Backtest completed")
        return self.finish()

    def process_event(self, event: Event) -> None:
        """处理一个已排序事件，供多个隔离引擎共享同一行情流。"""
        if self._finished_result is not None:
            raise RuntimeError("backtest engine is already finished")
        if self._first_event_time is None:
            self._first_event_time = event.available_time
        self._events_processed += 1
        self.virtual_time_ms = event.available_time

        trading_enabled = (
            self.config.trading_start_ms is None
            or self.virtual_time_ms >= self.config.trading_start_ms
        )
        if trading_enabled != self._trading_enabled:
            self._trading_enabled = trading_enabled
            self._set_strategy_trading_enabled(trading_enabled)

        if isinstance(event, Bar1s) or (
            isinstance(event, Kline) and event.interval == self.execution_timeframe
        ):
            self.last_prices[event.symbol] = event.close

        self._update_position_risk(event)
        # 成交判断必须先于策略事件处理，保持单引擎回测的既有语义。
        self._check_fills(event)
        # 同一 Bar 内无法还原穿价先后，成交后按保守口径记录该 Bar 的最不利价。
        self._update_position_risk(event)
        order_intents: list[OrderIntent] | None = None
        if isinstance(event, Bar1s):
            order_intents = self.strategy.on_bar1s(event)
        elif isinstance(event, Kline):
            order_intents = self.strategy.on_kline(event)

        if self._trading_enabled and order_intents:
            for intent in order_intents:
                order = self.executor.place_order(intent)
                if order.type == 'MARKET' and order.status == 'NEW':
                    fill = self._execute_fill(order, event)
                    if fill is None:
                        continue
                    self.fills.append(fill)
                    self.fill_records.append(fill)
                    if self._on_fill is not None:
                        self._on_fill(fill)
            self._update_position_risk(event)
        self._collect_strategy_audit_events()

    def finish(self) -> BacktestResult:
        """结束事件流并生成一次最终结果；重复调用返回同一结果。"""
        if self._finished_result is None:
            self._finished_result = self._generate_result()
        return self._finished_result

    def _set_strategy_trading_enabled(self, enabled: bool) -> None:
        setter = getattr(self.strategy, 'set_trading_enabled', None)
        if callable(setter):
            setter(enabled)

    def _collect_strategy_audit_events(self) -> None:
        if self._drain_audit_events is not None:
            self.audit_records.extend(self._drain_audit_events())

    # StrategyAccount implementation
    def get_order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    def iter_orders(self) -> tuple[Order, ...]:
        return tuple(self.orders.values())

    def has_open_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def get_position(self, symbol: str) -> Position | None:
        return self.positions.get(symbol)

    def cancel_order(self, order_id: str) -> bool:
        return self.executor.cancel_order(order_id)

    def _register_active_order(self, order: Order) -> None:
        """将仍可成交的订单加入按币种维护的撮合索引。"""
        self._active_orders_by_symbol.setdefault(order.symbol, {})[order.order_id] = order

    def _remove_active_order(self, order: Order) -> None:
        """从撮合索引移除终态订单，但保留全量订单历史。"""
        symbol_orders = self._active_orders_by_symbol.get(order.symbol)
        if symbol_orders is None:
            return
        symbol_orders.pop(order.order_id, None)
        if not symbol_orders:
            self._active_orders_by_symbol.pop(order.symbol, None)

    def iter_active_orders(self, symbol: str | None = None) -> tuple[Order, ...]:
        """返回指定币种或全账户当前可成交的订单。"""
        if symbol is not None:
            return tuple(self._active_orders_by_symbol.get(symbol, {}).values())
        return tuple(
            order
            for symbol_orders in self._active_orders_by_symbol.values()
            for order in symbol_orders.values()
        )

    def _check_fills(self, event: Event) -> None:
        """
        检查当前事件是否触发挂单成交

        简化触价模型：
        1. 只有策略声明的执行周期才能判断成交
        2. TTL 检查在价格检查之前
        3. 做空限价单：bar.high > order.price（严格穿透）
        4. 做多限价单：bar.low < order.price（严格穿透）
        5. SELL 成交价 = max(挂单价, 触发 bar 开盘价)；BUY 成交价 = 挂单价
        6. 全部成交，不模拟部分成交

        Args:
            event: 当前事件
        """
        event_timeframe = "1s" if isinstance(event, Bar1s) else event.interval
        if event_timeframe != self.execution_timeframe:
            return

        symbol = event.symbol

        # 只遍历活跃索引；终态订单保留在 self.orders 供查询和结果分析。
        for order in self.iter_active_orders(symbol):
            # 成交回调可能在同一 Bar 内撤销或替换后续订单；快照项必须复查当前索引。
            if (
                order.status not in {'NEW', 'PARTIALLY_FILLED'}
                or self._active_orders_by_symbol.get(symbol, {}).get(order.order_id)
                is not order
            ):
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
                if fill is None:
                    continue
                self.fills.append(fill)
                self.fill_records.append(fill)

                # 通知策略
                if self._on_fill is not None:
                    self._on_fill(fill)
                    self._collect_strategy_audit_events()

    def _execute_fill(self, order: Order, event: Event) -> Fill | None:
        """
        执行成交

        Args:
            order: 订单
            event: 触发成交的 Bar

        Returns:
            成交记录
        """
        # 成交价：SELL 限价单按触发成交的 1s bar 开盘价成交（不差于挂单价），
        # 反映现价已高于挂单价时以更优市价卖出的情况；其余按挂单价（保守）。
        if order.side == 'SELL':
            fill_price = max(order.price, event.open)
        else:
            fill_price = order.price
        remaining_qty = order.quantity - order.filled_quantity
        fill_qty = remaining_qty
        if order.type == 'LIMIT':
            per_bar_qty = order.quantity * Decimal(
                str(self.config.limit_fill_fraction_per_bar)
            )
            fill_qty = min(fill_qty, per_bar_qty)
        expire_remainder = False
        if order.reduce_only:
            position = self.positions.get(order.symbol)
            closing_side = (
                "BUY" if position is not None and position.side == "SHORT" else "SELL"
            )
            if position is None or order.side != closing_side:
                self._expire_order(order)
                return None
            if fill_qty > position.quantity:
                fill_qty = position.quantity
                expire_remainder = True
            if fill_qty <= 0:
                self._expire_order(order)
                return None
        else:
            position = self.positions.get(order.symbol)
            if position is not None:
                is_closing = (
                    (position.side == "SHORT" and order.side == "BUY")
                    or (position.side == "LONG" and order.side == "SELL")
                )
                if is_closing and fill_qty > position.quantity:
                    raise ValueError(
                        "order would reverse position: "
                        f"fill_qty={fill_qty}, position_qty={position.quantity}"
                    )

        is_maker = order.type != 'MARKET'
        fee_rate = (
            self.config.maker_fee_rate if is_maker else self.config.taker_fee_rate
        )
        commission = fill_qty * fill_price * Decimal(str(fee_rate))

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
            is_maker=is_maker
        )

        # 更新订单状态
        order.filled_quantity += fill_qty
        order.status = 'FILLED' if order.filled_quantity == order.quantity else 'PARTIALLY_FILLED'
        order.fill_time = self.virtual_time_ms
        if expire_remainder:
            order.status = 'EXPIRED'
            order.cancel_time = self.virtual_time_ms
        if order.status == 'PARTIALLY_FILLED':
            self._register_active_order(order)
        else:
            self._remove_active_order(order)

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
        self._remove_active_order(order)

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

    def _update_position_risk(self, event: Event) -> None:
        """记录价格路径风险，不改变订单、持仓或策略退出结果。"""
        event_timeframe = '1s' if isinstance(event, Bar1s) else event.interval
        if event_timeframe != self.execution_timeframe:
            return
        position = self.positions.get(event.symbol)
        if position is None:
            return

        adverse_price = event.high if position.side == 'SHORT' else event.low
        unrealized = (
            (position.entry_price - adverse_price) * position.quantity
            if position.side == 'SHORT'
            else (adverse_price - position.entry_price) * position.quantity
        )
        if (
            position.max_adverse_price is None
            or (position.side == 'SHORT' and adverse_price > position.max_adverse_price)
            or (position.side == 'LONG' and adverse_price < position.max_adverse_price)
        ):
            position.max_adverse_price = adverse_price
        if unrealized < position.max_unrealized_loss:
            position.max_unrealized_loss = unrealized
            position.max_adverse_return = (
                unrealized / (position.entry_price * position.quantity)
            )
            position.liquidation_position_ratio = (
                Decimal('1') / abs(position.max_adverse_return)
                if position.max_adverse_return else None
            )
            if (
                position.max_adverse_return <= Decimal('-1')
                and not position.full_position_liquidation
            ):
                position.full_position_liquidation = True
                position.full_position_liquidation_time = self.virtual_time_ms

    def _generate_result(self) -> BacktestResult:
        """
        生成回测结果

        Returns:
            回测结果对象
        """
        # 未确认期末结算规则前，不得把未平仓仓位伪装成已平仓。
        for pos in list(self.positions.values()):
            last_price = self.last_prices.get(pos.symbol)
            if last_price is not None:
                if pos.side == 'SHORT':
                    pos.unrealized_pnl = (
                        pos.entry_price - last_price
                    ) * pos.quantity
                else:
                    pos.unrealized_pnl = (
                        last_price - pos.entry_price
                    ) * pos.quantity
            self.position_records.append(pos)

        return BacktestResult(
            virtual_time_start=(
                self.config.trading_start_ms
                if self.config.trading_start_ms is not None
                else (self._first_event_time or 0)
            ),
            virtual_time_end=self.virtual_time_ms,
            orders=self.order_records,
            fills=self.fill_records,
            positions=self.position_records,
            config=self.config,
            events_processed=self._events_processed,
            audit_events=self.audit_records,
        )
