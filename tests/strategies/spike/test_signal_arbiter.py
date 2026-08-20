import pytest

from trading_platform.strategies.spike.signal_arbiter import SignalArbiter


def candidate(arbiter: SignalArbiter, symbol: str, sequence_time: int):
    return arbiter.enqueue(
        symbol=symbol,
        campaign_id=f"spike_short:{symbol}:{sequence_time}",
        signal_time=sequence_time,
        received_at=sequence_time,
    )


def test_fifo_sequence_wins_independent_of_symbol_configuration_order():
    arbiter = SignalArbiter()
    later = candidate(arbiter, "BTCUSDT", 2_000)
    earlier = candidate(arbiter, "ETHUSDT", 1_000)

    assert later.arrival_sequence == 1
    assert earlier.arrival_sequence == 2
    results = arbiter.arbitrate(now_ms=2_000)
    assert [result.status for result in results] == ["acquired", "skipped_overlap"]
    assert results[0].candidate.symbol == "BTCUSDT"


def test_active_campaign_skips_old_candidates_and_next_campaign_can_acquire_after_release():
    arbiter = SignalArbiter()
    first = candidate(arbiter, "BTCUSDT", 1_000)
    second = candidate(arbiter, "ETHUSDT", 1_001)

    results = arbiter.arbitrate(now_ms=1_001)
    assert results[0].candidate == first
    assert results[0].status == "acquired"
    assert results[1].candidate == second
    assert results[1].status == "skipped_overlap"
    arbiter.release(first.campaign_id)

    third = candidate(arbiter, "BNBUSDT", 1_002)
    result = arbiter.arbitrate(now_ms=1_002)
    assert len(result) == 1
    assert result[0].status == "acquired"
    assert result[0].candidate == third


def test_stale_candidate_is_not_promoted_to_a_campaign():
    arbiter = SignalArbiter(stale_after_ms=100)
    candidate(arbiter, "BTCUSDT", 1_000)

    result = arbiter.arbitrate(now_ms=1_101)
    assert len(result) == 1
    assert result[0].status == "skipped_stale"
    assert arbiter.active_campaign_id is None


def test_invalid_future_signal_is_skipped_and_next_candidate_can_win():
    arbiter = SignalArbiter()
    future = arbiter.enqueue(
        symbol="BTCUSDT",
        campaign_id="future",
        signal_time=2_000,
        received_at=1_000,
    )
    valid = candidate(arbiter, "ETHUSDT", 1_000)
    result = arbiter.arbitrate(now_ms=1_000)
    assert [item.status for item in result] == ["skipped_invalid", "acquired"]
    assert result[1].candidate == valid

    with pytest.raises(ValueError, match="active Campaign"):
        arbiter.release(future.campaign_id)
