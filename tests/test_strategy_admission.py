from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.events import Order
from trading_platform.strategies.admission import SubcategoryAdmissionService


def _order(
    order_id: str,
    *,
    status: str = "NEW",
    strategy_id: str = "spike_short",
    trigger_reason: str = "spike_tier1",
) -> Order:
    return Order(
        order_id=order_id,
        client_order_id=f"client-{order_id}",
        account_id="account-1",
        symbol="BTCUSDT",
        side="SELL",
        type="LIMIT",
        price=Decimal("100"),
        quantity=Decimal("1"),
        status=status,
        created_at=1_000,
        strategy_id=strategy_id,
        trigger_reason=trigger_reason,
    )


def _service(*, enabled=True, orders=(), cancel_result=True):
    source = Mock(is_subcategory_enabled=AsyncMock(return_value=enabled))
    gate = Mock(set_entry_enabled=Mock())
    account = Mock(
        iter_orders=Mock(return_value=tuple(orders)),
        cancel_order=Mock(return_value=cancel_result),
    )
    service = SubcategoryAdmissionService(
        source=source,
        gate=gate,
        account=account,
        subcategory="spike",
        strategy_id="spike_short",
        entry_trigger_reasons={"spike_tier1", "spike_tier2", "spike_tier3"},
    )
    return service, source, gate, account


@pytest.mark.asyncio
async def test_enabled_admission_opens_gate_without_touching_orders():
    service, source, gate, account = _service(
        enabled=True, orders=[_order("entry-1")]
    )

    result = await service.refresh_once()

    source.is_subcategory_enabled.assert_awaited_once_with("spike")
    gate.set_entry_enabled.assert_called_once_with(True)
    account.cancel_order.assert_not_called()
    assert result.enabled is True
    assert result.source_healthy is True


@pytest.mark.asyncio
async def test_closed_admission_cancels_only_known_unfilled_entry_orders():
    orders = [
        _order("new"),
        _order("partial", status="PARTIALLY_FILLED", trigger_reason="spike_tier2"),
        _order("unknown", status="SUBMIT_UNKNOWN", trigger_reason="spike_tier3"),
        _order("filled", status="FILLED"),
        _order("exit", trigger_reason="campaign_timeout_exit"),
        _order("foreign", strategy_id="other"),
    ]
    service, _, gate, account = _service(enabled=False, orders=orders)

    result = await service.refresh_once()

    gate.set_entry_enabled.assert_called_once_with(False)
    assert account.cancel_order.call_args_list == [
        (("new",),),
        (("partial",),),
    ]
    assert result.cancelled_order_ids == ("new", "partial")
    assert result.unresolved_unknown_order_ids == ("unknown",)


@pytest.mark.asyncio
async def test_admission_source_failure_closes_gate_and_cancels_entry():
    service, source, gate, account = _service(
        enabled=True, orders=[_order("entry-1")]
    )
    source.is_subcategory_enabled.side_effect = RuntimeError("database unavailable")

    result = await service.refresh_once()

    gate.set_entry_enabled.assert_called_once_with(False)
    account.cancel_order.assert_called_once_with("entry-1")
    assert result.enabled is False
    assert result.source_healthy is False
    assert isinstance(service.last_error, RuntimeError)


@pytest.mark.asyncio
async def test_failed_cancel_remains_visible_for_next_refresh():
    service, _, _, account = _service(
        enabled=False, orders=[_order("entry-1")], cancel_result=False
    )

    result = await service.refresh_once()

    account.cancel_order.assert_called_once_with("entry-1")
    assert result.cancelled_order_ids == ()
    assert result.failed_cancel_order_ids == ("entry-1",)


@pytest.mark.asyncio
async def test_cancel_exception_does_not_stop_fail_closed_refresh():
    service, _, gate, account = _service(
        enabled=False, orders=[_order("entry-1")]
    )
    account.cancel_order.side_effect = RuntimeError("executor unavailable")

    result = await service.refresh_once()

    gate.set_entry_enabled.assert_called_once_with(False)
    assert result.account_healthy is False
    assert result.failed_cancel_order_ids == ("entry-1",)


@pytest.mark.asyncio
async def test_order_snapshot_failure_keeps_gate_closed_and_is_reported():
    service, _, gate, account = _service(enabled=False)
    account.iter_orders.side_effect = RuntimeError("account unavailable")

    result = await service.refresh_once()

    gate.set_entry_enabled.assert_called_once_with(False)
    assert result.account_healthy is False
    assert isinstance(service.last_error, RuntimeError)


@pytest.mark.asyncio
async def test_universe_scan_is_the_only_refresh_trigger():
    service, source, _, _ = _service(enabled=True)
    await service.on_universe_scan()
    source.is_subcategory_enabled.assert_awaited_once_with("spike")
    assert not hasattr(service, "poll_interval_seconds")
