"""Binance 公开真实源门禁；默认不参与离线测试。"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from trading_platform.market.feed.aggregator import Bar1sAggregator
from trading_platform.market.feed.binance_ws import (
    BinanceWebSocketClient,
    build_stream_names,
    parse_aggtrade_message,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_EXTERNAL_SOURCE_TESTS") != "1",
    reason="RUN_EXTERNAL_SOURCE_TESTS=1 is required",
)


@pytest.mark.asyncio
async def test_public_rest_ws_and_project_aggregator_form_continuous_bars():
    symbol = os.getenv("EXTERNAL_SOURCE_SYMBOL", "BTCUSDT").upper()
    rest_url = os.getenv("BINANCE_PUBLIC_REST_URL", "https://fapi.binance.com")
    ws_url = os.getenv("BINANCE_PUBLIC_WS_URL", "wss://fstream.binance.com")

    async with httpx.AsyncClient(base_url=rest_url, timeout=10) as rest:
        response = await rest.get("/fapi/v1/exchangeInfo")
        response.raise_for_status()
        metadata = next(
            item for item in response.json()["symbols"] if item["symbol"] == symbol
        )
    assert metadata["status"] == "TRADING"
    assert metadata["contractType"] == "PERPETUAL"

    client = BinanceWebSocketClient(
        ws_base_url=ws_url,
        reconnect_delay=0.1,
        max_reconnect_attempts=2,
    )
    aggregator = Bar1sAggregator()
    bars = []
    trade_ids = []
    try:
        await client.connect(build_stream_names([symbol], ["bar1s"]))

        async def collect() -> None:
            async for message in client.receive_messages():
                parsed = parse_aggtrade_message(message)
                if parsed is None:
                    continue
                parsed_symbol, trade = parsed
                if parsed_symbol != symbol:
                    continue
                trade_ids.append(trade["agg_trade_id"])
                bars.extend(
                    aggregator.add_trade(
                        parsed_symbol,
                        trade["price"],
                        trade["quantity"],
                        trade["timestamp"],
                        trade["agg_trade_id"],
                    )
                )
                if len(bars) >= 3:
                    return

        await asyncio.wait_for(collect(), timeout=20)
    finally:
        await client.disconnect()

    assert len(trade_ids) > 0
    assert trade_ids == sorted(set(trade_ids))
    assert len(bars) >= 3
    assert all(bar.symbol == symbol for bar in bars)
    assert all(bar.available_time == bar.timestamp + 1_000 for bar in bars)
    assert all(
        current.timestamp > previous.timestamp
        for previous, current in zip(bars, bars[1:])
    )
    assert all(bar.first_aggregate_trade_id is not None for bar in bars)
    assert all(bar.last_aggregate_trade_id is not None for bar in bars)


@pytest.mark.asyncio
async def test_real_market_websocket_reconnects_after_active_disconnect():
    symbol = os.getenv("EXTERNAL_SOURCE_SYMBOL", "BTCUSDT").upper()
    ws_url = os.getenv("BINANCE_PUBLIC_WS_URL", "wss://fstream.binance.com")
    client = BinanceWebSocketClient(
        ws_base_url=ws_url,
        reconnect_delay=0.1,
        max_reconnect_attempts=3,
    )
    aggregator = Bar1sAggregator()
    trade_ids = []
    before_trade_ids = []
    after_trade_ids = []
    before_bars = []
    after_bars = []
    initial_generation = 0
    observed_disconnect_ms = None

    try:
        await asyncio.wait_for(
            client.connect(build_stream_names([symbol], ["bar1s"])), timeout=10
        )
        initial_generation = client.connection_generation
        assert initial_generation > 0

        async def collect_across_reconnect() -> None:
            nonlocal observed_disconnect_ms
            disconnected = False
            async for message in client.receive_messages():
                parsed = parse_aggtrade_message(message)
                if parsed is None:
                    continue
                parsed_symbol, trade = parsed
                if parsed_symbol != symbol:
                    continue

                generation = client.connection_generation
                trade_id = trade["agg_trade_id"]
                trade_ids.append(trade_id)
                target_ids = (
                    before_trade_ids
                    if generation == initial_generation
                    else after_trade_ids
                )
                target_ids.append(trade_id)
                emitted = aggregator.add_trade(
                    parsed_symbol,
                    trade["price"],
                    trade["quantity"],
                    trade["timestamp"],
                    trade_id,
                )
                for bar in emitted:
                    if bar.last_aggregate_trade_id in before_trade_ids:
                        before_bars.append(bar)
                    if bar.last_aggregate_trade_id in after_trade_ids:
                        after_bars.append(bar)

                if not disconnected and before_bars:
                    websocket = client._ws
                    assert websocket is not None
                    await websocket.close()
                    disconnected = True
                    continue
                if (
                    disconnected
                    and generation > initial_generation
                    and after_bars
                ):
                    observed_disconnect_ms = client.last_disconnected_at_ms
                    return

        await asyncio.wait_for(collect_across_reconnect(), timeout=30)
    finally:
        await client.disconnect()

    assert client.connection_generation > initial_generation
    assert observed_disconnect_ms is not None
    assert before_trade_ids
    assert after_trade_ids
    assert before_bars
    assert after_bars
    assert trade_ids == sorted(set(trade_ids))
    all_bars = before_bars + after_bars
    assert all(bar.symbol == symbol for bar in all_bars)
    assert all(bar.timestamp % 1_000 == 0 for bar in all_bars)
    assert all(
        bar.available_time == bar.timestamp + 1_000
        for bar in all_bars
    )
    assert all(
        current.timestamp > previous.timestamp
        for previous, current in zip(all_bars, all_bars[1:])
    )
