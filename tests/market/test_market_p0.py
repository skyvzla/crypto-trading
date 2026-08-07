from fastapi.testclient import TestClient

from trading_platform.market.feed.binance_ws import unwrap_stream_message
from trading_platform.market.main import MarketLayerConfig, create_app
from trading_platform.shared.config import BinanceConfig


def test_combined_stream_message_is_unwrapped():
    event = {"e": "aggTrade", "s": "BTCUSDT"}
    assert unwrap_stream_message({"stream": "btcusdt@aggTrade", "data": event}) == event
    assert unwrap_stream_message(event) == event


def test_testnet_selects_futures_endpoints(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.delenv("BINANCE_BASE_URL", raising=False)
    monkeypatch.delenv("BINANCE_WS_BASE_URL", raising=False)
    config = BinanceConfig()
    assert config.base_url == "https://demo-fapi.binance.com"
    assert config.ws_base_url == "wss://stream.binancefuture.com"

    _, service = create_app(MarketLayerConfig(), "test-epoch")
    assert service.ws_client.ws_base_url == "wss://stream.binancefuture.com"


def test_subscription_route_refreshes_websocket():
    app, service = create_app(MarketLayerConfig(), "test-epoch")
    refreshed = []

    async def fake_refresh():
        refreshed.append(True)

    service.refresh_ws_streams = fake_refresh
    client = TestClient(app)
    response = client.put(
        "/subscriptions/consumer-1",
        json={"symbols": ["BTCUSDT"], "types": ["bar1s"]},
    )

    assert response.status_code == 200
    assert refreshed == [True]
