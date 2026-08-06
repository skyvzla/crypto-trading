"""
生成示例回测数据

用于演示和测试回测引擎。
"""
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def generate_sample_aggtrades(
    symbol: str,
    start_time: datetime,
    duration_hours: int,
    output_dir: Path
) -> None:
    """
    生成示例 aggTrade 数据

    Args:
        symbol: 币种符号
        start_time: 开始时间
        duration_hours: 持续小时数
        output_dir: 输出目录
    """
    logger.info(f"Generating aggTrades for {symbol}...")

    # 计算时间范围
    start_ms = int(start_time.timestamp() * 1000)
    end_ms = start_ms + duration_hours * 3600 * 1000

    # 生成数据（每秒约100笔交易）
    num_trades = duration_hours * 3600 * 100

    # 基础价格（模拟随机游走）
    base_price = 50000.0 if 'BTC' in symbol else 3000.0
    price_changes = np.random.normal(0, base_price * 0.0001, num_trades)
    prices = base_price + np.cumsum(price_changes)

    # 生成时间戳（均匀分布）
    trade_times = np.linspace(start_ms, end_ms - 1, num_trades, dtype=np.int64)

    # 生成交易量
    quantities = np.random.exponential(0.01, num_trades)

    # 生成买卖方向
    sides = np.random.choice(['BUY', 'SELL'], num_trades)

    # 创建 DataFrame
    df = pd.DataFrame({
        'symbol': symbol,
        'trade_id': range(1, num_trades + 1),
        'price': prices,
        'qty': quantities,
        'side': sides,
        'trade_time': trade_times,
    })

    # 按时间排序
    df = df.sort_values('trade_time').reset_index(drop=True)

    # 保存
    output_path = output_dir / 'aggtrades' / f'{symbol}.parquet'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)

    logger.info(f"Saved {len(df)} aggTrades to {output_path}")


def generate_sample_klines(
    symbol: str,
    interval: str,
    start_time: datetime,
    duration_hours: int,
    output_dir: Path
) -> None:
    """
    生成示例 Kline 数据

    Args:
        symbol: 币种符号
        interval: K 线周期（如 '1m', '5m'）
        start_time: 开始时间
        duration_hours: 持续小时数
        output_dir: 输出目录
    """
    logger.info(f"Generating {interval} klines for {symbol}...")

    # 解析周期
    interval_map = {
        '1m': 60 * 1000,
        '5m': 5 * 60 * 1000,
        '15m': 15 * 60 * 1000,
    }

    interval_ms = interval_map.get(interval)
    if not interval_ms:
        raise ValueError(f"Unsupported interval: {interval}")

    # 计算时间范围
    start_ms = int(start_time.timestamp() * 1000)
    end_ms = start_ms + duration_hours * 3600 * 1000

    # 生成 K 线时间戳
    num_klines = (end_ms - start_ms) // interval_ms
    open_times = np.arange(start_ms, end_ms, interval_ms, dtype=np.int64)[:num_klines]
    close_times = open_times + interval_ms - 1

    # 基础价格
    base_price = 50000.0 if 'BTC' in symbol else 3000.0

    # 生成 OHLC（随机游走）
    opens = []
    highs = []
    lows = []
    closes = []
    volumes = []

    current_price = base_price

    for i in range(num_klines):
        # 开盘价
        open_price = current_price

        # 随机变化
        change = np.random.normal(0, base_price * 0.002)
        close_price = open_price + change

        # 高低价
        high_price = max(open_price, close_price) + abs(np.random.normal(0, base_price * 0.001))
        low_price = min(open_price, close_price) - abs(np.random.normal(0, base_price * 0.001))

        # 成交量
        volume = np.random.exponential(10.0)

        opens.append(open_price)
        highs.append(high_price)
        lows.append(low_price)
        closes.append(close_price)
        volumes.append(volume)

        current_price = close_price

    # 创建 DataFrame
    df = pd.DataFrame({
        'symbol': symbol,
        'interval': interval,
        'open_time': open_times,
        'close_time': close_times,
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes,
        'is_final': True,
    })

    # 保存
    output_path = output_dir / 'klines' / f'{symbol}_{interval}.parquet'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)

    logger.info(f"Saved {len(df)} {interval} klines to {output_path}")


def main():
    """生成所有示例数据"""
    # 配置
    symbols = ['BTCUSDT', 'ETHUSDT']
    start_time = datetime(2026, 6, 1, 0, 0, 0)
    duration_hours = 24  # 1天
    output_dir = Path('data/market')

    logger.info("=" * 60)
    logger.info("生成示例回测数据")
    logger.info("=" * 60)
    logger.info(f"币种: {symbols}")
    logger.info(f"开始时间: {start_time}")
    logger.info(f"持续时间: {duration_hours} 小时")
    logger.info(f"输出目录: {output_dir}")
    logger.info("=" * 60)

    # 生成数据
    for symbol in symbols:
        # 生成 aggTrades
        generate_sample_aggtrades(symbol, start_time, duration_hours, output_dir)

        # 生成 Klines
        for interval in ['1m', '5m', '15m']:
            generate_sample_klines(symbol, interval, start_time, duration_hours, output_dir)

    logger.info("=" * 60)
    logger.info("示例数据生成完成！")
    logger.info("=" * 60)
    logger.info(f"数据位置: {output_dir.absolute()}")
    logger.info("\n现在可以运行回测：")
    logger.info("python -m trading_platform.backtest.runner \\")
    logger.info("    --strategy minimal \\")
    logger.info("    --symbols BTCUSDT ETHUSDT \\")
    logger.info("    --start 2026-06-01 \\")
    logger.info("    --end 2026-06-02 \\")
    logger.info("    --data-dir data/market")


if __name__ == '__main__':
    main()
