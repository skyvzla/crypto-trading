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
from collections import deque
from decimal import Decimal
from itertools import islice
from typing import TYPE_CHECKING, Iterable, List, Literal, Optional
from dataclasses import dataclass, field, replace

from trading_platform.shared.events import (
    Bar1s,
    Fill,
    Kline,
    OrderIntent,
    StrategyAuditEvent,
)
from trading_platform.shared.execution import StrategyAccount
from trading_platform.strategies.spike.exit_features import (
    CandidateFeatureConfig,
    CandidateFeatureSnapshot,
    candidate_feature_snapshot,
)
from trading_platform.strategies.spike.entry_features import (
    EntryContextFeatures,
    entry_context_features,
)
from trading_platform.strategies.spike.shared_features import (
    append_kline_and_evict_expired,
)

if TYPE_CHECKING:
    from trading_platform.strategies.spike.shared_features import (
        SpikeSharedFeatureProvider,
    )
from trading_platform.strategies.spike.exit_policy import (
    CandidateV1Config,
    ExitAction,
    ExitObservation,
    SpikeExitPolicyState,
    candidate_v1_risks,
)

MS_PER_SECOND = 1000
MS_PER_MINUTE = 60 * MS_PER_SECOND
BINANCE_CLIENT_ORDER_ID_MAX_LENGTH = 36
EntryTierMode = Literal["three-tier", "tier3-only", "single-entry"]


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
    if not symbol.isalnum() or reason not in {"t", "r", "h", "c"}:
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

    # 回测审计字段；不参与下单决策，保留信号复核所需的原始指标。
    spike_high_time: int | None = None
    rise_5s: Decimal | None = None
    rise_window_returns: dict[int, Decimal] = field(default_factory=dict)
    volume_5s: Decimal | None = None
    median_volume_1s: Decimal | None = None
    volume_multiple_5s: Decimal | None = None
    low_12h: Decimal | None = None
    rise_from_12h_low: Decimal | None = None
    origin_floor: Decimal | None = None
    pullback_threshold: Decimal | None = None
    pullback_time: int | None = None
    pullback_low: Decimal | None = None
    prior_high: Decimal | None = None
    prior_high_time: int | None = None
    # 保留 4h 字段，兼容已有报告；非 4h 回测时为空。
    prior_high_4h: Decimal | None = None
    prior_high_4h_time: int | None = None
    rise_low: Decimal | None = None
    rise_low_time: int | None = None
    rise_low_age_minutes: int | None = None
    entry_context: EntryContextFeatures | None = None
    # 箱体/通道突破审计字段
    box_upper_3d: Decimal | None = None
    box_upper_7d: Decimal | None = None
    box_lower_3d: Decimal | None = None
    box_lower_7d: Decimal | None = None
    box_breakthrough: Decimal | None = None
    box_break_lower: Decimal | None = None
    box_break_first_time: int | None = None
    box_break_minutes: int | None = None
    box_break_hours: float | None = None
    # 过早触发过滤审计字段：信号触发价相对前 30m 均价的偏离、前 60m 价格极差
    spike_avg_deviation_pct: float | None = None
    spike_range_pct: float | None = None
    # VWAP 偏离过滤审计字段：信号触发价相对前 100m 聚合 20 根 5m VWAP 的偏离
    spike_vwap_deviation_pct: float | None = None


class DynamicSpikeShortStrategy:
    """
    动态逼空做空策略（单币种）

    策略只依赖 StrategyAccount 小接口，不感知 replay/testnet/live 的具体执行实现。
    """

    # ---- 冻结参数（来自实验脚本，不得擅自修改）----
    TIER_WEIGHTS = (Decimal("0.30"), Decimal("0.40"), Decimal("0.30"))
    SINGLE_ENTRY_ATR = Decimal("0.35")
    RETEST_ATR = Decimal("0.75")  # 主目标位（第二档）ATR 倍数
    SPREAD_ATR = Decimal("0.40")  # 档位间隔 ATR 倍数
    ORIGIN_MIN_RISE = Decimal("0.10")  # 三档价格不得低于 origin × 1.10

    SPIKE_RISE_5S = Decimal("0.05")  # 5 秒涨幅阈值
    VOLUME_MULTIPLE_5S = Decimal("3.0")  # 5 秒成交量倍数
    RISE_FROM_12H_LOW = Decimal("0.20")  # 12 小时低点涨幅阈值

    # 加速豁免：5s 涨幅不足但呈分钟级持续加速（蠕升）时放行。
    # 条件：5s 涨幅 >= accel_rise_5s_min，当前分钟实时涨幅（1s 累计）
    # 达到前 accel_prev_minutes 分钟已完成 1m 涨幅均值的 accel_ratio 倍。
    # 默认关闭（阈值=0）。秒级加速无需豁免：1s 持续放量必然触发 5s 阈值。
    ACCEL_RISE_5S_MIN = Decimal("0.0")  # 豁免路径的最低 5s 涨幅
    ACCEL_RATIO = Decimal("2.0")  # 当前分钟涨幅 / 前 N 分钟涨幅均值的倍率
    ACCEL_PREV_MINUTES = 5  # 前窗口分钟数
    ACCEL_PREV2_MINUTES = 5  # 加速对比的前置窗口分钟数
    ACCEL_PREV_AVG_MIN = Decimal("0.02")  # 前窗口均值涨幅下限
    ACCEL_PREV_RATIO = Decimal("1.5")  # 前窗口均值 / 前置窗口均值的倍率

    INVALID_SPIKE_ATR = Decimal("3.5")  # 失效价 spike_high 分支
    INVALID_PRIMARY_ATR = Decimal("2.0")  # 失效价主目标位分支

    LOW_12H_MINUTES = 720  # 12 小时
    ORIGIN_MINUTES = 16 * 60  # 16 小时起涨点窗口
    SPIKE_HIGH_MINUTES = 30  # spike_high 回溯窗口
    PRIOR_HIGH_LOOKBACK_MINUTES = 4 * 60  # 4 小时前高过滤窗口
    ATR_PERIOD = 14  # 5m ATR 周期

    SIGNAL_COOLDOWN = 180  # 信号冷却时间（秒）
    ORDER_TTL = 180  # 订单有效期（秒）
    NON_POSITIVE_EXIT_AFTER_MS = 900 * MS_PER_SECOND

    # 触发判定需要的 1s Bar 数量：索引 i-60 .. i
    BAR_BUFFER = 61
    RISE_CAP_WINDOW_MAX_SECONDS = 60
    strategy_name = "dynamic-base"

    def __init__(
        self,
        symbol: str,
        total_notional: Decimal,
        account: Optional[StrategyAccount] = None,
        account_id: str = "backtest",
        exit_policy: Literal["execution-test-d007", "candidate-v1"] = "execution-test-d007",
        prior_high_lookback_minutes: int | None = None,
        entry_tier_mode: EntryTierMode = "three-tier",
        reject_below_current: bool = False,
        rise_low_lookback_minutes: int = 0,
        min_rise_duration_minutes: int = 0,
        box_duration_min_minutes: int = 0,
        spike_avg_deviation_max_pct: float = 0,
        spike_range_max_pct: float = 0,
        spike_vwap_deviation_max_pct: float = 0,
        early_profit_unlock_ratio: Decimal | None = None,
        rise_5s_threshold: Decimal | None = None,
        max_rise_5s_percent: Decimal | None = None,
        max_rise_window_seconds: int = 5,
        max_rise_window_percent: Decimal | None = None,
        accel_rise_5s_min: Decimal | None = None,
        accel_ratio: Decimal | None = None,
        accel_prev_minutes: int | None = None,
        accel_prev2_minutes: int | None = None,
        accel_prev_avg_min: Decimal | None = None,
        accel_prev_ratio: Decimal | None = None,
        max_volume_multiple_5s: Decimal | None = None,
        prior_high_tolerance_percent: Decimal = Decimal("0"),
        exit_strict_age_ms: int | None = None,
        exit_flat_agreement: int | None = None,
        time_risk_grace_ms: int = 0,
        time_risk_grace_loss_ratio: Decimal = Decimal("0.01"),
        strong_strict_age_ms: int | None = None,
        weak_strict_age_ms: int | None = None,
        strong_bucket_strict_age_ms: int | None = None,
        weak_bucket_strict_age_ms: int | None = None,
        profit_unlock_ratio: Decimal | None = None,
        profit_drawdown_ratio: Decimal | None = None,
        profit_drawdown_peak_ratio: Decimal | None = None,
    ):
        """
        Args:
            symbol: 交易对
            total_notional: 每轮固定总名义金额（D-005）。该值必须由配置显式提供，
                            不设默认值；single-entry 模式全部用于唯一入场单。
            account: 订单、持仓查询与撤单适配器
            account_id: 账户 ID
        """
        if total_notional is None or total_notional <= 0:
            raise ValueError("total_notional must be a positive Decimal")

        self.symbol = symbol
        self.total_notional = Decimal(total_notional)
        self.account_id = account_id
        self.exit_policy = exit_policy
        if entry_tier_mode not in {"three-tier", "tier3-only", "single-entry"}:
            raise ValueError(
                "entry_tier_mode must be three-tier, tier3-only, or single-entry"
            )
        self.entry_tier_mode = entry_tier_mode
        self.reject_below_current = bool(reject_below_current)
        if (rise_low_lookback_minutes <= 0) != (min_rise_duration_minutes <= 0):
            raise ValueError(
                "rise_low_lookback_minutes and min_rise_duration_minutes "
                "must both be positive or both be zero"
            )
        if min_rise_duration_minutes > rise_low_lookback_minutes:
            raise ValueError(
                "min_rise_duration_minutes must not exceed rise_low_lookback_minutes"
            )
        self.rise_low_lookback_minutes = int(rise_low_lookback_minutes)
        self.min_rise_duration_minutes = int(min_rise_duration_minutes)
        if box_duration_min_minutes < 0:
            raise ValueError("box_duration_min_minutes must not be negative")
        self.box_duration_min_minutes = int(box_duration_min_minutes)
        if spike_avg_deviation_max_pct < 0 or spike_range_max_pct < 0:
            raise ValueError(
                "spike_avg_deviation_max_pct and spike_range_max_pct must not be negative"
            )
        if (spike_avg_deviation_max_pct > 0) != (spike_range_max_pct > 0):
            raise ValueError(
                "spike_avg_deviation_max_pct and spike_range_max_pct "
                "must both be zero or both be positive"
            )
        self.spike_avg_deviation_max_pct = float(spike_avg_deviation_max_pct)
        self.spike_range_max_pct = float(spike_range_max_pct)
        if spike_vwap_deviation_max_pct < 0:
            raise ValueError("spike_vwap_deviation_max_pct must not be negative")
        self.spike_vwap_deviation_max_pct = float(spike_vwap_deviation_max_pct)
        if early_profit_unlock_ratio is not None:
            early_profit_unlock_ratio = Decimal(early_profit_unlock_ratio)
            if not Decimal("0") < early_profit_unlock_ratio < Decimal("1"):
                raise ValueError("early_profit_unlock_ratio must be between 0 and 1")
        self.early_profit_unlock_ratio = early_profit_unlock_ratio
        self.rise_5s_threshold = (
            self.SPIKE_RISE_5S if rise_5s_threshold is None else Decimal(rise_5s_threshold)
        )
        if self.rise_5s_threshold < 0:
            raise ValueError("rise_5s_threshold must not be negative")
        self.accel_rise_5s_min = (
            self.ACCEL_RISE_5S_MIN
            if accel_rise_5s_min is None
            else Decimal(accel_rise_5s_min)
        )
        self.accel_ratio = (
            self.ACCEL_RATIO if accel_ratio is None else Decimal(accel_ratio)
        )
        self.accel_prev_minutes = (
            self.ACCEL_PREV_MINUTES
            if accel_prev_minutes is None
            else int(accel_prev_minutes)
        )
        self.accel_prev2_minutes = (
            self.ACCEL_PREV2_MINUTES
            if accel_prev2_minutes is None
            else int(accel_prev2_minutes)
        )
        self.accel_prev_avg_min = (
            self.ACCEL_PREV_AVG_MIN
            if accel_prev_avg_min is None
            else Decimal(accel_prev_avg_min)
        )
        self.accel_prev_ratio = (
            self.ACCEL_PREV_RATIO
            if accel_prev_ratio is None
            else Decimal(accel_prev_ratio)
        )
        if self.accel_rise_5s_min > 0 and (
            self.accel_rise_5s_min > self.rise_5s_threshold
        ):
            raise ValueError(
                "accel_rise_5s_min must not exceed rise_5s_threshold"
            )
        if self.accel_rise_5s_min < 0:
            raise ValueError("accel_rise_5s_min must not be negative")
        if self.accel_ratio <= 1:
            raise ValueError("accel_ratio must be greater than 1")
        if self.accel_prev_minutes < 1:
            raise ValueError("accel_prev_minutes must be positive")
        if self.accel_prev2_minutes < 1:
            raise ValueError("accel_prev2_minutes must be positive")
        if self.accel_prev_avg_min < 0:
            raise ValueError("accel_prev_avg_min must not be negative")
        if self.accel_prev_ratio < 1:
            raise ValueError("accel_prev_ratio must be at least 1")
        self.max_rise_window_seconds = int(max_rise_window_seconds)
        if self.max_rise_window_seconds != max_rise_window_seconds:
            raise ValueError("max_rise_window_seconds must be an integer")
        if not 1 <= self.max_rise_window_seconds <= self.RISE_CAP_WINDOW_MAX_SECONDS:
            raise ValueError(
                "max_rise_window_seconds must be between 1 and "
                f"{self.RISE_CAP_WINDOW_MAX_SECONDS}"
            )
        if (
            max_rise_5s_percent is not None
            and max_rise_window_percent is not None
        ):
            raise ValueError(
                "max_rise_5s_percent and max_rise_window_percent "
                "cannot both be set"
            )
        max_rise_percent = (
            max_rise_window_percent
            if max_rise_window_percent is not None
            else max_rise_5s_percent
        )
        self.max_rise_window = (
            None
            if max_rise_percent is None or Decimal(max_rise_percent) == 0
            else Decimal(max_rise_percent) / Decimal("100")
        )
        if self.max_rise_window is not None and self.max_rise_window < 0:
            raise ValueError("max_rise_window_percent must not be negative")
        if (
            self.max_rise_window is not None
            and self.max_rise_window < self.rise_5s_threshold
        ):
            raise ValueError(
                "max_rise_window_percent must be greater than or equal to "
                "rise_5s_threshold"
            )
        # 旧审计和外部调用仍引用此属性；默认 5s 的语义保持不变。
        self.max_rise_5s = self.max_rise_window
        self.max_volume_multiple_5s = (
            None
            if max_volume_multiple_5s is None or Decimal(max_volume_multiple_5s) == 0
            else Decimal(max_volume_multiple_5s)
        )
        if (
            self.max_volume_multiple_5s is not None
            and self.max_volume_multiple_5s < 0
        ):
            raise ValueError("max_volume_multiple_5s must not be negative")
        if (
            self.max_volume_multiple_5s is not None
            and self.max_volume_multiple_5s < self.VOLUME_MULTIPLE_5S
        ):
            raise ValueError(
                "max_volume_multiple_5s must be zero or at least the lower volume threshold"
            )
        self.prior_high_tolerance_percent = Decimal(prior_high_tolerance_percent)
        if not Decimal("0") <= self.prior_high_tolerance_percent <= Decimal("100"):
            raise ValueError("prior_high_tolerance_percent must be between 0 and 100")
        self.prior_high_lookback_minutes = (
            self.PRIOR_HIGH_LOOKBACK_MINUTES
            if prior_high_lookback_minutes is None
            else int(prior_high_lookback_minutes)
        )
        if self.prior_high_lookback_minutes <= 0:
            raise ValueError("prior_high_lookback_minutes must be positive")
        self.exit_strict_age_ms = (
            CandidateV1Config.strict_age_ms
            if exit_strict_age_ms is None
            else int(exit_strict_age_ms)
        )
        if self.exit_strict_age_ms <= 0:
            raise ValueError("exit_strict_age_ms must be positive")
        if exit_flat_agreement is not None:
            exit_flat_agreement = int(exit_flat_agreement)
            if not 1 <= exit_flat_agreement <= 3:
                raise ValueError("exit_flat_agreement must be between 1 and 3")
        self.exit_flat_agreement = exit_flat_agreement
        if time_risk_grace_ms < 0:
            raise ValueError("time_risk_grace_ms must not be negative")
        self.time_risk_grace_ms = int(time_risk_grace_ms)
        if not 0 < time_risk_grace_loss_ratio <= 1:
            raise ValueError("time_risk_grace_loss_ratio must be between 0 and 1")
        self.time_risk_grace_loss_ratio = Decimal(str(time_risk_grace_loss_ratio))
        for label, value in (
            ("strong_strict_age_ms", strong_strict_age_ms),
            ("weak_strict_age_ms", weak_strict_age_ms),
            ("strong_bucket_strict_age_ms", strong_bucket_strict_age_ms),
            ("weak_bucket_strict_age_ms", weak_bucket_strict_age_ms),
        ):
            if value is not None:
                value = int(value)
                if value <= 0:
                    raise ValueError(f"{label} must be positive")
            setattr(self, label, value)
        for label, value in (
            ("profit_unlock_ratio", profit_unlock_ratio),
            ("profit_drawdown_ratio", profit_drawdown_ratio),
            ("profit_drawdown_peak_ratio", profit_drawdown_peak_ratio),
        ):
            if value is not None:
                value = Decimal(str(value))
                if not Decimal("0") < value < Decimal("1"):
                    raise ValueError(f"{label} must be between 0 and 1")
            setattr(self, label, value)
        self._account = account
        self._trading_enabled = True
        self._execution_enabled = True
        self._entry_enabled = True

        # 数据缓存
        self.bars_1s: List[Bar1s] = []
        self.klines_1m: deque[Kline] = deque()
        self.klines_5m: deque[Kline] = deque()
        self.klines_15m: deque[Kline] = deque()
        self._kline_cache_time_ordered = {"1m": True, "5m": True, "15m": True}
        self._shared_feature_provider: SpikeSharedFeatureProvider | None = None

        # 信号状态
        self.last_signal_time: Optional[int] = None
        self._last_cap_rejection_audit: tuple[int, tuple[str, ...]] | None = None
        self._last_entry_filter_rejection_audit: tuple[int, tuple[str, ...]] | None = None
        self.active_signals: List[SpikeSignal] = []
        self.first_fill_time: Optional[int] = None
        self._campaign_id_for_timing: str | None = None
        self._timeout_checked = False
        self._exit_requested = False
        self._campaign_origin_price: Decimal | None = None
        self._candidate_exit_state = SpikeExitPolicyState()
        self._candidate_exit_config = CandidateV1Config(
            risk_start_ms=(
                self.exit_strict_age_ms
                if self.exit_flat_agreement is not None
                else CandidateV1Config.risk_start_ms
            ),
            medium_age_ms=(
                self.exit_strict_age_ms
                if self.exit_flat_agreement is not None
                else CandidateV1Config.medium_age_ms
            ),
            strict_age_ms=self.exit_strict_age_ms,
            flat_momentum_agreement=self.exit_flat_agreement,
            time_risk_grace_ms=self.time_risk_grace_ms,
            time_risk_grace_loss_ratio=self.time_risk_grace_loss_ratio,
            strong_strict_age_ms=self.strong_strict_age_ms,
            weak_strict_age_ms=self.weak_strict_age_ms,
            strong_bucket_strict_age_ms=self.strong_bucket_strict_age_ms,
            weak_bucket_strict_age_ms=self.weak_bucket_strict_age_ms,
            profit_unlock_ratio=self.profit_unlock_ratio,
            profit_drawdown_ratio=self.profit_drawdown_ratio,
            profit_drawdown_peak_ratio=self.profit_drawdown_peak_ratio,
        )
        self._early_profit_risk_unlocked = False
        self._candidate_peak_price: Decimal | None = None
        self._candidate_peak_1m_price: Decimal | None = None
        self._candidate_profit_unlocked = False
        self._candidate_drawdown_armed = False
        self._candidate_entry_bucket: str | None = None
        self._candidate_feature_config = CandidateFeatureConfig()
        self._candidate_features: CandidateFeatureSnapshot | None = None
        self._pending_rotation: SpikeSignal | None = None
        self._rotation_exit_requested = False
        self._candidate_exit_waiting = False
        self._audit_events: List[StrategyAuditEvent] = []

    def bind_account(self, account: StrategyAccount) -> None:
        """由运行模式适配器注入最小账户执行接口。"""
        self._account = account

    def bind_shared_feature_provider(
        self, provider: "SpikeSharedFeatureProvider"
    ) -> None:
        """在 sweep 开始前绑定同回放上下文的共享行情窗口。"""
        if self.bars_1s or self.klines_1m or self.klines_5m or self.klines_15m:
            raise RuntimeError("shared features must be bound before market events")
        self._shared_feature_provider = provider
        self.bars_1s = provider.bars_1s
        self.klines_1m = provider.klines_1m
        self.klines_5m = provider.klines_5m
        self.klines_15m = provider.klines_15m

    def set_trading_enabled(self, enabled: bool) -> None:
        """预热阶段只更新数据缓存，不检测或推进交易信号。"""
        self._trading_enabled = enabled

    def set_entry_enabled(self, enabled: bool) -> None:
        """控制新信号准入；已有信号仍继续失效、撤单和到期处理。"""
        self._entry_enabled = enabled

    def set_execution_enabled(self, enabled: bool) -> None:
        """执行事实不可信时只缓存行情，不推进订单状态机。"""
        self._execution_enabled = enabled

    def refresh_candidate_features(self) -> None:
        """预热批量写入完成后只计算一次当前候选特征。"""
        if self.exit_policy != "candidate-v1" or self.first_fill_time is None:
            return
        if self._shared_feature_provider is not None:
            self._candidate_features = (
                self._shared_feature_provider.candidate_features(
                    self._candidate_feature_config
                )
            )
        else:
            self._candidate_features = candidate_feature_snapshot(
                self.klines_1m,
                self.klines_5m,
                self.klines_15m,
                config=self._candidate_feature_config,
            )

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
            recovered_campaign_id = self._campaign_id_for_timing
            self.first_fill_time = fill.fill_time
            self._campaign_id_for_timing = self._campaign_id_from_client_order(
                order.client_order_id
            )
            if self.exit_policy == "candidate-v1":
                signal = next(
                    (
                        signal
                        for signal in self.active_signals
                        if self._campaign_id(signal) == self._campaign_id_for_timing
                    ),
                    None,
                )
                if signal is None:
                    if (
                        recovered_campaign_id != self._campaign_id_for_timing
                        or self._campaign_origin_price is None
                    ):
                        raise RuntimeError(
                            "candidate exit cannot recover origin from Campaign facts"
                        )
                else:
                    self._campaign_origin_price = signal.origin_price
                self._candidate_entry_bucket = self._entry_bucket(
                    signal.rise_from_12h_low
                )
                self.refresh_candidate_features()
            self._record_audit(
                event_time=fill.fill_time,
                event_type="campaign_first_fill",
                campaign_id=self._campaign_id_for_timing,
                details={
                    "order_id": fill.order_id,
                    "fill_id": fill.fill_id,
                    "price": str(fill.price),
                    "quantity": str(fill.quantity),
                    **self._entry_pattern_details(
                        self._campaign_id_for_timing, fill.fill_time
                    ),
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
        self._campaign_origin_price = None
        self._candidate_exit_state = SpikeExitPolicyState()
        self._candidate_entry_bucket = None
        self._early_profit_risk_unlocked = False
        self._candidate_peak_price = None
        self._candidate_peak_1m_price = None
        self._candidate_profit_unlocked = False
        self._candidate_drawdown_armed = False
        self._candidate_features = None
        self._pending_rotation = None
        self._rotation_exit_requested = False
        self._candidate_exit_waiting = False

    def restore_campaign_timing(
        self,
        campaign_id: str,
        first_fill_time: int,
        *,
        origin_price: Decimal | None = None,
        origin_checked: bool = False,
        reduced_at_origin: bool = False,
        exit_requested: bool = False,
        entry_bucket: str | None = None,
    ) -> None:
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
        self._candidate_exit_waiting = False
        if self.exit_policy == "candidate-v1" and origin_price is None:
            raise ValueError("candidate-v1 recovery requires origin_price")
        self._campaign_origin_price = origin_price
        self._candidate_entry_bucket = entry_bucket
        self._candidate_exit_state = SpikeExitPolicyState(
            origin_checked=origin_checked,
            reduced_at_origin=reduced_at_origin,
            exit_requested=exit_requested,
        )
        self._early_profit_risk_unlocked = False

    def restore_pending_campaign(
        self, campaign_id: str, *, origin_price: Decimal | None
    ) -> None:
        """为重启时尚未成交的入场单恢复 Campaign 身份。"""
        if self.exit_policy != "candidate-v1":
            return
        expected_prefix = f"spike_short:{self.symbol}:"
        if not campaign_id.startswith(expected_prefix) or origin_price is None:
            raise ValueError("candidate-v1 pending Campaign requires origin_price")
        self._campaign_id_for_timing = campaign_id
        self._campaign_origin_price = origin_price

    def campaign_origin_price(self, campaign_id: str) -> Decimal | None:
        signal = next(
            (
                signal
                for signal in self.active_signals
                if self._campaign_id(signal) == campaign_id
            ),
            None,
        )
        return None if signal is None else signal.origin_price

    def campaign_entry_bucket(self, campaign_id: str) -> str | None:
        if self._campaign_id_for_timing == campaign_id:
            return self._candidate_entry_bucket
        return None

    def campaign_exit_state(self) -> tuple[bool, bool, bool] | None:
        if self.exit_policy != "candidate-v1" or self.first_fill_time is None:
            return None
        state = self._candidate_exit_state
        return state.origin_checked, state.reduced_at_origin, state.exit_requested

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

        if not self._trading_enabled or not self._execution_enabled:
            return []

        timeout_intent = (
            self._manage_candidate_exit(bar.available_time, bar.close)
            if self.exit_policy == "candidate-v1"
            else self._manage_non_positive_timeout(bar)
        )

        if timeout_intent:
            return timeout_intent

        if len(self.bars_1s) < self.BAR_BUFFER:
            return timeout_intent

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
                    details=self._signal_audit_details(
                        trigger_price=signal.trigger_price,
                        rise_5s=signal.rise_5s,
                        rise_window_returns=signal.rise_window_returns,
                        volume_5s=signal.volume_5s,
                        median_volume_1s=signal.median_volume_1s,
                        volume_multiple_5s=signal.volume_multiple_5s,
                        low_12h=signal.low_12h,
                        rise_from_12h_low=signal.rise_from_12h_low,
                        entry_context=signal.entry_context,
                    ),
                )
                self._record_audit(
                    event_time=signal.signal_time,
                    event_type="entry_plan_created",
                    campaign_id=campaign_id,
                    details={
                        "spike_high": str(signal.spike_high),
                        "spike_high_time": signal.spike_high_time,
                        "prior_high": (
                            str(signal.prior_high)
                            if signal.prior_high is not None
                            else None
                        ),
                        "prior_high_time": signal.prior_high_time,
                        "prior_high_4h": (
                            str(signal.prior_high_4h)
                            if signal.prior_high_4h is not None
                            else None
                        ),
                        "prior_high_4h_time": signal.prior_high_4h_time,
                        "prior_high_lookback_minutes": self.prior_high_lookback_minutes,
                        "prior_high_guard_all_tiers_above": True,
                        "entry_tier_mode": self.entry_tier_mode,
                        "rise_low_lookback_minutes": self.rise_low_lookback_minutes,
                        "min_rise_duration_minutes": self.min_rise_duration_minutes,
                        "rise_low": (
                            str(signal.rise_low)
                            if signal.rise_low is not None
                            else None
                        ),
                        "rise_low_time": signal.rise_low_time,
                        "rise_low_age_minutes": signal.rise_low_age_minutes,
                        "box_upper_3d": (
                            str(signal.box_upper_3d)
                            if signal.box_upper_3d is not None
                            else None
                        ),
                        "box_upper_7d": (
                            str(signal.box_upper_7d)
                            if signal.box_upper_7d is not None
                            else None
                        ),
                        "box_lower_3d": (
                            str(signal.box_lower_3d)
                            if signal.box_lower_3d is not None
                            else None
                        ),
                        "box_lower_7d": (
                            str(signal.box_lower_7d)
                            if signal.box_lower_7d is not None
                            else None
                        ),
                        "box_breakthrough": (
                            str(signal.box_breakthrough)
                            if signal.box_breakthrough is not None
                            else None
                        ),
                        "box_break_lower": (
                            str(signal.box_break_lower)
                            if signal.box_break_lower is not None
                            else None
                        ),
                        "box_break_first_time": signal.box_break_first_time,
                        "box_break_minutes": signal.box_break_minutes,
                        "box_break_hours": signal.box_break_hours,
                        "spike_avg_deviation_pct": signal.spike_avg_deviation_pct,
                        "spike_range_pct": signal.spike_range_pct,
                        "spike_vwap_deviation_pct": signal.spike_vwap_deviation_pct,
                        "origin_price": str(signal.origin_price),
                        "origin_floor": (
                            str(signal.origin_floor)
                            if signal.origin_floor is not None
                            else None
                        ),
                        "atr": str(signal.atr),
                        "pullback_threshold": (
                            str(signal.pullback_threshold)
                            if signal.pullback_threshold is not None
                            else None
                        ),
                        "pullback_atr": str(self.RETEST_ATR),
                        "signal_cooldown_seconds": self.SIGNAL_COOLDOWN,
                        "order_ttl_seconds": self.ORDER_TTL,
                        **(
                            signal.entry_context.to_audit_details()
                            if signal.entry_context is not None
                            else {}
                        ),
                        "exit_policy": self.exit_policy,
                        "strategy_version": self.strategy_name,
                        "early_profit_unlock_ratio": (
                            str(self.early_profit_unlock_ratio)
                            if self.early_profit_unlock_ratio is not None
                            else None
                        ),
                        "tier_prices": [str(price) for price in signal.tier_prices],
                        "tier_weights": [str(weight) for weight in signal.tier_weights],
                        **(
                            {"entry_price": str(self._single_entry_price(signal))}
                            if self.entry_tier_mode == "single-entry"
                            else {}
                        ),
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
        if (
            self._pending_rotation is not None
            or self._rotation_exit_requested
            or self._candidate_exit_waiting
            or self._candidate_exit_state.exit_requested
        ):
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
                reduce_only=True,
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
                reduce_only=True,
                strategy_id="spike_short",
                trigger_reason="campaign_timeout_exit",
            )
        ]

    def _manage_candidate_exit(
        self, event_time: int, mark_price: Decimal
    ) -> List[OrderIntent]:
        if (
            self.first_fill_time is None
            or self._campaign_origin_price is None
            or self._account is None
            or self._candidate_features is None
        ):
            return []
        position = self._account.get_position(self.symbol)
        if position is None or position.side != "SHORT" or position.quantity <= 0:
            return []
        features = self._candidate_features
        elapsed_ms = event_time - self.first_fill_time
        if elapsed_ms < 0:
            return []
        net_pnl = (
            (position.entry_price - mark_price) * position.quantity
            - position.total_commission
        )
        if self._candidate_peak_price is None or mark_price < self._candidate_peak_price:
            self._candidate_peak_price = mark_price
        unlock_ratio = self._candidate_exit_config.profit_unlock_ratio
        if (
            not self._candidate_profit_unlocked
            and unlock_ratio is not None
            and self._candidate_peak_price is not None
            and position.entry_price > 0
            and (position.entry_price - self._candidate_peak_price)
            / position.entry_price
            >= unlock_ratio
        ):
            self._candidate_profit_unlocked = True
        profit_drawdown = False
        drawdown_ratio = self._candidate_exit_config.profit_drawdown_ratio
        peak_ratio = self._candidate_exit_config.profit_drawdown_peak_ratio
        last_1m_close = None
        if self.klines_1m:
            k = self.klines_1m[-1]
            if k.open_time >= self.first_fill_time:
                last_1m_close = k.close
        if last_1m_close is not None:
            if (
                self._candidate_peak_1m_price is None
                or last_1m_close < self._candidate_peak_1m_price
            ):
                self._candidate_peak_1m_price = last_1m_close
        if (
            drawdown_ratio is not None
            and self._candidate_peak_1m_price is not None
            and last_1m_close is not None
        ):
            if peak_ratio is not None:
                if (
                    not self._candidate_drawdown_armed
                    and position.entry_price > 0
                    and (position.entry_price - self._candidate_peak_1m_price)
                    / position.entry_price
                    >= peak_ratio
                ):
                    self._candidate_drawdown_armed = True
                unlocked = self._candidate_drawdown_armed
                peak_price = self._candidate_peak_1m_price
                current_price = last_1m_close
            else:
                unlocked = (
                    unlock_ratio is None or self._candidate_profit_unlocked
                )
                peak_price = self._candidate_peak_price
                current_price = mark_price
            profit_drawdown = (
                unlocked
                and peak_price is not None
                and peak_price > 0
                and (current_price - peak_price) / peak_price >= drawdown_ratio
            )
        gross_return = (position.entry_price - mark_price) / position.entry_price
        if (
            not self._early_profit_risk_unlocked
            and self.early_profit_unlock_ratio is not None
            and gross_return > self.early_profit_unlock_ratio
        ):
            self._early_profit_risk_unlocked = True
            self._candidate_exit_state.min_risk_age_ms = 0
            self._record_audit(
                event_time=event_time,
                event_type="candidate_early_risk_unlocked",
                campaign_id=self._campaign_id_for_timing,
                details={
                    "gross_return": str(gross_return),
                    "threshold": str(self.early_profit_unlock_ratio),
                },
            )
        risk_elapsed_ms = (
            max(elapsed_ms, self._candidate_exit_config.risk_start_ms)
            if self._early_profit_risk_unlocked
            else elapsed_ms
        )
        time_risk, momentum_risk = candidate_v1_risks(
            elapsed_ms=risk_elapsed_ms,
            decay_agreement=features.decay_agreement,
            net_pnl=net_pnl,
            down_channel_5m=features.down_channel_5m,
            down_channel_15m=features.down_channel_15m,
            config=self._candidate_exit_config,
            notional=self.total_notional,
            profit_unlocked=self._candidate_profit_unlocked,
            entry_bucket=self._candidate_entry_bucket,
        )
        observation = ExitObservation(
            event_time=event_time,
            first_fill_time=self.first_fill_time,
            price=mark_price,
            origin_price=self._campaign_origin_price,
            decay_agreement=features.decay_agreement,
            time_risk=time_risk,
            momentum_risk=momentum_risk,
            profit_drawdown=profit_drawdown,
            stable_breakout_5m=features.stable_breakout_5m,
            stable_breakout_15m=features.stable_breakout_15m,
        )
        preview = replace(self._candidate_exit_state).evaluate(observation)
        self._candidate_exit_waiting = preview.action != ExitAction.HOLD
        if preview.action != ExitAction.HOLD:
            cancelled = sum(
                self._cancel_signal_orders(signal)
                for signal in self.active_signals
            )
            blocked_by_recovered_orders = self._block_exit_for_campaign_entries(
                event_time
            )
            pending_cancellations = bool(
                getattr(self._account, "has_pending_cancellations", False)
            )
            pending_position_update = getattr(
                self._account, "has_pending_position_update", None
            )
            if cancelled or blocked_by_recovered_orders or pending_cancellations or (
                callable(pending_position_update)
                and pending_position_update(self.symbol)
            ):
                self._record_audit(
                    event_time=event_time,
                    event_type="candidate_exit_waiting_entry_cancel",
                    campaign_id=self._campaign_id_for_timing,
                    details={"cancelled_orders": cancelled},
                )
                return []

        decision = self._candidate_exit_state.evaluate(observation)
        if decision.reason in {
            "origin_momentum_continues",
            "origin_momentum_decay",
        }:
            self._record_audit(
                event_time=event_time,
                event_type="candidate_origin_check",
                campaign_id=self._campaign_id_for_timing,
                details={
                    "decision": decision.action.value,
                    "decay_agreement": features.decay_agreement,
                    "mark_price": str(mark_price),
                },
            )
        if decision.action == ExitAction.HOLD:
            return []

        self._candidate_exit_waiting = False

        reduce_half = decision.action == ExitAction.REDUCE_HALF
        quantity = position.quantity / 2 if reduce_half else position.quantity
        reason = (
            "candidate_origin_reduce"
            if reduce_half
            else {
                "time_risk": "candidate_time_risk_exit",
                "momentum_risk": "candidate_momentum_exit",
                "profit_drawdown": "candidate_profit_drawdown_exit",
            }.get(decision.reason, "candidate_trend_exit")
        )
        if not reduce_half:
            self._exit_requested = True
        self._record_audit(
            event_time=event_time,
            event_type="candidate_exit_requested",
            campaign_id=self._campaign_id_for_timing,
            details={
                "action": decision.action.value,
                "reason": decision.reason,
                "quantity": str(quantity),
                "mark_price": str(mark_price),
                "net_pnl": str(net_pnl),
            },
        )
        return [
            OrderIntent(
                symbol=self.symbol,
                side="BUY",
                price=mark_price,
                quantity=quantity,
                client_order_id=build_exit_client_order_id(
                    self.symbol,
                    event_time,
                    "h" if reduce_half else "c",
                ),
                order_type="MARKET",
                reduce_only=True,
                strategy_id="spike_short",
                trigger_reason=reason,
            )
        ]

    def _block_exit_for_campaign_entries(self, event_time: int) -> bool:
        """重启后也必须先处理本 Campaign 的全部非 reduce-only 入场单。"""
        if self._account is None or self._campaign_id_for_timing is None:
            return False
        blocked = False
        for order in self._account.iter_orders():
            if getattr(order, "reduce_only", False):
                continue
            parsed = parse_entry_client_order_id(
                order.client_order_id, expected_symbol=self.symbol
            )
            if parsed is None:
                continue
            symbol, signal_time = parsed
            if f"spike_short:{symbol}:{signal_time}" != self._campaign_id_for_timing:
                continue
            if order.status == "SUBMIT_UNKNOWN":
                blocked = True
            elif order.status in {"NEW", "PARTIALLY_FILLED"}:
                self._account.cancel_order(order.order_id)
                blocked = True
        if blocked:
            self._record_audit(
                event_time=event_time,
                event_type="candidate_exit_waiting_campaign_entries",
                campaign_id=self._campaign_id_for_timing,
                details={},
            )
        return blocked

    def _append_kline_and_evict_expired(
        self, interval: str, cache: deque[Kline], kline: Kline, cutoff: int
    ) -> None:
        self._kline_cache_time_ordered[interval] = append_kline_and_evict_expired(
            cache,
            kline,
            cutoff,
            is_time_ordered=self._kline_cache_time_ordered[interval],
        )

    def on_kline(self, kline: Kline) -> List[OrderIntent]:
        """处理已完成 K 线事件"""
        if self._shared_feature_provider is None:
            if kline.interval == "1m":
                retained_minutes = max(30 * 60, self.rise_low_lookback_minutes)
                cutoff = kline.close_time - retained_minutes * MS_PER_MINUTE
                self._append_kline_and_evict_expired(
                    "1m", self.klines_1m, kline, cutoff
                )

            elif kline.interval == "5m":
                cutoff = kline.close_time - 40 * 3600 * MS_PER_SECOND
                self._append_kline_and_evict_expired(
                    "5m", self.klines_5m, kline, cutoff
                )

            elif kline.interval == "15m":
                cutoff = kline.close_time - 40 * 3600 * MS_PER_SECOND
                self._append_kline_and_evict_expired(
                    "15m", self.klines_15m, kline, cutoff
                )

        if (
            self.exit_policy == "candidate-v1"
            and self.first_fill_time is not None
            and self._trading_enabled
        ):
            self.refresh_candidate_features()

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

            # 记录首笔卖出前是否出现了足够深的回调，供交易归因使用。
            pullback_threshold = sig.pullback_threshold
            if pullback_threshold is None:
                pullback_threshold = sig.spike_high - sig.atr * self.RETEST_ATR
                sig.pullback_threshold = pullback_threshold
            if sig.pullback_time is None and bar.low <= pullback_threshold:
                sig.pullback_time = bar.timestamp
                sig.pullback_low = bar.low
            elif sig.pullback_time is not None and (
                sig.pullback_low is None or bar.low < sig.pullback_low
            ):
                sig.pullback_low = bar.low

            # 4. 按模式挂单（client_order_id 保持兼容旧 WAL）。
            if self.entry_tier_mode == "single-entry":
                entry_levels = (
                    (3, self._single_entry_price(sig), Decimal("1"), "spike_entry"),
                )
            else:
                entry_levels = (
                    (tier_idx, tier_price, tier_weight, f"spike_tier{tier_idx}")
                    for tier_idx, (tier_price, tier_weight) in enumerate(
                        zip(sig.tier_prices, sig.tier_weights), start=1
                    )
                )

            for tier_idx, tier_price, tier_weight, trigger_reason in entry_levels:
                if tier_weight <= 0:
                    continue
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
                        reduce_only=False,
                        strategy_id="spike_short",
                        trigger_reason=trigger_reason,
                        campaign_id=self._campaign_id(sig),
                    )
                )
                sig.placed_client_order_ids.add(client_order_id)

        return intents

    def _client_order_id(self, sig: SpikeSignal, tier_idx: int) -> str:
        return build_entry_client_order_id(self.symbol, sig.signal_time, tier_idx)

    def _single_entry_price(self, sig: SpikeSignal) -> Decimal:
        return sig.spike_high - sig.atr * self.SINGLE_ENTRY_ATR

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

    def _campaign_id_at(self, signal_time: int) -> str:
        return f"spike_short:{self.symbol}:{signal_time}"

    def _campaign_id(self, sig: SpikeSignal) -> str:
        return self._campaign_id_at(sig.signal_time)

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

    def _signal_audit_details(
        self,
        *,
        trigger_price: Decimal,
        rise_5s: Decimal | None,
        rise_window_returns: dict[int, Decimal] | None,
        volume_5s: Decimal | None,
        median_volume_1s: Decimal | None,
        volume_multiple_5s: Decimal | None,
        low_12h: Decimal | None = None,
        rise_from_12h_low: Decimal | None = None,
        entry_context: EntryContextFeatures | None = None,
    ) -> dict:
        details = {
            "trigger_price": str(trigger_price),
            "rise_threshold_5s": str(self.rise_5s_threshold),
            "volume_threshold_5s": str(self.VOLUME_MULTIPLE_5S),
            "rise_5s": str(rise_5s) if rise_5s is not None else None,
            "rise_10s": (
                str(rise_window_returns[10])
                if rise_window_returns is not None and 10 in rise_window_returns
                else None
            ),
            "rise_15s": (
                str(rise_window_returns[15])
                if rise_window_returns is not None and 15 in rise_window_returns
                else None
            ),
            "rise_60s": (
                str(rise_window_returns[60])
                if rise_window_returns is not None and 60 in rise_window_returns
                else None
            ),
            "volume_5s": str(volume_5s) if volume_5s is not None else None,
            "median_volume_1s": (
                str(median_volume_1s) if median_volume_1s is not None else None
            ),
            "volume_multiple_5s": (
                str(volume_multiple_5s)
                if volume_multiple_5s is not None
                else None
            ),
            "low_12h": str(low_12h) if low_12h is not None else None,
            "rise_from_12h_low": (
                str(rise_from_12h_low)
                if rise_from_12h_low is not None
                else None
            ),
        }
        if entry_context is not None:
            details.update(entry_context.to_audit_details())
        return details

    def _record_cap_rejection(
        self,
        *,
        event_time: int,
        rejection_reasons: tuple[str, ...],
        trigger_price: Decimal,
        rise_5s: Decimal,
        rise_window_returns: dict[int, Decimal],
        volume_5s: Decimal,
        median_volume_1s: Decimal,
        volume_multiple_5s: Decimal,
        low_12h: Decimal,
        rise_from_12h_low: Decimal,
        entry_context: EntryContextFeatures | None = None,
    ) -> None:
        previous = self._last_cap_rejection_audit
        if (
            previous is not None
            and previous[1] == rejection_reasons
            and event_time - previous[0] < self.SIGNAL_COOLDOWN * MS_PER_SECOND
        ):
            return
        self._last_cap_rejection_audit = (event_time, rejection_reasons)
        details = self._signal_audit_details(
            trigger_price=trigger_price,
            rise_5s=rise_5s,
            rise_window_returns=rise_window_returns,
            volume_5s=volume_5s,
            median_volume_1s=median_volume_1s,
            volume_multiple_5s=volume_multiple_5s,
            low_12h=low_12h,
            rise_from_12h_low=rise_from_12h_low,
            entry_context=entry_context,
        )
        details.update({
            "rejection_stage": "post_base_entry_filters",
            "rejection_reasons": list(rejection_reasons),
            "max_rise_5s": (
                str(self.max_rise_window)
                if self.max_rise_window_seconds == 5
                else None
            ),
            "max_rise_window_seconds": self.max_rise_window_seconds,
            "max_rise_window": str(self.max_rise_window),
            "max_volume_multiple_5s": str(self.max_volume_multiple_5s),
        })
        self._record_audit(
            event_time=event_time,
            event_type="signal_rejected",
            campaign_id=self._campaign_id_at(event_time),
            details=details,
        )

    # ------------------------------------------------------------------
    # 缓存与信号检测
    # ------------------------------------------------------------------

    def _update_cache(self, bar: Bar1s) -> None:
        if self._shared_feature_provider is not None:
            return
        self.bars_1s.append(bar)
        if len(self.bars_1s) > self.BAR_BUFFER:
            # 原地删除过期前缀，避免每秒重新分配并复制整个窗口。
            del self.bars_1s[:-self.BAR_BUFFER]

    def _rise_window_returns(
        self, bars: list[Bar1s], current: Bar1s
    ) -> dict[int, Decimal]:
        """返回可复测的短时涨幅；窗口均只读取当前及此前的 1s Bar。"""
        return {
            seconds: current.close / bars[-(seconds + 1)].close - Decimal("1")
            for seconds in (5, 10, 15, 60)
        }

    def _accel_exempt(self, bars: list[Bar1s], rise_5s: Decimal) -> bool:
        """加速豁免：5s 涨幅不足但呈分钟级持续加速（蠕升）时放行。

        条件：
        - rise_5s >= accel_rise_5s_min；
        - 前 accel_prev_minutes 分钟已完成 1m K 线涨幅均值 >=
          accel_prev_avg_min，且 > accel_prev_ratio × 再前
          accel_prev2_minutes 分钟涨幅均值（分钟级加速放大）；
        - 当前分钟实时涨幅（本分钟首秒 close 至触发 Bar）>=
          accel_ratio × 前窗口涨幅均值。

        秒级加速无需豁免：1s 持续放量上涨必然同时满足 5s 涨幅阈值，
        豁免只针对分钟级蠕升（每 5s 涨幅不足但分钟涨幅持续放大）。
        """
        if self.accel_rise_5s_min <= 0:
            return False
        if rise_5s < self.accel_rise_5s_min:
            return False
        current = bars[-1]
        minute_start = current.timestamp - (current.timestamp % MS_PER_MINUTE)
        minute_open = None
        for bar in reversed(bars):
            if bar.timestamp < minute_start:
                minute_open = bar.close
                break
        if minute_open is None or minute_open <= 0:
            return False
        intrabar_rise = current.close / minute_open - Decimal("1")
        if intrabar_rise < self.accel_rise_5s_min:
            return False

        prev_total = self.accel_prev_minutes + self.accel_prev2_minutes
        prev_minutes = self._completed_1m_window(minute_start, prev_total + 1)
        if len(prev_minutes) < prev_total + 1:
            return False
        rises = []
        prev_close = prev_minutes[-(prev_total + 1)].close
        for kline in prev_minutes[-(prev_total + 1):]:
            if prev_close > 0:
                rises.append(kline.close / prev_close - Decimal("1"))
            prev_close = kline.close
        if len(rises) != prev_total:
            return False
        earlier_rises = rises[: self.accel_prev2_minutes]
        recent_rises = rises[self.accel_prev2_minutes:]
        recent_avg = sum(recent_rises, Decimal("0")) / Decimal(len(recent_rises))
        if recent_avg < self.accel_prev_avg_min:
            return False
        if self.accel_prev2_minutes > 0 and recent_avg <= self.accel_prev_ratio * (
            sum(earlier_rises, Decimal("0")) / Decimal(len(earlier_rises))
        ):
            return False
        return intrabar_rise >= self.accel_ratio * recent_avg

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
        shared_bar_features = (
            self._shared_feature_provider.bar_features(bar)
            if self._shared_feature_provider is not None
            else None
        )
        if self._shared_feature_provider is not None:
            if shared_bar_features is None or not shared_bar_features.continuous:
                return None
        else:
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

        # 3. 原始 5 秒涨幅触发保持不变；额外窗口只用于顶部急涨上限过滤。
        rise_5s = (
            shared_bar_features.rise_5s
            if shared_bar_features is not None
            else current.close / bar_5s_ago.close - Decimal("1")
        )
        if rise_5s < self.rise_5s_threshold:
            if not self._accel_exempt(bars, rise_5s):
                return None
        rise_window_returns = self._rise_window_returns(bars, current)
        # 使用与基础触发一致的 5s 精确值，避免共享与非共享路径的数值语义漂移。
        rise_window_returns[5] = rise_5s
        rise_cap_window = rise_window_returns[self.max_rise_window_seconds]

        # 4. 成交量倍数：sum(volume[i-4..i]) / (median(volume[i-60..i-1]) × 5)
        if self._shared_feature_provider is not None:
            shared_bar_features = self._shared_feature_provider.volume_features(bar)
            if shared_bar_features is None:
                return None
            volume_5s = shared_bar_features.volume_5s
            median_volume = shared_bar_features.median_volume_1s
            if volume_5s is None or median_volume is None:
                return None
        else:
            volume_5s = sum((b.volume for b in bars[-5:]), Decimal("0"))
            baseline_volumes = sorted(b.volume for b in bars[-61:-1])
            median_volume = baseline_volumes[30]
        if median_volume <= 0:
            return None
        volume_multiple_5s = volume_5s / (median_volume * Decimal("5"))
        if volume_multiple_5s < self.VOLUME_MULTIPLE_5S:
            return None

        # 5. 12 小时低点涨幅
        minute_start = current.timestamp - (current.timestamp % MS_PER_MINUTE)
        low_12h = self._min_low_1m(minute_start, self.LOW_12H_MINUTES)
        if low_12h is None or low_12h <= 0:
            return None
        if current.close / low_12h - Decimal("1") < self.RISE_FROM_12H_LOW:
            return None
        rise_from_12h_low = current.close / low_12h - Decimal("1")

        rise_low = None
        rise_low_time = None
        rise_low_age_minutes = None
        if self.rise_low_lookback_minutes:
            rise_low_point = self._min_low_point_1m(
                minute_start, self.rise_low_lookback_minutes
            )
            if rise_low_point is None:
                return None
            rise_low, rise_low_time = rise_low_point
            rise_low_age_minutes = (minute_start - rise_low_time) // MS_PER_MINUTE
            if rise_low_age_minutes < self.min_rise_duration_minutes:
                return None

        # 5b. 箱体/通道突破时长过滤：现价须已站上突破线（3d/7d 上沿均值）
        #     超过 box_duration_min_minutes 才允许入场（0=关闭）。
        box_break = None
        if self.box_duration_min_minutes > 0:
            box_break = self._box_breakthrough(minute_start, current.close)
            if box_break is None or box_break["box_break_minutes"] < self.box_duration_min_minutes:
                return None

        # 5c. 过早触发过滤：信号触发价相对前 30m 均价偏离度与 60m 极差
        #     同时超阈值时判定为在脉冲顶部触发，做空接飞刀风险高（0=关闭）。
        premature = None
        if self.spike_avg_deviation_max_pct > 0 and self.spike_range_max_pct > 0:
            premature = self._premature_spike_filter(minute_start, current.close)
            if premature is not None and premature["rejected"]:
                return None

        # 5d. VWAP 偏离过滤：触发价相对前 100m 聚合 20 根 5m VWAP 偏离超阈值
        #     判定为追顶触发，15m 内继续冲高概率高（0=关闭）。
        vwap_dev = None
        if self.spike_vwap_deviation_max_pct > 0:
            vwap_dev = self._vwap_deviation_filter(minute_start, current.close)
            if vwap_dev is not None and vwap_dev["rejected"]:
                return None

        # 6. 起涨点（16 小时最低价）
        origin_price = self._min_low_1m(minute_start, self.ORIGIN_MINUTES)
        if origin_price is None or origin_price <= 0:
            return None

        # 7. spike_high（已完成 1m K 线 + 已缓存 1s Bar，避免未完成 K 线泄漏）
        spike_high_point = self._spike_high_point(minute_start)
        if spike_high_point is None:
            return None
        spike_high, spike_high_time = spike_high_point

        # 4 小时前高过滤：三档入场价必须全部高于回调前的前高，
        # 避免价格在前高附近或下方重新挂出空单。
        prior_high_point = self._prior_high_point(minute_start)
        if prior_high_point is None:
            return None
        prior_high, prior_high_time = prior_high_point

        # 8. ATR（已完成 5m K 线，14 周期）
        atr = self._atr_5m()
        if atr is None or atr <= 0:
            return None

        # 9. 三档价格：spike_high - ATR × (1.15, 0.75, 0.35)
        #    tier1（最低档）用于保护线检查，不随 tier_atr_shift 上移，避免放宽入场门槛
        tier_atr_shift = self._entry_tier_atr_shift(rise_from_12h_low)
        tier_prices = [
            spike_high
            - atr
            * (
                self.RETEST_ATR
                - Decimal(n - 1) * self.SPREAD_ATR
                - (tier_atr_shift if n > 0 else Decimal("0"))
            )
            for n in range(3)
        ]

        # 10. 价格合理性：最低档不得低于 origin_floor。
        #     reject_below_current=False（默认）时不再拦截"现价已高于
        #     挂单档位"的信号：做空时现价高于挂单价是更优卖出价，
        #     交给成交价模型（SELL 按触发 bar 的 1s 开盘价成交）处理。
        #     仅当 reject_below_current=True 时恢复原拦截（低卖防护）。
        origin_floor = origin_price * (Decimal("1") + self.ORIGIN_MIN_RISE)
        lowest_tier = min(tier_prices)
        if lowest_tier < origin_floor:
            return None
        if self.entry_tier_mode == "single-entry":
            entry_tier = spike_high - atr * self.SINGLE_ENTRY_ATR
        elif self.entry_tier_mode == "tier3-only":
            entry_tier = tier_prices[-1]
        else:
            entry_tier = lowest_tier
        if self.reject_below_current and entry_tier <= current.close:
            return None
        allowed_prior_high = prior_high * (
            Decimal("1") - self.prior_high_tolerance_percent / Decimal("100")
        )
        if (
            lowest_tier < allowed_prior_high
            or self.prior_high_tolerance_percent == 0
            and lowest_tier == allowed_prior_high
        ):
            return None

        prior_high_4h = (
            prior_high
            if self.prior_high_lookback_minutes == self.PRIOR_HIGH_LOOKBACK_MINUTES
            else None
        )
        prior_high_4h_time = (
            prior_high_time
            if prior_high_4h is not None
            else None
        )

        # 11. 失效价
        invalid_price = max(
            spike_high + atr * self.INVALID_SPIKE_ATR,
            tier_prices[1] + atr * self.INVALID_PRIMARY_ATR,
        )
        entry_context = entry_context_features(self.klines_5m, self.klines_15m)

        # 仅当核心入场条件已完整满足时，才把版本指标过滤记为可复测信号。
        # 这不会改变过滤判定，只避免把后续本会失败的半成品候选写入报告。
        entry_filters_pass, rejection_details = self._entry_filter_decision(
            current.timestamp,
            rise_from_12h_low=rise_from_12h_low,
        )
        if not entry_filters_pass:
            if rejection_details is not None:
                rejection_details = {
                    **self._signal_audit_details(
                        trigger_price=current.close,
                        rise_5s=rise_5s,
                        rise_window_returns=rise_window_returns,
                        volume_5s=volume_5s,
                        median_volume_1s=median_volume,
                        volume_multiple_5s=volume_multiple_5s,
                        low_12h=low_12h,
                        rise_from_12h_low=rise_from_12h_low,
                        entry_context=entry_context,
                    ),
                    **rejection_details,
                }
                self._record_entry_filter_rejection(
                    event_time=current.timestamp,
                    details=rejection_details,
                )
            return None

        rejection_reasons = tuple(
            reason
            for reason, rejected in (
                (
                    (
                        "max_rise_5s"
                        if self.max_rise_window_seconds == 5
                        else "max_rise_window"
                    ),
                    self.max_rise_window is not None
                    and rise_cap_window > self.max_rise_window,
                ),
                (
                    "max_volume_multiple_5s",
                    self.max_volume_multiple_5s is not None
                    and volume_multiple_5s > self.max_volume_multiple_5s,
                ),
            )
            if rejected
        )
        if rejection_reasons:
            self._record_cap_rejection(
                event_time=current.timestamp,
                rejection_reasons=rejection_reasons,
                trigger_price=current.close,
                rise_5s=rise_5s,
                rise_window_returns=rise_window_returns,
                volume_5s=volume_5s,
                median_volume_1s=median_volume,
                volume_multiple_5s=volume_multiple_5s,
                low_12h=low_12h,
                rise_from_12h_low=rise_from_12h_low,
                entry_context=entry_context,
            )
            return None

        active_time = current.timestamp + MS_PER_SECOND
        return SpikeSignal(
            signal_time=current.timestamp,
            trigger_price=current.close,
            spike_high=spike_high,
            origin_price=origin_price,
            atr=atr,
            tier_prices=tier_prices,
            tier_weights=(
                [Decimal("0"), Decimal("0"), Decimal("1")]
                if self.entry_tier_mode in {"tier3-only", "single-entry"}
                else list(self.TIER_WEIGHTS)
            ),
            invalid_price=invalid_price,
            active_time=active_time,
            expire_time=active_time + self.ORDER_TTL * MS_PER_SECOND,
            spike_high_time=spike_high_time,
            rise_5s=rise_5s,
            rise_window_returns=rise_window_returns,
            volume_5s=volume_5s,
            median_volume_1s=median_volume,
            volume_multiple_5s=volume_multiple_5s,
            low_12h=low_12h,
            rise_from_12h_low=current.close / low_12h - Decimal("1"),
            origin_floor=origin_floor,
            pullback_threshold=spike_high - atr * self.RETEST_ATR,
            prior_high=prior_high,
            prior_high_time=prior_high_time,
            prior_high_4h=prior_high_4h,
            prior_high_4h_time=prior_high_4h_time,
            rise_low=rise_low,
            rise_low_time=rise_low_time,
            rise_low_age_minutes=rise_low_age_minutes,
            entry_context=entry_context,
            box_upper_3d=box_break.get("box_upper_3d") if box_break else None,
            box_upper_7d=box_break.get("box_upper_7d") if box_break else None,
            box_lower_3d=box_break.get("box_lower_3d") if box_break else None,
            box_lower_7d=box_break.get("box_lower_7d") if box_break else None,
            box_breakthrough=box_break.get("box_breakthrough") if box_break else None,
            box_break_lower=box_break.get("box_break_lower") if box_break else None,
            box_break_first_time=box_break.get("box_break_first_time") if box_break else None,
            box_break_minutes=box_break.get("box_break_minutes") if box_break else None,
            box_break_hours=box_break.get("box_break_hours") if box_break else None,
            spike_avg_deviation_pct=(
                premature.get("spike_avg_deviation_pct") if premature else None
            ),
            spike_range_pct=(
                premature.get("spike_range_pct") if premature else None
            ),
            spike_vwap_deviation_pct=(
                vwap_dev.get("spike_vwap_deviation_pct") if vwap_dev else None
            ),
        )

    # ------------------------------------------------------------------
    # 指标计算
    # ------------------------------------------------------------------

    def _entry_filters_pass(self, event_ms: int) -> bool:
        """版本策略可覆盖的入场过滤扩展点。"""
        return True

    def _entry_tier_atr_shift(
        self, rise_from_12h_low: Decimal | None
    ) -> Decimal:
        """按动能分组调整三档挂单价（ATR 系数偏移，正数上移挂单价）。

        基类默认不调整；版本策略可按分组覆盖，供强势桶提高做空挂单价。
        """
        return Decimal("0")

    def _entry_bucket(self, rise_from_12h_low: Decimal | None) -> str | None:
        """按入场信号快照确定强弱桶（"strong"/"weak"），持仓期不变。

        基类默认不分组；版本策略可按 rise_from_12h_low 覆盖。
        """
        return None

    def _entry_filter_decision(
        self,
        event_ms: int,
        rise_from_12h_low: Decimal | None = None,
    ) -> tuple[bool, dict[str, object] | None]:
        """返回入场过滤结果及可写入审计的拒绝详情。

        ``rise_from_12h_low`` 为信号时刻从 12h 低点的涨幅，由版本策略用于
        按动能分组的两套准入标准；基础版本可忽略该参数。
        """
        return self._entry_filters_pass(event_ms), None

    def _record_entry_filter_rejection(
        self, *, event_time: int, details: dict[str, object]
    ) -> None:
        rejection_reasons = tuple(
            str(reason) for reason in details.get("rejection_reasons", ())
        )
        previous = self._last_entry_filter_rejection_audit
        if (
            previous is not None
            and previous[1] == rejection_reasons
            and event_time - previous[0] < self.SIGNAL_COOLDOWN * MS_PER_SECOND
        ):
            return
        self._last_entry_filter_rejection_audit = (event_time, rejection_reasons)
        self._record_audit(
            event_time=event_time,
            event_type="signal_rejected",
            campaign_id=self._campaign_id_at(event_time),
            details=details,
        )

    def _min_low_1m(self, minute_start: int, minutes: int) -> Optional[Decimal]:
        """已完成 1m K 线在 [minute_start - minutes, minute_start) 内的最低价"""
        point = self._min_low_point_1m(minute_start, minutes)
        return point[0] if point is not None else None

    def _min_low_point_1m(
        self, minute_start: int, minutes: int
    ) -> tuple[Decimal, int] | None:
        """返回窗口最低点；相同低价取最近一次，避免低估上涨持续时间。"""
        window = self._completed_1m_window(minute_start, minutes)
        if not window:
            return None
        kline = min(window, key=lambda item: (item.low, -item.open_time))
        return kline.low, kline.open_time

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

    def _prior_high_point(
        self, minute_start: int
    ) -> tuple[Decimal, int] | None:
        completed = self._completed_1m_window(
            minute_start, self.prior_high_lookback_minutes
        )
        if not completed:
            return None
        return max(
            ((k.high, k.open_time) for k in completed),
            key=lambda point: (point[0], point[1]),
        )

    def _box_breakthrough(self, minute_start: int, current_close: Decimal) -> dict | None:
        """
        计算箱体/通道突破信息，用于突破时长入场过滤。

        对 3d/7d 两个窗口分别：
        - 用完整 1m K 聚合成 1h（high=max, low=min, close=末根），并排除信号
          所在的当前 1h bar（避免同一小时内已发生的 spike 污染箱体/突破判定）
        - log(high) 线性回归得到斜率；|slope|<3bps/bar 视为横盘，
          上沿=Winsorized5% 箱体上沿、下沿=Winsorized5% 箱体下沿；
          否则按上升/下降通道用回归线 ±1.5σ（上轨/下轨）
        - 突破线 = 两窗口上沿的均值
        - 突破时长 = 最近一次 1h 收盘跌破突破线后，重新站上至今的连续时长
          （从最后一个 close<突破线的 bar 收盘时刻起算，即突破须仍在持续）

        返回 dict 或 None（数据不足时返回 None）。
        """
        hour_ms = MS_PER_MINUTE * 60
        current_hour_start = minute_start - (minute_start % hour_ms)
        windows = []
        for days in (3, 7):
            window = self._completed_1m_window(
                minute_start, days * 24 * 60
            )
            if not window:
                return None
            # 聚合为 1h，排除当前小时（信号所在 bar，可能已被 spike 污染）
            by_hour: dict[int, list] = {}
            for k in window:
                if k.open_time >= current_hour_start:
                    continue
                hour = k.open_time - (k.open_time % hour_ms)
                by_hour.setdefault(hour, []).append(k)
            hours = sorted(by_hour)
            highs = [max(k.high for k in by_hour[h]) for h in hours]
            lows = [min(k.low for k in by_hour[h]) for h in hours]
            if len(highs) < 40:
                return None
            windows.append((hours, highs, lows, by_hour))

        # 分别计算两个窗口的上下沿
        uppers = []
        lowers = []
        for hours, highs, lows, by_hour in windows:
            band = self._box_band(highs, lows)
            if band is None:
                return None
            uppers.append(band[0])
            lowers.append(band[1])
        breakthrough = sum(uppers) / Decimal(len(uppers))
        box_lower = sum(lowers) / Decimal(len(lowers))

        # 突破时长：从最后一个 1h 收盘 < 突破线的 bar 收盘时刻起算。
        # 若窗口内从未跌破（收盘恒在突破线上方），从窗口起点起算。
        last_break_hour = None
        for hours, highs, lows, by_hour in windows:
            for h in hours:
                bar_close = by_hour[h][-1].close
                if bar_close < breakthrough:
                    if last_break_hour is None or h > last_break_hour:
                        last_break_hour = h
        if last_break_hour is None:
            # 全程站上：以两窗口中最晚的起点起算
            last_break_hour = min(hours[0] for hours, _, _, _ in windows)
        break_start_time = last_break_hour + hour_ms
        duration_minutes = (minute_start - break_start_time) // MS_PER_MINUTE
        if duration_minutes < 0:
            duration_minutes = 0
        return {
            "box_upper_3d": uppers[0],
            "box_upper_7d": uppers[1],
            "box_lower_3d": lowers[0],
            "box_lower_7d": lowers[1],
            "box_breakthrough": breakthrough,
            "box_break_lower": box_lower,
            "box_break_first_time": break_start_time,
            "box_break_minutes": duration_minutes,
            "box_break_hours": duration_minutes / 60,
        }

    def _premature_spike_filter(
        self, minute_start: int, current_close: Decimal
    ) -> dict | None:
        """过早触发过滤：信号触发价相对前 30m 均价偏离度与 60m 价格极差。

        偏离度 = current_close / 前 30m 1m close 均值 - 1（百分比）。
        极差 = (max high - min low) / min low × 100（前 60m 已完成 1m K）。
        两者都超过阈值时判定为"价格在瞬间脉冲顶部触发"，做空接飞刀风险高，拒绝。

        返回 dict（含审计指标与判定）或 None（数据不足时，保守不拦截）。
        """
        window_30 = self._completed_1m_window(minute_start, 30)
        window_60 = self._completed_1m_window(minute_start, 60)
        if not window_30 or not window_60:
            return None
        avg_30 = sum(k.close for k in window_30) / Decimal(len(window_30))
        if avg_30 <= 0:
            return None
        deviation_pct = float((current_close / avg_30 - Decimal("1")) * 100)
        min_low = min(k.low for k in window_60)
        max_high = max(k.high for k in window_60)
        if min_low <= 0:
            return None
        range_pct = float((max_high / min_low - Decimal("1")) * 100)
        rejected = (
            deviation_pct >= self.spike_avg_deviation_max_pct
            and range_pct >= self.spike_range_max_pct
        )
        return {
            "spike_avg_deviation_pct": deviation_pct,
            "spike_range_pct": range_pct,
            "rejected": rejected,
        }

    def _vwap_deviation_filter(
        self, minute_start: int, current_close: Decimal
    ) -> dict | None:
        """VWAP 偏离过滤：信号触发价相对前 100m 聚合的 20 根 5m VWAP 偏离。

        研究（spike_signal_shortterm_metrics.csv）显示 vwap_dev_5m 是最强
        单指标入场过滤：>9% 的信号 15m 内追顶风险高（止损率 47% vs 20%），
        9 笔大亏单（<-50U）vwap_dev 全部 >9。偏离越大 = 开仓点位越晚。

        返回 dict（含审计指标与判定）或 None（数据不足时，保守不拦截）。
        """
        window = self._completed_1m_window(minute_start, 100)
        if not window or len(window) < 100:
            return None
        by_5m: dict[int, list[Kline]] = {}
        for k in window:
            bucket = k.open_time - (k.open_time % (5 * MS_PER_MINUTE))
            by_5m.setdefault(bucket, []).append(k)
        o5 = [g[0].open for g in sorted(by_5m.values(), key=lambda g: g[0].open_time)]
        h5 = [max(g, key=lambda k: k.high).high for g in sorted(by_5m.values(), key=lambda g: g[0].open_time)]
        l5 = [min(g, key=lambda k: k.low).low for g in sorted(by_5m.values(), key=lambda g: g[0].open_time)]
        c5 = [g[-1].close for g in sorted(by_5m.values(), key=lambda g: g[0].open_time)]
        v5 = [sum(k.volume for k in g) for g in sorted(by_5m.values(), key=lambda g: g[0].open_time)]
        if len(c5) < 20:
            return None
        typical = [
            (o + h + l + c) / Decimal(4)
            for o, h, l, c in zip(o5[-20:], h5[-20:], l5[-20:], c5[-20:])
        ]
        vol = [Decimal(v) for v in v5[-20:]]
        denom = sum(vol)
        if denom <= 0:
            return None
        vwap = sum(t * vol[i] for i, t in enumerate(typical)) / denom
        deviation_pct = float((current_close / vwap - Decimal("1")) * 100)
        rejected = deviation_pct >= self.spike_vwap_deviation_max_pct
        return {
            "spike_vwap_deviation_pct": deviation_pct,
            "rejected": rejected,
        }

    def _box_band(self, highs: list, lows: list) -> tuple[Decimal, Decimal] | None:
        """单窗口上下沿：|slope|<3bps/bar 视为横盘（Winsorized5% 上下沿），
        否则按方向用回归线 ±1.5σ（上轨/下轨，对最新 1h 位置）。"""
        import math as _math
        n = len(highs)
        if n < 10:
            return None
        log_h = [_math.log(float(h)) for h in highs]
        x_vals = list(range(n))
        x_mean = sum(x_vals) / n
        y_mean = sum(log_h) / n
        s_xy = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x_vals, log_h))
        s_xx = sum((xi - x_mean) ** 2 for xi in x_vals)
        if s_xx == 0:
            return None
        slope = s_xy / s_xx
        intercept = y_mean - slope * x_mean
        resid = [yi - (intercept + slope * xi) for xi, yi in zip(x_vals, log_h)]
        sigma = _math.sqrt(sum(r * r for r in resid) / n)
        slope_bps = slope * 10000
        if abs(slope_bps) < 3:
            # 横盘：Winsorized 5% 上下沿
            return (
                Decimal(str(self._winsorized_value(highs, "upper"))),
                Decimal(str(self._winsorized_value(lows, "lower"))),
            )
        # 通道：回归线 ± 1.5σ 上下轨（对最新 1h 位置）
        latest = intercept + slope * (n - 1)
        return (
            Decimal(str(_math.exp(latest + 1.5 * sigma))),
            Decimal(str(_math.exp(latest - 1.5 * sigma))),
        )

    @staticmethod
    def _winsorized_value(values: list, side: str) -> float:
        """Winsorized 5% 值：side='upper' 用两端 5% 缩尾后均值；
        side='lower' 同理。缩尾即把极端值替换为 5% 边界值。"""
        import math as _math
        vals = sorted(float(v) for v in values)
        n = len(vals)
        k = max(1, int(_math.floor(n * 0.05)))
        if n <= 2 * k:
            return float(sum(vals) / n)
        low_clip = vals[k]
        high_clip = vals[-k - 1]
        clipped = [low_clip] * k + vals[k:-k] + [high_clip] * k
        return sum(clipped) / len(clipped)

    def _spike_high(self, minute_start: int) -> Optional[Decimal]:
        point = self._spike_high_point(minute_start)
        return point[0] if point is not None else None

    def _spike_high_point(
        self, minute_start: int
    ) -> tuple[Decimal, int] | None:
        """
        Return ``(spike_high, timestamp)`` using only information available at
        the current 1s bar.

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
        points = [(k.high, k.open_time) for k in completed]
        points.extend(
            (b.high, b.timestamp)
            for b in self.bars_1s
            if b.timestamp >= minute_start
        )
        return max(points, key=lambda point: (point[0], point[1])) if points else None

    def _entry_pattern_details(
        self, campaign_id: str | None, fill_time: int
    ) -> dict:
        signal = next(
            (
                signal
                for signal in self.active_signals
                if self._campaign_id(signal) == campaign_id
            ),
            None,
        )
        if signal is None:
            return {"entry_pattern": "unknown", "pullback_before_fill": False}
        pullback_before_fill = (
            signal.pullback_time is not None
            and signal.pullback_time < fill_time
        )
        return {
            "entry_pattern": (
                "short_term_high_pullback_rebreak"
                if pullback_before_fill
                else "direct_entry_without_pullback"
            ),
            "pullback_before_fill": pullback_before_fill,
            "pullback_time": signal.pullback_time,
            "pullback_low": (
                str(signal.pullback_low)
                if signal.pullback_low is not None
                else None
            ),
            "pullback_threshold": (
                str(signal.pullback_threshold)
                if signal.pullback_threshold is not None
                else None
            ),
        }

    def _atr_5m(self) -> Optional[Decimal]:
        """已完成 5m K 线的 14 周期 ATR"""
        if len(self.klines_5m) < self.ATR_PERIOD + 1:
            return None

        atr_klines = list(
            islice(
                self.klines_5m,
                len(self.klines_5m) - (self.ATR_PERIOD + 1),
                None,
            )
        )
        if any(
            current.open_time - previous.open_time != 5 * MS_PER_MINUTE
            for previous, current in zip(atr_klines, atr_klines[1:])
        ):
            return None

        true_ranges = []
        for i in range(1, self.ATR_PERIOD + 1):
            k = atr_klines[-i]
            k_prev = atr_klines[-i - 1]
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
        exit_policy: Literal["execution-test-d007", "candidate-v1"] = "execution-test-d007",
        prior_high_lookback_minutes: int | None = None,
        entry_tier_mode: EntryTierMode = "three-tier",
        rise_low_lookback_minutes: int = 0,
        min_rise_duration_minutes: int = 0,
        early_profit_unlock_ratio: Decimal | None = None,
        strategy_class: type[DynamicSpikeShortStrategy] = DynamicSpikeShortStrategy,
        strategy_parameters: dict[str, object] | None = None,
    ):
        self.strategies = {
            symbol: strategy_class(
                symbol,
                total_notional=total_notional,
                account=account,
                exit_policy=exit_policy,
                prior_high_lookback_minutes=prior_high_lookback_minutes,
                entry_tier_mode=entry_tier_mode,
                rise_low_lookback_minutes=rise_low_lookback_minutes,
                min_rise_duration_minutes=min_rise_duration_minutes,
                early_profit_unlock_ratio=early_profit_unlock_ratio,
                **(strategy_parameters or {}),
            )
            for symbol in symbols
        }
        self._account: Optional[StrategyAccount] = account
        self._entry_enabled = True
        self._blocked_entry_symbols: frozenset[str] = frozenset()
        self.active_symbol: Optional[str] = None

    def bind_account(self, account: StrategyAccount) -> None:
        self._account = account
        for strategy in self.strategies.values():
            strategy.bind_account(account)

    def bind_shared_feature_provider(
        self, provider: "SpikeSharedFeatureProvider"
    ) -> None:
        for strategy in self.strategies.values():
            strategy.bind_shared_feature_provider(provider)

    def set_trading_enabled(self, enabled: bool) -> None:
        for strategy in self.strategies.values():
            strategy.set_trading_enabled(enabled)

    def set_entry_enabled(self, enabled: bool) -> None:
        """统一控制多币种适配器的新入场准入；已有信号仍继续管理。"""
        self._entry_enabled = enabled
        for strategy in self.strategies.values():
            strategy.set_entry_enabled(enabled)

    def set_blocked_entry_symbols(self, symbols: Iterable[str]) -> None:
        self._blocked_entry_symbols = frozenset(
            symbol.strip().upper() for symbol in symbols if symbol.strip()
        )

    def is_symbol_entry_enabled(self, symbol: str) -> bool:
        return symbol.strip().upper() not in self._blocked_entry_symbols

    @property
    def blocked_entry_symbols(self) -> frozenset[str]:
        return self._blocked_entry_symbols

    def set_execution_enabled(self, enabled: bool) -> None:
        for strategy in self.strategies.values():
            strategy.set_execution_enabled(enabled)

    def refresh_candidate_features(self) -> None:
        for strategy in self.strategies.values():
            strategy.refresh_candidate_features()

    def on_bar1s(self, bar: Bar1s) -> List[OrderIntent]:
        strategy = self.strategies.get(bar.symbol)
        if strategy is None:
            return []

        strategy.set_entry_enabled(
            self._entry_enabled
            and self.is_symbol_entry_enabled(bar.symbol)
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
        self,
        symbol: str,
        campaign_id: str,
        first_fill_time: int,
        **exit_state,
    ) -> None:
        strategy = self.strategies.get(symbol)
        if strategy is None:
            raise ValueError(f"unknown Spike symbol: {symbol}")
        strategy.restore_campaign_timing(campaign_id, first_fill_time, **exit_state)
        self.active_symbol = symbol

    def restore_pending_campaign(
        self, symbol: str, campaign_id: str, *, origin_price: Decimal | None
    ) -> None:
        strategy = self.strategies.get(symbol)
        if strategy is None:
            raise ValueError(f"unknown Spike symbol: {symbol}")
        strategy.restore_pending_campaign(campaign_id, origin_price=origin_price)
        self.active_symbol = symbol

    def campaign_origin_price(self, campaign_id: str) -> Decimal | None:
        parts = campaign_id.split(":")
        if len(parts) != 3:
            return None
        strategy = self.strategies.get(parts[1])
        return None if strategy is None else strategy.campaign_origin_price(campaign_id)

    def campaign_entry_bucket(self, campaign_id: str) -> str | None:
        parts = campaign_id.split(":")
        if len(parts) != 3:
            return None
        strategy = self.strategies.get(parts[1])
        return None if strategy is None else strategy.campaign_entry_bucket(campaign_id)

    def campaign_exit_state(
        self, symbol: str
    ) -> tuple[bool, bool, bool] | None:
        strategy = self.strategies.get(symbol)
        return None if strategy is None else strategy.campaign_exit_state()

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
