"""
订单状态机和状态转换验证
"""
from typing import Literal

OrderStatus = Literal[
    'NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'EXPIRED', 'SUBMIT_UNKNOWN'
]

# 允许的状态转换
VALID_TRANSITIONS = {
    'NEW': {'FILLED', 'CANCELLED', 'EXPIRED', 'PARTIALLY_FILLED'},
    'PARTIALLY_FILLED': {'FILLED', 'CANCELLED'},
    'FILLED': set(),  # 终态
    'CANCELLED': set(),  # 终态
    'EXPIRED': set(),  # 终态
    'SUBMIT_UNKNOWN': {
        'NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'EXPIRED'
    },  # 查单后以交易所事实为准
}


def is_valid_transition(old_status: OrderStatus, new_status: OrderStatus) -> bool:
    """
    验证状态转换是否合法

    Args:
        old_status: 当前状态
        new_status: 目标状态

    Returns:
        是否为合法转换
    """
    # 同状态始终允许（User Data Stream 重复推送）
    if old_status == new_status:
        return True

    # 检查状态转换表
    return new_status in VALID_TRANSITIONS.get(old_status, set())


def is_terminal_status(status: OrderStatus) -> bool:
    """判断是否为终态"""
    return status in {'FILLED', 'CANCELLED', 'EXPIRED'}


def map_binance_status(binance_status: str) -> OrderStatus:
    """
    映射 Binance 订单状态到内部状态

    Binance 状态: NEW, PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED
    """
    mapping = {
        'NEW': 'NEW',
        'PARTIALLY_FILLED': 'PARTIALLY_FILLED',
        'FILLED': 'FILLED',
        'CANCELED': 'CANCELLED',
        'EXPIRED': 'EXPIRED',
    }
    return mapping.get(binance_status, 'NEW')
