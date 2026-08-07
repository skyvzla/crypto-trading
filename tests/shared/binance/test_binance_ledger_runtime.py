from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.ledger.binance_runtime import (
    BinanceLedgerCallbacks,
    create_binance_execution_runtime,
)


@pytest.mark.asyncio
async def test_execution_report_updates_wal_and_ledger():
    executor = Mock(handle_order_trade_update=Mock())
    execution_ledger = Mock(handle=AsyncMock())
    account_ledger = Mock(handle=AsyncMock())
    callbacks = BinanceLedgerCallbacks(
        executor, execution_ledger, account_ledger
    )
    report = {"c": "cid-1", "X": "NEW"}

    await callbacks.handle_execution_report(report)

    executor.handle_order_trade_update.assert_called_once_with(report)
    execution_ledger.handle.assert_awaited_once_with(report)


@pytest.mark.asyncio
async def test_execution_report_attempts_ledger_when_wal_update_fails():
    executor = Mock(
        handle_order_trade_update=Mock(side_effect=ValueError("invalid WAL update"))
    )
    execution_ledger = Mock(handle=AsyncMock())
    callbacks = BinanceLedgerCallbacks(
        executor, execution_ledger, Mock(handle=AsyncMock())
    )

    with pytest.raises(ExceptionGroup, match="ORDER_TRADE_UPDATE handling failed"):
        await callbacks.handle_execution_report({"c": "cid-1"})

    execution_ledger.handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_execution_report_attempts_wal_when_ledger_update_fails():
    executor = Mock(handle_order_trade_update=Mock())
    execution_ledger = Mock(
        handle=AsyncMock(side_effect=RuntimeError("database unavailable"))
    )
    callbacks = BinanceLedgerCallbacks(
        executor, execution_ledger, Mock(handle=AsyncMock())
    )

    with pytest.raises(ExceptionGroup, match="ORDER_TRADE_UPDATE handling failed"):
        await callbacks.handle_execution_report({"c": "cid-1"})

    executor.handle_order_trade_update.assert_called_once()


@pytest.mark.asyncio
async def test_account_update_is_written_to_position_ledger():
    account_ledger = Mock(handle=AsyncMock())
    callbacks = BinanceLedgerCallbacks(
        Mock(handle_order_trade_update=Mock()),
        Mock(handle=AsyncMock()),
        account_ledger,
    )
    event = {"e": "ACCOUNT_UPDATE"}

    await callbacks.handle_account_update(event)

    account_ledger.handle.assert_awaited_once_with(event)


def test_factory_wires_callbacks_and_explicit_recovery_parameters():
    runtime = create_binance_execution_runtime(
        rest_client=Mock(),
        executor=Mock(),
        db=Mock(),
        account_id="account-1",
        strategy_id="spike_short",
        managed_symbols=["AKEUSDT"],
        dedicated_strategy_account=True,
        ws_base_url="wss://testnet.example/ws",
        poll_interval_seconds=7,
        max_poll_attempts=9,
    )

    assert runtime.user_stream.ws_base_url == "wss://testnet.example/ws"
    assert runtime.user_stream.on_execution_report is not None
    assert runtime.user_stream.on_account_update is not None
    assert runtime.unknown_poller.poll_interval_seconds == 7
    assert runtime.unknown_poller.max_attempts == 9
    assert runtime.startup_reconciler.synchronizer.symbols == ("AKEUSDT",)


def test_factory_rejects_shared_account_without_routing_rules():
    with pytest.raises(ValueError, match="shared Binance accounts"):
        create_binance_execution_runtime(
            rest_client=Mock(),
            executor=Mock(),
            db=Mock(),
            account_id="account-1",
            strategy_id="spike_short",
            managed_symbols=["AKEUSDT"],
            dedicated_strategy_account=False,
            ws_base_url="wss://testnet.example/ws",
            poll_interval_seconds=7,
            max_poll_attempts=9,
        )
