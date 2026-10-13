import pytest

from trading_platform.shared.config import BinanceConfig


@pytest.mark.parametrize(
    ("testnet", "rest", "ws"),
    [
        ("true", "https://demo-fapi.binance.com", "wss://stream.binancefuture.com"),
        ("false", "https://fapi.binance.com", "wss://fstream.binance.com"),
    ],
)
def test_environment_selects_fixed_endpoints_despite_legacy_urls(
    monkeypatch, testnet, rest, ws,
):
    monkeypatch.setenv("BINANCE_TESTNET", testnet)
    monkeypatch.setenv("BINANCE_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("BINANCE_WS_BASE_URL", "wss://example.invalid")

    config = BinanceConfig()

    assert config.testnet is (testnet == "true")
    assert config.base_url == rest
    assert config.ws_base_url == ws


def test_dotenv_legacy_urls_cannot_override_endpoint_selection(tmp_path, monkeypatch):
    for key in ("BINANCE_TESTNET", "BINANCE_BASE_URL", "BINANCE_WS_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    config_file = tmp_path / "binance.env"
    config_file.write_text(
        "BINANCE_TESTNET=false\n"
        "BINANCE_BASE_URL=https://demo-fapi.binance.com\n"
        "BINANCE_WS_BASE_URL=wss://stream.binancefuture.com\n",
    )

    config = BinanceConfig(_env_file=config_file)

    assert config.base_url == "https://fapi.binance.com"
    assert config.ws_base_url == "wss://fstream.binance.com"


def test_explicit_endpoint_injection_remains_available_for_security_checks():
    config = BinanceConfig(
        testnet=True,
        base_url="https://example.invalid",
        ws_base_url="wss://example.invalid",
    )

    assert config.base_url == "https://example.invalid"
    assert config.ws_base_url == "wss://example.invalid"


@pytest.mark.parametrize("testnet", [True, False])
def test_secret_files_cannot_override_endpoint_selection(tmp_path, testnet):
    (tmp_path / "BINANCE_BASE_URL").write_text("https://example.invalid")
    (tmp_path / "BINANCE_WS_BASE_URL").write_text("wss://example.invalid")

    config = BinanceConfig(testnet=testnet, _secrets_dir=tmp_path)

    assert config.base_url == (
        "https://demo-fapi.binance.com" if testnet else "https://fapi.binance.com"
    )
    assert config.ws_base_url == (
        "wss://stream.binancefuture.com" if testnet else "wss://fstream.binance.com"
    )
