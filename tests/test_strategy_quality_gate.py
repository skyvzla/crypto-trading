from unittest.mock import AsyncMock

import pytest

from trading_platform.shared.config import (
    BinanceConfig,
    RedisConfig,
    StrategyConfig,
)
from trading_platform.strategies.kline.example import ExampleKlineStrategy
from trading_platform.strategies.tick.example import ExampleTickStrategy


class Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def strategy_config() -> StrategyConfig:
    return StrategyConfig(account_id="quality-test")


@pytest.mark.asyncio
async def test_kline_wait_retries_until_market_quality_is_ready(monkeypatch):
    strategy = ExampleKlineStrategy(
        strategy_name="quality-kline",
        consumer_id="quality-kline-1",
        symbols=["BTCUSDT"],
        intervals=["1m"],
        account_id="quality-test",
        binance_config=BinanceConfig(),
        redis_config=RedisConfig(),
        strategy_config=strategy_config(),
    )
    strategy.http_client = AsyncMock()
    strategy.http_client.get.side_effect = [
        Response(200, {"instance_epoch": "epoch-1"}),
        Response(503, {"ready": False}),
        Response(200, {"instance_epoch": "epoch-1"}),
        Response(200, {"ready": True}),
    ]

    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    await strategy._wait_for_market_layer()

    assert strategy.market_data_ready is True


@pytest.mark.asyncio
async def test_tick_wait_accepts_ready_quality():
    strategy = ExampleTickStrategy(
        strategy_name="quality-tick",
        consumer_id="quality-tick-1",
        symbols=["BTCUSDT"],
        account_id="quality-test",
        binance_config=BinanceConfig(),
        redis_config=RedisConfig(),
        strategy_config=strategy_config(),
    )
    strategy.http_client = AsyncMock()
    strategy.http_client.get.side_effect = [
        Response(200, {"instance_epoch": "epoch-1"}),
        Response(200, {"ready": True}),
    ]

    await strategy._wait_for_market_layer()

    assert strategy.market_data_ready is True
    assert strategy.last_known_epoch == "epoch-1"
