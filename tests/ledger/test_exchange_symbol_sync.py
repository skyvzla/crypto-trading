from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.ledger import exchange_symbols


@pytest.mark.asyncio
async def test_data_layer_sync_retries_and_persists_complete_snapshot():
    payload = {"symbols": [{"symbol": "BTCUSDT"}]}
    fetch = AsyncMock(side_effect=[RuntimeError("temporary"), payload])
    db = Mock(sync_exchange_symbols=AsyncMock(return_value=1))
    sleep = AsyncMock()
    retries = []

    synced, returned = await exchange_symbols.sync_exchange_symbol_metadata(
        db,
        fetch,
        attempts=2,
        retry_base_seconds=0,
        sleep=sleep,
        on_retry=lambda attempt, total, error: retries.append(
            (attempt, total, str(error))
        ),
    )

    assert synced == 1
    assert returned is payload
    assert fetch.await_count == 2
    db.sync_exchange_symbols.assert_awaited_once_with(payload)
    assert retries == [(2, 2, "temporary")]


@pytest.mark.asyncio
async def test_manual_sync_always_uses_production_metadata_endpoint(monkeypatch):
    payload = {"symbols": [{"symbol": "BTCUSDT"}]}
    rest = Mock(get_exchange_info=AsyncMock(return_value=payload), close=AsyncMock())
    rest_factory = Mock(return_value=rest)
    pool = Mock(close=AsyncMock())
    db = Mock(
        sync_exchange_symbols=AsyncMock(return_value=1),
        list_tradeable_exchange_symbols=AsyncMock(return_value=["BTCUSDT"]),
    )
    monkeypatch.setattr(exchange_symbols, "BinanceRestClient", rest_factory)
    monkeypatch.setattr(
        exchange_symbols,
        "create_connection_pool",
        AsyncMock(return_value=pool),
    )
    monkeypatch.setattr(exchange_symbols, "LedgerDB", Mock(return_value=db))

    report = await exchange_symbols.run_exchange_symbol_sync_once(
        dsn="postgresql://archive",
        attempts=1,
        timeout=7,
    )

    assert report == exchange_symbols.ExchangeSymbolSyncReport(1, 1)
    rest_factory.assert_called_once_with(
        "",
        "",
        base_url="https://fapi.binance.com",
        timeout=7,
    )
    db.sync_exchange_symbols.assert_awaited_once_with(payload)
    db.list_tradeable_exchange_symbols.assert_awaited_once_with(freeze_days=15)
    rest.close.assert_awaited_once()
    pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_sync_records_failed_state_after_retries(monkeypatch):
    error = RuntimeError("network unavailable")
    rest = Mock(get_exchange_info=AsyncMock(side_effect=error), close=AsyncMock())
    pool = Mock(close=AsyncMock())
    db = Mock(mark_exchange_symbol_sync_failed=AsyncMock())
    monkeypatch.setattr(exchange_symbols, "BinanceRestClient", Mock(return_value=rest))
    monkeypatch.setattr(
        exchange_symbols,
        "create_connection_pool",
        AsyncMock(return_value=pool),
    )
    monkeypatch.setattr(exchange_symbols, "LedgerDB", Mock(return_value=db))

    with pytest.raises(RuntimeError, match="network unavailable"):
        await exchange_symbols.run_exchange_symbol_sync_once(
            dsn="postgresql://archive",
            attempts=2,
        )

    assert rest.get_exchange_info.await_count == 2
    db.mark_exchange_symbol_sync_failed.assert_awaited_once_with(error)
    rest.close.assert_awaited_once()
    pool.close.assert_awaited_once()


def test_manual_sync_cli_prints_plain_summary(monkeypatch, capsys):
    async def run_once(**_kwargs):
        return exchange_symbols.ExchangeSymbolSyncReport(700, 420)

    monkeypatch.setattr(exchange_symbols, "run_exchange_symbol_sync_once", run_once)

    assert exchange_symbols.main(["--dsn", "postgresql://archive"]) == 0
    assert capsys.readouterr().out == (
        "Synchronized 700 exchange symbols; 420 currently tradable.\n"
    )
