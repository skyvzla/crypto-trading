"""
Dynamic Spike Short Strategy - 冻结基线实现

基线来源: scripts/backtest_dynamic_spike.py（2026-08-06 确认为唯一参数事实源）

策略逻辑:
1. 检测逼空信号：5 秒涨幅 > 5%，5 秒成交量 > 前 60 秒 1s 中位数 × 5 × 3
2. 验证起涨点：12 小时低点涨幅 ≥ 20%，三档价格不得低于 16 小时低点 × 1.10
3. 三档分层做空：spike_high - ATR × (1.15 / 0.75 / 0.35)
4. 失效保护：max(spike_high + 3.5×ATR, 主目标位 + 2.0×ATR)

与脚本的已知偏差（有意保留，见 docs/spike_trader/decisions.md）:
- spike_high 不使用信号所在分钟的未完成 1m K 线，改用已完成 K 线 + 已缓存 1s Bar
- ATR 只使用已完成的 5m K 线，不使用信号所在的未完成 5m 周期
两处偏差都是为了消除脚本中的未来数据泄漏，会导致与原 CSV 逐笔存在可解释差异。

当前已实现全局轮次互斥和第一笔成交计时；完整 Campaign 恢复及持仓退出仍待后续阶段。
"""
from decimal import Decimal
from typing import List, Optional
from dataclasses import dataclass, field

from trading_platform.shared.events import (
    Bar1s,
    Fill,
    Kline,
    OrderIntent,
    StrategyAuditEvent,
)
from trading_platform.shared.execution import StrategyAccount

MS_PER_SECOND = 1000
MS_PER_MINUTE = 60 * MS_PER_SECOND
BINANCE_CLIENT_ORDER_ID_MAX_LENGTH = 36


def _base36(value: int) -> str:
    if value < 0:
        raise ValueError("base36 value must be non-negative")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    encoded = ""
    while value:
        value, remainder = divmod(value, 36)
        encoded = alphabet[remainder] + encoded
    return encoded


def build_entry_client_order_id(symbol: str, signal_time: int, tier: int) -> str:
    """生成 Binance 允许的短入场 ID，同时保留可逆的 signal time。"""
    if not symbol.isalnum() or tier not in {1, 2, 3}:
        raise ValueError("invalid Spike entry order identity")
    value = f"s_{symbol}_{_base36(signal_time)}_e{tier}"
    if len(value) > BINANCE_CLIENT_ORDER_ID_MAX_LENGTH:
        raise ValueError(f"Spike client order ID exceeds Binance limit: {symbol}")
    return value


def parse_entry_client_order_id(
    client_order_id: str,
    *,
    expected_symbol: str | None = None,
) -> tuple[str, int] | None:
    """解析当前短 ID，并兼容已写入旧 WAL 的长格式。"""
    prefix, separator, tier = client_order_id.rpartition("_e")
    if separator and tier in {"1", "2", "3"} and prefix.startswith("s_"):
        symbol, separator, encoded_time = prefix[len("s_") :].rpartition("_")
        if separator and symbol and encoded_time:
            try:
                signal_time = int(encoded_time, 36)
            except ValueError:
                return None
            if expected_symbol is None or symbol == expected_symbol:
                return symbol, signal_time

    prefix, separator, tier = client_order_id.rpartition("_tier")
    if not separator or tier not in {"1", "2", "3"} or not prefix.startswith("spike_short_"):
        return None
    symbol_and_time = prefix[len("spike_short_") :]
    symbol, separator, signal_time = symbol_and_time.rpartition("_")
    if not separator or not signal_time.isdigit():
        return None
    if expected_symbol is not None and symbol != expected_symbol:
        return None
    return symbol, int(signal_time)


def build_exit_client_order_id(symbol: str, event_time: int, reason: str) -> str:
    if not symbol.isalnum() or reason not in {"t", "r"}:
        raise ValueError("invalid Spike exit order identity")
    value = f"x_{symbol}_{_base36(event_time)}_{reason}"
    if len(value) > BINANCE_CLIENT_ORDER_ID_MAX_LENGTH:
        raise ValueError(f"Spike client order ID exceeds Binance limit: {symbol}")
    return value


@dataclass
class SpikeSignal:
    """逼空信号"""
    signal_time: int  # 毫秒时间戳，等于触发 Bar 的 timestamp
    trigger_price: Decimal
    spike_high: Decimal
    origin_price: Decimal
    atr: Decimal
    tier_prices: List[Decimal]  # 3 档做空价格，由低到高
    tier_weights: List[Decimal]  # 3 档名义金额权重
    invalid_price: Decimal  # 失效价
    active_time: int  # 订单激活时间
    expire_time: int  # 订单过期时间

    # 已提交的 client_order_id，用于幂等
    placed_client_order_ids: set = field(default_factory=set)


class DynamicSpikeShortStrategy:
    """
    动态逼空做空策略（单币种）

    策略只依赖 StrategyAccount 小接口，不感知 replay/testnet/live 的具体执行实现。
    """

    # ---- 冻结参数（来自实验脚本，不得擅自修改）----
    TIER_WEIGHTS = (Decimal("0.30"), Decimal("0.40"), Decimal("0.30"))
    RETEST_ATR = Decimal("0.75")  # 主目标位（第二档）ATR 倍数
    SPREAD_ATR = Decimal("0.40")  # 档位间隔 ATR 倍数
    ORIGIN_MIN_RISE = Decimal("0.10")  # 三档价格不得低于 origin × 1.10

    SPIKE_RISE_5S = Decimal("0.05")  # 5 秒涨幅阈值
    VOLUME_MULTIPLE_5S = Decimal("3.0")  # 5 秒成交量倍数
    RISE_FROM_12H_LOW = Decimal("0.20")  # 12 小时低点涨幅阈值

    INVALID_SPIKE_ATR = Decimal("3.5")  # 失效价 spike_high 分支
    INVALID_PRIMARY_ATR = Decimal("2.0")  # 失效价主目标位分支

    LOW_12H_MINUTES = 720  # 12 小时
    ORIGIN_MINUTES = 16 * 60  # 16 小时起涨点窗口
    SPIKE_HIGH_MINUTES = 30  # spike_high 回溯窗口
    ATR_PERIOD = 14  # 5m ATR 周期

    SIGNAL_COOLDOWN = 180  # 信号冷却时间（秒）
    ORDER_TTL = 180  # 订单有效期（秒）
    NON_POSITIVE_EXIT_AFTER_MS = 900 * MS_PER_SECOND

    # 触发判定需要的 1s Bar 数量：索引 i-60 .. i
    BAR_BUFFER = 61

    def __init__(
        self,
        symbol: str,
        total_notional: Decimal,
        account: Optional[StrategyAccount] = None,
        account_id: str = "backtest",
    ):
        """
        Args:
            symbol: 交易对
            total_notional: 每轮固定总名义金额（D-005）。三档按 30/40/30 分配。
                            该值必须由配置显式提供，不设默认值。
            account: 订单、持仓查询与撤单适配器
            account_id: 账户 ID
        """
        if total_notional is None or total_notional <= 0:
            raise ValueError("total_notional must be a positive Decimal")

        self.symbol = symbol
        self.total_notional = Decimal(total_notional)
        self.account_id = account_id
        self._account = account
        self._trading_enabled = True
        self._entry_enabled = True

        # 数据缓存
        self.bars_1s: List[Bar1s] = []
        self.klines_1m: List[Kline] = []
        self.klines_5m: List[Kline] = []

        # 信号状态
        self.last_signal_time: Optional[int] = None
        self.active_signals: List[SpikeSignal] = []
        self.first_fill_time: Optional[int] = None
        self._campaign_id_for_timing: str | None = None
        self._timeout_checked = False
        self._exit_requested = False
        self._pending_rotation: SpikeSignal | None = None
        self._rotation_exit_requested = False
        self._audit_events: List[StrategyAuditEvent] = []

    def bind_account(self, account: StrategyAccount) -> None:
        """由运行模式适配器注入最小账户执行接口。"""
        self._account = account

    def set_trading_enabled(self, enabled: bool) -> None:
        """预热阶段只更新数据缓存，不检测或推进交易信号。"""
        self._trading_enabled = enabled

    def set_entry_enabled(self, enabled: bool) -> None:
        """控制新信号准入；已有信号仍继续失效、撤单和到期处理。"""
        self._entry_enabled = enabled

    def on_fill(self, fill: Fill) -> None:
        """记录本轮第一笔真实成交时间，作为 900 秒计时起点。"""
        if fill.symbol != self.symbol:
            return
        if self._account is None:
            return
        order = self._account.get_order(fill.order_id)
        if order is None or order.strategy_id != "spike_short":
            return
        if self.first_fill_time is None and fill.side == "SELL":
            self.first_fill_time = fill.fill_time
            self._campaign_id_for_timing = self._campaign_id_from_client_order(
                order.client_order_id
            )
            self._record_audit(
                event_time=fill.fill_time,
                event_type="campaign_first_fill",
                campaign_id=self._campaign_id_for_timing,
                details={
                    "order_id": fill.order_id,
                    "fill_id": fill.fill_id,
                    "price": str(fill.price),
                    "quantity": str(fill.quantity),
                },
            )
        elif (
            fill.side == "BUY"
            and order.trigger_reason in {"campaign_timeout_exit", "campaign_rotation_exit"}
        ):
            self._record_audit(
                event_time=fill.fill_time,
                event_type=(
                    "campaign_rotation_exit_filled"
                    if order.trigger_reason == "campaign_rotation_exit"
                    else "campaign_timeout_exit_filled"
                ),
                campaign_id=self._campaign_id_for_timing,
                details={
                    "order_id": fill.order_id,
                    "fill_id": fill.fill_id,
                    "price": str(fill.price),
                    "quantity": str(fill.quantity),
                },
            )

    def reset_campaign_timing(self) -> None:
        self.first_fill_time = None
        self._campaign_id_for_timing = None
        self._timeout_checked = False
        self._exit_requested = False
        self._pending_rotation = None
        self._rotation_exit_requested = False

    def restore_campaign_timing(self, campaign_id: str, first_fill_time: int) -> None:
        """从持久化执行事实恢复当前持仓的退出计时状态。"""
        expected_prefix = f"spike_short:{self.symbol}:"
        if not campaign_id.startswith(expected_prefix) or first_fill_time <= 0:
            raise ValueError("invalid recovered Spike campaign timing")
        self.first_fill_time = first_fill_time
        self._campaign_id_for_timing = campaign_id
        self._timeout_checked = False
        self._exit_requested = False
        self._pending_rotation = None
        self._rotation_exit_requested = False

    def drain_audit_events(self) -> List[StrategyAuditEvent]:
        """返回并清空尚未被运行适配器收集的审计事件。"""
        events = self._audit_events
        self._audit_events = []
        return events

    # ------------------------------------------------------------------
    # 事件入口（符合 backtest.engine.Strategy 协议）
    # ------------------------------------------------------------------

    def on_bar1s(self, bar: Bar1s) -> List[OrderIntent]:
        """处理 1 秒 Bar 事件"""
        self._update_cache(bar)

        if not self._trading_enabled:
            return []

        if len(self.bars_1s) < self.BAR_BUFFER:
            return []

        timeout_intent = self._manage_non_positive_timeout(bar)

        rotation_intent: List[OrderIntent] = []
        if self._pending_rotation is not None and not self._has_live_campaign():
            signal = self._pending_rotation
            self.reset_campaign_timing()
            self.active_signals.append(signal)
            self.last_signal_time = signal.signal_time
            self._record_audit(
                event_time=bar.timestamp,
                event_type="campaign_rotation_activated",
                campaign_id=self._campaign_id(signal),
                details={"origin_signal_time": signal.signal_time},
            )

        if self._entry_enabled:
            signal = self._detect_signal(bar)
            if signal and self._has_live_campaign():
                rotation_intent = self._prepare_rotation(signal, bar)
            elif signal:
                self.active_signals.append(signal)
                self.last_signal_time = signal.signal_time
                campaign_id = self._campaign_id(signal)
                self._record_audit(
                    event_time=signal.signal_time,
                    event_type="signal_triggered",
                    campaign_id=campaign_id,
                    details={
                        "trigger_price": str(signal.trigger_price),
                        "rise_threshold_5s": str(self.SPIKE_RISE_5S),
                        "volume_threshold_5s": str(self.VOLUME_MULTIPLE_5S),
                    },
                )
                self._record_audit(
                    event_time=signal.signal_time,
                    event_type="entry_plan_created",
                    campaign_id=campaign_id,
                    details={
                        "spike_high": str(signal.spike_high),
                        "origin_price": str(signal.origin_price),
                        "atr": str(signal.atr),
                        "tier_prices": [str(price) for price in signal.tier_prices],
                        "tier_weights": [str(weight) for weight in signal.tier_weights],
                        "invalid_price": str(signal.invalid_price),
                        "active_time": signal.active_time,
                        "expire_time": signal.expire_time,
                    },
                )

        return timeout_intent + rotation_intent + self._manage_signals(bar)

    def _has_live_campaign(self) -> bool:
        if self.active_signals:
            return True
        if self._account is None:
            return False
        if self._account.has_open_position(self.symbol):
            return True
        return any(
            order.symbol == self.symbol
            and order.status in {"NEW", "PARTIALLY_FILLED", "SUBMIT_UNKNOWN"}
            and order.strategy_id == "spike_short"
            for order in self._account.iter_orders()
        )

    def _prepare_rotation(self, signal: SpikeSignal, bar: Bar1s) -> List[OrderIntent]:
        """D-009：盈利且超过 900 秒时，先排队新信号并平旧仓。"""
        if self._pending_rotation is not None or self._rotation_exit_requested:
            return []
        if self.first_fill_time is None or self._account is None:
            return []
        if bar.available_time < self.first_fill_time + self.NON_POSITIVE_EXIT_AFTER_MS:
            return []
        position = self._account.get_position(self.symbol)
        if position is None or position.side != "SHORT" or position.quantity <= 0:
            return []
        net_pnl = (position.entry_price - bar.close) * position.quantity - position.total_commission
        if net_pnl <= 0:
            return []

        for active in list(self.active_signals):
            cancelled = self._cancel_signal_orders(active)
            self._record_signal_terminal(
                active, "campaign_rotation_old_signal_closed", bar.timestamp, cancelled
            )
        self.active_signals.clear()
        self._pending_rotation = signal
        self._rotation_exit_requested = True
        campaign_id = self._campaign_id(signal)
        self._record_audit(
            event_time=bar.available_time,
            event_type="campaign_rotation_exit_requested",
            campaign_id=campaign_id,
            details={"old_campaign_id": self._campaign_id_for_timing, "net_pnl": str(net_pnl)},
        )
        return [
            OrderIntent(
                symbol=self.symbol,
                side="BUY",
                price=bar.close,
                quantity=position.quantity,
                client_order_id=build_exit_client_order_id(
                    self.symbol, bar.timestamp, "r"
                ),
                order_type="MARKET",
                strategy_id="spike_short",
                trigger_reason="campaign_rotation_exit",
            )
        ]

    def _manage_non_positive_timeout(self, bar: Bar1s) -> List[OrderIntent]:
        """执行已确认的 D-007：首成交后 900 秒净收益不为正则退出。

        D-008 的盈利仓位管理不在这里实现；若 900 秒时仍盈利，只记录检查结果。
        """
        if (
            self.first_fill_time is None
            or self._timeout_checked
            or self._exit_requested
            or self._account is None
            or bar.available_time < self.first_fill_time + self.NON_POSITIVE_EXIT_AFTER_MS
        ):
            return []
        position = self._account.get_position(self.symbol)
        if position is None or position.quantity <= 0:
            return []
        if position.side != "SHORT":
            return []
        self._timeout_checked = True
        gross_pnl = (position.entry_price - bar.close) * position.quantity
        net_pnl = gross_pnl - position.total_commission
        self._record_audit(
            event_time=bar.available_time,
            event_type="campaign_timeout_check",
            campaign_id=self._campaign_id_for_timing,
            details={
                "first_fill_time": self.first_fill_time,
                "mark_price": str(bar.close),
                "gross_pnl": str(gross_pnl),
                "entry_commission": str(position.total_commission),
                "net_pnl": str(net_pnl),
                "exit_required": net_pnl <= 0,
            },
        )
        if net_pnl > 0:
            return []
        self._exit_requested = True
        self._record_audit(
            event_time=bar.available_time,
            event_type="campaign_timeout_exit_requested",
            campaign_id=self._campaign_id_for_timing,
            details={"quantity": str(position.quantity), "mark_price": str(bar.close)},
        )
        return [
            OrderIntent(
                symbol=self.symbol,
                side="BUY",
                price=bar.close,
                quantity=position.quantity,
                client_order_id=build_exit_client_order_id(
                    self.symbol, self.first_fill_time, "t"
                ),
                order_type="MARKET",
                strategy_id="spike_short",
                trigger_reason="campaign_timeout_exit",
            )
        ]

    def on_kline(self, kline: Kline) -> List[OrderIntent]:
        """处理已完成 K 线事件"""
        if kline.interval == "1m":
            self.klines_1m.append(kline)
            cutoff = kline.close_time - 30 * 3600 * MS_PER_SECOND
            self.klines_1m = [k for k in self.klines_1m if k.close_time >= cutoff]

        elif kline.interval == "5m":
            self.klines_5m.append(kline)
            cutoff = kline.close_time - 40 * 3600 * MS_PER_SECOND
            self.klines_5m = [k for k in self.klines_5m if k.close_time >= cutoff]

        return []

    # ------------------------------------------------------------------
    # 信号生命周期
    # ------------------------------------------------------------------

    def _manage_signals(self, bar: Bar1s) -> List[OrderIntent]:
        """推进活跃信号，返回本 Bar 需要提交的订单意图"""
        intents: List[OrderIntent] = []

        for sig in list(self.active_signals):
            # 1. 过期：撤销未成交入场单（D-006）
            if bar.timestamp >= sig.expire_time:
                cancelled = self._cancel_signal_orders(sig)
                self._record_signal_terminal(sig, "signal_expired", bar.timestamp, cancelled)
                self.active_signals.remove(sig)
                continue

            # 2. 触及失效价：撤销未成交入场单，已成交仓位交由退出逻辑处理（D-006）
            if bar.high >= sig.invalid_price:
                cancelled = self._cancel_signal_orders(sig)
                self._record_signal_terminal(
                    sig, "signal_invalidated", bar.timestamp, cancelled
                )
                self.active_signals.remove(sig)
                continue

            # 3. 未到激活时间
            if bar.timestamp < sig.active_time:
                continue

            # 4. 三档挂单（按 client_order_id 幂等）
            for tier_idx, (tier_price, tier_weight) in enumerate(
                zip(sig.tier_prices, sig.tier_weights), start=1
            ):
                client_order_id = self._client_order_id(sig, tier_idx)
                if client_order_id in sig.placed_client_order_ids:
                    continue

                notional = self.total_notional * tier_weight
                quantity = notional / tier_price

                intents.append(
                    OrderIntent(
                        symbol=self.symbol,
                        side="SELL",
                        price=tier_price,
                        quantity=quantity,
                        client_order_id=client_order_id,
                        ttl_ms=sig.expire_time - bar.timestamp,
                        strategy_id="spike_short",
                        trigger_reason=f"spike_tier{tier_idx}",
                    )
                )
                sig.placed_client_order_ids.add(client_order_id)

        return intents

    def _client_order_id(self, sig: SpikeSignal, tier_idx: int) -> str:
        return build_entry_client_order_id(self.symbol, sig.signal_time, tier_idx)

    def _cancel_signal_orders(self, sig: SpikeSignal) -> int:
        """
        撤销该信号已提交且仍未成交的入场单。

        通过账户抽象下发撤单，不依赖运行模式的具体执行器。
        """
        if self._account is None or not sig.placed_client_order_ids:
            return 0

        cancelled = 0
        for order in self._account.iter_orders():
            if order.client_order_id not in sig.placed_client_order_ids:
                continue
            if order.status not in {"NEW", "PARTIALLY_FILLED"}:
                continue
            if self._account.cancel_order(order.order_id):
                cancelled += 1
        return cancelled

    def _campaign_id(self, sig: SpikeSignal) -> str:
        return f"spike_short:{self.symbol}:{sig.signal_time}"

    def _campaign_id_from_client_order(self, client_order_id: str) -> str | None:
        parsed = parse_entry_client_order_id(
            client_order_id, expected_symbol=self.symbol
        )
        if parsed is None:
            return None
        symbol, signal_time = parsed
        return f"spike_short:{symbol}:{signal_time}"

    def _record_signal_terminal(
        self,
        sig: SpikeSignal,
        event_type: str,
        event_time: int,
        cancelled_orders: int,
    ) -> None:
        self._record_audit(
            event_time=event_time,
            event_type=event_type,
            campaign_id=self._campaign_id(sig),
            details={"cancelled_orders": cancelled_orders},
        )

    def _record_audit(
        self,
        *,
        event_time: int,
        event_type: str,
        campaign_id: str | None,
        details: dict,
    ) -> None:
        self._audit_events.append(
            StrategyAuditEvent(
                event_time=event_time,
                event_type=event_type,
                symbol=self.symbol,
                strategy_id="spike_short",
                campaign_id=campaign_id,
                details=details,
            )
        )

    # ------------------------------------------------------------------
    # 缓存与信号检测
    # ------------------------------------------------------------------

    def _update_cache(self, bar: Bar1s) -> None:
        self.bars_1s.append(bar)
        if len(self.bars_1s) > self.BAR_BUFFER:
            self.bars_1s = self.bars_1s[-self.BAR_BUFFER:]

    def _detect_signal(self, bar: Bar1s) -> Optional[SpikeSignal]:
        """
        检测逼空信号。

        窗口语义与脚本一致：以触发 Bar 的 timestamp（该秒起点）为基准，
        bars_1s[-1] 为触发 Bar，bars_1s[-6] 为 5 秒前，bars_1s[-61] 为 60 秒前。
        """
        bars = self.bars_1s
        if len(bars) < self.BAR_BUFFER:
            return None

        current = bars[-1]
        bar_5s_ago = bars[-6]
        bar_60s_ago = bars[-61]

        # 1. 数据连续性：缺口直接放弃本 Bar（脚本同口径）
        if current.timestamp - bar_5s_ago.timestamp != 5 * MS_PER_SECOND:
            return None
        if current.timestamp - bar_60s_ago.timestamp != 60 * MS_PER_SECOND:
            return None

        # 2. 信号冷却
        if self.last_signal_time is not None:
            if current.timestamp - self.last_signal_time < self.SIGNAL_COOLDOWN * MS_PER_SECOND:
                return None

        # 3. 5 秒涨幅：close[i] / close[i-5] - 1
        rise_5s = current.close / bar_5s_ago.close - Decimal("1")
        if rise_5s < self.SPIKE_RISE_5S:
            return None

        # 4. 成交量倍数：sum(volume[i-4..i]) / (median(volume[i-60..i-1]) × 5)
        volume_5s = sum((b.volume for b in bars[-5:]), Decimal("0"))
        baseline_volumes = sorted(b.volume for b in bars[-61:-1])
        median_volume = baseline_volumes[30]
        if median_volume <= 0:
            return None
        if volume_5s / (median_volume * Decimal("5")) < self.VOLUME_MULTIPLE_5S:
            return None

        # 5. 12 小时低点涨幅
        minute_start = current.timestamp - (current.timestamp % MS_PER_MINUTE)
        low_12h = self._min_low_1m(minute_start, self.LOW_12H_MINUTES)
        if low_12h is None or low_12h <= 0:
            return None
        if current.close / low_12h - Decimal("1") < self.RISE_FROM_12H_LOW:
            return None

        # 6. 起涨点（16 小时最低价）
        origin_price = self._min_low_1m(minute_start, self.ORIGIN_MINUTES)
        if origin_price is None or origin_price <= 0:
            return None

        # 7. spike_high（已完成 1m K 线 + 已缓存 1s Bar，避免未完成 K 线泄漏）
        spike_high = self._spike_high(minute_start)
        if spike_high is None:
            return None

        # 8. ATR（已完成 5m K 线，14 周期）
        atr = self._atr_5m()
        if atr is None or atr <= 0:
            return None

        # 9. 三档价格：spike_high - ATR × (1.15, 0.75, 0.35)
        tier_prices = [
            spike_high - atr * (self.RETEST_ATR - Decimal(n - 1) * self.SPREAD_ATR)
            for n in range(3)
        ]

        # 10. 价格合理性：最低档不得低于 origin_floor，且必须高于触发价
        origin_floor = origin_price * (Decimal("1") + self.ORIGIN_MIN_RISE)
        lowest_tier = min(tier_prices)
        if lowest_tier < origin_floor or lowest_tier <= current.close:
            return None

        # 11. 失效价
        invalid_price = max(
            spike_high + atr * self.INVALID_SPIKE_ATR,
            tier_prices[1] + atr * self.INVALID_PRIMARY_ATR,
        )

        active_time = current.timestamp + MS_PER_SECOND
        return SpikeSignal(
            signal_time=current.timestamp,
            trigger_price=current.close,
            spike_high=spike_high,
            origin_price=origin_price,
            atr=atr,
            tier_prices=tier_prices,
            tier_weights=list(self.TIER_WEIGHTS),
            invalid_price=invalid_price,
            active_time=active_time,
            expire_time=active_time + self.ORDER_TTL * MS_PER_SECOND,
        )

    # ------------------------------------------------------------------
    # 指标计算
    # ------------------------------------------------------------------

    def _min_low_1m(self, minute_start: int, minutes: int) -> Optional[Decimal]:
        """已完成 1m K 线在 [minute_start - minutes, minute_start) 内的最低价"""
        window = self._completed_1m_window(minute_start, minutes)
        return min((k.low for k in window), default=None) if window else None

    def _completed_1m_window(
        self, minute_start: int, minutes: int
    ) -> List[Kline]:
        """返回连续完整的 1m 窗口；缺任一分钟时返回空列表。"""
        window_start = minute_start - minutes * MS_PER_MINUTE
        by_open_time = {
            k.open_time: k
            for k in self.klines_1m
            if window_start <= k.open_time < minute_start
        }
        expected_times = range(window_start, minute_start, MS_PER_MINUTE)
        if any(open_time not in by_open_time for open_time in expected_times):
            return []
        return [by_open_time[open_time] for open_time in expected_times]

    def _spike_high(self, minute_start: int) -> Optional[Decimal]:
        """
        spike_high = max(
            已完成 1m K 线在最近 30 分钟内的最高价,
            已缓存 1s Bar 的最高价（覆盖当前未完成分钟）
        )
        """
        completed = self._completed_1m_window(
            minute_start, self.SPIKE_HIGH_MINUTES
        )
        if not completed:
            return None
        highs = [k.high for k in completed]
        highs.extend(b.high for b in self.bars_1s if b.timestamp >= minute_start)
        return max(highs) if highs else None

    def _atr_5m(self) -> Optional[Decimal]:
        """已完成 5m K 线的 14 周期 ATR"""
        if len(self.klines_5m) < self.ATR_PERIOD + 1:
            return None

        atr_klines = self.klines_5m[-(self.ATR_PERIOD + 1):]
        if any(
            current.open_time - previous.open_time != 5 * MS_PER_MINUTE
            for previous, current in zip(atr_klines, atr_klines[1:])
        ):
            return None

        true_ranges = []
        for i in range(1, self.ATR_PERIOD + 1):
            k = self.klines_5m[-i]
            k_prev = self.klines_5m[-i - 1]
            true_ranges.append(
                max(
                    k.high - k.low,
                    abs(k.high - k_prev.close),
                    abs(k.low - k_prev.close),
                )
            )

        return sum(true_ranges) / Decimal(len(true_ranges))


class DynamicSpikeBacktestStrategy:
    """
    多币种适配器，符合 backtest.engine.Strategy 协议。

    引擎按 on_bar1s(bar) / on_kline(kline) 调用，不再使用 on_event。
    """

    def __init__(
        self,
        symbols: List[str],
        total_notional: Decimal,
        account: Optional[StrategyAccount] = None,
    ):
        self.strategies = {
            symbol: DynamicSpikeShortStrategy(
                symbol, total_notional=total_notional, account=account
            )
            for symbol in symbols
        }
        self._account: Optional[StrategyAccount] = account
        self._entry_enabled = True
        self.active_symbol: Optional[str] = None

    def bind_account(self, account: StrategyAccount) -> None:
        self._account = account
        for strategy in self.strategies.values():
            strategy.bind_account(account)

    def set_trading_enabled(self, enabled: bool) -> None:
        for strategy in self.strategies.values():
            strategy.set_trading_enabled(enabled)

    def set_entry_enabled(self, enabled: bool) -> None:
        """统一控制多币种适配器的新入场准入；已有信号仍继续管理。"""
        self._entry_enabled = enabled
        for strategy in self.strategies.values():
            strategy.set_entry_enabled(enabled)

    def on_bar1s(self, bar: Bar1s) -> List[OrderIntent]:
        strategy = self.strategies.get(bar.symbol)
        if strategy is None:
            return []

        strategy.set_entry_enabled(
            self._entry_enabled
            and (self.active_symbol is None or self.active_symbol == bar.symbol)
        )
        intents = strategy.on_bar1s(bar)

        if self.active_symbol is None and self._has_live_campaign(bar.symbol):
            self.active_symbol = bar.symbol
        elif (
            self.active_symbol == bar.symbol
            and not self._has_live_campaign(bar.symbol)
        ):
            strategy.reset_campaign_timing()
            self.active_symbol = None

        return intents

    def on_kline(self, kline: Kline) -> List[OrderIntent]:
        strategy = self.strategies.get(kline.symbol)
        return strategy.on_kline(kline) if strategy else []

    def on_fill(self, fill: Fill) -> None:
        strategy = self.strategies.get(fill.symbol)
        if strategy is not None:
            strategy.on_fill(fill)

    def restore_campaign_timing(
        self, symbol: str, campaign_id: str, first_fill_time: int
    ) -> None:
        strategy = self.strategies.get(symbol)
        if strategy is None:
            raise ValueError(f"unknown Spike symbol: {symbol}")
        strategy.restore_campaign_timing(campaign_id, first_fill_time)
        self.active_symbol = symbol

    def drain_audit_events(self) -> List[StrategyAuditEvent]:
        events: List[StrategyAuditEvent] = []
        for strategy in self.strategies.values():
            events.extend(strategy.drain_audit_events())
        return events

    def _has_live_campaign(self, symbol: str) -> bool:
        strategy = self.strategies[symbol]
        if strategy.active_signals:
            return True
        if self._account is None:
            return False
        if self._account.has_open_position(symbol):
            return True
        return any(
            order.symbol == symbol
            and order.status in {"NEW", "PARTIALLY_FILLED", "SUBMIT_UNKNOWN"}
            and order.strategy_id == "spike_short"
            for order in self._account.iter_orders()
        )
