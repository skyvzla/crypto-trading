"""
风控守卫 - 进程内风控层。

持仓和已提交但尚未被账户仓位事实确认的开仓订单共同构成风险暴露。订单
reservation 只在 WAL 已经落盘后创建，进程重启时由 WAL 重建。
"""

from dataclasses import dataclass
from decimal import Decimal
import logging
import threading
from typing import Dict, Set

logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    """风控配置"""

    max_position_value_usdt: Decimal = Decimal("10000")
    max_symbols: int = 10
    max_leverage: int = 3


@dataclass
class _OpenReservation:
    """一个非 reduce-only 订单尚未被账户事实确认的风险占额。"""

    symbol: str
    order_value_usdt: Decimal
    status: str | None = None
    filled_value_usdt: Decimal = Decimal("0")
    # Exchange responses carry cumulative executed value while account stream
    # reports are de-duplicated, incremental fills. Keep both counters so a
    # response followed by its matching trade report is not counted twice.
    reported_filled_value_usdt: Decimal = Decimal("0")
    noted_filled_value_usdt: Decimal = Decimal("0")
    confirmed_filled_value_usdt: Decimal = Decimal("0")
    fill_seen: bool = False
    full_fill: bool = False
    last_fill_ms: int | None = None
    notional_known: bool = True

    @property
    def reserved_value_usdt(self) -> Decimal:
        """未被账户仓位事实确认的风险价值。"""

        return max(
            Decimal("0"),
            max(self.order_value_usdt, self.filled_value_usdt)
            - self.confirmed_filled_value_usdt,
        )


class RiskGuard:
    """管理持仓、开仓订单 reservation、symbol 阻塞和进程级 halt。"""

    def __init__(self, account_id: str, config: RiskConfig):
        self.account_id = account_id
        self.config = config

        self.positions: Dict[str, Decimal] = {}
        self._open_reservations: dict[str, _OpenReservation] = {}
        # ACCOUNT_UPDATE 可能先于 ORDER_TRADE_UPDATE；后到的订单事实使用该水位
        # 判断它是否已经包含在账户仓位中。
        self._position_watermarks: dict[str, int] = {}
        self._lock = threading.RLock()

        self.blocked_symbols: Set[str] = set()
        self.block_reasons: Dict[str, str] = {}
        self._halted = False
        self._halt_reason = ""

    def check_can_open(
        self,
        symbol: str,
        value_usdt: Decimal,
        leverage: int = 1,
    ) -> tuple[bool, str]:
        """检查开仓请求，但不占额。"""

        with self._lock:
            return self._check_can_open_locked(symbol, value_usdt, leverage)

    def reserve_open(
        self,
        symbol: str,
        value_usdt: Decimal,
        client_order_id: str,
        *,
        leverage: int = 1,
    ) -> tuple[bool, str]:
        """原子检查并占用开仓订单名义价值。

        ``client_order_id`` 是 reservation 的幂等键。调用方必须在 WAL intent
        成功落盘后保留 reservation；若 WAL 写入失败，应调用 ``release_open``。
        """

        if not client_order_id:
            return False, "Client order id is required"
        with self._lock:
            existing = self._open_reservations.get(client_order_id)
            if existing is not None:
                if existing.symbol != symbol:
                    return False, f"Reservation symbol mismatch: {client_order_id}"
                if existing.notional_known and existing.order_value_usdt != value_usdt:
                    return False, f"Reservation value mismatch: {client_order_id}"
                return True, "already reserved"

            allowed, reason = self._check_can_open_locked(
                symbol, value_usdt, leverage
            )
            if not allowed:
                return False, reason
            self._open_reservations[client_order_id] = _OpenReservation(
                symbol=symbol,
                order_value_usdt=value_usdt,
            )
            return True, "ok"

    def release_open(self, client_order_id: str) -> None:
        """回滚尚未写入 WAL 的 reservation。"""

        with self._lock:
            self._open_reservations.pop(client_order_id, None)

    def restore_open_reservation(
        self,
        *,
        client_order_id: str,
        symbol: str,
        value_usdt: Decimal | None,
        status: str | None,
        has_fills: bool | None,
        filled_value_usdt: Decimal | None = None,
        fill_time_ms: int | None = None,
    ) -> None:
        """从 WAL 重建 reservation。

        旧 WAL 没有名义价值时按最大额度保守占用；缺少成交时间时不能用旧快照
        乐观释放已成交订单。
        """

        if not client_order_id:
            raise ValueError("client_order_id is required")
        with self._lock:
            existing = self._open_reservations.get(client_order_id)
            if existing is None:
                known = value_usdt is not None and value_usdt > 0
                value = value_usdt if known else self.config.max_position_value_usdt
                if value <= 0:
                    raise ValueError("reservation value must be positive")
                existing = _OpenReservation(
                    symbol=symbol,
                    order_value_usdt=value,
                    notional_known=known,
                )
                self._open_reservations[client_order_id] = existing
            elif existing.symbol != symbol:
                raise ValueError(f"reservation symbol mismatch: {client_order_id}")
            self._update_order_locked(
                existing,
                status,
                has_fills=has_fills,
                filled_value_usdt=filled_value_usdt,
                fill_time_ms=fill_time_ms,
            )

    def update_order_status(
        self,
        client_order_id: str,
        status: str,
        *,
        has_fills: bool | None = None,
        filled_value_usdt: Decimal | None = None,
        fill_time_ms: int | None = None,
    ) -> None:
        """将交易所状态同步到 reservation，按订单 id 精确处理。"""

        with self._lock:
            reservation = self._open_reservations.get(client_order_id)
            if reservation is None:
                return
            self._update_order_locked(
                reservation,
                status,
                has_fills=has_fills,
                filled_value_usdt=filled_value_usdt,
                fill_time_ms=fill_time_ms,
            )

    def note_fill(
        self,
        client_order_id: str,
        symbol: str,
        fill_time_ms: int,
        fill_value_usdt: Decimal | None = None,
    ) -> None:
        """记录成交回报；账户仓位事实确认前仍保留未确认占额。"""

        with self._lock:
            reservation = self._open_reservations.get(client_order_id)
            if reservation is None:
                # 回报可能在恢复重放前到达，宁可保守占满额度。
                reservation = _OpenReservation(
                    symbol=symbol,
                    order_value_usdt=self.config.max_position_value_usdt,
                    notional_known=False,
                )
                self._open_reservations[client_order_id] = reservation
            elif reservation.symbol != symbol:
                raise ValueError(f"reservation symbol mismatch: {client_order_id}")
            reservation.fill_seen = True
            if fill_value_usdt is not None:
                if fill_value_usdt <= 0:
                    raise ValueError("fill value must be positive")
                reservation.noted_filled_value_usdt += fill_value_usdt
                reservation.filled_value_usdt = max(
                    reservation.filled_value_usdt,
                    reservation.reported_filled_value_usdt,
                    reservation.noted_filled_value_usdt,
                )
            reservation.last_fill_ms = max(
                reservation.last_fill_ms or fill_time_ms, fill_time_ms
            )
            self._try_confirm_locked(reservation)

    def confirm_position_update(
        self,
        symbol: str,
        update_ms: int,
        *,
        from_snapshot: bool = False,
    ) -> None:
        """以账户仓位事实确认已经成交的 reservation 部分。

        ``from_snapshot`` 仅记录调用语义，不会绕过成交时间水位。这样旧的
        get_position_risk 快照不会在成交回报之前提前释放风险额度。
        """

        del from_snapshot
        with self._lock:
            self._position_watermarks[symbol] = max(
                self._position_watermarks.get(symbol, -1), update_ms
            )
            for reservation in tuple(self._open_reservations.values()):
                if reservation.symbol == symbol:
                    self._try_confirm_locked(reservation)

    def get_total_reserved_value(self) -> Decimal:
        """获取活动开仓订单未确认风险价值。"""

        with self._lock:
            return sum(
                (
                    reservation.reserved_value_usdt
                    for reservation in self._open_reservations.values()
                ),
                start=Decimal("0"),
            )

    def get_total_risk_value(self) -> Decimal:
        """获取持仓与活动开仓订单的合计风险价值。"""

        with self._lock:
            return sum(self.positions.values(), start=Decimal("0")) + sum(
                (
                    reservation.reserved_value_usdt
                    for reservation in self._open_reservations.values()
                ),
                start=Decimal("0"),
            )

    @property
    def halted(self) -> bool:
        """进程级停止新增风险状态；只能由进程重启清除。"""

        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def halt(self, reason: str) -> None:
        """关键事实无法证明一致时，停止所有新开仓。"""

        if not reason:
            raise ValueError("halt reason is required")
        with self._lock:
            if not self._halted:
                self._halted = True
                self._halt_reason = reason
                logger.error("[%s] Risk guard halted: %s", self.account_id, reason)

    def block_symbol(self, symbol: str, reason: str) -> None:
        """阻塞币种（SUBMIT_UNKNOWN 等待解决）。"""

        with self._lock:
            self.blocked_symbols.add(symbol)
            self.block_reasons[symbol] = reason
        logger.warning("[%s] Blocked %s: %s", self.account_id, symbol, reason)

    def unblock_symbol(self, symbol: str) -> None:
        """解除阻塞。"""

        with self._lock:
            self.blocked_symbols.discard(symbol)
            self.block_reasons.pop(symbol, None)
        logger.info("[%s] Unblocked %s", self.account_id, symbol)

    def update_position(self, symbol: str, value_usdt: Decimal) -> None:
        """更新持仓价值；0 表示已平仓。"""

        with self._lock:
            if value_usdt <= 0:
                self.positions.pop(symbol, None)
            else:
                self.positions[symbol] = value_usdt

    def get_total_position_value(self) -> Decimal:
        """获取总持仓价值。"""

        with self._lock:
            return sum(self.positions.values(), start=Decimal("0"))

    def get_available_margin(self) -> Decimal:
        """获取可用保证金（扣除活动订单 reservation）。"""

        return self.config.max_position_value_usdt - self.get_total_risk_value()

    def _check_can_open_locked(
        self, symbol: str, value_usdt: Decimal, leverage: int
    ) -> tuple[bool, str]:
        if self._halted:
            return False, f"Risk guard halted: {self._halt_reason}"
        if value_usdt <= 0:
            return False, "Position value must be positive"
        if leverage < 1 or leverage > self.config.max_leverage:
            return False, (
                f"Leverage out of range: {leverage}, "
                f"allowed 1..{self.config.max_leverage}"
            )
        if symbol in self.blocked_symbols:
            reason = self.block_reasons.get(symbol, "unknown")
            return False, f"Symbol blocked: {reason}"

        risk_symbols = set(self.positions)
        risk_symbols.update(
            reservation.symbol for reservation in self._open_reservations.values()
        )
        if symbol not in risk_symbols and len(risk_symbols) >= self.config.max_symbols:
            return False, f"Max symbols reached: {self.config.max_symbols}"

        total_value = self.get_total_risk_value() + value_usdt
        if total_value > self.config.max_position_value_usdt:
            return False, (
                f"Max position value exceeded: {total_value} > "
                f"{self.config.max_position_value_usdt}"
            )
        return True, "ok"

    def _update_order_locked(
        self,
        reservation: _OpenReservation,
        status: str | None,
        *,
        has_fills: bool | None,
        filled_value_usdt: Decimal | None,
        fill_time_ms: int | None,
    ) -> None:
        if status is not None:
            reservation.status = status
            if status == "FILLED":
                reservation.full_fill = True
        if has_fills is True:
            reservation.fill_seen = True
        if filled_value_usdt is not None:
            if filled_value_usdt < 0:
                raise ValueError("filled value must be non-negative")
            reservation.reported_filled_value_usdt = max(
                reservation.reported_filled_value_usdt, filled_value_usdt
            )
            reservation.filled_value_usdt = max(
                reservation.filled_value_usdt,
                reservation.reported_filled_value_usdt,
                reservation.noted_filled_value_usdt,
            )
            if filled_value_usdt > 0:
                reservation.fill_seen = True
        if fill_time_ms is not None:
            reservation.last_fill_ms = max(
                reservation.last_fill_ms or fill_time_ms, fill_time_ms
            )

        if status == "REJECTED" and not reservation.fill_seen:
            self._remove_reservation_locked(reservation)
            return
        if status in {"CANCELLED", "EXPIRED"} and has_fills is False:
            if not reservation.fill_seen:
                self._remove_reservation_locked(reservation)
                return
        self._try_confirm_locked(reservation)

    def _remove_reservation_locked(self, reservation: _OpenReservation) -> None:
        for client_order_id, current in tuple(self._open_reservations.items()):
            if current is reservation:
                self._open_reservations.pop(client_order_id, None)
                return

    def _try_confirm_locked(self, reservation: _OpenReservation) -> None:
        """按账户仓位水位确认成交，并处理终态订单剩余占额。"""

        if not reservation.fill_seen or reservation.last_fill_ms is None:
            return
        watermark = self._position_watermarks.get(reservation.symbol, -1)
        if watermark < reservation.last_fill_ms:
            return

        reservation.confirmed_filled_value_usdt = max(
            reservation.confirmed_filled_value_usdt,
            reservation.filled_value_usdt,
        )
        if (
            reservation.status == "FILLED"
            and reservation.full_fill
            and reservation.filled_value_usdt > 0
        ):
            self._remove_reservation_locked(reservation)
        elif reservation.status in {"CANCELLED", "EXPIRED"}:
            # 部分成交撤单的未成交部分没有后续风险；只有在成交价值已知且
            # 已被账户仓位水位覆盖时才能释放整个 reservation。
            if reservation.filled_value_usdt > 0:
                self._remove_reservation_locked(reservation)
