"""
账本层主程序入口
python -m trading_platform.ledger
"""
from trading_platform.ledger.main import main
import asyncio

if __name__ == "__main__":
    asyncio.run(main())
