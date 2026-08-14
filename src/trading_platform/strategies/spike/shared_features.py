"""Spike 回测的可选共享市场特征。

该模块只负责按交易对维护一次行情窗口和因果特征。交易状态仍由每个
策略实例独立维护；未绑定此提供器的策略不会产生任何额外计算。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Iterable

from trading_platform.backtest.strategy_definition import FeatureSpec
from trading_platform.shared.events import Bar1s, Kline
from trading_platform.strategies.spike.definition import (
    SPIKE_CANDIDATE_EXIT_FEATURE,
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
    SPIKE_CANDIDATE_EXIT_FEATURE,
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
    volume_ready: bool = False
    volume_5s: Decimal | None = None
    median_volume_1s: Decimal | None = None
    volume_multiple_5s: Decimal | None = None


class SpikeSharedFeatureProvider:
    """为一组相同回放上下文的 Spike 实例共享行情窗口和特征。

    ``process_event`` 必须由 sweep 在策略事件之前调用。1s 成交量窗口采用
    惰性计算：只有某个策略的涨幅条件通过后才排序 60 个基准值。
    """

    def __init__(
        self,
        *,
        shared_features: Iterable[FeatureSpec] = (),
        retained_1m_minutes: int = 30 * 60,
    ) -> None:
        self.shared_features = frozenset(shared_features)
        unsupported = self.shared_features - SUPPORTED_FEATURES
        if unsupported:
            names = ", ".join(
                sorted(f"{feature.name}@{feature.timeframe}" for feature in unsupported)
            )
            raise ValueError(f"unsupported Spike shared features: {names}")
        self._requires_1s = SPIKE_RISE_5S_FEATURE in self.shared_features
        self._requires_kline = SPIKE_CANDIDATE_EXIT_FEATURE in self.shared_features
        self.bars_1s: list[Bar1s] = []
        self.klines_1m: deque[Kline] = deque()
        self.klines_5m: deque[Kline] = deque()
        self.klines_15m: deque[Kline] = deque()
        self._kline_cache_time_ordered = {"1m": True, "5m": True, "15m": True}
        self.retained_1m_minutes = max(30 * 60, int(retained_1m_minutes))
        self._latest_bar: Bar1s | None = None
        self._latest_bar_features: SpikeBarFeatures | None = None
        self._candidate_version = 0
        self._candidate_cache: dict[
            tuple[int, CandidateFeatureConfig], CandidateFeatureSnapshot | None
        ] = {}

    def bind(self, consumer: object) -> None:
        """绑定一个策略消费者，并在首个事件前扩大共享保留窗口。"""
        binder = getattr(consumer, "bind_shared_feature_provider", None)
        if not callable(binder):
            raise TypeError("shared feature consumer does not support binding")
        strategies = getattr(consumer, "strategies", {})
        for strategy in strategies.values():
            self.retained_1m_minutes = max(
                self.retained_1m_minutes,
                int(getattr(strategy, "rise_low_lookback_minutes", 0)),
            )
        binder(self)

    def process_event(self, event: Bar1s | Kline) -> None:
        """在同组策略收到事件前推进共享行情状态。"""
        if isinstance(event, Bar1s):
            if not self._requires_1s:
                return
            self.bars_1s.append(event)
            if len(self.bars_1s) > 61:
                del self.bars_1s[:-61]
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
        elif event.interval in {"5m", "15m"}:
            cache = self.klines_5m if event.interval == "5m" else self.klines_15m
            self._append_and_evict(
                event.interval,
                cache,
                event,
                event.close_time - 40 * 60 * 60 * MS_PER_SECOND,
            )
        self._candidate_version += 1

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
            self._latest_bar_features = SpikeBarFeatures(
                timestamp=current.timestamp,
                continuous=continuous,
                rise_5s=current.close / previous_5s.close - Decimal("1"),
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
            volume_ready=True,
            volume_5s=volume_5s,
            median_volume_1s=median,
            volume_multiple_5s=multiple,
        )
        return self._latest_bar_features

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
