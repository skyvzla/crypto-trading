"""Binance 执行回报、账户回报与 PostgreSQL 账本的组合入口。"""

from __future__ import annotations

from typing import Any, Protocol

from trading_platform.ledger.binance_account_updates import BinanceAccountUpdateLedger
from trading_platform.ledger.binance_reports import BinanceExecutionReportLedger
from trading_platform.ledger.db.models import LedgerDB
from trading_platform.shared.binance.live_executor import BinanceOrderExecutor
from trading_platform.shared.binance.rest_client import BinanceRestClient
from trading_platform.shared.binance.runtime import BinanceExecutionRuntime
from trading_platform.shared.binance.user_stream import UserDataStream
from trading_platform.shared.execution_recovery import SubmitUnknownPollingService


class OrderUpdateSink(Protocol):
    def handle_order_trade_update(self, order_data: dict[str, Any]) -> object:
        ...


class BinanceLedgerCallbacks:
    """让每条订单回报独立更新 WAL 和数据库，账户回报更新持仓快照。"""

    def __init__(
        self,
        executor: OrderUpdateSink,
        execution_ledger: BinanceExecutionReportLedger,
        account_ledger: BinanceAccountUpdateLedger,
    ):
        self.executor = executor
        self.execution_ledger = execution_ledger
        self.account_ledger = account_ledger

    async def handle_execution_report(self, order_data: dict[str, Any]) -> None:
        errors: list[Exception] = []
        try:
            self.executor.handle_order_trade_update(order_data)
        except Exception as exc:
            errors.append(exc)
        try:
            await self.execution_ledger.handle(order_data)
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise ExceptionGroup("ORDER_TRADE_UPDATE handling failed", errors)

    async def handle_account_update(self, event: dict[str, Any]) -> None:
        await self.account_ledger.handle(event)


def create_binance_execution_runtime(
    *,
    rest_client: BinanceRestClient,
    executor: BinanceOrderExecutor,
    db: LedgerDB,
    account_id: str,
    strategy_id: str,
    dedicated_strategy_account: bool,
    ws_base_url: str,
    poll_interval_seconds: float,
    max_poll_attempts: int,
) -> BinanceExecutionRuntime:
    """使用显式业务归属和恢复参数构建 testnet/live 共用运行时。"""
    if not dedicated_strategy_account:
        raise ValueError(
            "shared Binance accounts require explicit order and position routing"
        )
    callbacks = BinanceLedgerCallbacks(
        executor,
        BinanceExecutionReportLedger(
            db, account_id=account_id, strategy_id=strategy_id
        ),
        BinanceAccountUpdateLedger(
            db, account_id=account_id, strategy_id=strategy_id
        ),
    )
    user_stream = UserDataStream(
        rest_client,
        ws_base_url=ws_base_url,
        on_execution_report=callbacks.handle_execution_report,
        on_account_update=callbacks.handle_account_update,
    )
    poller = SubmitUnknownPollingService(
        executor,
        poll_interval_seconds=poll_interval_seconds,
        max_attempts=max_poll_attempts,
    )
    return BinanceExecutionRuntime(user_stream, poller)
