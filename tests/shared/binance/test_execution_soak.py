from decimal import Decimal

import httpx
import pytest

from trading_platform.shared.binance import (
    BinanceOrderExecutor,
    BinanceSymbolRuleBook,
    BinanceSymbolRules,
)
from trading_platform.shared.events import OrderIntent
from trading_platform.shared.execution_recovery import OrderWAL
from trading_platform.shared.risk import RiskConfig, RiskGuard


class DeterministicExchange:
    """每 10 个订单制造一次“已接单但响应超时”。"""

    def __init__(self):
        self.orders = {}
        self.submit_count = {}

    async def post_order(self, **kwargs):
        client_id = kwargs["new_client_order_id"]
        self.submit_count[client_id] = self.submit_count.get(client_id, 0) + 1
        response = {
            "symbol": kwargs["symbol"],
            "clientOrderId": client_id,
            "orderId": len(self.orders) + 1,
            "status": "NEW",
        }
        self.orders[client_id] = response
        if int(client_id.rsplit("-", 1)[1]) % 10 == 0:
            raise httpx.ReadTimeout("response lost after exchange accepted order")
        return response

    async def query_order(self, symbol, *, orig_client_order_id):
        order = self.orders.get(orig_client_order_id)
        assert order is None or order["symbol"] == symbol
        return order


def rules():
    return BinanceSymbolRuleBook(
        {
            "BTCUSDT": BinanceSymbolRules(
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
                min_price=Decimal("0.1"),
                max_price=Decimal("1000000"),
                lot_step_size=Decimal("0.001"),
                min_quantity=Decimal("0.001"),
                max_quantity=Decimal("100"),
                market_step_size=Decimal("0.001"),
                market_min_quantity=Decimal("0.001"),
                market_max_quantity=Decimal("100"),
                min_notional=Decimal("5"),
            )
        }
    )


@pytest.mark.asyncio
async def test_reliable_executor_100_round_soak_never_resubmits_unknown(tmp_path):
    exchange = DeterministicExchange()
    guard = RiskGuard("soak", RiskConfig(max_position_value_usdt=Decimal("100000")))
    executor = BinanceOrderExecutor(
        exchange,
        OrderWAL(tmp_path / "orders.jsonl"),
        account_id="soak",
        risk_guard=guard,
        symbol_rules=rules(),
    )

    for index in range(100):
        intent = OrderIntent(
            symbol="BTCUSDT",
            side="SELL",
            price=Decimal("100.01"),
            quantity=Decimal("0.0519"),
            client_order_id=f"soak-{index}",
            strategy_id="spike_short",
            trigger_reason="spike_tier1",
        )
        first = await executor.submit(intent)
        if first.status == "SUBMIT_UNKNOWN":
            resolution = await executor.resolve_submit_unknown(first)
            assert resolution.resolved is True
            assert resolution.status == "NEW"
        duplicate = await executor.submit(intent)
        assert duplicate.status == "NEW"

    assert len(exchange.orders) == 100
    assert set(exchange.submit_count.values()) == {1}
    assert guard.blocked_symbols == set()
