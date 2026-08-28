"""Spike 回测的可选共享市场特征。

该模块只负责按交易对维护一次行情窗口和因果特征。交易状态仍由每个
策略实例独立维护；未绑定此提供器的策略不会产生任何额外计算。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Iterable, Sequence

from trading_platform.backtest.strategy_definition import (
    FeatureSpec,
    SharedMetricResult,
    SharedMetricSpec,
    is_shared_metric_result,
    normalize_shared_metric_specs,
)
from trading_platform.shared.events import Bar1s, Kline
from trading_platform.strategies.spike.definition import (
    SPIKE_CANDIDATE_EXIT_FEATURE,
    SPIKE_ORDERFLOW_FEATURE,
    SPIKE_MIN_LOW_1M_FEATURE,
    SPIKE_PRIOR_HIGH_1M_FEATURE,
    SPIKE_RISE_60S_FEATURE,
    SPIKE_RISE_5S_FEATURE,
)
from trading_platform.strategies.spike.exit_features import (
    CandidateFeatureConfig,
    CandidateFeatureSnapshot,
    candidate_feature_snapshot,
)

MS_PER_SECOND = 1_000
MS_PER_MINUTE = 60_000
SUPPORTED_FEATURES = frozenset({
    SPIKE_RISE_5S_FEATURE,
    SPIKE_RISE_60S_FEATURE,
    SPIKE_ORDERFLOW_FEATURE,
    SPIKE_CANDIDATE_EXIT_FEATURE,
    SPIKE_MIN_LOW_1M_FEATURE,
    SPIKE_PRIOR_HIGH_1M_FEATURE,
})


def append_kline_and_evict_expired(
    cache: deque[Kline],
    kline: Kline,
    cutoff: int,
    *,
    is_time_ordered: bool,
) -> bool:
    """追加 K 线并淘汰过期数据，返回缓存是否仍按 close_time 有序。

    正常回放按时间递增，队首淘汰为摊销 O(1)。迟到 K 线会破坏这个前提，
    此时退回与旧 list 实现相同的全量过滤，并在重新有序后恢复快路径。
    """
    cache.append(kline)
    if is_time_ordered and (
        len(cache) == 1 or cache[-2].close_time <= kline.close_time
    ):
        while cache and cache[0].close_time < cutoff:
            cache.popleft()
        return True

    retained = [item for item in cache if item.close_time >= cutoff]
    cache.clear()
    cache.extend(retained)
    return all(
        previous.close_time <= current.close_time
        for previous, current in pairwise(cache)
    )


@dataclass(frozen=True)
class SpikeBarFeatures:
    """当前 1s Bar 的共享、因果入场输入。"""

    timestamp: int
    continuous: bool
    rise_5s: Decimal
    continuous_60s: bool = False
    rise_60s: Decimal | None = None
    volume_ready: bool = False
    volume_5s: Decimal | None = None
    median_volume_1s: Decimal | None = None
    volume_multiple_5s: Decimal | None = None
    orderflow_ready: bool = False
    taker_buy_volume_5s: Decimal | None = None
    taker_sell_volume_5s: Decimal | None = None
    raw_trade_count_5s: int | None = None
    cvd_5s: Decimal | None = None
    cvd_1m: Decimal | None = None
    cvd_5m: Decimal | None = None
    taker_buy_ratio_5s: Decimal | None = None


class SpikeSharedFeatureProvider:
    """为一组相同回放上下文的 Spike 实例共享行情窗口和特征。

    ``process_event`` 必须由 sweep 在策略事件之前调用。1s 成交量窗口采用
    惰性计算：只有某个策略的涨幅条件通过后才排序 60 个基准值。
    """

    def __init__(
        self,
        *,
        shared_features: Iterable[FeatureSpec] = (),
        shared_metrics: Iterable[SharedMetricSpec] = (),
        retained_1m_minutes: int,
    ) -> None:
        self.shared_features = frozenset(shared_features)
        self.shared_metrics = normalize_shared_metric_specs(shared_metrics)
        metric_by_feature = {
            metric.feature: metric for metric in self.shared_metrics
        }
        self._metric_by_feature = metric_by_feature
        required_metrics = {
            SPIKE_MIN_LOW_1M_FEATURE,
            SPIKE_PRIOR_HIGH_1M_FEATURE,
        } & self.shared_features
        missing_metrics = required_metrics - metric_by_feature.keys()
        if missing_metrics:
            names = ", ".join(
                sorted(f"{feature.name}@{feature.timeframe}" for feature in missing_metrics)
            )
            raise ValueError(f"missing shared metric declarations: {names}")
        unsupported = self.shared_features - SUPPORTED_FEATURES
        if unsupported:
            names = ", ".join(
                sorted(f"{feature.name}@{feature.timeframe}" for feature in unsupported)
            )
            raise ValueError(f"unsupported Spike shared features: {names}")
        self._requires_1s = bool(
            {
                SPIKE_RISE_5S_FEATURE,
                SPIKE_RISE_60S_FEATURE,
                SPIKE_ORDERFLOW_FEATURE,
            }
            & self.shared_features
        )
        self._requires_rise_60s = SPIKE_RISE_60S_FEATURE in self.shared_features
        self._requires_kline = bool(
            SPIKE_CANDIDATE_EXIT_FEATURE in self.shared_features
            or self._metric_by_feature
        )
        self.bars_1s: list[Bar1s] = []
        self.klines_1m: deque[Kline] = deque()
        self.klines_5m: deque[Kline] = deque()
        self.klines_15m: deque[Kline] = deque()
        self._kline_cache_time_ordered = {"1m": True, "5m": True, "15m": True}
        if isinstance(retained_1m_minutes, bool) or retained_1m_minutes <= 0:
            raise ValueError("retained_1m_minutes must be positive")
        self.retained_1m_minutes = int(retained_1m_minutes)
        self._latest_bar: Bar1s | None = None
        self._latest_bar_features: SpikeBarFeatures | None = None
        self._first_bar1s_timestamp: int | None = None
        self._last_bar1s_timestamp: int | None = None
        self._continuous_1s_count = 0
        self._candidate_version = 0
        self._candidate_cache: dict[
            tuple[int, CandidateFeatureConfig], CandidateFeatureSnapshot | None
        ] = {}
        self._kline_window_versions = {"1m": 0, "5m": 0, "15m": 0}
        self._window_metric_cache: dict[
            tuple[FeatureSpec, int, int, int], SharedMetricResult
        ] = {}
        self._completed_window_cache: dict[
            tuple[str, int, int, int], Sequence[Kline]
        ] = {}

    def bind(self, consumer: object) -> None:
        """绑定一个策略消费者；保留窗口必须由声明在构造时显式提供。"""
        binder = getattr(consumer, "bind_shared_feature_provider", None)
        if not callable(binder):
            raise TypeError("shared feature consumer does not support binding")
        binder(self)

    def process_event(self, event: Bar1s | Kline) -> None:
        """在同组策略收到事件前推进共享行情状态。"""
        if isinstance(event, Bar1s):
            if not self._requires_1s:
                return
            if (
                self._last_bar1s_timestamp is not None
                and event.timestamp - self._last_bar1s_timestamp == MS_PER_SECOND
            ):
                self._continuous_1s_count += 1
            else:
                self._continuous_1s_count = 1
            self._last_bar1s_timestamp = event.timestamp
            self.bars_1s.append(event)
            if self._first_bar1s_timestamp is None:
                self._first_bar1s_timestamp = event.timestamp
            cutoff = event.timestamp - 300 * MS_PER_SECOND
            while self.bars_1s and self.bars_1s[0].timestamp < cutoff:
                del self.bars_1s[0]
            self._latest_bar = event
            self._latest_bar_features = None
            return
        if not isinstance(event, Kline) or not self._requires_kline:
            return
        if event.interval == "1m":
            self._append_and_evict(
                "1m",
                self.klines_1m,
                event,
                event.close_time - self.retained_1m_minutes * MS_PER_MINUTE,
            )
            self._kline_window_versions["1m"] += 1
            self._window_metric_cache.clear()
            self._completed_window_cache.clear()
        elif event.interval in {"5m", "15m"}:
            cache = self.klines_5m if event.interval == "5m" else self.klines_15m
            self._append_and_evict(
                event.interval,
                cache,
                event,
                event.close_time - 40 * 60 * 60 * MS_PER_SECOND,
            )
            self._kline_window_versions[event.interval] += 1
        self._candidate_version += 1

    def supports_metric(self, feature: FeatureSpec) -> bool:
        """返回该 provider 是否声明了指定的纯行情窗口指标。"""
        return feature in self._metric_by_feature

    def window_metric(
        self, feature: FeatureSpec, minute_start: int, minutes: int
    ) -> SharedMetricResult:
        """按稳定的指标/事件版本/窗口 key 共享一次窗口计算结果。"""
        if minutes <= 0:
            raise ValueError("shared metric window must be positive")
        if feature not in self._metric_by_feature:
            raise KeyError(f"shared metric is not enabled: {feature.name}")
        interval = feature.timeframe
        if interval not in self._kline_window_versions:
            raise ValueError(f"unsupported shared metric timeframe: {interval}")
        version = self._kline_window_versions[interval]
        key = (feature, version, minute_start, minutes)
        if key not in self._window_metric_cache:
            window_key = (interval, version, minute_start, minutes)
            if window_key not in self._completed_window_cache:
                self._completed_window_cache[window_key] = self._completed_window(
                    interval, minute_start, minutes
                )
            window = self._completed_window_cache[window_key]
            result = self._metric_by_feature[feature].compute(window)
            if not is_shared_metric_result(result):
                raise TypeError(
                    "shared metric compute must return an immutable SharedMetricResult: "
                    f"{feature.name}@{feature.timeframe} returned "
                    f"{type(result).__name__}"
                )
            self._window_metric_cache[key] = result
        return self._window_metric_cache[key]

    def min_low_point_1m(
        self, minute_start: int, minutes: int
    ) -> tuple[Decimal, int] | None:
        return self.window_metric(SPIKE_MIN_LOW_1M_FEATURE, minute_start, minutes)

    def prior_high_point_1m(
        self, minute_start: int, minutes: int
    ) -> tuple[Decimal, int] | None:
        return self.window_metric(SPIKE_PRIOR_HIGH_1M_FEATURE, minute_start, minutes)

    def _compute_completed_1m_window(
        self, minute_start: int, minutes: int
    ) -> Sequence[Kline]:
        """返回连续完整的 1m 窗口；缺任一分钟时返回空序列。"""
        return self._completed_window("1m", minute_start, minutes)

    def _completed_window(
        self, interval: str, window_end: int, periods: int
    ) -> Sequence[Kline]:
        interval_ms = {
            "1m": MS_PER_MINUTE,
            "5m": 5 * MS_PER_MINUTE,
            "15m": 15 * MS_PER_MINUTE,
        }[interval]
        cache = {
            "1m": self.klines_1m,
            "5m": self.klines_5m,
            "15m": self.klines_15m,
        }[interval]
        window_start = window_end - periods * interval_ms
        by_open_time = {
            k.open_time: k
            for k in cache
            if window_start <= k.open_time < window_end
        }
        expected_times = range(window_start, window_end, interval_ms)
        if any(open_time not in by_open_time for open_time in expected_times):
            return ()
        return tuple(by_open_time[open_time] for open_time in expected_times)

    def _append_and_evict(
        self, interval: str, cache: deque[Kline], kline: Kline, cutoff: int
    ) -> None:
        self._kline_cache_time_ordered[interval] = append_kline_and_evict_expired(
            cache,
            kline,
            cutoff,
            is_time_ordered=self._kline_cache_time_ordered[interval],
        )

    def bar_features(self, bar: Bar1s) -> SpikeBarFeatures | None:
        """返回当前 Bar 的涨幅输入；非共享 1s 策略得到 ``None``。"""
        if not self._requires_1s or self._latest_bar is None:
            return None
        if bar.timestamp != self._latest_bar.timestamp:
            raise RuntimeError("shared Spike features must be consumed in event order")
        if self._latest_bar_features is None:
            if len(self.bars_1s) < 61:
                return None
            current = self.bars_1s[-1]
            previous_5s = self.bars_1s[-6]
            previous_60s = self.bars_1s[-61]
            continuous = (
                current.timestamp - previous_5s.timestamp == 5 * MS_PER_SECOND
                and current.timestamp - previous_60s.timestamp == 60 * MS_PER_SECOND
            )
            continuous_60s = False
            rise_60s = None
            if self._requires_rise_60s:
                continuous_60s = self._continuous_1s_count >= 61
                rise_60s = current.close / previous_60s.close - Decimal("1")
            self._latest_bar_features = SpikeBarFeatures(
                timestamp=current.timestamp,
                continuous=continuous,
                rise_5s=current.close / previous_5s.close - Decimal("1"),
                continuous_60s=continuous_60s,
                rise_60s=rise_60s,
            )
        return self._latest_bar_features

    def volume_features(self, bar: Bar1s) -> SpikeBarFeatures | None:
        """按需补充当前 Bar 的成交量窗口，并缓存给同组参数。"""
        features = self.bar_features(bar)
        if features is None:
            return None
        if features.volume_ready:
            return features
        volume_5s = sum((item.volume for item in self.bars_1s[-5:]), Decimal("0"))
        median = sorted(item.volume for item in self.bars_1s[-61:-1])[30]
        multiple = (
            None
            if median <= 0
            else volume_5s / (median * Decimal("5"))
        )
        self._latest_bar_features = SpikeBarFeatures(
            timestamp=features.timestamp,
            continuous=features.continuous,
            rise_5s=features.rise_5s,
            continuous_60s=features.continuous_60s,
            rise_60s=features.rise_60s,
            volume_ready=True,
            volume_5s=volume_5s,
            median_volume_1s=median,
            volume_multiple_5s=multiple,
            orderflow_ready=features.orderflow_ready,
            taker_buy_volume_5s=features.taker_buy_volume_5s,
            taker_sell_volume_5s=features.taker_sell_volume_5s,
            raw_trade_count_5s=features.raw_trade_count_5s,
            cvd_5s=features.cvd_5s,
            cvd_1m=features.cvd_1m,
            cvd_5m=features.cvd_5m,
            taker_buy_ratio_5s=features.taker_buy_ratio_5s,
        )
        return self._latest_bar_features

    def orderflow_features(self, bar: Bar1s) -> SpikeBarFeatures | None:
        """按需计算滚动主动成交与 CVD；只依赖 Bar1s 归档原始聚合。"""
        features = self.bar_features(bar)
        if features is None:
            # orderflow-only 消费者不需要等待 60 秒的涨幅基线。
            if not self._requires_1s or self._latest_bar is None:
                return None
            if bar.timestamp != self._latest_bar.timestamp:
                raise RuntimeError("shared Spike features must be consumed in event order")
            features = SpikeBarFeatures(
                timestamp=bar.timestamp,
                continuous=False,
                rise_5s=Decimal("0"),
            )
            self._latest_bar_features = features
        if features.orderflow_ready:
            return features

        five = self._orderflow_window(bar.timestamp, 5)
        one_minute = self._orderflow_window(bar.timestamp, 60)
        five_minutes = self._orderflow_window(bar.timestamp, 300)
        if five is None:
            return features
        buy_5s, sell_5s, raw_count_5s, cvd_5s = five
        total_5s = buy_5s + sell_5s
        self._latest_bar_features = SpikeBarFeatures(
            timestamp=features.timestamp,
            continuous=features.continuous,
            rise_5s=features.rise_5s,
            continuous_60s=features.continuous_60s,
            rise_60s=features.rise_60s,
            volume_ready=features.volume_ready,
            volume_5s=features.volume_5s,
            median_volume_1s=features.median_volume_1s,
            volume_multiple_5s=features.volume_multiple_5s,
            orderflow_ready=True,
            taker_buy_volume_5s=buy_5s,
            taker_sell_volume_5s=sell_5s,
            raw_trade_count_5s=raw_count_5s,
            cvd_5s=cvd_5s,
            cvd_1m=None if one_minute is None else one_minute[3],
            cvd_5m=None if five_minutes is None else five_minutes[3],
            taker_buy_ratio_5s=(
                None if total_5s <= 0 else buy_5s / total_5s
            ),
        )
        return self._latest_bar_features

    def _orderflow_window(
        self, timestamp: int, seconds: int
    ) -> tuple[Decimal, Decimal, int | None, Decimal] | None:
        if self._first_bar1s_timestamp is None:
            return None
        required_start = timestamp - (seconds - 1) * MS_PER_SECOND
        # 至少观察满一个完整窗口后才输出，避免启动时把短样本冒充完整窗口。
        if self._first_bar1s_timestamp > required_start:
            return None
        selected = [
            item
            for item in self.bars_1s
            if required_start <= item.timestamp <= timestamp
        ]
        if len(selected) != seconds:
            return None
        if any(
            current.timestamp - previous.timestamp != MS_PER_SECOND
            for previous, current in pairwise(selected)
        ):
            return None
        if any(not item.orderflow_available for item in selected):
            return None
        buy = sum(
            (item.taker_buy_volume or Decimal("0") for item in selected),
            Decimal("0"),
        )
        sell = sum(
            (item.taker_sell_volume or Decimal("0") for item in selected),
            Decimal("0"),
        )
        raw_counts = [item.raw_trade_count for item in selected]
        raw_count = (
            None
            if any(value is None for value in raw_counts)
            else sum(int(value) for value in raw_counts if value is not None)
        )
        return buy, sell, raw_count, buy - sell

    def candidate_features(
        self, config: CandidateFeatureConfig
    ) -> CandidateFeatureSnapshot | None:
        """按 K 线版本和配置缓存 candidate-v1 的最新因果 snapshot。"""
        key = (self._candidate_version, config)
        if key not in self._candidate_cache:
            self._candidate_cache[key] = candidate_feature_snapshot(
                self.klines_1m,
                self.klines_5m,
                self.klines_15m,
                config=config,
            )
            if len(self._candidate_cache) > 8:
                oldest = next(iter(self._candidate_cache))
                del self._candidate_cache[oldest]
        return self._candidate_cache[key]
