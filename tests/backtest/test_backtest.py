"""
回测引擎单元测试

测试核心功能：
1. 数据加载
2. 事件排序
3. 订单成交判断
4. 持仓管理
5. 确定性验证
"""
import unittest
from decimal import Decimal
from datetime import datetime

from trading_platform.shared.events import Bar1s, Kline, OrderIntent, Fill, Position
from trading_platform.shared.config import BacktestConfig
from trading_platform.backtest.engine import BacktestEngine
from trading_platform.backtest.executor import BacktestExecutor
from trading_platform.backtest.result import ResultAnalyzer
from trading_platform.shared.binance.symbol_rules import (
    BinanceSymbolRuleBook,
    BinanceSymbolRules,
)


class MockStrategy:
    """
    测试用模拟策略
    """

    def __init__(self):
        self.bars_received = []
        self.klines_received = []
        self.fills_received = []

    def on_bar1s(self, bar: Bar1s) -> list[OrderIntent] | None:
        self.bars_received.append(bar)

        # 在第一个 Bar 时下单
        if len(self.bars_received) == 1:
            return [
                OrderIntent(
                    symbol=bar.symbol,
                    side='SELL',
                    price=bar.close - Decimal('10'),  # 低于当前价10
                    quantity=Decimal('0.001'),
                    client_order_id='test_order_1',
                    ttl_ms=None,
                    strategy_id='test',
                    trigger_reason='test_trigger'
                )
            ]

        return None

    def on_kline(self, kline: Kline) -> list[OrderIntent] | None:
        self.klines_received.append(kline)
        return None

    def on_fill(self, fill: Fill) -> None:
        self.fills_received.append(fill)


class TestBacktestEngine(unittest.TestCase):
    """回测引擎测试"""

    def test_events_can_be_pushed_incrementally_before_finishing(self):
        strategy = MockStrategy()
        events = [
            Bar1s(
                symbol='BTCUSDT', timestamp=index * 1_000,
                available_time=(index + 1) * 1_000,
                open=Decimal('100'), high=Decimal('101'), low=Decimal('99'),
                close=Decimal('100'), volume=Decimal('1'), trade_count=1,
                vwap=Decimal('100'),
            )
            for index in range(2)
        ]
        engine = BacktestEngine(strategy, [], BacktestConfig())

        for event in events:
            engine.process_event(event)
        result = engine.finish()

        self.assertEqual(result.events_processed, 2)
        self.assertEqual(strategy.bars_received, events)
        with self.assertRaisesRegex(RuntimeError, "finished"):
            engine.process_event(events[-1])

    def test_full_position_liquidation_risk_does_not_change_later_recovery(self):
        class OpenShortOnce(MockStrategy):
            def on_bar1s(self, bar):
                self.bars_received.append(bar)
                if len(self.bars_received) == 1:
                    return [OrderIntent(
                        symbol=bar.symbol,
                        side='SELL',
                        price=Decimal('100'),
                        quantity=Decimal('10'),
                        client_order_id='short-entry',
                        order_type='MARKET',
                        campaign_id='spike_short:BTCUSDT:1000',
                    )]
                if len(self.bars_received) == 3:
                    return [OrderIntent(
                        symbol=bar.symbol,
                        side='BUY',
                        price=Decimal('50'),
                        quantity=Decimal('10'),
                        client_order_id='short-exit',
                        order_type='MARKET',
                        reduce_only=True,
                        campaign_id='spike_short:BTCUSDT:1000',
                    )]
                return []

        bars = [
            Bar1s(
                symbol='BTCUSDT', timestamp=0, available_time=1_000,
                open=Decimal('100'), high=Decimal('101'), low=Decimal('99'),
                close=Decimal('100'), volume=Decimal('1'), trade_count=1,
                vwap=Decimal('100'),
            ),
            Bar1s(
                symbol='BTCUSDT', timestamp=1_000, available_time=2_000,
                open=Decimal('100'), high=Decimal('210'), low=Decimal('95'),
                close=Decimal('200'), volume=Decimal('1'), trade_count=1,
                vwap=Decimal('150'),
            ),
            Bar1s(
                symbol='BTCUSDT', timestamp=2_000, available_time=3_000,
                open=Decimal('200'), high=Decimal('200'), low=Decimal('50'),
                close=Decimal('50'), volume=Decimal('1'), trade_count=1,
                vwap=Decimal('100'),
            ),
        ]
        result = BacktestEngine(
            OpenShortOnce(),
            bars,
            BacktestConfig(),
        ).run()

        position = result.positions[0]
        self.assertTrue(position.full_position_liquidation)
        self.assertEqual(position.status, 'CLOSED')
        self.assertEqual(position.closed_at, 3_000)
        self.assertEqual(position.full_position_liquidation_time, 2_000)
        self.assertEqual(position.max_adverse_price, Decimal('210'))
        self.assertEqual(position.max_adverse_return, Decimal('-1.1'))
        self.assertEqual(position.liquidation_position_ratio, Decimal('10') / Decimal('11'))

        analyzer = ResultAnalyzer(result)
        trade = analyzer.dfs['trades'].iloc[0]
        self.assertTrue(trade['full_position_liquidation'])
        self.assertGreater(trade['net_pnl'], 0)
        self.assertEqual(analyzer.analyze()['liquidation_risk']['total'], 1)

    def test_strategy_is_bound_to_engine(self):
        """支持回测适配能力的策略会在引擎初始化时完成绑定。"""
        class BindableStrategy(MockStrategy):
            def bind_engine(self, engine):
                self.engine = engine

        strategy = BindableStrategy()
        engine = BacktestEngine(strategy, [], BacktestConfig())

        self.assertIs(strategy.engine, engine)

    def test_client_order_id_is_idempotent(self):
        """重复 clientOrderId 不应创建第二张订单。"""
        engine = BacktestEngine(MockStrategy(), [], BacktestConfig())
        intent = OrderIntent(
            symbol='BTCUSDT',
            side='SELL',
            price=Decimal('50000'),
            quantity=Decimal('0.001'),
            client_order_id='same-client-order-id',
        )

        first = engine.executor.place_order(intent)
        second = engine.executor.place_order(intent)

        self.assertIs(first, second)
        self.assertEqual(len(engine.orders), 1)
        self.assertEqual(len(engine.order_records), 1)

    def test_client_order_id_is_idempotent_after_order_is_terminal(self):
        """终态订单仍应参与 clientOrderId 幂等查找。"""
        for terminal_state in ('FILLED', 'EXPIRED', 'CANCELLED'):
            with self.subTest(terminal_state=terminal_state):
                engine = BacktestEngine(MockStrategy(), [], BacktestConfig())
                intent = OrderIntent(
                    symbol='BTCUSDT',
                    side='SELL',
                    price=Decimal('100'),
                    quantity=Decimal('1'),
                    client_order_id=f'terminal-{terminal_state}',
                )
                first = engine.executor.place_order(intent)

                if terminal_state == 'FILLED':
                    engine._execute_fill(first, Bar1s(
                        symbol='BTCUSDT', timestamp=0, available_time=1_000,
                        open=Decimal('100'), high=Decimal('101'), low=Decimal('99'),
                        close=Decimal('100'), volume=Decimal('1'), trade_count=1,
                        vwap=Decimal('100'),
                    ))
                elif terminal_state == 'EXPIRED':
                    engine._expire_order(first)
                else:
                    self.assertTrue(engine.cancel_order(first.order_id))

                second = engine.executor.place_order(intent)

                self.assertIs(second, first)
                self.assertEqual(len(engine.orders), 1)
                self.assertEqual(len(engine.order_records), 1)

    def test_check_fills_does_not_scan_terminal_order_history(self):
        """撮合只应读取活跃索引，不应遍历终态订单历史。"""
        engine = BacktestEngine(MockStrategy(), [], BacktestConfig())
        terminal = engine.executor.place_order(OrderIntent(
            symbol='BTCUSDT', side='SELL', price=Decimal('100'),
            quantity=Decimal('1'), client_order_id='terminal-history',
        ))
        active = engine.executor.place_order(OrderIntent(
            symbol='BTCUSDT', side='SELL', price=Decimal('100'),
            quantity=Decimal('1'), client_order_id='active-order',
        ))
        self.assertTrue(engine.cancel_order(terminal.order_id))

        class HistoryMustNotBeScanned(dict):
            def items(self):
                raise AssertionError('terminal order history was scanned')

        engine.orders = HistoryMustNotBeScanned(engine.orders)
        engine.virtual_time_ms = 1_000
        engine._check_fills(Bar1s(
            symbol='BTCUSDT', timestamp=0, available_time=1_000,
            open=Decimal('100'), high=Decimal('101'), low=Decimal('99'),
            close=Decimal('100'), volume=Decimal('1'), trade_count=1,
            vwap=Decimal('100'),
        ))

        self.assertEqual(active.status, 'FILLED')
        self.assertIs(engine.get_order(terminal.order_id), terminal)
        self.assertEqual(terminal.status, 'CANCELLED')

    def test_fill_callback_cancelling_sibling_order_skips_snapshot_entry(self):
        """首单成交回调撤销同 Bar 兄弟单后，兄弟单不应继续成交。"""
        class CancelSiblingOnFill(MockStrategy):
            def on_fill(self, fill):
                self.fills_received.append(fill)
                self.engine.cancel_order(self.sibling_order_id)

        strategy = CancelSiblingOnFill()
        engine = BacktestEngine(strategy, [], BacktestConfig())
        strategy.engine = engine
        first = engine.executor.place_order(OrderIntent(
            symbol='BTCUSDT', side='SELL', price=Decimal('100'),
            quantity=Decimal('1'), client_order_id='first-order',
        ))
        sibling = engine.executor.place_order(OrderIntent(
            symbol='BTCUSDT', side='SELL', price=Decimal('100'),
            quantity=Decimal('1'), client_order_id='sibling-order',
        ))
        strategy.sibling_order_id = sibling.order_id
        engine.virtual_time_ms = 1_000

        engine._check_fills(Bar1s(
            symbol='BTCUSDT', timestamp=0, available_time=1_000,
            open=Decimal('100'), high=Decimal('101'), low=Decimal('99'),
            close=Decimal('100'), volume=Decimal('1'), trade_count=1,
            vwap=Decimal('100'),
        ))

        self.assertEqual(first.status, 'FILLED')
        self.assertEqual(sibling.status, 'CANCELLED')
        self.assertEqual(len(engine.fills), 1)
        self.assertEqual(len(strategy.fills_received), 1)

    def test_limit_order_can_fill_deterministically_across_multiple_bars(self):
        class PartialFillStrategy(MockStrategy):
            def on_bar1s(self, bar):
                self.bars_received.append(bar)
                if len(self.bars_received) == 1:
                    return [OrderIntent(
                        symbol=bar.symbol, side='SELL', price=Decimal('100'),
                        quantity=Decimal('2'), client_order_id='partial-entry',
                    )]
                return []

        events = [
            Bar1s(
                symbol='BTCUSDT', timestamp=index * 1_000,
                available_time=(index + 1) * 1_000,
                open=Decimal('101'), high=Decimal('102'), low=Decimal('100'),
                close=Decimal('101'), volume=Decimal('1'), trade_count=1,
                vwap=Decimal('101'),
            )
            for index in range(3)
        ]
        strategy = PartialFillStrategy()
        result = BacktestEngine(
            strategy,
            events,
            BacktestConfig(limit_fill_fraction_per_bar=0.5),
        ).run()

        self.assertEqual([fill.quantity for fill in result.fills], [Decimal('1'), Decimal('1')])
        self.assertEqual(result.orders[0].status, 'FILLED')
        self.assertEqual(result.orders[0].filled_quantity, Decimal('2'))
        self.assertEqual(len(strategy.fills_received), 2)

    def test_partial_fill_residual_expires_before_next_price_check(self):
        engine = BacktestEngine(
            MockStrategy(),
            [],
            BacktestConfig(limit_fill_fraction_per_bar=0.5),
        )
        engine.virtual_time_ms = 1_000
        order = engine.executor.place_order(OrderIntent(
            symbol='BTCUSDT', side='SELL', price=Decimal('100'),
            quantity=Decimal('2'), client_order_id='partial-ttl', ttl_ms=1_000,
        ))
        first = Bar1s(
            symbol='BTCUSDT', timestamp=1_000, available_time=1_500,
            open=Decimal('101'), high=Decimal('102'), low=Decimal('100'),
            close=Decimal('101'), volume=Decimal('1'), trade_count=1,
            vwap=Decimal('101'),
        )
        engine.virtual_time_ms = first.available_time
        engine._check_fills(first)
        self.assertEqual(order.status, 'PARTIALLY_FILLED')
        self.assertEqual(order.filled_quantity, Decimal('1'))

        expired = Bar1s(
            symbol='BTCUSDT', timestamp=2_000, available_time=2_000,
            open=Decimal('101'), high=Decimal('102'), low=Decimal('100'),
            close=Decimal('101'), volume=Decimal('1'), trade_count=1,
            vwap=Decimal('101'),
        )
        engine.virtual_time_ms = expired.available_time
        engine._check_fills(expired)
        self.assertEqual(order.status, 'EXPIRED')
        self.assertEqual(order.filled_quantity, Decimal('1'))

    def test_replay_executor_applies_same_symbol_rule_normalization(self):
        rules = BinanceSymbolRules(
            symbol='BTCUSDT', tick_size=Decimal('0.1'),
            min_price=Decimal('1'), max_price=Decimal('1000000'),
            lot_step_size=Decimal('0.01'), min_quantity=Decimal('0.01'),
            max_quantity=Decimal('1000'), market_step_size=Decimal('0.01'),
            market_min_quantity=Decimal('0.01'),
            market_max_quantity=Decimal('1000'), min_notional=Decimal('5'),
        )
        book = BinanceSymbolRuleBook({'BTCUSDT': rules})
        engine = BacktestEngine(
            MockStrategy(), [], BacktestConfig(), symbol_rules=book
        )
        raw = OrderIntent(
            symbol='BTCUSDT', side='SELL', price=Decimal('100.01'),
            quantity=Decimal('1.019'), client_order_id='normalized',
        )

        order = engine.executor.place_order(raw)
        expected = rules.normalize_intent(raw)

        self.assertEqual(order.price, expected.price)
        self.assertEqual(order.quantity, expected.quantity)

    def test_reduce_only_rejects_empty_position(self):
        engine = BacktestEngine(MockStrategy(), [], BacktestConfig())

        with self.assertRaisesRegex(ValueError, "requires an open position"):
            engine.executor.place_order(OrderIntent(
                symbol='BTCUSDT',
                side='BUY',
                price=Decimal('100'),
                quantity=Decimal('1'),
                client_order_id='empty-reduce',
                reduce_only=True,
            ))

        self.assertEqual(engine.orders, {})

    def test_reduce_only_rejects_increasing_side_and_reserved_overflow(self):
        engine = BacktestEngine(MockStrategy(), [], BacktestConfig())
        engine.positions['BTCUSDT'] = Position(
            symbol='BTCUSDT', side='SHORT', entry_price=Decimal('100'),
            quantity=Decimal('1'), total_commission=Decimal('0'),
            unrealized_pnl=Decimal('0'), realized_pnl=Decimal('0'), opened_at=0,
        )

        with self.assertRaisesRegex(ValueError, "would increase"):
            engine.executor.place_order(OrderIntent(
                symbol='BTCUSDT', side='SELL', price=Decimal('101'),
                quantity=Decimal('0.1'), client_order_id='wrong-side',
                reduce_only=True,
            ))

        first = engine.executor.place_order(OrderIntent(
            symbol='BTCUSDT', side='BUY', price=Decimal('99'),
            quantity=Decimal('0.6'), client_order_id='reserved-1',
            reduce_only=True,
        ))
        self.assertTrue(first.reduce_only)
        with self.assertRaisesRegex(ValueError, "exceeds unreserved position"):
            engine.executor.place_order(OrderIntent(
                symbol='BTCUSDT', side='BUY', price=Decimal('98'),
                quantity=Decimal('0.5'), client_order_id='reserved-2',
                reduce_only=True,
            ))

    def test_reduce_only_market_order_partially_reduces_position(self):
        class PartialExitStrategy(MockStrategy):
            def on_bar1s(self, bar):
                return [OrderIntent(
                    symbol=bar.symbol, side='BUY', price=bar.close,
                    quantity=Decimal('0.4'), client_order_id='partial-exit',
                    order_type='MARKET', reduce_only=True,
                )]

        event = Bar1s(
            symbol='BTCUSDT', timestamp=1_000, available_time=2_000,
            open=Decimal('90'), high=Decimal('91'), low=Decimal('89'),
            close=Decimal('90'), volume=Decimal('1'), trade_count=1,
            vwap=Decimal('90'),
        )
        engine = BacktestEngine(PartialExitStrategy(), [event], BacktestConfig())
        engine.positions['BTCUSDT'] = Position(
            symbol='BTCUSDT', side='SHORT', entry_price=Decimal('100'),
            quantity=Decimal('1'), total_commission=Decimal('0'),
            unrealized_pnl=Decimal('0'), realized_pnl=Decimal('0'), opened_at=0,
        )

        result = engine.run()

        self.assertEqual(result.fills[0].quantity, Decimal('0.4'))
        self.assertEqual(engine.positions['BTCUSDT'].quantity, Decimal('0.6'))
        self.assertTrue(result.orders[0].reduce_only)

    def test_reduce_only_fill_is_capped_if_position_shrinks_while_pending(self):
        event = Bar1s(
            symbol='BTCUSDT', timestamp=1_000, available_time=2_000,
            open=Decimal('90'), high=Decimal('91'), low=Decimal('89'),
            close=Decimal('90'), volume=Decimal('1'), trade_count=1,
            vwap=Decimal('90'),
        )
        engine = BacktestEngine(MockStrategy(), [event], BacktestConfig())
        engine.positions['BTCUSDT'] = Position(
            symbol='BTCUSDT', side='SHORT', entry_price=Decimal('100'),
            quantity=Decimal('1'), total_commission=Decimal('0'),
            unrealized_pnl=Decimal('0'), realized_pnl=Decimal('0'), opened_at=0,
        )
        order = engine.executor.place_order(OrderIntent(
            symbol='BTCUSDT', side='BUY', price=Decimal('90'),
            quantity=Decimal('0.8'), client_order_id='stale-reduce',
            reduce_only=True,
        ))
        engine.positions['BTCUSDT'].quantity = Decimal('0.5')

        fill = engine._execute_fill(order, event)

        self.assertIsNotNone(fill)
        self.assertEqual(fill.quantity, Decimal('0.5'))
        self.assertEqual(order.filled_quantity, Decimal('0.5'))
        self.assertEqual(order.status, 'EXPIRED')
        self.assertNotIn('BTCUSDT', engine.positions)

    def test_market_order_intent_fills_on_current_bar(self):
        class ExitStrategy(MockStrategy):
            def on_bar1s(self, bar):
                return [OrderIntent(
                    symbol=bar.symbol,
                    side='BUY',
                    price=bar.close,
                    quantity=Decimal('1'),
                    client_order_id='market-exit',
                    order_type='MARKET',
                    strategy_id='test',
                    trigger_reason='timeout',
                )]

        event = Bar1s(
            symbol='BTCUSDT', timestamp=1_000, available_time=2_000,
            open=Decimal('110'), high=Decimal('111'), low=Decimal('109'),
            close=Decimal('110'), volume=Decimal('1'), trade_count=1,
            vwap=Decimal('110'),
        )
        engine = BacktestEngine(ExitStrategy(), [event], BacktestConfig())
        result = engine.run()
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].price, Decimal('110'))
        self.assertEqual(
            result.fills[0].commission,
            Decimal('110') * Decimal(str(engine.config.taker_fee_rate)),
        )
        self.assertFalse(result.fills[0].is_maker)
        self.assertEqual(result.orders[0].type, 'MARKET')

    def test_warmup_events_update_strategy_without_creating_orders(self):
        class WarmupAwareStrategy(MockStrategy):
            def __init__(self):
                super().__init__()
                self.enabled = True

            def set_trading_enabled(self, enabled):
                self.enabled = enabled

            def on_bar1s(self, bar):
                self.bars_received.append(bar)
                if not self.enabled:
                    return []
                return [
                    OrderIntent(
                        symbol=bar.symbol,
                        side='SELL',
                        price=bar.close + Decimal('10'),
                        quantity=Decimal('1'),
                        client_order_id=f'order-{bar.timestamp}',
                    )
                ]

        events = [
            Bar1s(
                symbol='BTCUSDT', timestamp=1_000, available_time=2_000,
                open=Decimal('100'), high=Decimal('100'), low=Decimal('100'),
                close=Decimal('100'), volume=Decimal('1'), trade_count=1,
                vwap=Decimal('100'),
            ),
            Bar1s(
                symbol='BTCUSDT', timestamp=2_000, available_time=3_000,
                open=Decimal('100'), high=Decimal('100'), low=Decimal('100'),
                close=Decimal('100'), volume=Decimal('1'), trade_count=1,
                vwap=Decimal('100'),
            ),
        ]
        strategy = WarmupAwareStrategy()
        config = BacktestConfig(trading_start_ms=3_000)

        result = BacktestEngine(strategy, events, config).run()

        self.assertEqual(len(strategy.bars_received), 2)
        self.assertEqual(len(result.orders), 1)
        self.assertEqual(result.orders[0].client_order_id, 'order-2000')
        self.assertEqual(result.virtual_time_start, 3_000)

    def test_event_sorting(self):
        """测试事件排序"""
        # 创建测试事件（乱序）
        events = [
            Bar1s(
                symbol='BTCUSDT',
                timestamp=1000,
                available_time=2000,
                open=Decimal('50000'),
                high=Decimal('50100'),
                low=Decimal('49900'),
                close=Decimal('50050'),
                volume=Decimal('10'),
                trade_count=100,
                vwap=Decimal('50025'),
                type_priority=1,
                sequence=0
            ),
            Kline(
                symbol='BTCUSDT',
                interval='1m',
                open_time=1000,
                close_time=60000,
                available_time=2000,  # 与 Bar 相同时间
                open=Decimal('50000'),
                high=Decimal('50200'),
                low=Decimal('49800'),
                close=Decimal('50100'),
                volume=Decimal('100'),
                type_priority=2,
                sequence=0
            ),
            Bar1s(
                symbol='BTCUSDT',
                timestamp=2000,
                available_time=3000,
                open=Decimal('50050'),
                high=Decimal('50150'),
                low=Decimal('49950'),
                close=Decimal('50100'),
                volume=Decimal('12'),
                trade_count=120,
                vwap=Decimal('50075'),
                type_priority=1,
                sequence=1
            ),
        ]

        # 排序
        events.sort(
            key=lambda e: (
                e.available_time,
                e.type_priority,
                e.symbol,
                e.sequence
            )
        )

        # 验证排序结果
        self.assertEqual(len(events), 3)
        # 第一个应该是 Bar（type_priority=1）
        self.assertIsInstance(events[0], Bar1s)
        self.assertEqual(events[0].available_time, 2000)
        # 第二个应该是 Kline（type_priority=2）
        self.assertIsInstance(events[1], Kline)
        self.assertEqual(events[1].available_time, 2000)
        # 第三个应该是第二个 Bar
        self.assertIsInstance(events[2], Bar1s)
        self.assertEqual(events[2].available_time, 3000)

    def test_order_placement(self):
        """测试订单下单"""
        # 创建简单事件
        events = [
            Bar1s(
                symbol='BTCUSDT',
                timestamp=1000,
                available_time=2000,
                open=Decimal('50000'),
                high=Decimal('50100'),
                low=Decimal('49900'),
                close=Decimal('50000'),
                volume=Decimal('10'),
                trade_count=100,
                vwap=Decimal('50000'),
                type_priority=1,
                sequence=0
            )
        ]

        config = BacktestConfig()
        strategy = MockStrategy()
        engine = BacktestEngine(strategy, events, config)

        # 运行回测
        result = engine.run()

        # 验证订单已创建
        self.assertEqual(len(strategy.bars_received), 1)
        self.assertEqual(len(result.orders), 1)

        order = result.orders[0]
        self.assertEqual(order.symbol, 'BTCUSDT')
        self.assertEqual(order.side, 'SELL')
        self.assertEqual(order.status, 'NEW')  # 未成交

    def test_order_fill_sell(self):
        """测试做空订单成交"""
        # 创建事件：第二个 Bar 触及挂单价
        events = [
            Bar1s(
                symbol='BTCUSDT',
                timestamp=1000,
                available_time=2000,
                open=Decimal('50000'),
                high=Decimal('50100'),
                low=Decimal('49900'),
                close=Decimal('50000'),
                volume=Decimal('10'),
                trade_count=100,
                vwap=Decimal('50000'),
                type_priority=1,
                sequence=0
            ),
            Bar1s(
                symbol='BTCUSDT',
                timestamp=2000,
                available_time=3000,
                open=Decimal('50000'),
                high=Decimal('50200'),  # 触及挂单价（50000 - 10 = 49990）
                low=Decimal('49800'),
                close=Decimal('50100'),
                volume=Decimal('12'),
                trade_count=120,
                vwap=Decimal('50000'),
                type_priority=1,
                sequence=1
            )
        ]

        config = BacktestConfig()
        strategy = MockStrategy()
        engine = BacktestEngine(strategy, events, config)

        # 运行回测
        result = engine.run()

        # 验证成交
        self.assertEqual(len(result.fills), 1)
        fill = result.fills[0]
        self.assertEqual(fill.symbol, 'BTCUSDT')
        self.assertEqual(fill.side, 'SELL')
        self.assertEqual(fill.price, Decimal('50000'))  # 触发 bar 开盘价（不差于挂单价 49990）

        # 验证订单状态
        order = result.orders[0]
        self.assertEqual(order.status, 'FILLED')
        self.assertEqual(order.filled_quantity, Decimal('0.001'))

        # 未确认期末强平规则时，回测必须如实保留未平仓状态。
        self.assertEqual(len(result.positions), 1)
        position = result.positions[0]
        self.assertEqual(position.status, 'OPEN')
        self.assertIsNone(position.closed_at)
        self.assertEqual(position.unrealized_pnl, Decimal('-0.100'))
        summary = ResultAnalyzer(result).analyze()
        self.assertEqual(summary['positions']['open'], 1)
        self.assertAlmostEqual(summary['pnl']['total_unrealized'], -0.10)
        self.assertAlmostEqual(summary['pnl']['net_pnl'], -0.11)

    def test_ttl_expiration(self):
        """测试订单 TTL 过期"""
        # 创建事件，第二个 Bar 时订单已过期
        events = [
            Bar1s(
                symbol='BTCUSDT',
                timestamp=1000,
                available_time=2000,
                open=Decimal('50000'),
                high=Decimal('50100'),
                low=Decimal('49900'),
                close=Decimal('50000'),
                volume=Decimal('10'),
                trade_count=100,
                vwap=Decimal('50000'),
                type_priority=1,
                sequence=0
            ),
            Bar1s(
                symbol='BTCUSDT',
                timestamp=62000,  # 60秒后
                available_time=63000,
                open=Decimal('50000'),
                high=Decimal('50200'),
                low=Decimal('49800'),
                close=Decimal('50100'),
                volume=Decimal('12'),
                trade_count=120,
                vwap=Decimal('50000'),
                type_priority=1,
                sequence=1
            )
        ]

        config = BacktestConfig()

        # 使用带 TTL 的策略
        class TTLStrategy(MockStrategy):
            def on_bar1s(self, bar: Bar1s) -> list[OrderIntent] | None:
                self.bars_received.append(bar)
                if len(self.bars_received) == 1:
                    return [
                        OrderIntent(
                            symbol=bar.symbol,
                            side='SELL',
                            price=bar.close - Decimal('10'),
                            quantity=Decimal('0.001'),
                            client_order_id='test_order_1',
                            ttl_ms=60000,  # 60秒 TTL
                            strategy_id='test',
                            trigger_reason='test_trigger'
                        )
                    ]
                return None

        strategy = TTLStrategy()
        engine = BacktestEngine(strategy, events, config)

        # 运行回测
        result = engine.run()

        # 验证订单已过期，未成交
        self.assertEqual(len(result.fills), 0)
        order = result.orders[0]
        self.assertEqual(order.status, 'EXPIRED')

    def test_kline_execution_timeframe_can_fill_without_1s_events(self):
        class KlineStrategy:
            def __init__(self):
                self.sent = False

            def on_bar1s(self, bar):
                return None

            def on_kline(self, kline):
                if not self.sent:
                    self.sent = True
                    return [OrderIntent(
                        symbol=kline.symbol, side="SELL", price=Decimal("110"),
                        quantity=Decimal("1"), client_order_id="kline-order",
                        strategy_id="test", trigger_reason="test",
                    )]
                return None

            def on_fill(self, fill):
                pass

        def make_kline(open_time, high):
            return Kline(
                symbol="BTCUSDT", interval="1m", open_time=open_time,
                close_time=open_time + 59_999, available_time=open_time + 60_000,
                open=Decimal("100"), high=Decimal(str(high)), low=Decimal("99"),
                close=Decimal("100"), volume=Decimal("1"),
            )

        engine = BacktestEngine(
            KlineStrategy(), [make_kline(0, 101), make_kline(60_000, 111)],
            BacktestConfig(), execution_timeframe="1m",
        )
        result = engine.run()
        self.assertEqual(len(result.fills), 1)

    def test_position_management(self):
        """测试持仓管理"""
        # 创建事件：开仓 -> 平仓
        events = [
            # 第一个 Bar：下开仓单
            Bar1s(
                symbol='BTCUSDT',
                timestamp=1000,
                available_time=2000,
                open=Decimal('50000'),
                high=Decimal('50100'),
                low=Decimal('49900'),
                close=Decimal('50000'),
                volume=Decimal('10'),
                trade_count=100,
                vwap=Decimal('50000'),
                type_priority=1,
                sequence=0
            ),
            # 第二个 Bar：开仓成交
            Bar1s(
                symbol='BTCUSDT',
                timestamp=2000,
                available_time=3000,
                open=Decimal('50000'),
                high=Decimal('50200'),
                low=Decimal('49800'),
                close=Decimal('49900'),
                volume=Decimal('12'),
                trade_count=120,
                vwap=Decimal('50000'),
                type_priority=1,
                sequence=1
            ),
        ]

        config = BacktestConfig()
        strategy = MockStrategy()
        engine = BacktestEngine(strategy, events, config)

        # 运行回测
        result = engine.run()

        # 验证持仓
        self.assertEqual(len(result.positions), 1)
        pos = result.positions[0]
        self.assertEqual(pos.symbol, 'BTCUSDT')
        self.assertEqual(pos.side, 'SHORT')
        self.assertEqual(pos.entry_price, Decimal('50000'))  # SELL 按触发 bar 开盘价成交


def run_tests():
    """运行所有测试"""
    unittest.main(argv=[''], verbosity=2, exit=False)


if __name__ == '__main__':
    run_tests()
