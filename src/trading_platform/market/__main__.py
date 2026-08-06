"""
行情层主程序入口
python -m trading_platform.market
"""
from trading_platform.market.main import main
import asyncio

if __name__ == "__main__":
    asyncio.run(main())
