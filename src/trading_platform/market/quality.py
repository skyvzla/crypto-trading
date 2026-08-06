"""Per-stream market-data continuity tracking."""

from dataclasses import dataclass, field
from typing import Any, Literal


QualityStatus = Literal["awaiting_data", "healthy", "degraded"]


@dataclass
class StreamQuality:
    stream: str
    status: QualityStatus = "awaiting_data"
    connection_generation: int = 0
    last_event_time_ms: int | None = None
    last_received_at_ms: int | None = None
    message_count: int = 0
    duplicate_count: int = 0
    gap_count: int = 0
    issue: str | None = None
    last_sequence: int | None = field(default=None, repr=False)
    last_kline_close_time: int | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "status": self.status,
            "connection_generation": self.connection_generation,
            "last_event_time_ms": self.last_event_time_ms,
            "last_received_at_ms": self.last_received_at_ms,
            "message_count": self.message_count,
            "duplicate_count": self.duplicate_count,
            "gap_count": self.gap_count,
            "issue": self.issue,
        }


class MarketDataQualityTracker:
    """Tracks only deterministic transport continuity facts.

    A detected gap is sticky because reconnecting cannot recover missing events.
    Recovery requires an explicit backfill/reconciliation implementation.
    """

    def __init__(self) -> None:
        self._streams: dict[str, StreamQuality] = {}

    def set_expected_streams(self, streams: list[str]) -> None:
        expected = set(streams)
        self._streams = {
            stream: quality
            for stream, quality in self._streams.items()
            if stream in expected
        }
        for stream in expected:
            self._streams.setdefault(stream, StreamQuality(stream=stream))

    def begin_connection(self, streams: list[str], generation: int) -> None:
        self.set_expected_streams(streams)
        for stream in streams:
            quality = self._streams[stream]
            quality.connection_generation = generation
            if quality.status != "degraded":
                quality.status = "awaiting_data"

    @property
    def ready(self) -> bool:
        return all(item.status == "healthy" for item in self._streams.values())

    @property
    def issue_count(self) -> int:
        return sum(item.status != "healthy" for item in self._streams.values())

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            stream: self._streams[stream].to_dict()
            for stream in sorted(self._streams)
        }

    def observe_aggtrade(
        self,
        symbol: str,
        aggregate_trade_id: int | None,
        event_time_ms: int,
        received_at_ms: int,
    ) -> bool:
        stream = f"{symbol.lower()}@aggTrade"
        quality = self._streams.get(stream)
        if quality is None:
            return True

        if quality.status == "degraded":
            return False

        if aggregate_trade_id is not None and quality.last_sequence is not None:
            if aggregate_trade_id == quality.last_sequence:
                quality.duplicate_count += 1
                return False
            if aggregate_trade_id != quality.last_sequence + 1:
                direction = "out_of_order" if aggregate_trade_id < quality.last_sequence else "gap"
                self._degrade(
                    quality,
                    f"aggtrade_{direction}:expected={quality.last_sequence + 1},actual={aggregate_trade_id}",
                )
                return False

        if quality.last_event_time_ms is not None and event_time_ms < quality.last_event_time_ms:
            self._degrade(
                quality,
                f"event_time_regression:previous={quality.last_event_time_ms},actual={event_time_ms}",
            )
            return False

        if aggregate_trade_id is not None:
            quality.last_sequence = aggregate_trade_id
        self._accept(quality, event_time_ms, received_at_ms)
        return True

    def observe_kline(
        self,
        symbol: str,
        interval: str,
        open_time_ms: int,
        close_time_ms: int,
        received_at_ms: int,
    ) -> bool:
        stream = f"{symbol.lower()}@kline_{interval}"
        quality = self._streams.get(stream)
        if quality is None:
            return True

        if quality.status == "degraded":
            return False

        previous_close = quality.last_kline_close_time
        if previous_close is not None:
            if close_time_ms == previous_close:
                quality.duplicate_count += 1
                return False
            if open_time_ms != previous_close + 1:
                direction = "out_of_order" if close_time_ms < previous_close else "gap"
                self._degrade(
                    quality,
                    f"kline_{direction}:expected_open={previous_close + 1},actual_open={open_time_ms}",
                )
                return False

        quality.last_kline_close_time = close_time_ms
        self._accept(quality, close_time_ms, received_at_ms)
        return True

    @staticmethod
    def _accept(
        quality: StreamQuality,
        event_time_ms: int,
        received_at_ms: int,
    ) -> None:
        quality.status = "healthy"
        quality.last_event_time_ms = event_time_ms
        quality.last_received_at_ms = received_at_ms
        quality.message_count += 1
        quality.issue = None

    @staticmethod
    def _degrade(quality: StreamQuality, issue: str) -> None:
        quality.status = "degraded"
        quality.gap_count += 1
        quality.issue = issue
