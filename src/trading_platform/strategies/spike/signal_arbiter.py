"""账户级信号先到先得仲裁。

仲裁器只处理本地顺序和生命周期，不执行网络请求。调用方应在单一策略事件
循环中调用它，并把真正的交易所 Campaign 抢占放在获胜候选上。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal


ArbitrationStatus = Literal[
    "acquired", "skipped_overlap", "skipped_stale", "skipped_invalid"
]


@dataclass(frozen=True, slots=True)
class SignalCandidate:
    symbol: str
    campaign_id: str
    signal_time: int
    received_at: int
    arrival_sequence: int


@dataclass(frozen=True, slots=True)
class ArbitrationResult:
    candidate: SignalCandidate
    status: ArbitrationStatus


class SignalArbiter:
    """为一个账户/策略分配单调序号并按 FIFO 选择一个 Campaign。"""

    def __init__(self, *, stale_after_ms: int = 5_000) -> None:
        if stale_after_ms < 0:
            raise ValueError("stale_after_ms must be non-negative")
        self.stale_after_ms = stale_after_ms
        self._next_sequence = 1
        self._pending: deque[SignalCandidate] = deque()
        self._active_campaign_id: str | None = None

    @property
    def active_campaign_id(self) -> str | None:
        return self._active_campaign_id

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def enqueue(
        self, *, symbol: str, campaign_id: str, signal_time: int, received_at: int
    ) -> SignalCandidate:
        if not symbol or not campaign_id:
            raise ValueError("symbol and campaign_id are required")
        if signal_time < 0 or received_at < 0:
            raise ValueError("signal_time and received_at must be non-negative")
        candidate = SignalCandidate(
            symbol=symbol,
            campaign_id=campaign_id,
            signal_time=signal_time,
            received_at=received_at,
            arrival_sequence=self._next_sequence,
        )
        self._next_sequence += 1
        self._pending.append(candidate)
        return candidate

    def arbitrate(self, *, now_ms: int) -> tuple[ArbitrationResult, ...]:
        """按 FIFO 处理全部待选信号，并至多选出一个 Campaign。"""

        if now_ms < 0:
            raise ValueError("now_ms must be non-negative")
        results: list[ArbitrationResult] = []
        while self._pending:
            candidate = self._pending.popleft()
            if self._active_campaign_id is not None:
                results.append(ArbitrationResult(candidate, "skipped_overlap"))
            elif now_ms - candidate.received_at > self.stale_after_ms:
                results.append(ArbitrationResult(candidate, "skipped_stale"))
            elif candidate.signal_time > candidate.received_at:
                results.append(ArbitrationResult(candidate, "skipped_invalid"))
            else:
                self._active_campaign_id = candidate.campaign_id
                results.append(ArbitrationResult(candidate, "acquired"))
        return tuple(results)

    def release(self, campaign_id: str) -> None:
        if not campaign_id or campaign_id != self._active_campaign_id:
            raise ValueError("only the active Campaign can be released")
        self._active_campaign_id = None
