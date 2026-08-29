from trading_platform.research.amplitude_cycles import (
    DAY_MS,
    MINUTE_MS,
    DailyCandidate,
    ScanConfig,
    analyze_cycles,
    assess_coverage,
    directional_change_pivots,
    merge_daily_candidates,
)


def _candidate(day: int) -> DailyCandidate:
    opened = day * DAY_MS
    return DailyCandidate("abcusdt", opened, 100, 180, 90, 150, opened + DAY_MS - 1)


def test_adjacent_daily_candidates_merge_and_keep_exclusive_boundary() -> None:
    blocks = merge_daily_candidates([_candidate(31), _candidate(30)])
    assert len(blocks) == 1
    assert blocks[0].symbol == "ABCUSDT"
    assert blocks[0].start_ms == 30 * DAY_MS
    assert blocks[0].end_ms == 32 * DAY_MS


def test_default_candidate_gap_does_not_bridge_quiet_days() -> None:
    config = ScanConfig()
    blocks = merge_daily_candidates([_candidate(1), _candidate(4)], max_gap_days=config.candidate_gap_days)
    assert len(blocks) == 2


def test_daily_amplitude_uses_low_denominator() -> None:
    assert _candidate(0).amplitude == 100.0


def test_daily_amplitude_and_candidate_score_have_distinct_semantics() -> None:
    candidate = DailyCandidate(
        "ABCUSDT", 0, 100, 102, 100, 101,
        candidate_score_percent=50,
    )

    assert candidate.amplitude == 2.0
    assert candidate.candidate_score == 50.0


def test_coverage_distinguishes_archive_boundary_from_internal_gap() -> None:
    bars = [(minute * MINUTE_MS, 1, 1, 1, 1) for minute in range(2, 8)]
    boundary = assess_coverage(
        bars, 0, 10 * MINUTE_MS,
        archive_start_ms=2 * MINUTE_MS,
        archive_end_ms=8 * MINUTE_MS,
    )
    assert boundary.status == "partial_archive_boundary"
    missing = assess_coverage(bars[:-1], 2 * MINUTE_MS, 8 * MINUTE_MS)
    assert missing.status == "incomplete_gap"
    assert missing.missing_count == 1

def test_adaptive_dc_is_price_scale_invariant_and_confirmations_are_causal() -> None:
    config = ScanConfig(scale_window=3, dc_k=3, dc_floor_percent=2, dc_cap_percent=20)
    closes = [100, 102, 105, 110, 106, 100, 96, 101, 108]

    def bars(multiplier: float) -> list[tuple[int, float, float, float, float]]:
        return [(i * MINUTE_MS, p * multiplier, p * multiplier, p * multiplier, p * multiplier) for i, p in enumerate(closes)]
    left = directional_change_pivots(bars(1), config)
    right = directional_change_pivots(bars(1000), config)
    assert [(p.index, p.kind, p.confirmed_index) for p in left] == [(p.index, p.kind, p.confirmed_index) for p in right]
    assert all(p.confirmed_index is not None and p.confirmed_index >= p.index for p in left)


def test_confirmed_dc_prefix_is_append_stable() -> None:
    config = ScanConfig(scale_window=3, dc_k=3, dc_floor_percent=2, dc_cap_percent=20)
    closes = [100, 110, 120, 105, 90, 100, 115, 108, 95, 105]
    bars = [(i * MINUTE_MS, p, p, p, p) for i, p in enumerate(closes)]
    prefix = directional_change_pivots(bars[:7], config)
    extended = directional_change_pivots(bars, config)
    assert prefix == extended[: len(prefix)]


def test_dc_downward_confirmation_uses_exact_percentage_drop() -> None:
    config = ScanConfig(scale_window=1, dc_k=1, dc_floor_percent=20, dc_cap_percent=20)
    closes = [100, 125, 104]
    bars = [(i * MINUTE_MS, p, p, p, p) for i, p in enumerate(closes)]

    pivots = directional_change_pivots(bars, config)

    assert [(pivot.index, pivot.kind) for pivot in pivots] == [(0, "low")]


def test_candidate_block_order_does_not_change_adaptive_events() -> None:
    blocks = merge_daily_candidates([_candidate(0), _candidate(1)])
    closes = [70, 90, 130, 100, 75, 90, 120, 80]
    bars = [(i * MINUTE_MS, p, p, p, p) for i, p in enumerate(closes)]
    coverage = assess_coverage(bars, 0, len(bars) * MINUTE_MS)
    config = ScanConfig(expand_before_days=0, expand_after_days=0)
    forward = analyze_cycles(blocks, bars, config=config, coverage=coverage)
    reverse = analyze_cycles(list(reversed(blocks)), bars, config=config, coverage=coverage)
    assert [item.event["event_id"] for item in forward] == [item.event["event_id"] for item in reverse]


def test_up_spikes_between_the_same_confirmed_lows_form_one_event() -> None:
    block = merge_daily_candidates([_candidate(0)])[0]
    closes = [110, 100, 106, 120, 118, 116, 90, 120]
    bars = [(index * MINUTE_MS, close, close, close, close) for index, close in enumerate(closes)]
    bars[3] = (3 * MINUTE_MS, 106, 150, 104, 120)
    bars[4] = (4 * MINUTE_MS, 120, 170, 115, 118)
    coverage = assess_coverage(bars, 0, len(bars) * MINUTE_MS)
    config = ScanConfig(
        scale_window=3,
        dc_k=2,
        dc_floor_percent=5,
        dc_cap_percent=20,
        spike_threshold_percent=15,
        spike_k=2,
    )
    results = analyze_cycles([block], bars, config=config, coverage=coverage)
    assert len(results) == 1
    assert results[0].event["spike_count"] == 2
    assert results[0].event["peak_price"] == 170


def test_up_spike_without_confirmed_right_low_is_not_a_complete_event() -> None:
    block = merge_daily_candidates([_candidate(0)])[0]
    bars = [
        (0, 100, 100, 100, 100),
        (MINUTE_MS, 100, 150, 98, 120),
        (2 * MINUTE_MS, 120, 130, 115, 125),
    ]
    coverage = assess_coverage(bars, 0, len(bars) * MINUTE_MS)
    config = ScanConfig(scale_window=1, dc_floor_percent=5, dc_cap_percent=20)
    assert analyze_cycles([block], bars, config=config, coverage=coverage) == []


def test_up_spike_before_left_low_confirmation_is_not_complete() -> None:
    block = merge_daily_candidates([_candidate(0)])[0]
    bars = [
        (0, 105, 105, 105, 105),
        (MINUTE_MS, 105, 105, 100, 100),
        (2 * MINUTE_MS, 100, 150, 99, 101),
        (3 * MINUTE_MS, 101, 110, 101, 110),
        (4 * MINUTE_MS, 110, 110, 90, 90),
        (5 * MINUTE_MS, 90, 100, 90, 100),
    ]
    coverage = assess_coverage(bars, 0, len(bars) * MINUTE_MS)
    config = ScanConfig(scale_window=1, dc_k=1, dc_floor_percent=5, dc_cap_percent=5)

    assert analyze_cycles([block], bars, config=config, coverage=coverage) == []


def test_internal_minute_gap_rejects_events() -> None:
    block = merge_daily_candidates([_candidate(0)])[0]
    bars = [
        (0, 100, 100, 100, 100),
        (MINUTE_MS, 100, 110, 100, 110),
        (3 * MINUTE_MS, 110, 160, 105, 120),
        (4 * MINUTE_MS, 120, 120, 80, 80),
        (5 * MINUTE_MS, 80, 100, 80, 100),
    ]
    coverage = assess_coverage(bars, 0, 6 * MINUTE_MS)

    assert coverage.status == "incomplete_gap"
    assert analyze_cycles([block], bars, coverage=coverage) == []


def test_default_coverage_path_does_not_hide_duplicate_minutes() -> None:
    block = merge_daily_candidates([_candidate(0)])[0]
    bars = [(index * MINUTE_MS, 100, 100, 100, 100) for index in range(1440)]
    bars[:6] = [
        (0, 110, 110, 110, 110),
        (MINUTE_MS, 100, 100, 100, 100),
        (2 * MINUTE_MS, 100, 106, 100, 106),
        (3 * MINUTE_MS, 106, 150, 105, 120),
        (4 * MINUTE_MS, 120, 120, 90, 90),
        (5 * MINUTE_MS, 90, 100, 90, 100),
    ]
    bars.insert(4, bars[3])
    config = ScanConfig(
        max_context_days=0,
        scale_window=1,
        dc_k=1,
        dc_floor_percent=5,
        dc_cap_percent=5,
    )

    assert analyze_cycles([block], bars, config=config) == []


def test_partial_archive_boundary_allows_only_internal_resolved_event() -> None:
    block = merge_daily_candidates([_candidate(0)])[0]
    bars = [
        (0, 110, 110, 110, 110),
        (MINUTE_MS, 100, 100, 100, 100),
        (2 * MINUTE_MS, 100, 106, 100, 106),
        (3 * MINUTE_MS, 106, 150, 105, 120),
        (4 * MINUTE_MS, 120, 120, 90, 90),
        (5 * MINUTE_MS, 90, 100, 90, 100),
    ]
    coverage = assess_coverage(
        bars,
        -MINUTE_MS,
        7 * MINUTE_MS,
        archive_start_ms=0,
        archive_end_ms=6 * MINUTE_MS,
    )
    config = ScanConfig(
        scale_window=1,
        dc_k=1,
        dc_floor_percent=5,
        dc_cap_percent=5,
        spike_k=1,
    )

    results = analyze_cycles([block], bars, config=config, coverage=coverage)

    assert coverage.status == "partial_archive_boundary"
    assert len(results) == 1
    assert results[0].event["analysis_status"] == "resolved"


def test_non_up_spike_cycles_are_not_emitted() -> None:
    block = merge_daily_candidates([_candidate(0)])[0]
    closes = [100, 120, 90, 100]
    bars = [(index * MINUTE_MS, close, close, close, close) for index, close in enumerate(closes)]
    coverage = assess_coverage(bars, 0, len(bars) * MINUTE_MS)
    assert analyze_cycles([block], bars, coverage=coverage) == []
