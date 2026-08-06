"""策略核心使用的最小账户执行接口。"""

from typing import Iterable, Protocol

from trading_platform.shared.events import Order


class StrategyAccount(Protocol):
    """隔离策略核心与 replay/testnet/live 的具体执行实现。"""

    def get_order(self, order_id: str) -> Order | None:
        """按本地订单 ID 查询订单事实。"""
        ...

    def iter_orders(self) -> Iterable[Order]:
        """返回当前已知订单的稳定快照。"""
        ...

    def has_open_position(self, symbol: str) -> bool:
        """查询交易对是否仍有仓位。"""
        ...

    def cancel_order(self, order_id: str) -> bool:
        """请求撤销订单，返回是否接受本次撤销。"""
        ...
