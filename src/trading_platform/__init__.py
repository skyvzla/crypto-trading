"""
Trading Platform - 三层架构量化交易平台

架构：
- market/: 行情层（Binance WS → 1s Bar → Redis）
- strategies/: 策略层（K线策略群 + 1s事件策略群）
- ledger/: 账本层（PostgreSQL + FastAPI + Vue3）
- shared/: 共享代码库（执行层、配置、事件定义）
- backtest/: 回测引擎（虚拟时钟驱动）
"""
__version__ = '1.0.0'
