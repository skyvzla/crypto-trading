"""
回测引擎模块

确定性回测引擎，基于历史数据验证策略性能。
"""
from .engine import BacktestEngine
from .executor import BacktestExecutor
from .loader import BacktestDataLoader, MetricsDataLoader
from .result import BacktestResult, ResultAnalyzer

__all__ = [
    'BacktestEngine',
    'BacktestExecutor',
    'BacktestDataLoader',
    'MetricsDataLoader',
    'BacktestResult',
    'ResultAnalyzer',
]
