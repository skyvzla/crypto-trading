"""旧脚本退出逻辑的研究专用适配器。

信号、入场、数据可用时间和全局 Campaign 均复用当前实现；这里只复刻旧脚本的
invalid stop、2R target 和 900 秒退出触发，不能用于 testnet/live。
"""

from decimal import Decimal
from typing import List

from trading_platform.shared.events import Bar1s, Fill, OrderIntent, StrategyAuditEvent
from trading_platform.shared.execution import StrategyAccount
from trading_platform.strategies.spike_short import (
    DynamicSpikeBacktestStrategy,
    DynamicSpikeShortStrategy,
    MS_PER_SECOND,
    SpikeSignal,
)


class LegacyScriptExitSpikeShortStrategy(DynamicSpikeShortStrategy):
    """在当前可靠回放语义下测试旧脚本的固定退出规则。"""

    LEGACY_EXIT_AFTER_MS = 900 * MS_PER_SECOND
    LEGACY_REWARD_RISK = Decimal("2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._legacy_invalid_price: Decimal | None = None
        self._legacy_last_entry_fill_time: int | None = None

    def on_fill(self, fill: Fill) -> None:
        super().on_fill(fill)
        if fill.symbol != self.symbol or fill.side != "SELL" or self._account is None:
            return
        order = self._account.get_order(fill.order_id)
        if order is None or order.strategy_id != "spike_short":
            return
        campaign_id = self._campaign_id_from_client_order(order.client_order_id)
        signal = next(
            (sig for sig in self.active_signals if self._campaign_id(sig) == campaign_id),
            None,
        )
        if signal is not None:
            self._legacy_invalid_price = signal.invalid_price
        self._legacy_last_entry_fill_time = fill.fill_time

    def reset_campaign_timing(self) -> None:
        super().reset_campaign_timing()
        self._legacy_invalid_price = None
        self._legacy_last_entry_fill_time = None

    def _prepare_rotation(self, signal: SpikeSignal, bar: Bar1s) -> List[OrderIntent]:
        # 旧脚本逐笔独立结算；在当前全局 Campaign 约束下不使用 D-009 轮换。
        return []

    def _manage_non_positive_timeout(self, bar: Bar1s) -> List[OrderIntent]:
        if (
            self._exit_requested
            or self._account is None
            or self._legacy_invalid_price is None
            or self._legacy_last_entry_fill_time is None
        ):
            return []
        position = self._account.get_position(self.symbol)
        if position is None or position.side != "SHORT" or position.quantity <= 0:
            return []

        # 旧脚本在收集本轮实际成交后才计算平均入场价和退出窗口；不能在
        # 第一档成交的同一事件里先平仓，再让其余入场档位变成裸仓。
        campaign_orders = [
            order
            for order in self._account.iter_orders()
            if order.strategy_id == "spike_short"
            and order.symbol == self.symbol
            and order.client_order_id.startswith(
                f"spike_short_{self.symbol}_"
            )
        ]
        if any(order.status in {"NEW", "SUBMIT_UNKNOWN"} for order in campaign_orders):
            return []

        target_price = position.entry_price - (
            self._legacy_invalid_price - position.entry_price
        ) * self.LEGACY_REWARD_RISK
        stop_touched = bar.high >= self._legacy_invalid_price
        target_touched = bar.low <= target_price
        timed_out = (
            bar.available_time
            >= self._legacy_last_entry_fill_time + self.LEGACY_EXIT_AFTER_MS
        )
        if not (stop_touched or target_touched or timed_out):
            return []

        if stop_touched and target_touched:
            reason = "legacy_ambiguous_exit"
        elif stop_touched:
            reason = "legacy_stop_exit"
        elif target_touched:
            reason = "legacy_target_exit"
        else:
            reason = "legacy_timeout_exit"

        self._exit_requested = True
        self._record_audit(
            event_time=bar.available_time,
            event_type=reason + "_requested",
            campaign_id=self._campaign_id_for_timing,
            details={
                "entry_price": str(position.entry_price),
                "invalid_price": str(self._legacy_invalid_price),
                "target_price": str(target_price),
                "observed_close": str(bar.close),
                "quantity": str(position.quantity),
            },
        )
        return [
            OrderIntent(
                symbol=self.symbol,
                side="BUY",
                price=bar.close,
                quantity=position.quantity,
                client_order_id=(
                    f"{self._campaign_id_for_timing or 'spike_short'}_{reason}"
                ),
                order_type="MARKET",
                reduce_only=True,
                strategy_id="spike_short",
                trigger_reason=reason,
            )
        ]


class LegacyScriptExitSpikeBacktestStrategy(DynamicSpikeBacktestStrategy):
    """多币种研究适配器；只应由 replay CLI 构造。"""

    def __init__(
        self,
        symbols: List[str],
        total_notional: Decimal,
        account: StrategyAccount | None = None,
    ):
        super().__init__(symbols, total_notional, account)
        self.strategies = {
            symbol: LegacyScriptExitSpikeShortStrategy(
                symbol, total_notional=total_notional, account=account
            )
            for symbol in symbols
        }
