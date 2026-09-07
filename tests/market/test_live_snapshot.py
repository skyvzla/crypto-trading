from decimal import Decimal

import pyarrow.parquet as pq
import pyarrow as pa
import pytest

from trading_platform.market.live_snapshot import (
    LiveSnapshotStore,
    read_campaign_snapshot_candles,
)
from trading_platform.shared.events import Bar1s


def _bar(
    timestamp: int,
    *,
    symbol: str = "BTCUSDT",
    aggregate_id: int | None = None,
    last_aggregate_id: int | None = None,
    price: str = "100",
) -> Bar1s:
    value = Decimal(price)
    return Bar1s(
        symbol=symbol,
        timestamp=timestamp,
        available_time=timestamp + 1_000,
        open=value,
        high=value + 1,
        low=value - 1,
        close=value + Decimal("0.5"),
        volume=Decimal("2"),
        trade_count=2,
        vwap=value,
        quote_volume=Decimal("200"),
        raw_trade_count=3,
        taker_buy_volume=Decimal("1.25"),
        taker_sell_volume=Decimal("0.75"),
        taker_buy_quote_volume=Decimal("125"),
        taker_sell_quote_volume=Decimal("75"),
        taker_buy_trade_count=2,
        taker_sell_trade_count=1,
        taker_buy_agg_trade_count=1,
        taker_sell_agg_trade_count=1,
        max_agg_trade_quantity=Decimal("1.5"),
        max_taker_buy_agg_trade_quantity=Decimal("1.25"),
        max_taker_sell_agg_trade_quantity=Decimal("0.75"),
        first_aggregate_trade_id=aggregate_id,
        last_aggregate_trade_id=(
            None
            if aggregate_id is None
            else (
                aggregate_id
                if last_aggregate_id is None
                else last_aggregate_id
            )
        ),
        first_trade_id=(None if aggregate_id is None else aggregate_id * 10),
        last_trade_id=(None if aggregate_id is None else aggregate_id * 10 + 2),
    )


def test_signal_window_publishes_one_zstd_parquet_and_preserves_fields(tmp_path):
    published = []
    store = LiveSnapshotStore(
        tmp_path,
        pre_window_ms=2_000,
        post_window_ms=3_000,
        on_manifest=published.append,
    )

    for timestamp in range(0, 6_000, 1_000):
        store.observe_bar(_bar(timestamp, aggregate_id=timestamp // 1_000 + 100))
    capture = store.start_snapshot(
        "signal-1",
        "btcusdt",
        5_000,
        campaign_id="campaign-1",
        metadata={"release_hash": "abc"},
    )
    assert capture.start_time_ms == 3_000
    assert capture.due_time_ms == 8_000
    assert sorted(capture.bars) == [3_000, 4_000, 5_000]

    assert store.observe_bar(_bar(6_000, aggregate_id=106), now_ms=7_000) == ()
    manifests = store.observe_bar(_bar(7_000, aggregate_id=107), now_ms=8_000)

    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest is published[0]
    assert manifest.symbol == "BTCUSDT"
    assert manifest.row_count == 5
    assert manifest.complete is True
    assert manifest.continuity_ok is True
    assert manifest.relative_path == "BTCUSDT/signal-1.parquet"
    assert manifest.path == tmp_path.resolve() / "BTCUSDT/signal-1.parquet"
    assert len(manifest.sha256) == 64
    assert not store.active_snapshot_ids

    parquet_file = pq.ParquetFile(manifest.path)
    assert parquet_file.metadata.row_group(0).column(0).compression == "ZSTD"
    assert parquet_file.schema_arrow.names == [
        "symbol",
        "timeframe",
        "timestamp",
        "available_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "trade_count",
        "vwap",
        "quote_volume",
        "raw_trade_count",
        "taker_buy_volume",
        "taker_sell_volume",
        "taker_buy_quote_volume",
        "taker_sell_quote_volume",
        "taker_buy_trade_count",
        "taker_sell_trade_count",
        "taker_buy_agg_trade_count",
        "taker_sell_agg_trade_count",
        "max_agg_trade_quantity",
        "max_taker_buy_agg_trade_quantity",
        "max_taker_sell_agg_trade_quantity",
        "first_aggregate_trade_id",
        "last_aggregate_trade_id",
        "first_trade_id",
        "last_trade_id",
    ]

    rows = store.read_snapshot(manifest)
    assert [row["timestamp"] for row in rows] == [3_000, 4_000, 5_000, 6_000, 7_000]
    assert rows[0]["taker_buy_quote_volume"] == 125
    assert rows[0]["taker_sell_quote_volume"] == 75
    assert rows[0]["max_taker_buy_agg_trade_quantity"] == 1.25
    assert store.read_bars(manifest)[0].quote_volume == Decimal("200")
    assert store.read_bars(manifest)[0].taker_sell_volume == Decimal("0.75")


def test_early_close_is_durable_but_incomplete(tmp_path):
    store = LiveSnapshotStore(tmp_path, pre_window_ms=1_000, post_window_ms=5_000)
    store.observe_bar(_bar(10_000, aggregate_id=1))
    store.start_snapshot("early", "BTCUSDT", 10_000)
    store.observe_bar(_bar(11_000, aggregate_id=2), now_ms=12_000)

    [manifest] = store.close(now_ms=12_000)
    assert manifest.complete is False
    assert manifest.end_time_ms == 12_000
    assert manifest.row_count == 2
    assert manifest.boundary_missing_seconds == (9_000,)
    assert manifest.missing_seconds == ()
    assert manifest.continuity_ok is True
    assert manifest.coverage_status == "incomplete"


def test_no_trade_second_is_empty_not_a_stream_gap(tmp_path):
    store = LiveSnapshotStore(tmp_path)
    manifest = store.write_snapshot(
        "empty-second",
        [
            _bar(1_000, aggregate_id=10, last_aggregate_id=11),
            _bar(3_000, aggregate_id=12),
        ],
        symbol="BTCUSDT",
        start_time_ms=1_000,
        end_time_ms=4_000,
    )

    assert manifest.empty_seconds == (2_000,)
    assert manifest.missing_seconds == ()
    assert manifest.boundary_missing_seconds == ()
    assert manifest.agg_trade_gaps == ()
    assert manifest.continuity_ok is True
    assert manifest.gaps == []
    assert manifest.coverage["coverage_expected"] == 3
    assert manifest.coverage["coverage_received"] == 3
    assert manifest.coverage_status == "complete"


def test_manifest_reports_second_and_aggregate_trade_gaps(tmp_path):
    store = LiveSnapshotStore(tmp_path)
    manifest = store.write_snapshot(
        "gaps",
        [
            _bar(1_000, aggregate_id=10, last_aggregate_id=11),
            _bar(3_000, aggregate_id=20),
        ],
        symbol="BTCUSDT",
        start_time_ms=1_000,
        end_time_ms=4_000,
    )

    assert manifest.missing_seconds == (2_000,)
    assert manifest.empty_seconds == ()
    assert manifest.agg_trade_gaps == ((12, 19),)
    assert manifest.continuity_ok is False
    assert manifest.coverage_status == "gapped"
    assert manifest.first_aggregate_trade_id == 10
    assert manifest.last_aggregate_trade_id == 20


@pytest.mark.parametrize(
    ("previous_ids", "current_ids", "expected_ids"),
    [
        ((10, 11), (11, 12), (12, 11)),
        ((10, 11), (8, 9), (12, 8)),
        ((10, 11), (10, 11), (12, 10)),
    ],
    ids=("partial-overlap", "regression", "duplicate-range"),
)
def test_manifest_reports_aggregate_trade_id_integrity_gaps(
    tmp_path, previous_ids, current_ids, expected_ids
):
    store = LiveSnapshotStore(tmp_path)
    manifest = store.write_snapshot(
        f"integrity-{previous_ids[0]}-{current_ids[0]}",
        [
            _bar(
                1_000,
                aggregate_id=previous_ids[0],
                last_aggregate_id=previous_ids[1],
            ),
            _bar(
                2_000,
                aggregate_id=current_ids[0],
                last_aggregate_id=current_ids[1],
            ),
        ],
        symbol="BTCUSDT",
        start_time_ms=1_000,
        end_time_ms=3_000,
    )

    assert manifest.agg_trade_gaps == ()
    assert manifest.agg_trade_non_monotonic == (expected_ids,)
    assert manifest.gaps == [
        {
            "type": "aggregate_trade_non_monotonic",
            "expected_first_id": expected_ids[0],
            "actual_first_id": expected_ids[1],
        }
    ]
    assert manifest.continuity_ok is False
    assert manifest.coverage_status == "gapped"


def test_manifest_non_monotonic_gaps_round_trip_and_old_outbox_is_compatible(
    tmp_path,
):
    store = LiveSnapshotStore(tmp_path)
    manifest = store.write_snapshot(
        "non-monotonic-round-trip",
        [
            _bar(1_000, aggregate_id=10, last_aggregate_id=11),
            _bar(2_000, aggregate_id=11, last_aggregate_id=12),
        ],
        symbol="BTCUSDT",
    )

    restored = type(manifest).from_dict(manifest.to_dict(), root=tmp_path)
    assert restored.agg_trade_non_monotonic == ((12, 11),)
    legacy = manifest.to_dict()
    legacy.pop("agg_trade_non_monotonic")
    assert type(manifest).from_dict(
        legacy, root=tmp_path
    ).agg_trade_non_monotonic == ()


def test_decimal_precision_round_trips_without_float_storage(tmp_path):
    store = LiveSnapshotStore(tmp_path)
    original = _bar(1_000, price="123.123456789012345678")
    manifest = store.write_snapshot("decimal", [original], symbol="BTCUSDT")

    [restored] = store.read_bars(manifest)
    assert restored.close == original.close
    assert restored.open == original.open
    parquet_file = pq.ParquetFile(manifest.path)
    assert parquet_file.schema_arrow.field("close").type == pa.decimal128(38, 18)


def test_manifest_outbox_survives_restart_and_validates_payload_hash(tmp_path):
    store = LiveSnapshotStore(tmp_path)
    manifest = store.write_snapshot("outbox", [_bar(1_000)], symbol="BTCUSDT")
    assert [item.snapshot_id for item in store.pending_manifests()] == ["outbox"]

    restarted = LiveSnapshotStore(tmp_path)
    assert [item.snapshot_id for item in restarted.pending_manifests()] == ["outbox"]
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        restarted.ack_manifest("outbox", sha256="0" * 64)
    assert (tmp_path / ".outbox" / "outbox.json").is_file()

    restarted.ack_manifest(manifest)
    assert restarted.pending_manifests() == ()
    assert not (tmp_path / ".outbox" / "outbox.json").exists()


def test_snapshot_rejects_duplicate_rows_and_path_traversal(tmp_path):
    store = LiveSnapshotStore(tmp_path)
    with pytest.raises(ValueError, match="duplicate timestamp"):
        store.write_snapshot(
            "duplicate",
            [_bar(0), _bar(0)],
            symbol="BTCUSDT",
        )
    with pytest.raises(ValueError, match="path separators and traversal"):
        store.write_snapshot("../escape", [_bar(0)], symbol="BTCUSDT")
    with pytest.raises(ValueError, match="path separators"):
        store.write_snapshot("bad", [_bar(0, symbol="BTC/USDT")])


def test_staging_wal_recovers_after_restart_and_is_removed_after_publish(tmp_path):
    first = LiveSnapshotStore(tmp_path, pre_window_ms=1_000, post_window_ms=3_000)
    first.observe_bar(_bar(5_000, aggregate_id=5))
    first.start_snapshot("recover", "BTCUSDT", 5_000)
    first.observe_bar(_bar(6_000, aggregate_id=6), now_ms=6_000)
    wal = tmp_path / ".staging" / "recover.jsonl"
    assert wal.is_file()

    second = LiveSnapshotStore(tmp_path, pre_window_ms=1_000, post_window_ms=3_000)
    assert second.active_snapshot_ids == ("recover",)
    second.observe_bar(_bar(7_000, aggregate_id=7), now_ms=7_000)
    [manifest] = second.observe_bar(_bar(8_000, aggregate_id=8), now_ms=8_000)
    assert manifest.row_count == 3
    assert not wal.exists()


def test_restart_rebuilds_outbox_from_payload_published_before_outbox(
    tmp_path, monkeypatch
):
    first = LiveSnapshotStore(tmp_path, pre_window_ms=0, post_window_ms=1_000)
    first.observe_bar(_bar(1_000, aggregate_id=10))
    first.start_snapshot("rename-crash", "BTCUSDT", 1_000)
    wal = tmp_path / ".staging" / "rename-crash.jsonl"
    payload = tmp_path / "BTCUSDT" / "rename-crash.parquet"

    def fail_outbox(_manifest):
        raise OSError("injected failure before outbox publication")

    monkeypatch.setattr(first, "_write_outbox", fail_outbox)
    with pytest.raises(OSError, match="injected failure"):
        first.flush_due(now_ms=2_000)

    assert payload.is_file()
    assert wal.is_file()
    assert first.active_snapshot_ids == ("rename-crash",)
    original_payload = payload.read_bytes()

    restarted = LiveSnapshotStore(tmp_path, pre_window_ms=0, post_window_ms=1_000)
    [manifest] = restarted.recover_staging(2_000, finalize_expired=True)

    assert payload.read_bytes() == original_payload
    assert manifest.path == payload
    assert manifest.complete is True
    assert manifest.post_window_covered is True
    assert [item.snapshot_id for item in restarted.pending_manifests()] == [
        "rename-crash"
    ]
    assert not wal.exists()


def test_finalize_retries_existing_payload_after_durable_outbox_callback_failure(
    tmp_path,
):
    attempts = 0

    def fail_once(_manifest):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("injected callback failure")

    store = LiveSnapshotStore(
        tmp_path,
        pre_window_ms=0,
        post_window_ms=1_000,
        on_manifest=fail_once,
    )
    store.observe_bar(_bar(1_000, aggregate_id=10))
    store.start_snapshot("callback-crash", "BTCUSDT", 1_000)
    wal = tmp_path / ".staging" / "callback-crash.jsonl"

    with pytest.raises(RuntimeError, match="injected callback failure"):
        store.flush_due(now_ms=2_000)
    assert wal.is_file()
    assert [item.snapshot_id for item in store.pending_manifests()] == [
        "callback-crash"
    ]

    [manifest] = store.flush_due(now_ms=2_000)
    assert manifest.snapshot_id == "callback-crash"
    assert attempts == 2
    assert not wal.exists()


def test_restart_immediately_resumes_early_finalize_with_original_state(
    tmp_path, monkeypatch
):
    first = LiveSnapshotStore(tmp_path, pre_window_ms=0, post_window_ms=5_000)
    first.observe_bar(_bar(1_000, aggregate_id=10))
    first.start_snapshot("early-finalize", "BTCUSDT", 1_000)
    wal = tmp_path / ".staging" / "early-finalize.jsonl"

    def fail_outbox(_manifest):
        raise OSError("injected failure before outbox publication")

    monkeypatch.setattr(first, "_write_outbox", fail_outbox)
    with pytest.raises(OSError, match="injected failure"):
        first.close(now_ms=2_000)

    restarted = LiveSnapshotStore(tmp_path, pre_window_ms=0, post_window_ms=5_000)
    [manifest] = restarted.recover_staging(2_000, finalize_expired=True)

    assert manifest.complete is False
    assert manifest.end_time_ms == 2_000
    assert manifest.post_window_covered is False
    assert not wal.exists()


def test_recovery_rejects_existing_payload_that_conflicts_with_wal(
    tmp_path, monkeypatch
):
    first = LiveSnapshotStore(tmp_path, pre_window_ms=0, post_window_ms=1_000)
    first.observe_bar(_bar(1_000, aggregate_id=10))
    first.start_snapshot("payload-conflict", "BTCUSDT", 1_000)
    wal = tmp_path / ".staging" / "payload-conflict.jsonl"
    payload = tmp_path / "BTCUSDT" / "payload-conflict.parquet"

    def fail_outbox(_manifest):
        raise OSError("injected failure")

    monkeypatch.setattr(first, "_write_outbox", fail_outbox)
    with pytest.raises(OSError, match="injected failure"):
        first.flush_due(now_ms=2_000)

    table = pq.read_table(payload)
    changed = table.set_column(
        table.schema.get_field_index("close"),
        "close",
        pa.array([Decimal("999")], type=table.schema.field("close").type),
    )
    pq.write_table(changed, payload)

    restarted = LiveSnapshotStore(tmp_path, pre_window_ms=0, post_window_ms=1_000)
    with pytest.raises(ValueError, match="conflicts with recovered WAL"):
        restarted.recover_staging(2_000, finalize_expired=True)
    assert wal.is_file()
    assert restarted.pending_manifests() == ()


def test_campaign_reader_returns_extended_1s_api_rows(tmp_path):
    store = LiveSnapshotStore(tmp_path)
    manifest = store.write_snapshot(
        "api",
        [_bar(1_000, aggregate_id=10)],
        symbol="BTCUSDT",
    )
    [row] = read_campaign_snapshot_candles(
        manifest.path,
        symbol="btcusdt",
        interval="1s",
        start_ms=1_000,
        end_ms=2_000,
    )
    assert row["time"] == 1
    assert row["quote_volume"] == 200.0
    assert row["taker_buy_quote_volume"] == 125.0
    assert isinstance(row["close"], float)
    with pytest.raises(ValueError, match="only support"):
        read_campaign_snapshot_candles(manifest.path, symbol="BTCUSDT", interval="1m")
