"""
回测结果分析

计算性能指标、生成报告。
"""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from decimal import Decimal
from typing import Any

import pandas as pd
import numpy as np

from trading_platform.shared.events import Order, Fill, Position, StrategyAuditEvent
from trading_platform.shared.config import BacktestConfig

logger = logging.getLogger(__name__)


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
            'orders': pd.DataFrame(orders_data),
            'fills': pd.DataFrame(fills_data),
            'positions': pd.DataFrame(positions_data),
            'audit_events': pd.DataFrame(audit_data),
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
            'pnl': self._analyze_pnl(),
        }

        return summary

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

    def save_results(self, output_dir: str, run_id: str) -> None:
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

        # 保存汇总指标
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
            }
        }

        with open(output_path / 'run_meta.json', 'w') as f:
            json.dump(run_meta, f, indent=2)

        logger.info(f"Results saved: {output_path}")
