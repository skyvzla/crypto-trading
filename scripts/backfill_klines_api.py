"""用 Binance fapi klines API 补齐最近缺失的 1m/5m/15m K 线。

背景：Vision 月度归档延迟，1s/metrics 已更新到 8/16，但 1m/5m/15m 只到 8/1。
只补 2026-08-01T08:00:00+08:00 之后的缺口，直接写入 parquet 分区（幂等 upsert）。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from trading_platform.market.archive.models import Candle
from trading_platform.market.archive.parquet import ParquetCandleArchive
from trading_platform.shared.binance.rate_limiter import RateLimitRule, RateLimiter
from trading_platform.shared.binance.rest_client import BinanceRestClient

ARCHIVE_ROOT = "data/market/candles"
START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)      # 08:00 +08
END = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)       # 08:00 +08, 含 8/15 全天
TIMEFRAMES = ("1m", "5m", "15m")
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}


def load_universe() -> list[str]:
    import csv
    rows = list(csv.DictReader(open("reports/spike-v2-consecutive-ls-audit/universe.csv")))
    symbols = [
        str(r["symbol"]).strip().upper()
        for r in rows
        if r.get("selected") == "True"
    ]
    return sorted(symbols)


class Collector:
    def __init__(self) -> None:
        self.rest = BinanceRestClient(
            api_key="", api_secret="",
            rate_limiter=RateLimiter([RateLimitRule(60, 1200)]),
        )
        self.archive = ParquetCandleArchive(ARCHIVE_ROOT, rebuild_index_on_close=True)

    async def fetch_one(self, symbol: str, timeframe: str) -> list[Candle]:
        interval_ms = INTERVAL_MS[timeframe]
        candles: list[Candle] = []
        start_ms = int(START.timestamp() * 1000)
        end_ms = int(END.timestamp() * 1000)
        while start_ms < end_ms:
            rows = await self.rest.get_klines(
                symbol, timeframe, limit=1500,
                start_time=start_ms, end_time=end_ms - 1,
            )
            await asyncio.sleep(1.2)
            if not rows:
                break
            for row in rows:
                open_ms, close_ms = int(row[0]), int(row[6])
                if open_ms >= end_ms:
                    continue
                candles.append(Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=datetime.fromtimestamp(open_ms / 1000, tz=UTC),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    close_time=datetime.fromtimestamp(close_ms / 1000, tz=UTC),
                ))
            last_open = int(rows[-1][0])
            next_open = last_open + interval_ms
            if next_open <= start_ms:
                break
            start_ms = next_open
        return candles

    async def run(self) -> None:
        symbols = load_universe()
        print(f"universe: {len(symbols)} symbols, {len(TIMEFRAMES)} timeframes")
        total_ok = 0
        for i, symbol in enumerate(symbols, 1):
            for timeframe in TIMEFRAMES:
                try:
                    candles = await self.fetch_one(symbol, timeframe)
                    if candles:
                        self.archive.upsert(candles)
                        total_ok += len(candles)
                        print(f"[{i}/{len(symbols)}] {symbol} {timeframe}: +{len(candles)}")
                except Exception as exc:
                    print(f"[{i}/{len(symbols)}] {symbol} {timeframe}: FAIL {type(exc).__name__}: {exc}")
        self.archive.close()
        await self.rest.close()
        print(f"done, total rows written: {total_ok}")


async def main() -> int:
    await Collector().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))