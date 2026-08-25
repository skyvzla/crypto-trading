"""
回测结果分析

计算性能指标、生成报告。
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Any

import pandas as pd
import numpy as np

from trading_platform.shared.events import Order, Fill, Position, StrategyAuditEvent
from trading_platform.shared.config import BacktestConfig

logger = logging.getLogger(__name__)

TRADE_COLUMNS = [
    'trade_id', 'symbol', 'side', 'campaign_id', 'signal_time',
    'signal_time_iso', 'entry_pattern', 'pullback_before_first_entry',
    'pullback_time', 'pullback_time_iso', 'pullback_low',
    'pullback_threshold', 'pullback_atr', 'signal_cooldown_seconds',
    'order_ttl_seconds', 'exit_policy', 'strategy_version', 'entry_tier_mode',
    'early_profit_unlock_ratio', 'spike_high', 'spike_high_time',
    'spike_high_time_iso', 'prior_high', 'prior_high_time',
    'prior_high_time_iso', 'prior_high_4h', 'prior_high_4h_time',
    'prior_high_4h_time_iso', 'prior_high_lookback_minutes',
    'rise_low_lookback_minutes', 'min_rise_duration_minutes',
    'rise_low', 'rise_low_time', 'rise_low_time_iso', 'rise_low_age_minutes',
    'atr', 'origin_price', 'origin_floor',
    'trigger_price', 'rise_5s', 'volume_5s', 'median_volume_1s',
    'volume_multiple_5s', 'low_12h', 'rise_from_12h_low', 'tier_prices',
    'td_sell_setup_5m', 'td_sell_setup_15m', 'upper_wick_ratio_5m',
    'upper_wick_ratio_15m', 'volume_multiple_5m',
    'tier_weights',
    'tier1_price', 'tier2_price', 'tier3_price',
    'tier1_weight', 'tier2_weight', 'tier3_weight',
    'tier1_order_status', 'tier2_order_status', 'tier3_order_status',
    'tier1_fill_count', 'tier2_fill_count', 'tier3_fill_count',
    'tier1_fill_quantity', 'tier2_fill_quantity', 'tier3_fill_quantity',
    'tier1_avg_fill_price', 'tier2_avg_fill_price', 'tier3_avg_fill_price',
    'invalid_price', 'entry_action', 'entry_time',
    'entry_time_iso', 'entry_price', 'entry_quantity', 'entry_notional',
    'entry_fill_count', 'max_adverse_price', 'max_adverse_return',
    'max_unrealized_loss', 'liquidation_position_ratio',
    'full_position_liquidation', 'full_position_liquidation_time',
    'full_position_liquidation_time_iso', 'exit_action',
    'exit_time', 'exit_time_iso',
    'exit_price', 'exit_quantity', 'exit_fill_count', 'exit_reason',
    'status', 'gross_pnl', 'commission', 'net_pnl', 'net_return', 'winner',
]


@dataclass
class BacktestResult:
    """
    回测结果

    包含所有订单、成交、持仓记录，以及时间范围
    """
    virtual_time_start: int
    virtual_time_end: int
    orders: list[Order]
    fills: list[Fill]
    positions: list[Position]
    config: BacktestConfig
    events_processed: int = 0
    audit_events: list[StrategyAuditEvent] = field(default_factory=list)

    def to_dataframes(self) -> dict[str, pd.DataFrame]:
        """
        转换为 DataFrame 格式

        Returns:
            包含 orders, fills, positions 的字典
        """
        # 转换 Order
        orders_data = []
        for order in self.orders:
            orders_data.append({
                'order_id': order.order_id,
                'client_order_id': order.client_order_id,
                'account_id': order.account_id,
                'symbol': order.symbol,
                'side': order.side,
                'type': order.type,
                'price': float(order.price),
                'quantity': float(order.quantity),
                'status': order.status,
                'created_at': order.created_at,
                'ttl_ms': order.ttl_ms,
                'reduce_only': order.reduce_only,
                'filled_quantity': float(order.filled_quantity),
                'fill_time': order.fill_time,
                'cancel_time': order.cancel_time,
                'strategy_id': order.strategy_id,
                'trigger_reason': order.trigger_reason,
                'campaign_id': order.campaign_id,
            })

        # 转换 Fill
        fills_data = []
        for fill in self.fills:
            fills_data.append({
                'fill_id': fill.fill_id,
                'order_id': fill.order_id,
                'symbol': fill.symbol,
                'side': fill.side,
                'price': float(fill.price),
                'quantity': float(fill.quantity),
                'commission': float(fill.commission),
                'commission_asset': fill.commission_asset,
                'fill_time': fill.fill_time,
                'is_maker': fill.is_maker,
            })

        # 转换 Position
        positions_data = []
        for pos in self.positions:
            positions_data.append({
                'symbol': pos.symbol,
                'side': pos.side,
                'entry_price': float(pos.entry_price),
                'quantity': float(pos.quantity),
                'total_commission': float(pos.total_commission),
                'unrealized_pnl': float(pos.unrealized_pnl),
                'realized_pnl': float(pos.realized_pnl),
                'opened_at': pos.opened_at,
                'closed_at': pos.closed_at,
                'status': pos.status,
                'max_adverse_price': (
                    float(pos.max_adverse_price)
                    if pos.max_adverse_price is not None else None
                ),
                'max_adverse_return': float(pos.max_adverse_return),
                'max_unrealized_loss': float(pos.max_unrealized_loss),
                'liquidation_position_ratio': (
                    float(pos.liquidation_position_ratio)
                    if pos.liquidation_position_ratio is not None else None
                ),
                'full_position_liquidation': pos.full_position_liquidation,
                'full_position_liquidation_time': (
                    pos.full_position_liquidation_time
                ),
            })

        audit_data = []
        for event in self.audit_events:
            audit_data.append({
                'event_time': event.event_time,
                'event_type': event.event_type,
                'symbol': event.symbol,
                'strategy_id': event.strategy_id,
                'campaign_id': event.campaign_id,
                'details': json.dumps(
                    event.details,
                    sort_keys=True,
                    ensure_ascii=True,
                ),
            })

        return {
            'orders': pd.DataFrame(orders_data, columns=[
                'order_id', 'client_order_id', 'account_id', 'symbol', 'side',
                'type', 'price', 'quantity', 'status', 'created_at', 'ttl_ms',
                'reduce_only', 'filled_quantity', 'fill_time', 'cancel_time',
                'strategy_id', 'trigger_reason', 'campaign_id',
            ]),
            'fills': pd.DataFrame(fills_data, columns=[
                'fill_id', 'order_id', 'symbol', 'side', 'price', 'quantity',
                'commission', 'commission_asset', 'fill_time', 'is_maker',
            ]),
            'positions': pd.DataFrame(positions_data, columns=[
                'symbol', 'side', 'entry_price', 'quantity', 'total_commission',
                'unrealized_pnl', 'realized_pnl', 'opened_at', 'closed_at',
                'status', 'max_adverse_price', 'max_adverse_return',
                'max_unrealized_loss', 'liquidation_position_ratio',
                'full_position_liquidation', 'full_position_liquidation_time',
            ]),
            'audit_events': pd.DataFrame(audit_data, columns=[
                'event_time', 'event_type', 'symbol', 'strategy_id',
                'campaign_id', 'details',
            ]),
        }


class ResultAnalyzer:
    """
    结果分析器

    计算各类性能指标：
    - Profit Factor
    - Sharpe Ratio
    - 最大回撤
    - 胜率
    - 止损分析
    """

    def __init__(self, result: BacktestResult):
        """
        Args:
            result: 回测结果
        """
        self.result = result
        self.dfs = result.to_dataframes()
        self.dfs['trades'] = self._build_trades()

    def analyze(self) -> dict[str, Any]:
        """
        完整分析

        Returns:
            包含所有指标的字典
        """
        summary = {
            'time_range': {
                'start_ms': self.result.virtual_time_start,
                'end_ms': self.result.virtual_time_end,
                'duration_days': (
                    self.result.virtual_time_end - self.result.virtual_time_start
                ) / (1000 * 86400)
            },
            'orders': self._analyze_orders(),
            'positions': self._analyze_positions(),
            'trades': self._analyze_trades(),
            'liquidation_risk': self._analyze_liquidation_risk(),
            'pnl': self._analyze_pnl(),
        }

        return summary

    @staticmethod
    def _timestamp_iso(value: Any) -> str | None:
        if value is None or pd.isna(value):
            return None
        return datetime.fromtimestamp(
            int(value) / 1000, tz=timezone.utc
        ).isoformat()

    @staticmethod
    def _detail_number(details: dict, key: str) -> float | None:
        value = details.get(key)
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _tier_details(
        plan: dict,
        campaign_orders: pd.DataFrame,
        entries: pd.DataFrame,
    ) -> dict[str, Any]:
        """展开三档计划和实际入场成交，便于直接复核分档执行。"""
        prices = plan.get('tier_prices')
        weights = plan.get('tier_weights')
        details: dict[str, Any] = {}

        for tier in range(1, 4):
            index = tier - 1
            tier_name = f'tier{tier}'
            planned_price = None
            if isinstance(prices, list) and index < len(prices):
                try:
                    planned_price = float(prices[index])
                except (TypeError, ValueError):
                    planned_price = None
            planned_weight = None
            if isinstance(weights, list) and index < len(weights):
                try:
                    planned_weight = float(weights[index])
                except (TypeError, ValueError):
                    planned_weight = None

            tier_orders = campaign_orders[
                campaign_orders['trigger_reason'] == f'spike_tier{tier}'
            ]
            order_statuses = sorted(
                {
                    str(status)
                    for status in tier_orders['status'].dropna()
                    if str(status)
                }
            )
            tier_entries = entries[
                entries['trigger_reason'] == f'spike_tier{tier}'
            ]
            fill_quantity = float(tier_entries['quantity'].sum())
            fill_notional = float(
                (tier_entries['price'] * tier_entries['quantity']).sum()
            )

            details[f'{tier_name}_price'] = planned_price
            details[f'{tier_name}_weight'] = planned_weight
            details[f'{tier_name}_order_status'] = (
                ';'.join(order_statuses) if order_statuses else 'NOT_PLACED'
            )
            details[f'{tier_name}_fill_count'] = int(len(tier_entries))
            details[f'{tier_name}_fill_quantity'] = fill_quantity
            details[f'{tier_name}_avg_fill_price'] = (
                fill_notional / fill_quantity if fill_quantity > 0 else None
            )

        return details

    def _build_trades(self) -> pd.DataFrame:
        """将订单、成交、持仓和策略审计合并为逐笔交易复核表。"""
        orders = self.dfs['orders']
        fills = self.dfs['fills']
        positions = self.dfs['positions']
        audits = self.dfs['audit_events']

        plans: dict[str, dict] = {}
        triggers: dict[str, dict] = {}
        first_fills: dict[str, dict] = {}
        for event in audits.itertuples(index=False):
            if not isinstance(event.campaign_id, str):
                continue
            try:
                details = json.loads(event.details or '{}')
            except (TypeError, json.JSONDecodeError):
                details = {}
            if event.event_type == 'entry_plan_created':
                plans[event.campaign_id] = details
            elif event.event_type == 'signal_triggered':
                triggers[event.campaign_id] = details
            elif event.event_type == 'campaign_first_fill':
                first_fills[event.campaign_id] = details

        order_fields = [
            'order_id', 'reduce_only', 'campaign_id', 'trigger_reason'
        ]
        fill_orders = fills.merge(
            orders[order_fields], on='order_id', how='left'
        )
        rows: list[dict[str, Any]] = []

        for position in positions.itertuples(index=False):
            opened_at = int(position.opened_at)
            is_closed = position.status == 'CLOSED' and not pd.isna(position.closed_at)
            closed_at = int(position.closed_at) if is_closed else None
            period = fill_orders[
                (fill_orders['symbol'] == position.symbol)
                & (fill_orders['fill_time'] >= opened_at)
            ]
            if closed_at is not None:
                period = period[period['fill_time'] <= closed_at]

            entries = period[
                (period['side'] == ('SELL' if position.side == 'SHORT' else 'BUY'))
                & (period['reduce_only'] == False)
            ]
            exits = period[
                (period['side'] == ('BUY' if position.side == 'SHORT' else 'SELL'))
                & (period['reduce_only'] == True)
            ]

            campaign_id = None
            if not entries.empty:
                campaign_values = entries['campaign_id'].dropna()
                if not campaign_values.empty:
                    campaign_id = str(campaign_values.iloc[0])

            campaign_orders = (
                orders[orders['campaign_id'] == campaign_id]
                if campaign_id
                else orders.iloc[0:0]
            )

            plan = plans.get(campaign_id or '', {})
            trigger = triggers.get(campaign_id or '', {})
            first_fill = first_fills.get(campaign_id or '', {})
            metrics = dict(trigger)
            metrics.update(plan)

            signal_time = None
            if campaign_id and ':' in campaign_id:
                try:
                    signal_time = int(campaign_id.rsplit(':', 1)[1])
                except ValueError:
                    signal_time = None

            entry_time = (
                int(entries['fill_time'].min())
                if not entries.empty else opened_at
            )
            exit_time = (
                int(exits['fill_time'].max())
                if not exits.empty else closed_at
            )
            entry_quantity = float(entries['quantity'].sum())
            exit_quantity = float(exits['quantity'].sum())
            entry_notional = float(
                (entries['price'] * entries['quantity']).sum()
            )
            if entry_notional <= 0:
                entry_notional = float(position.entry_price * position.quantity)
            exit_price = None
            if exit_quantity > 0:
                exit_price = float(
                    (exits['price'] * exits['quantity']).sum() / exit_quantity
                )

            gross_pnl = float(
                position.realized_pnl
                if is_closed else position.unrealized_pnl
            )
            commission = float(position.total_commission)
            net_pnl = gross_pnl - commission
            exit_reasons = sorted(
                {
                    str(reason)
                    for reason in exits['trigger_reason'].dropna()
                    if str(reason)
                }
            )
            entry_pattern = first_fill.get('entry_pattern', 'unknown')
            pullback_before = first_fill.get('pullback_before_fill')
            if pullback_before is not None:
                pullback_before = bool(pullback_before)

            tier_details = self._tier_details(
                plan, campaign_orders, entries
            )

            def detail_number(key: str) -> float | None:
                return self._detail_number(metrics, key)

            def first_number(key: str) -> float | None:
                return self._detail_number(first_fill, key)

            pullback_time = first_fill.get('pullback_time')
            if pullback_time is not None:
                try:
                    pullback_time = int(pullback_time)
                except (TypeError, ValueError):
                    pullback_time = None
            spike_high_time = metrics.get('spike_high_time')
            if spike_high_time is not None:
                try:
                    spike_high_time = int(spike_high_time)
                except (TypeError, ValueError):
                    spike_high_time = None

            prior_high_time = metrics.get('prior_high_time')
            if prior_high_time is not None:
                try:
                    prior_high_time = int(prior_high_time)
                except (TypeError, ValueError):
                    prior_high_time = None
            prior_high_4h_time = metrics.get('prior_high_4h_time')
            if prior_high_4h_time is not None:
                try:
                    prior_high_4h_time = int(prior_high_4h_time)
                except (TypeError, ValueError):
                    prior_high_4h_time = None
            rise_low_time = metrics.get('rise_low_time')
            if rise_low_time is not None:
                try:
                    rise_low_time = int(rise_low_time)
                except (TypeError, ValueError):
                    rise_low_time = None

            rows.append({
                'trade_id': campaign_id or f'{position.symbol}:{opened_at}',
                'symbol': position.symbol,
                'side': position.side,
                'campaign_id': campaign_id,
                'signal_time': signal_time,
                'signal_time_iso': self._timestamp_iso(signal_time),
                'entry_pattern': entry_pattern,
                'pullback_before_first_entry': pullback_before,
                'pullback_time': pullback_time,
                'pullback_time_iso': self._timestamp_iso(pullback_time),
                'pullback_low': first_number('pullback_low'),
                'pullback_threshold': detail_number('pullback_threshold'),
                'pullback_atr': detail_number('pullback_atr'),
                'signal_cooldown_seconds': detail_number('signal_cooldown_seconds'),
                'order_ttl_seconds': detail_number('order_ttl_seconds'),
                'exit_policy': metrics.get('exit_policy'),
                'strategy_version': metrics.get('strategy_version', 'v1'),
                'entry_tier_mode': metrics.get('entry_tier_mode', 'three-tier'),
                'early_profit_unlock_ratio': detail_number(
                    'early_profit_unlock_ratio'
                ),
                'spike_high': detail_number('spike_high'),
                'spike_high_time': spike_high_time,
                'spike_high_time_iso': self._timestamp_iso(spike_high_time),
                'prior_high': detail_number('prior_high'),
                'prior_high_time': prior_high_time,
                'prior_high_time_iso': self._timestamp_iso(prior_high_time),
                'prior_high_4h': detail_number('prior_high_4h'),
                'prior_high_4h_time': prior_high_4h_time,
                'prior_high_4h_time_iso': self._timestamp_iso(prior_high_4h_time),
                'prior_high_lookback_minutes': detail_number(
                    'prior_high_lookback_minutes'
                ),
                'rise_low_lookback_minutes': detail_number(
                    'rise_low_lookback_minutes'
                ),
                'min_rise_duration_minutes': detail_number(
                    'min_rise_duration_minutes'
                ),
                'rise_low': detail_number('rise_low'),
                'rise_low_time': rise_low_time,
                'rise_low_time_iso': self._timestamp_iso(rise_low_time),
                'rise_low_age_minutes': detail_number('rise_low_age_minutes'),
                'atr': detail_number('atr'),
                'origin_price': detail_number('origin_price'),
                'origin_floor': detail_number('origin_floor'),
                'trigger_price': detail_number('trigger_price'),
                'rise_5s': detail_number('rise_5s'),
                'volume_5s': detail_number('volume_5s'),
                'median_volume_1s': detail_number('median_volume_1s'),
                'volume_multiple_5s': detail_number('volume_multiple_5s'),
                'low_12h': detail_number('low_12h'),
                'rise_from_12h_low': detail_number('rise_from_12h_low'),
                'td_sell_setup_5m': detail_number('td_sell_setup_5m'),
                'td_sell_setup_15m': detail_number('td_sell_setup_15m'),
                'upper_wick_ratio_5m': detail_number('upper_wick_ratio_5m'),
                'upper_wick_ratio_15m': detail_number('upper_wick_ratio_15m'),
                'volume_multiple_5m': detail_number('volume_multiple_5m'),
                'tier_prices': json.dumps(
                    metrics.get('tier_prices', []), ensure_ascii=True
                ),
                'tier_weights': json.dumps(
                    metrics.get('tier_weights', []), ensure_ascii=True
                ),
                **tier_details,
                'invalid_price': detail_number('invalid_price'),
                'entry_action': 'SELL' if position.side == 'SHORT' else 'BUY',
                'entry_time': entry_time,
                'entry_time_iso': self._timestamp_iso(entry_time),
                'entry_price': float(position.entry_price),
                'entry_quantity': entry_quantity,
                'entry_notional': entry_notional,
                'entry_fill_count': int(len(entries)),
                'max_adverse_price': position.max_adverse_price,
                'max_adverse_return': position.max_adverse_return,
                'max_unrealized_loss': position.max_unrealized_loss,
                'liquidation_position_ratio': position.liquidation_position_ratio,
                'full_position_liquidation': bool(
                    position.full_position_liquidation
                ),
                'full_position_liquidation_time': (
                    position.full_position_liquidation_time
                ),
                'full_position_liquidation_time_iso': self._timestamp_iso(
                    position.full_position_liquidation_time
                ),
                'exit_action': 'BUY' if position.side == 'SHORT' else 'SELL',
                'exit_time': exit_time,
                'exit_time_iso': self._timestamp_iso(exit_time),
                'exit_price': exit_price,
                'exit_quantity': exit_quantity,
                'exit_fill_count': int(len(exits)),
                'exit_reason': ';'.join(exit_reasons) if exit_reasons else (
                    'open' if not is_closed else None
                ),
                'status': position.status,
                'gross_pnl': gross_pnl,
                'commission': commission,
                'net_pnl': net_pnl,
                'net_return': net_pnl / entry_notional if entry_notional else None,
                'winner': (net_pnl > 0) if is_closed else None,
            })

        return pd.DataFrame(rows, columns=TRADE_COLUMNS)

    def _analyze_trades(self) -> dict[str, Any]:
        trades = self.dfs['trades']
        if trades.empty:
            return {
                'total': 0,
                'open': 0,
                'closed': 0,
                'profitable': 0,
                'loss': 0,
                'win_rate': 0.0,
                'short_term_high_pullback_rebreak': {
                    'total': 0, 'closed': 0, 'profitable': 0,
                    'loss': 0, 'win_rate': 0.0,
                },
            }

        closed = trades[trades['status'] == 'CLOSED']

        def stats(frame: pd.DataFrame) -> dict[str, Any]:
            net = frame['net_pnl'].astype(float)
            profitable = int((net > 0).sum())
            loss = int((net < 0).sum())
            return {
                'total': int(len(frame)),
                'closed': int((frame['status'] == 'CLOSED').sum()),
                'profitable': profitable,
                'loss': loss,
                'win_rate': profitable / len(frame) if len(frame) else 0.0,
                'net_pnl': float(net.sum()),
            }

        pattern = closed[
            closed['entry_pattern'] == 'short_term_high_pullback_rebreak'
        ]
        return {
            **stats(trades),
            'open': int((trades['status'] != 'CLOSED').sum()),
            'short_term_high_pullback_rebreak': stats(pattern),
            'by_pattern': {
                str(name): stats(group)
                for name, group in closed.groupby('entry_pattern', dropna=False)
            },
        }

    def _analyze_orders(self) -> dict[str, Any]:
        """分析订单统计"""
        orders_df = self.dfs['orders']

        if orders_df.empty:
            return {
                'total': 0,
                'filled': 0,
                'cancelled': 0,
                'expired': 0,
                'fill_rate': 0.0
            }

        status_counts = orders_df['status'].value_counts().to_dict()

        total = len(orders_df)
        filled = status_counts.get('FILLED', 0)

        return {
            'total': total,
            'filled': filled,
            'cancelled': status_counts.get('CANCELLED', 0),
            'expired': status_counts.get('EXPIRED', 0),
            'fill_rate': filled / total if total > 0 else 0.0,
            'by_symbol': orders_df.groupby('symbol').size().to_dict()
        }

    def _analyze_positions(self) -> dict[str, Any]:
        """分析持仓统计"""
        positions_df = self.dfs['positions']

        if positions_df.empty:
            return {
                'total': 0,
                'open': 0,
                'closed': 0,
                'profitable': 0,
                'loss': 0,
                'win_rate': 0.0
            }

        total = len(positions_df)
        closed_positions = positions_df[positions_df['status'] == 'CLOSED'].copy()
        closed_positions['net_position_pnl'] = (
            closed_positions['realized_pnl']
            - closed_positions['total_commission']
        )
        closed = len(closed_positions)
        profitable = len(closed_positions[closed_positions['net_position_pnl'] > 0])
        loss = len(closed_positions[closed_positions['net_position_pnl'] < 0])

        return {
            'total': total,
            'open': total - closed,
            'closed': closed,
            'profitable': profitable,
            'loss': loss,
            'win_rate': profitable / closed if closed > 0 else 0.0,
            'by_symbol': positions_df.groupby('symbol').size().to_dict()
        }

    def _analyze_liquidation_risk(self) -> dict[str, Any]:
        """汇总满仓理论爆仓风险，不改变回测原始收益。"""
        trades = self.dfs['trades']
        if trades.empty:
            return {
                'total': 0,
                'rate': 0.0,
                'final_net_pnl': 0.0,
            }
        liquidated = trades[
            trades['full_position_liquidation'].fillna(False).astype(bool)
        ]
        return {
            'total': int(len(liquidated)),
            'rate': float(len(liquidated) / len(trades)),
            'final_net_pnl': float(liquidated['net_pnl'].sum()),
        }

    def _analyze_pnl(self) -> dict[str, Any]:
        """分析盈亏指标"""
        positions_df = self.dfs['positions']

        if positions_df.empty:
            return {
                'total_realized': 0.0,
                'total_unrealized': 0.0,
                'total_commission': 0.0,
                'net_pnl': 0.0,
                'profit_factor': 0.0,
                'max_drawdown': 0.0,
                'sharpe_ratio': 0.0,
                'total_profit': 0.0,
                'total_loss': 0.0,
            }

        total_realized = positions_df['realized_pnl'].sum()
        total_unrealized = positions_df['unrealized_pnl'].sum()
        total_commission = positions_df['total_commission'].sum()
        net_pnl = total_realized + total_unrealized - total_commission

        closed_positions = positions_df[positions_df['status'] == 'CLOSED'].copy()
        closed_positions['net_position_pnl'] = (
            closed_positions['realized_pnl']
            - closed_positions['total_commission']
        )

        # Profit Factor: 盈利交易总额 / 亏损交易总额
        profitable_positions = closed_positions[
            closed_positions['net_position_pnl'] > 0
        ]
        loss_positions = closed_positions[
            closed_positions['net_position_pnl'] < 0
        ]

        total_profit = profitable_positions['net_position_pnl'].sum()
        total_loss = abs(loss_positions['net_position_pnl'].sum())

        profit_factor = total_profit / total_loss if total_loss > 0 else (
            float('inf') if total_profit > 0 else 0.0
        )

        # 最大回撤
        max_drawdown = self._calculate_max_drawdown(closed_positions)

        # Sharpe Ratio
        sharpe_ratio = self._calculate_sharpe_ratio(closed_positions)

        return {
            'total_realized': float(total_realized),
            'total_unrealized': float(total_unrealized),
            'total_commission': float(total_commission),
            'net_pnl': float(net_pnl),
            'profit_factor': float(profit_factor) if profit_factor != float('inf') else 999.0,
            'max_drawdown': float(max_drawdown),
            'sharpe_ratio': float(sharpe_ratio),
            'total_profit': float(total_profit),
            'total_loss': float(total_loss),
        }

    def _calculate_max_drawdown(self, positions_df: pd.DataFrame) -> float:
        """
        计算最大回撤

        Args:
            positions_df: 持仓 DataFrame

        Returns:
            最大回撤（负数）
        """
        if positions_df.empty:
            return 0.0

        # 按平仓时间排序
        positions_df = positions_df.sort_values('closed_at')

        # 计算累计盈亏
        pnl_column = (
            'net_position_pnl'
            if 'net_position_pnl' in positions_df.columns
            else 'realized_pnl'
        )
        cumulative_pnl = positions_df[pnl_column].cumsum()

        # 计算回撤
        running_max = cumulative_pnl.cummax()
        drawdown = cumulative_pnl - running_max

        return float(drawdown.min())

    def _calculate_sharpe_ratio(self, positions_df: pd.DataFrame) -> float:
        """
        计算 Sharpe Ratio

        Args:
            positions_df: 持仓 DataFrame

        Returns:
            Sharpe Ratio
        """
        if positions_df.empty or len(positions_df) < 2:
            return 0.0

        pnl_column = (
            'net_position_pnl'
            if 'net_position_pnl' in positions_df.columns
            else 'realized_pnl'
        )
        returns = positions_df[pnl_column].values

        mean_return = np.mean(returns)
        std_return = np.std(returns, ddof=1)

        if std_return == 0:
            return 0.0

        # 假设无风险利率为 0
        sharpe = mean_return / std_return

        # 年化（假设每天一个交易）
        days = (
            self.result.virtual_time_end - self.result.virtual_time_start
        ) / (1000 * 86400)
        trades_per_day = len(positions_df) / days if days > 0 else 1

        sharpe_annualized = sharpe * np.sqrt(trades_per_day * 365)

        return float(sharpe_annualized)

    def save_results(
        self,
        output_dir: str,
        run_id: str,
        *,
        summary: dict[str, Any] | None = None,
    ) -> None:
        """
        保存回测结果到文件

        Args:
            output_dir: 输出目录
            run_id: 运行ID
        """
        output_path = Path(output_dir) / run_id
        output_path.mkdir(parents=True, exist_ok=True)

        logger.info(f"Saving results to {output_path}")

        # 保存 Parquet 文件
        dfs = self.dfs
        dfs['orders'].to_parquet(output_path / 'orders.parquet', index=False)
        dfs['fills'].to_parquet(output_path / 'fills.parquet', index=False)
        dfs['positions'].to_parquet(output_path / 'positions.parquet', index=False)
        dfs['audit_events'].to_parquet(
            output_path / 'audit_events.parquet', index=False
        )
        dfs['trades'].to_parquet(output_path / 'trades.parquet', index=False)
        dfs['trades'].to_csv(output_path / 'trades.csv', index=False)

        # 保存汇总指标
        if summary is None:
            summary = self.analyze()
        with open(output_path / 'summary.json', 'w') as f:
            json.dump(summary, f, indent=2)

        # 保存运行元数据
        run_meta = {
            'run_id': run_id,
            'virtual_time_start': self.result.virtual_time_start,
            'virtual_time_end': self.result.virtual_time_end,
            'total_events': self.result.events_processed,
            'config': {
                'data_dir': self.result.config.data_dir,
                'maker_fee_rate': self.result.config.maker_fee_rate,
                'taker_fee_rate': self.result.config.taker_fee_rate,
                'limit_fill_fraction_per_bar': (
                    self.result.config.limit_fill_fraction_per_bar
                ),
                'bar1s_time_shift_ms': self.result.config.bar1s_time_shift_ms,
                'prior_high_lookback_minutes': (
                    self.result.config.prior_high_lookback_minutes
                ),
                'strategy_path': self.result.config.strategy_path,
                'spike_strategy_version': self.result.config.spike_strategy_version,
                'spike_entry_tier_mode': self.result.config.spike_entry_tier_mode,
                'spike_rise_low_lookback_minutes': (
                    self.result.config.spike_rise_low_lookback_minutes
                ),
                'spike_min_rise_duration_minutes': (
                    self.result.config.spike_min_rise_duration_minutes
                ),
                'spike_early_profit_unlock_ratio': (
                    self.result.config.spike_early_profit_unlock_ratio
                ),
            }
        }

        with open(output_path / 'run_meta.json', 'w') as f:
            json.dump(run_meta, f, indent=2)

        logger.info(f"Results saved: {output_path}")
