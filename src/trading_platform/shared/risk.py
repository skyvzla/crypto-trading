"""
风控守卫 - 进程内风控层
每个策略进程持有一个实例，统一管理该账户的风控规则
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Set
import logging

logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    """风控配置"""
    max_position_value_usdt: Decimal = Decimal('10000')  # 最大持仓价值
    max_symbols: int = 10  # 最大币种数
    max_leverage: int = 3  # 最大杠杆


class RiskGuard:
    """
    风控守卫

    职责：
    1. 维护账户总仓位
    2. 阻塞/解除阻塞币种（SUBMIT_UNKNOWN 场景）
    3. 检查开仓请求是否超限
    """

    def __init__(self, account_id: str, config: RiskConfig):
        self.account_id = account_id
        self.config = config

        # 当前持仓币种和价值
        self.positions: Dict[str, Decimal] = {}  # {symbol: position_value_usdt}

        # 阻塞的币种（SUBMIT_UNKNOWN 等待解决）
        self.blocked_symbols: Set[str] = set()
        self.block_reasons: Dict[str, str] = {}

    def check_can_open(self, symbol: str, value_usdt: Decimal) -> tuple[bool, str]:
        """
        检查是否可以开新仓

        Returns:
            (can_open, reason)
        """
        # 检查币种是否被阻塞
        if symbol in self.blocked_symbols:
            reason = self.block_reasons.get(symbol, 'unknown')
            return False, f"Symbol blocked: {reason}"

        # 检查币种数上限
        if symbol not in self.positions and len(self.positions) >= self.config.max_symbols:
            return False, f"Max symbols reached: {self.config.max_symbols}"

        # 检查总持仓价值上限
        total_value = sum(self.positions.values()) + value_usdt
        if total_value > self.config.max_position_value_usdt:
            return False, f"Max position value exceeded: {total_value} > {self.config.max_position_value_usdt}"

        return True, "ok"

    def block_symbol(self, symbol: str, reason: str) -> None:
        """阻塞币种（SUBMIT_UNKNOWN 场景）"""
        self.blocked_symbols.add(symbol)
        self.block_reasons[symbol] = reason
        logger.warning(f"[{self.account_id}] Blocked {symbol}: {reason}")

    def unblock_symbol(self, symbol: str) -> None:
        """解除阻塞"""
        self.blocked_symbols.discard(symbol)
        self.block_reasons.pop(symbol, None)
        logger.info(f"[{self.account_id}] Unblocked {symbol}")

    def update_position(self, symbol: str, value_usdt: Decimal) -> None:
        """
        更新持仓价值

        Args:
            symbol: 币种
            value_usdt: 持仓价值（USDT），0 表示已平仓
        """
        if value_usdt <= 0:
            self.positions.pop(symbol, None)
        else:
            self.positions[symbol] = value_usdt

    def get_total_position_value(self) -> Decimal:
        """获取总持仓价值"""
        return sum(self.positions.values())

    def get_available_margin(self) -> Decimal:
        """获取可用保证金"""
        return self.config.max_position_value_usdt - self.get_total_position_value()
