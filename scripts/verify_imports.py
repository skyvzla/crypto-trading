#!/usr/bin/env python3
"""
验证所有模块导入是否正常
"""
import sys
from pathlib import Path

# 允许在项目镜像内直接执行脚本；正式运行仍由已安装的包提供导入
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

def test_imports():
    """测试所有核心模块导入"""
    errors = []

    modules_to_test = [
        # 共享层
        "trading_platform.shared.config",
        "trading_platform.shared.events",
        "trading_platform.shared.logging_config",
        "trading_platform.shared.risk",
        "trading_platform.shared.order_states",
        "trading_platform.shared.execution_recovery",

        # Binance执行层
        "trading_platform.shared.binance.rest_client",
        "trading_platform.shared.binance.user_stream",
        "trading_platform.shared.binance.rate_limiter",
        "trading_platform.shared.binance.live_executor",

        # 行情层
        "trading_platform.market.feed.binance_ws",
        "trading_platform.market.feed.aggregator",
        "trading_platform.market.store.redis_pub",
        "trading_platform.market.store.kline_store",
        "trading_platform.market.api.routes",

        # 账本层
        "trading_platform.ledger.db.models",
        "trading_platform.ledger.binance_reports",
        "trading_platform.ledger.api.routes",

        # 策略层
        "trading_platform.strategies.kline.base",
        "trading_platform.strategies.tick.base",

        # 回测层
        "trading_platform.backtest.loader",
        "trading_platform.backtest.engine",
        "trading_platform.backtest.executor",
        "trading_platform.backtest.result",
        "trading_platform.backtest.runner",
    ]

    print("开始验证模块导入...")
    print("=" * 60)

    for module_name in modules_to_test:
        try:
            __import__(module_name)
            print(f"✅ {module_name}")
        except Exception as e:
            print(f"❌ {module_name}: {e}")
            errors.append((module_name, str(e)))

    print("=" * 60)

    if errors:
        print(f"\n❌ 发现 {len(errors)} 个导入错误:")
        for module, error in errors:
            print(f"  - {module}: {error}")
        return False
    else:
        print(f"\n✅ 所有 {len(modules_to_test)} 个模块导入成功！")
        return True

if __name__ == "__main__":
    success = test_imports()
    sys.exit(0 if success else 1)
