import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from trading_platform.shared.config import BinanceConfig, DatabaseConfig, StrategyConfig
from trading_platform.shared.events import Kline
from trading_platform.shared.execution_recovery import OrderWALRecord
from trading_platform.strategies.long_breakout import (
    LongBreakoutSettings,
    LongBreakoutStrategy,
)
from trading_platform.strategies.long_breakout.runtime import (
    LongBreakoutProcess,
    LongBreakoutRuntimeCallbacks,
)


ACCOUNT_ID = "long-breakout-test"
SYMBOL = "BTCUSDT"


def process(*, timeframe: str = "4h", lookback_bars: int | None = None):
    return LongBreakoutProcess(
        LongBreakoutSettings(
            account_id=ACCOUNT_ID,
            timeframe=timeframe,
            lookback_bars=lookback_bars,
        ),
        binance=BinanceConfig(
            api_key="key",
            api_secret="secret",
            testnet=True,
        ),
        database=DatabaseConfig(),
        strategy_config=StrategyConfig(
            account_id=ACCOUNT_ID,
            risk_max_position_value_usdt=50,
            risk_max_symbols=1,
        ),
    )


def kline(index: int, *, interval: str = "4h", symbol: str = SYMBOL) -> Kline:
    step = 4 * 60 * 60 * 1000 if interval == "4h" else 24 * 60 * 60 * 1000
    open_time = index * step
    return Kline(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=open_time + step - 1,
        available_time=open_time + step,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )


def test_process_rejects_non_testnet_endpoints_even_with_testnet_flag():
    with pytest.raises(ValueError, match="testnet REST URL"):
        LongBreakoutProcess(
            LongBreakoutSettings(account_id=ACCOUNT_ID),
            binance=BinanceConfig(
                api_key="key",
                api_secret="secret",
                testnet=True,
                base_url="https://example.invalid",
            ),
            database=DatabaseConfig(),
            strategy_config=StrategyConfig(account_id=ACCOUNT_ID),
        )


@pytest.mark.asyncio
async def test_warmup_requests_extra_candidate_for_incomplete_exchange_candle():
    runtime = process()
    runtime.strategy = LongBreakoutStrategy(symbol=SYMBOL, timeframe="4h")
    runtime._fetch_klines = AsyncMock(
        return_value=[kline(index) for index in range(42)]
    )

    await runtime._warm_market_history()

    runtime._fetch_klines.assert_awaited_once_with(SYMBOL, limit=43)
    assert runtime.strategy.box_high(SYMBOL) == Decimal("101")


@pytest.mark.asyncio
async def test_reconnect_rebuilds_strategy_state_from_reconciled_account():
    runtime = process()
    runtime.account = Mock(refresh_positions=AsyncMock())
    runtime.coordinator = Mock(restore_from_account=AsyncMock())
    runtime.runtime_callbacks = Mock(finish_startup_recovery=Mock())
    runtime._validate_account = AsyncMock()

    await runtime._on_execution_recovered()

    runtime._validate_account.assert_awaited_once_with()
    runtime.account.refresh_positions.assert_awaited_once_with()
    runtime.coordinator.restore_from_account.assert_awaited_once_with()
    runtime.runtime_callbacks.finish_startup_recovery.assert_called_once_with()
    assert runtime._execution_recovering is False
    assert runtime._gates["execution"] is True


@pytest.mark.asyncio
async def test_reconnect_blocks_callbacks_and_execution_until_recovery_finishes():
    runtime = process()
    delegate = Mock(
        handle_execution_report=AsyncMock(),
        handle_account_update=AsyncMock(),
    )
    account = Mock(
        refresh_positions=AsyncMock(),
        handle_account_update=AsyncMock(),
    )
    coordinator = Mock(restore_from_account=AsyncMock())
    callbacks = LongBreakoutRuntimeCallbacks(
        delegate=delegate,
        account=account,
        coordinator=coordinator,
    )
    callbacks.finish_startup_recovery()
    runtime.account = account
    runtime.coordinator = coordinator
    runtime.runtime_callbacks = callbacks
    runtime.strategy = Mock()
    runtime._validate_account = AsyncMock()
    runtime._execution_recovering = False
    runtime._gates["execution"] = True

    runtime._on_execution_disconnected()
    callback_task = asyncio.create_task(callbacks.handle_account_update({"a": {}}))
    await asyncio.sleep(0)

    assert runtime._execution_recovering is True
    assert runtime._gates["execution"] is False
    delegate.handle_account_update.assert_not_awaited()

    await runtime._on_execution_recovered()
    await callback_task

    delegate.handle_account_update.assert_awaited_once_with({"a": {}})
    account.handle_account_update.assert_awaited_once_with({"a": {}})
    assert runtime._execution_recovering is False
    assert runtime._gates["execution"] is True


@pytest.mark.asyncio
async def test_heartbeat_does_not_open_execution_gate_during_recovery():
    runtime = process()
    runtime.runtime = SimpleNamespace(
        is_running=True,
        user_stream=SimpleNamespace(connected=True),
    )
    runtime.coordinator = SimpleNamespace(
        worker_task=Mock(done=Mock(return_value=False))
    )
    runtime._execution_recovering = True
    runtime._publish_runtime_status = AsyncMock(side_effect=asyncio.CancelledError)

    with patch(
        "trading_platform.strategies.long_breakout.runtime.asyncio.sleep",
        AsyncMock(),
    ):
        with pytest.raises(asyncio.CancelledError):
            await runtime._heartbeat_loop()

    assert runtime._gates["execution"] is False


@pytest.mark.asyncio
async def test_cancelling_worker_watcher_does_not_cancel_execution_worker():
    runtime = process()
    worker_task = asyncio.create_task(asyncio.Event().wait())
    watcher = asyncio.create_task(runtime._watch_worker(worker_task))
    await asyncio.sleep(0)

    watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)

    assert worker_task.done() is False
    worker_task.cancel()
    await asyncio.gather(worker_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_process_stop_is_idempotent_before_resources_are_built():
    runtime = process()

    await runtime.stop()
    await runtime.stop()

    with pytest.raises(RuntimeError, match="already stopped"):
        await runtime.start()


@pytest.mark.asyncio
async def test_callback_applies_terminal_order_update_without_a_fill():
    record = OrderWALRecord(
        record_type="exchange_status",
        recorded_at=1,
        account_id=ACCOUNT_ID,
        client_order_id="entry-1",
        symbol=SYMBOL,
        side="BUY",
        order_type="MARKET",
        quantity="1",
        price="100",
        status="CANCELLED",
        payload={"strategy_id": "long_breakout", "reduce_only": False},
    )
    delegate = Mock(handle_execution_report=AsyncMock())
    account = Mock()
    account.handle_execution_report.return_value = None
    account.wal.recover_latest.return_value = {"entry-1": record}
    coordinator = Mock()
    callbacks = LongBreakoutRuntimeCallbacks(
        delegate=delegate,
        account=account,
        coordinator=coordinator,
    )
    callbacks.finish_startup_recovery()

    await callbacks.handle_execution_report({"c": "entry-1"})

    delegate.handle_execution_report.assert_awaited_once_with({"c": "entry-1"})
    coordinator.on_order_update.assert_called_once_with(record)
    coordinator.on_fill.assert_not_called()


@pytest.mark.asyncio
async def test_resource_build_binds_risk_guard_to_the_strategy_account():
    runtime = process()
    pool = Mock(close=AsyncMock())
    lease = Mock(acquire=AsyncMock(), release=AsyncMock())
    journal = Mock(start=AsyncMock(), append=AsyncMock())
    rest = Mock(
        close=AsyncMock(),
        get_exchange_info=AsyncMock(return_value={"symbols": []}),
    )
    http = Mock(aclose=AsyncMock())
    user_stream = SimpleNamespace(
        on_execution_report=AsyncMock(),
        on_account_update=AsyncMock(),
        on_disconnect=None,
    )
    execution_runtime = SimpleNamespace(
        user_stream=user_stream,
        on_startup_failure=None,
        on_recovered=None,
    )

    with (
        patch(
            "trading_platform.strategies.long_breakout.runtime.create_connection_pool",
            AsyncMock(return_value=pool),
        ),
        patch(
            "trading_platform.strategies.long_breakout.runtime.PostgresExecutionLease",
            return_value=lease,
        ),
        patch(
            "trading_platform.strategies.long_breakout.runtime.DurableExecutionEventJournal",
            return_value=journal,
        ),
        patch(
            "trading_platform.strategies.long_breakout.runtime.BinanceRestClient",
            return_value=rest,
        ),
        patch(
            "trading_platform.strategies.long_breakout.runtime.httpx.AsyncClient",
            return_value=http,
        ),
        patch.object(runtime, "_validate_account", AsyncMock()),
        patch(
            "trading_platform.strategies.long_breakout.runtime.BinanceSymbolRuleBook.from_exchange_info",
            return_value=Mock(),
        ),
        patch(
            "trading_platform.strategies.long_breakout.runtime.RiskGuard",
            return_value=Mock(),
        ) as risk_guard,
        patch(
            "trading_platform.strategies.long_breakout.runtime.BinanceStrategyAccount",
            return_value=Mock(),
        ),
        patch(
            "trading_platform.strategies.long_breakout.runtime.BinanceOrderExecutor",
            return_value=Mock(),
        ),
        patch(
            "trading_platform.strategies.long_breakout.runtime.LongBreakoutExecutionCoordinator",
            return_value=Mock(),
        ),
        patch(
            "trading_platform.strategies.long_breakout.runtime.create_binance_execution_runtime",
            return_value=execution_runtime,
        ),
    ):
        await runtime._build_resources()

    assert risk_guard.call_args.args[0] == ACCOUNT_ID
    await runtime._stack.aclose()
