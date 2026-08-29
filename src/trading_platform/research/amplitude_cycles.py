"""Annual amplitude-event detection on daily candidates and minute bars.

The module deliberately keeps the detector independent from DuckDB and pandas.
Archive adapters can turn rows into :class:`DailyCandidate` and :class:`Bar`
objects, while tests can exercise the complete event definition in memory.
All timestamps are UTC epoch milliseconds and all prices are positive floats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from math import log, log1p
from statistics import median
from typing import Iterable, Mapping, Sequence

DAY_MS = 86_400_000
MINUTE_MS = 60_000

Bar = tuple[int, float, float, float, float]


@dataclass(frozen=True)
class DailyCandidate:
    """A 1d bar selected by the broad amplitude screen."""

    symbol: str
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    close_time_ms: int | None = None
    amplitude_percent: float | None = None
    candidate_score_percent: float | None = None

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("candidate symbol must not be empty")
        if self.open_time_ms < 0:
            raise ValueError("candidate open_time_ms must be non-negative")
        if self.open <= 0 or self.high <= 0 or self.low <= 0 or self.close <= 0:
            raise ValueError("candidate prices must be positive")
        if self.high < self.low:
            raise ValueError("candidate high must not be below low")

    @property
    def normalized_symbol(self) -> str:
        return self.symbol.strip().upper()

    @property
    def end_time_ms(self) -> int:
        return self.close_time_ms + 1 if self.close_time_ms is not None else self.open_time_ms + DAY_MS

    @property
    def amplitude(self) -> float:
        if self.amplitude_percent is not None:
            return float(self.amplitude_percent)
        return (self.high - self.low) / self.low * 100.0

    @property
    def candidate_score(self) -> float:
        if self.candidate_score_percent is not None:
            return float(self.candidate_score_percent)
        return self.amplitude


@dataclass(frozen=True)
class CandidateBlock:
    """A contiguous block of daily candidates for one symbol."""

    symbol: str
    start_ms: int
    end_ms: int
    candidates: tuple[DailyCandidate, ...]

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def max_amplitude_percent(self) -> float:
        return max(item.amplitude for item in self.candidates)

    @property
    def candidate_dates(self) -> tuple[str, ...]:
        return tuple(_date(item.open_time_ms) for item in self.candidates)


@dataclass(frozen=True)
class ScanConfig:
    """Detector thresholds.

    Percent values are expressed as human percentages (15 means 15%).
    ``candidate_gap_days=1`` merges only adjacent selected UTC daily bars.
    Expanded candidate windows may still share one archive read; minute-level
    pivots, rather than the daily grouping, define and de-duplicate events.
    """

    candidate_threshold_percent: float = 15.0
    candidate_gap_days: int = 1
    expand_before_days: int = 2
    expand_after_days: int = 3
    spike_threshold_percent: float = 15.0
    spike_retrace_percent: float = 2.0
    violent_rise_percent: float = 50.0
    crash_start_percent: float = 10.0
    crash_percent: float = 30.0
    post_spike_window_minutes: int = 360
    cycle_end_recovery_percent: float = 10.0
    scale_window: int = 60
    dc_k: float = 6.0
    dc_floor_percent: float = 2.0
    dc_cap_percent: float = 30.0
    spike_k: float = 8.0
    max_context_days: int = 14

    def __post_init__(self) -> None:
        for name in (
            "candidate_threshold_percent",
            "spike_threshold_percent",
            "spike_retrace_percent",
            "violent_rise_percent",
            "crash_start_percent",
            "crash_percent",
            "cycle_end_recovery_percent",
            "dc_k",
            "dc_floor_percent",
            "dc_cap_percent",
            "spike_k",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "candidate_gap_days",
            "expand_before_days",
            "expand_after_days",
            "post_spike_window_minutes",
            "scale_window",
            "max_context_days",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.scale_window <= 0 or self.dc_cap_percent < self.dc_floor_percent:
            raise ValueError("adaptive directional-change configuration is invalid")


@dataclass(frozen=True)
class Coverage:
    """Minute coverage for the effective (archive-clipped) scan window."""

    requested_start_ms: int
    requested_end_ms: int
    effective_start_ms: int
    effective_end_ms: int
    expected_count: int
    actual_count: int
    unique_count: int
    missing_count: int
    duplicate_count: int
    gap_count: int
    max_gap_minutes: int
    first_open_ms: int | None
    last_open_ms: int | None
    status: str

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    @property
    def analysis_allowed(self) -> bool:
        return self.status in {"complete", "partial_archive_boundary"}

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_start_utc": _iso(self.requested_start_ms),
            "requested_end_utc": _iso(self.requested_end_ms),
            "effective_start_utc": _iso(self.effective_start_ms),
            "effective_end_utc": _iso(self.effective_end_ms),
            "expected_minute_count": self.expected_count,
            "actual_minute_count": self.actual_count,
            "unique_minute_count": self.unique_count,
            "missing_minute_count": self.missing_count,
            "duplicate_minute_count": self.duplicate_count,
            "gap_count": self.gap_count,
            "max_gap_minutes": self.max_gap_minutes,
            "first_minute_utc": _iso(self.first_open_ms),
            "last_minute_utc": _iso(self.last_open_ms),
            "coverage_status": self.status,
        }


@dataclass(frozen=True)
class Spike:
    index: int
    timestamp_ms: int
    high: float
    low: float
    close: float
    up_excursion_percent: float
    down_excursion_percent: float
    directions: tuple[str, ...]


@dataclass
class DetectionResult:
    """One candidate block's event row and its ordered node rows."""

    event: dict[str, object]
    nodes: list[dict[str, object]] = field(default_factory=list)


def _iso(value_ms: int | None) -> str:
    if value_ms is None:
        return ""
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).isoformat()


def _date(value_ms: int) -> str:
    return _iso(value_ms)[:10]


def _as_bar(value: Sequence[object]) -> Bar:
    if len(value) < 5:
        raise ValueError("bar must contain timestamp, open, high, low and close")
    timestamp, opened, high, low, close = value[:5]
    bar = (int(timestamp), float(opened), float(high), float(low), float(close))
    if bar[0] < 0 or min(bar[1:]) <= 0 or bar[2] < bar[3]:
        raise ValueError("bar timestamp/prices are invalid")
    return bar


def daily_candidate_from_row(row: Mapping[str, object], *, threshold_percent: float) -> DailyCandidate | None:
    """Convert a mapping from DuckDB/pandas and apply the 1d screen."""

    symbol = str(row.get("symbol", "")).strip().upper()
    timestamp = row.get("open_time_ms", row.get("timestamp_ms"))
    if timestamp is None:
        timestamp = row.get("open_time")
    if timestamp is None:
        raise ValueError("daily row is missing open_time")
    if hasattr(timestamp, "timestamp"):
        timestamp = int(timestamp.timestamp() * 1000)
    opened = float(row["open"])
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    amplitude = (high - low) / low * 100.0 if low > 0 else 0.0
    if amplitude < threshold_percent:
        return None
    close_time = row.get("close_time_ms", row.get("close_time"))
    if close_time is not None and hasattr(close_time, "timestamp"):
        close_time = int(close_time.timestamp() * 1000)
    return DailyCandidate(
        symbol=symbol,
        open_time_ms=int(timestamp),
        open=opened,
        high=high,
        low=low,
        close=close,
        close_time_ms=None if close_time is None else int(close_time),
        amplitude_percent=amplitude,
        candidate_score_percent=amplitude,
    )


def merge_daily_candidates(
    candidates: Iterable[DailyCandidate], *, max_gap_days: int = 1
) -> list[CandidateBlock]:
    """Merge selected daily bars by symbol while preserving deterministic order."""

    if max_gap_days < 1:
        raise ValueError("max_gap_days must be at least 1")
    grouped: dict[str, list[DailyCandidate]] = {}
    for item in candidates:
        grouped.setdefault(item.normalized_symbol, []).append(item)
    blocks: list[CandidateBlock] = []
    for symbol, items in grouped.items():
        ordered = sorted(items, key=lambda item: item.open_time_ms)
        current: list[DailyCandidate] = []
        for item in ordered:
            if current and item.open_time_ms - current[-1].open_time_ms > max_gap_days * DAY_MS:
                blocks.append(_candidate_block(symbol, current))
                current = []
            current.append(item)
        if current:
            blocks.append(_candidate_block(symbol, current))
    return sorted(blocks, key=lambda block: (block.symbol, block.start_ms))


def _candidate_block(symbol: str, items: Sequence[DailyCandidate]) -> CandidateBlock:
    first = items[0]
    last = items[-1]
    return CandidateBlock(
        symbol=symbol,
        start_ms=first.open_time_ms,
        end_ms=max(last.end_time_ms, last.open_time_ms + DAY_MS),
        candidates=tuple(items),
    )


def expanded_window(
    block: CandidateBlock, *, before_days: int, after_days: int
) -> tuple[int, int]:
    """Return an exclusive UTC window; this works across months and years."""

    if before_days < 0 or after_days < 0:
        raise ValueError("window expansion days must be non-negative")
    return (
        block.start_ms - before_days * DAY_MS,
        block.end_ms + after_days * DAY_MS,
    )


def assess_coverage(
    bars: Iterable[Sequence[object]],
    start_ms: int,
    end_ms: int,
    *,
    archive_start_ms: int | None = None,
    archive_end_ms: int | None = None,
    interval_ms: int = MINUTE_MS,
) -> Coverage:
    """Measure exact minute coverage and explicitly expose missing intervals.

    ``archive_start_ms``/``archive_end_ms`` are optional known archive bounds.
    Clipping at those bounds is reported as ``partial_archive_boundary`` rather
    than a false missing-data failure. Gaps or duplicates inside the effective
    window always produce ``incomplete_gap``.
    """

    if start_ms >= end_ms:
        raise ValueError("coverage start must be earlier than end")
    if interval_ms <= 0:
        raise ValueError("interval_ms must be positive")
    effective_start = start_ms if archive_start_ms is None else max(start_ms, archive_start_ms)
    effective_end = end_ms if archive_end_ms is None else min(end_ms, archive_end_ms)
    clipped = effective_start != start_ms or effective_end != end_ms
    if effective_start >= effective_end:
        return Coverage(
            start_ms, end_ms, effective_start, effective_end, 0, 0, 0, 0, 0, 0, 0,
            None, None, "missing_1m",
        )
    normalized = sorted(_as_bar(bar) for bar in bars if start_ms <= int(bar[0]) < end_ms)
    timestamps = [bar[0] for bar in normalized if effective_start <= bar[0] < effective_end]
    unique = sorted(set(timestamps))
    expected = max(0, (effective_end - effective_start + interval_ms - 1) // interval_ms)
    expected_set = set(range(effective_start, effective_end, interval_ms))
    missing = len(expected_set - set(unique))
    duplicate = len(timestamps) - len(unique)
    gaps: list[int] = []
    for previous, current in zip(unique, unique[1:]):
        if current - previous > interval_ms:
            gaps.append(current - previous)
    status = "complete"
    if not unique:
        status = "missing_1m"
    elif missing or duplicate or gaps:
        status = "incomplete_gap"
    elif clipped:
        status = "partial_archive_boundary"
    return Coverage(
        requested_start_ms=start_ms,
        requested_end_ms=end_ms,
        effective_start_ms=effective_start,
        effective_end_ms=effective_end,
        expected_count=expected,
        actual_count=len(timestamps),
        unique_count=len(unique),
        missing_count=missing,
        duplicate_count=duplicate,
        gap_count=len(gaps),
        max_gap_minutes=max((gap // interval_ms for gap in gaps), default=0),
        first_open_ms=unique[0] if unique else None,
        last_open_ms=unique[-1] if unique else None,
        status=status,
    )


@dataclass(frozen=True)
class Pivot:
    index: int
    price: float
    kind: str
    confirmed_index: int | None


def lagged_robust_scale(bars: Sequence[Bar], window: int) -> list[float | None]:
    """Lagged rolling median TR%, excluding the current bar."""
    tr: list[float] = []
    result: list[float | None] = []
    for index, bar in enumerate(bars):
        history = tr[max(0, len(tr) - window) :]
        result.append(median(history) if len(history) == window else None)
        previous = bars[index - 1][4] if index else bar[1]
        tr.append(max(bar[2] - bar[3], abs(bar[2] - previous), abs(bar[3] - previous)) / previous * 100)
    return result


def directional_change_pivots(bars: Sequence[Bar], config: ScanConfig, *, include_provisional: bool = False) -> list[Pivot]:
    """Causal adaptive DC on log-close; thresholds freeze at each extreme."""
    if not bars:
        return []
    scales = lagged_robust_scale(bars, config.scale_window)
    def delta(index: int) -> float:
        scale = scales[index] if scales[index] is not None else config.dc_floor_percent / config.dc_k
        return min(config.dc_cap_percent, max(config.dc_floor_percent, config.dc_k * scale)) / 100
    direction = 0
    high_i = low_i = 0
    high = low = log(bars[0][4])
    high_delta = low_delta = delta(0)
    pivots: list[Pivot] = []
    for index in range(1, len(bars)):
        value = log(bars[index][4])
        if direction == 0:
            if value > high:
                high_i, high, high_delta = index, value, delta(index)
            if value < low:
                low_i, low, low_delta = index, value, delta(index)
            if value - low + 1e-12 >= log1p(low_delta):
                pivots.append(Pivot(low_i, bars[low_i][4], "low", index))
                direction, high_i, high, high_delta = 1, index, value, delta(index)
            elif high - value + 1e-12 >= -log1p(-high_delta):
                pivots.append(Pivot(high_i, bars[high_i][4], "high", index))
                direction, low_i, low, low_delta = -1, index, value, delta(index)
        elif direction == 1:
            if value > high:
                high_i, high, high_delta = index, value, delta(index)
            elif high - value + 1e-12 >= -log1p(-high_delta):
                pivots.append(Pivot(high_i, bars[high_i][4], "high", index))
                direction, low_i, low, low_delta = -1, index, value, delta(index)
        else:
            if value < low:
                low_i, low, low_delta = index, value, delta(index)
            elif value - low + 1e-12 >= log1p(low_delta):
                pivots.append(Pivot(low_i, bars[low_i][4], "low", index))
                direction, high_i, high, high_delta = 1, index, value, delta(index)
    if include_provisional and direction == 1:
        pivots.append(Pivot(high_i, bars[high_i][4], "high", None))
    elif include_provisional and direction == -1:
        pivots.append(Pivot(low_i, bars[low_i][4], "low", None))
    return pivots


def _adaptive_spikes(bars: Sequence[Bar], config: ScanConfig) -> list[Spike]:
    scales = lagged_robust_scale(bars, config.scale_window)
    found: list[Spike] = []
    for index, bar in enumerate(bars):
        previous = bars[index - 1][4] if index else bar[1]
        scale = scales[index] or 0.0
        threshold = max(config.spike_threshold_percent, config.spike_k * scale)
        up = (bar[2] / previous - 1) * 100
        down = (1 - bar[3] / previous) * 100
        directions = []
        if up >= threshold and (bar[2] - bar[4]) / bar[2] * 100 >= config.spike_retrace_percent:
            directions.append("up")
        if down >= threshold and (bar[4] - bar[3]) / bar[4] * 100 >= config.spike_retrace_percent:
            directions.append("down")
        if directions:
            found.append(Spike(index, bar[0], bar[2], bar[3], bar[4], up, down, tuple(directions)))
    return found


def _analyze_dc_cycles(
    blocks: Sequence[CandidateBlock],
    bars: Iterable[Sequence[object]],
    *,
    config: ScanConfig,
    coverage: Coverage | None,
) -> list[DetectionResult]:
    if not blocks:
        return []
    raw_bars = [_as_bar(item) for item in bars]
    if not raw_bars:
        return []
    read_start = min(block.start_ms - config.max_context_days * DAY_MS for block in blocks)
    read_end = max(block.end_ms + config.max_context_days * DAY_MS for block in blocks)
    coverage = coverage or assess_coverage(raw_bars, read_start, read_end)
    if not coverage.analysis_allowed:
        return []
    normalized = sorted({bar[0]: bar for bar in raw_bars}.values())

    lows = [pivot for pivot in directional_change_pivots(normalized, config) if pivot.kind == "low"]
    up_spikes = [spike for spike in _adaptive_spikes(normalized, config) if "up" in spike.directions]
    grouped: dict[tuple[int, int], list[Spike]] = {}
    for spike in up_spikes:
        previous = next(
            (
                pivot
                for pivot in reversed(lows)
                if pivot.index < spike.index
                and pivot.index > 0
                and pivot.confirmed_index is not None
                and pivot.confirmed_index < spike.index
            ),
            None,
        )
        following = next((pivot for pivot in lows if pivot.index > spike.index), None)
        if previous is None or following is None:
            continue
        grouped.setdefault((previous.index, following.index), []).append(spike)

    results: list[DetectionResult] = []
    low_by_index = {pivot.index: pivot for pivot in lows}
    for (start_index, trough_index), spikes in grouped.items():
        primary = max(spikes, key=lambda spike: (spike.high, -spike.timestamp_ms))
        start_pivot, trough_pivot = low_by_index[start_index], low_by_index[trough_index]
        candidate_items = [
            item
            for block in blocks
            for item in block.candidates
            if item.open_time_ms < normalized[trough_index][0] + MINUTE_MS
            and item.end_time_ms > normalized[start_index][0]
        ]
        if not candidate_items:
            continue
        post_indexes = range(primary.index + 1, trough_index + 1)
        post_low_index = min(post_indexes, key=lambda index: (normalized[index][3], normalized[index][0]))
        drawdown = (primary.high - normalized[post_low_index][3]) / primary.high * 100
        rise = (primary.high - start_pivot.price) / start_pivot.price * 100
        crash_start = next(
            (
                index
                for index in post_indexes
                if normalized[index][4]
                <= primary.high * (1 - config.crash_start_percent / 100)
            ),
            trough_index,
        )
        labels = ["up_spike"]
        if rise >= config.violent_rise_percent:
            labels.insert(0, "violent_rise")
        is_dump = drawdown >= config.crash_percent
        if is_dump:
            labels.extend(["crash", "post_spike_dump"])
        event_id = sha256(
            f"up-spike-dc-v1|{blocks[0].symbol}|{normalized[start_index][0]}|{primary.timestamp_ms}|{normalized[trough_index][0]}".encode()
        ).hexdigest()[:24]
        event = {
            "event_id": event_id,
            "symbol": blocks[0].symbol,
            "candidate_start_utc": _iso(min(item.open_time_ms for item in candidate_items)),
            "candidate_end_utc": _iso(max(item.end_time_ms for item in candidate_items)),
            "candidate_dates": ",".join(sorted({_date(item.open_time_ms) for item in candidate_items})),
            "candidate_day_count": len(candidate_items),
            "candidate_max_amplitude_percent": round(max(item.amplitude for item in candidate_items), 6),
            "candidate_max_score_percent": round(max(item.candidate_score for item in candidate_items), 6),
            **coverage.as_dict(),
            "analysis_status": "resolved",
            "direction": "rise_spike_fall",
            "labels": ";".join(labels),
            "cycle_start_utc": _iso(normalized[start_index][0]),
            "rise_start_utc": _iso(normalized[start_index][0]),
            "peak_utc": _iso(primary.timestamp_ms),
            "peak_price": round(primary.high, 12),
            "spike_count": len(spikes),
            "up_spike_count": len(spikes),
            "down_spike_count": 0,
            "crash_start_utc": _iso(normalized[crash_start][0]),
            "trough_utc": _iso(normalized[trough_index][0]),
            "trough_price": round(trough_pivot.price, 12),
            "cycle_end_utc": _iso(normalized[trough_index][0]),
            "cycle_end_boundary": False,
            "rise_percent": round(rise, 6),
            "drawdown_percent": round(drawdown, 6),
            "post_spike_dump": is_dump,
            "post_spike_max_drawdown_percent": round(drawdown, 6),
            "post_spike_dump_start_utc": _iso(normalized[crash_start][0]) if is_dump else "",
            "post_spike_dump_trough_utc": _iso(normalized[post_low_index][0]) if is_dump else "",
            "post_spike_minutes_to_dump": (normalized[post_low_index][0] - primary.timestamp_ms) // MINUTE_MS if is_dump else "",
        }
        confirmed_start = _iso(normalized[start_pivot.confirmed_index][0] + MINUTE_MS)
        confirmed_trough = _iso(normalized[trough_pivot.confirmed_index][0] + MINUTE_MS)
        node_values = [
            ("cycle_start", normalized[start_index][0], confirmed_start, start_pivot.price),
            ("rise_start", normalized[start_index][0], confirmed_start, start_pivot.price),
            ("up_spike", primary.timestamp_ms, _iso(primary.timestamp_ms + MINUTE_MS), primary.high),
            ("crash_start", normalized[crash_start][0], _iso(normalized[crash_start][0] + MINUTE_MS), normalized[crash_start][4]),
            ("trough", normalized[trough_index][0], confirmed_trough, trough_pivot.price),
            ("cycle_end", normalized[trough_index][0], confirmed_trough, trough_pivot.price),
        ]
        nodes = [
            {
                "event_id": event_id,
                "node_index": index,
                "node_type": node_type,
                "occurrence_time_utc": _iso(occurrence) if isinstance(occurrence, int) else occurrence,
                "confirmed_time_utc": confirmed,
                "price": round(price, 12),
                "direction": "up" if node_type == "up_spike" else "",
                "source": "ohlc_wick" if node_type == "up_spike" else "directional_change",
            }
            for index, (node_type, occurrence, confirmed, price) in enumerate(node_values, 1)
        ]
        results.append(DetectionResult(event, nodes))
    return sorted(results, key=lambda result: (str(result.event["cycle_start_utc"]), str(result.event["peak_utc"])))




def analyze_cycles(
    blocks: Sequence[CandidateBlock],
    bars: Iterable[Sequence[object]],
    *,
    config: ScanConfig = ScanConfig(),
    coverage: Coverage | None = None,
) -> list[DetectionResult]:
    """Build unique events from causal adaptive directional-change legs."""
    return _analyze_dc_cycles(blocks, bars, config=config, coverage=coverage)


__all__ = [
    "Bar",
    "CandidateBlock",
    "Coverage",
    "DAY_MS",
    "DailyCandidate",
    "DetectionResult",
    "MINUTE_MS",
    "ScanConfig",
    "Spike",
    "analyze_cycles",
    "assess_coverage",
    "daily_candidate_from_row",
    "directional_change_pivots",
    "expanded_window",
    "merge_daily_candidates",
]
