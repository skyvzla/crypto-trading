#!/usr/bin/env python3
"""Scan annual cross-day amplitude cycles from the local candle archive only."""

from __future__ import annotations

import argparse
import csv
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from trading_platform.market.archive.index import (
    ARCHIVE_INDEX_FILENAME,
    load_archive_index,
    verify_archive_index_files,
)
from trading_platform.research.amplitude_cycles import (
    CandidateBlock,
    DailyCandidate,
    ScanConfig,
    analyze_cycles,
    assess_coverage,
    expanded_window,
    merge_daily_candidates,
)

DAY_MS = 86_400_000


def _date_ms(value: str, *, exclusive_end: bool = False) -> int:
    result = int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return result + (DAY_MS if exclusive_end else 0)


def _symbols(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {item.strip().upper() for item in value.split(",") if item.strip()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="1d initial screen and continuous 1m amplitude-cycle scan")
    parser.add_argument("--start-date", required=True, help="inclusive UTC date, e.g. 2025-01-01")
    parser.add_argument("--end-date", required=True, help="inclusive UTC date")
    parser.add_argument("--symbols", help="comma-separated symbols; default scans all archived symbols")
    parser.add_argument("--archive-index", type=Path, default=Path("data/market/candles") / ARCHIVE_INDEX_FILENAME)
    parser.add_argument("--output-dir", type=Path, default=Path("reports/amplitude_cycles"))
    parser.add_argument("--workers", type=int, default=13)
    parser.add_argument("--duckdb-threads", type=int, default=1)
    parser.add_argument("--candidate-threshold-percent", type=float, default=15.0)
    parser.add_argument("--candidate-gap-days", type=int, default=1)
    parser.add_argument("--expand-before-days", type=int, default=2)
    parser.add_argument("--expand-after-days", type=int, default=3)
    parser.add_argument("--spike-threshold-percent", type=float, default=15.0)
    parser.add_argument("--violent-rise-percent", type=float, default=50.0)
    parser.add_argument("--crash-percent", type=float, default=30.0)
    parser.add_argument("--post-spike-window-minutes", type=int, default=360)
    parser.add_argument("--scale-window", type=int, default=60)
    parser.add_argument("--dc-k", type=float, default=6.0)
    parser.add_argument("--dc-floor-percent", type=float, default=2.0)
    parser.add_argument("--dc-cap-percent", type=float, default=30.0)
    parser.add_argument("--spike-k", type=float, default=8.0)
    parser.add_argument("--max-context-days", type=int, default=14)
    return parser.parse_args()


def _query(paths: list[str], sql: str, params: list[Any], threads: int) -> list[tuple[Any, ...]]:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(f"SET threads={int(threads)}")
        connection.execute("SET TimeZone='UTC'")
        return connection.execute(sql, [paths, *params]).fetchall()
    finally:
        connection.close()


def _daily_candidates(index: Any, root: Path, start_ms: int, end_ms: int, selected_symbols: set[str] | None, config: ScanConfig, threads: int) -> list[DailyCandidate]:
    parts = index[index["timeframe"].eq("1d")]
    if selected_symbols is not None:
        parts = parts[parts["symbol"].isin(selected_symbols)]
    warmup_start_ms = start_ms - 6 * DAY_MS
    parts = parts[(parts["last_close_ms"] >= warmup_start_ms) & (parts["first_open_ms"] < end_ms)].drop_duplicates("relative_path")
    if parts.empty:
        return []
    paths = [str(root / item) for item in parts["relative_path"]]
    rows = _query(paths, """
        WITH daily AS (
          SELECT symbol, open_time, close_time, open, high, low, close
          FROM read_parquet(?, union_by_name=true)
          WHERE timeframe='1d' AND epoch_ms(open_time)>=? AND epoch_ms(open_time)<?
        ), scored AS (
          SELECT *,
            ((high-low)/NULLIF(low,0))*100 AS range_1d,
            ((max(high) OVER w3-min(low) OVER w3)/NULLIF(min(low) OVER w3,0))*100 AS range_3d,
            ((max(high) OVER w7-min(low) OVER w7)/NULLIF(min(low) OVER w7,0))*100 AS range_7d,
            abs((close/NULLIF(first_value(open) OVER w3,0)-1)*100) AS return_3d,
            abs((close/NULLIF(first_value(open) OVER w7,0)-1)*100) AS return_7d
          FROM daily
          WINDOW w3 AS (PARTITION BY symbol ORDER BY open_time ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
                 w7 AS (PARTITION BY symbol ORDER BY open_time ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
        ), eligible AS (
          SELECT *,
            count(*) OVER w3 AS count_3d,
            count(*) OVER w7 AS count_7d,
            epoch_ms(open_time)-epoch_ms(first_value(open_time) OVER w3) AS span_3d_ms,
            epoch_ms(open_time)-epoch_ms(first_value(open_time) OVER w7) AS span_7d_ms
          FROM scored
          WINDOW w3 AS (PARTITION BY symbol ORDER BY open_time ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
                 w7 AS (PARTITION BY symbol ORDER BY open_time ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
        ), candidates AS (
          SELECT *, greatest(
            range_1d,
            CASE WHEN count_3d=3 AND span_3d_ms=? THEN range_3d END,
            CASE WHEN count_7d=7 AND span_7d_ms=? THEN range_7d END,
            CASE WHEN count_3d=3 AND span_3d_ms=? THEN return_3d END,
            CASE WHEN count_7d=7 AND span_7d_ms=? THEN return_7d END
          ) AS candidate_score
          FROM eligible
        )
        SELECT symbol, epoch_ms(open_time), epoch_ms(close_time), open, high, low, close,
               range_1d, candidate_score
        FROM candidates
        WHERE epoch_ms(open_time)>=? AND candidate_score>=?
        ORDER BY symbol, open_time
    """, [
        start_ms - 6 * DAY_MS,
        end_ms,
        2 * DAY_MS,
        6 * DAY_MS,
        2 * DAY_MS,
        6 * DAY_MS,
        start_ms,
        config.candidate_threshold_percent,
    ], threads)
    return [
        DailyCandidate(
            symbol=str(symbol),
            open_time_ms=int(open_ms),
            open=float(opened),
            high=float(high),
            low=float(low),
            close=float(close),
            close_time_ms=int(close_ms),
            amplitude_percent=float(range_1d),
            candidate_score_percent=float(candidate_score),
        )
        for symbol, open_ms, close_ms, opened, high, low, close, range_1d, candidate_score in rows
    ]


def _scan_block(payload: tuple[list[CandidateBlock], list[str], int, int, int, dict[str, Any]]) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    blocks, paths, archive_start, archive_end, threads, config_values = payload
    block = blocks[0]
    config = ScanConfig(**config_values)
    requested_start = min(expanded_window(item, before_days=config.max_context_days, after_days=config.max_context_days)[0] for item in blocks)
    requested_end = max(expanded_window(item, before_days=config.max_context_days, after_days=config.max_context_days)[1] for item in blocks)
    try:
        rows = _query(paths, """
            SELECT epoch_ms(open_time), open, high, low, close
            FROM read_parquet(?, union_by_name=true)
            WHERE symbol=? AND timeframe='1m' AND epoch_ms(open_time)>=? AND epoch_ms(open_time)<?
            ORDER BY open_time
        """, [block.symbol, requested_start, requested_end], threads)
        bars = [(int(t), float(o), float(h), float(l), float(c)) for t, o, h, l, c in rows]
        coverage = assess_coverage(bars, requested_start, requested_end, archive_start_ms=archive_start, archive_end_ms=archive_end)
        locator = datetime.fromtimestamp(block.start_ms / 1000, tz=timezone.utc).isoformat()
        if not coverage.analysis_allowed:
            return [], [], [{"event_id": "", "symbol": block.symbol, "block_start_utc": locator, "stage": "coverage", "reason": coverage.status}]
        by_day: dict[int, list[tuple[int, float, float, float, float]]] = {}
        for bar in bars:
            by_day.setdefault(bar[0] // DAY_MS * DAY_MS, []).append(bar)
        valid_blocks: list[CandidateBlock] = []
        consistency_failures: list[dict[str, object]] = []
        for candidate_block in blocks:
            valid_candidates: list[DailyCandidate] = []
            for candidate in candidate_block.candidates:
                day_bars = by_day.get(candidate.open_time_ms, [])
                if len(day_bars) != 1440:
                    consistency_failures.append({
                        "event_id": "",
                        "symbol": block.symbol,
                        "block_start_utc": locator,
                        "stage": "consistency",
                        "reason": f"daily_1m_incomplete:{candidate.open_time_ms}:{len(day_bars)}",
                    })
                    continue
                minute_ohlc = (day_bars[0][1], max(item[2] for item in day_bars), min(item[3] for item in day_bars), day_bars[-1][4])
                daily_ohlc = (candidate.open, candidate.high, candidate.low, candidate.close)
                if any(abs(left - right) > max(abs(right) * 1e-8, 1e-12) for left, right in zip(minute_ohlc, daily_ohlc)):
                    consistency_failures.append({
                        "event_id": "",
                        "symbol": block.symbol,
                        "block_start_utc": locator,
                        "stage": "consistency",
                        "reason": f"daily_1m_ohlc_mismatch:{candidate.open_time_ms}",
                    })
                    continue
                valid_candidates.append(candidate)
            if valid_candidates:
                valid_blocks.append(CandidateBlock(
                    symbol=candidate_block.symbol,
                    start_ms=valid_candidates[0].open_time_ms,
                    end_ms=max(candidate.end_time_ms for candidate in valid_candidates),
                    candidates=tuple(valid_candidates),
                ))
        results = analyze_cycles(valid_blocks, bars, config=config, coverage=coverage)
        if not results:
            return [], [], consistency_failures
        failures = [{"event_id": result.event["event_id"], "symbol": block.symbol, "block_start_utc": result.event.get("candidate_start_utc", ""), "stage": "boundary", "reason": result.event["analysis_status"]} for result in results if result.event["analysis_status"] != "resolved"]
        failures.extend(consistency_failures)
        return [result.event for result in results], [node for result in results for node in result.nodes], failures
    except Exception as error:
        return [], [], [{"event_id": "", "symbol": block.symbol, "block_start_utc": str(block.start_ms), "stage": "minute_analysis", "reason": f"{type(error).__name__}: {error}"}]


def _read_groups(blocks: list[CandidateBlock], config: ScanConfig) -> list[list[CandidateBlock]]:
    groups: list[list[CandidateBlock]] = []
    for block in sorted(blocks, key=lambda item: (item.symbol, item.start_ms)):
        start, _ = expanded_window(block, before_days=config.max_context_days, after_days=config.max_context_days)
        if groups and groups[-1][0].symbol == block.symbol:
            previous_end = max(expanded_window(item, before_days=config.max_context_days, after_days=config.max_context_days)[1] for item in groups[-1])
            if start <= previous_end:
                groups[-1].append(block)
                continue
        groups.append([block])
    return groups


def _write_csv(path: Path, rows: list[dict[str, object]], default_fields: list[str]) -> None:
    fields = list(default_fields)
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    if args.workers <= 0 or args.duckdb_threads <= 0:
        raise SystemExit("workers and duckdb-threads must be positive")
    start_ms, end_ms = _date_ms(args.start_date), _date_ms(args.end_date, exclusive_end=True)
    if start_ms >= end_ms:
        raise SystemExit("start-date must not be after end-date")
    index_path = args.archive_index.resolve()
    index = load_archive_index(index_path)
    root = index_path.parent
    selected = _symbols(args.symbols)
    relevant = index[index["timeframe"].isin(["1d", "1m"])]
    if selected is not None:
        relevant = relevant[relevant["symbol"].isin(selected)]
    verify_archive_index_files(relevant.drop_duplicates("relative_path"), root)
    config = ScanConfig(
        candidate_threshold_percent=args.candidate_threshold_percent,
        candidate_gap_days=args.candidate_gap_days,
        expand_before_days=args.expand_before_days,
        expand_after_days=args.expand_after_days,
        spike_threshold_percent=args.spike_threshold_percent,
        violent_rise_percent=args.violent_rise_percent,
        crash_percent=args.crash_percent,
        post_spike_window_minutes=args.post_spike_window_minutes,
        scale_window=args.scale_window,
        dc_k=args.dc_k,
        dc_floor_percent=args.dc_floor_percent,
        dc_cap_percent=args.dc_cap_percent,
        spike_k=args.spike_k,
        max_context_days=args.max_context_days,
    )
    candidates = _daily_candidates(index, root, start_ms, end_ms, selected, config, args.duckdb_threads)
    blocks = merge_daily_candidates(candidates, max_gap_days=config.candidate_gap_days)
    minute = relevant[relevant["timeframe"].eq("1m")]
    payloads = []
    failures: list[dict[str, object]] = []
    groups = _read_groups(blocks, config)
    for group in groups:
        block = group[0]
        requested_start = min(expanded_window(item, before_days=config.max_context_days, after_days=config.max_context_days)[0] for item in group)
        requested_end = max(expanded_window(item, before_days=config.max_context_days, after_days=config.max_context_days)[1] for item in group)
        parts = minute[(minute["symbol"] == block.symbol) & (minute["last_close_ms"] >= requested_start) & (minute["first_open_ms"] < requested_end)].drop_duplicates("relative_path")
        if parts.empty:
            failures.append({"event_id": "", "symbol": block.symbol, "block_start_utc": datetime.fromtimestamp(block.start_ms / 1000, tz=timezone.utc).isoformat(), "stage": "partition_selection", "reason": "missing_1m_partitions"})
            continue
        paths = [str(root / item) for item in parts["relative_path"]]
        payloads.append((group, paths, int(parts["first_open_ms"].min()), int(parts["last_close_ms"].max()) + 1, args.duckdb_threads, asdict(config)))
    events: list[dict[str, object]] = []
    nodes: list[dict[str, object]] = []
    if payloads:
        with ProcessPoolExecutor(max_workers=min(args.workers, len(payloads))) as pool:
            futures = [pool.submit(_scan_block, payload) for payload in payloads]
            for future in as_completed(futures):
                group_events, event_nodes, group_failures = future.result()
                events.extend(group_events)
                nodes.extend(event_nodes)
                failures.extend(group_failures)
    unique_events: dict[str, dict[str, object]] = {}
    for event in events:
        event_id = str(event["event_id"])
        existing = unique_events.setdefault(event_id, event)
        if existing != event:
            failures.append({
                "event_id": event_id,
                "symbol": str(event.get("symbol", "")),
                "block_start_utc": str(event.get("candidate_start_utc", "")),
                "stage": "deduplication",
                "reason": "conflicting_duplicate_event_id",
            })
    events = list(unique_events.values())
    unique_nodes: dict[tuple[str, int], dict[str, object]] = {}
    for node in nodes:
        key = (str(node["event_id"]), int(node["node_index"]))
        existing = unique_nodes.setdefault(key, node)
        if existing != node:
            failures.append({
                "event_id": key[0],
                "symbol": "",
                "block_start_utc": "",
                "stage": "deduplication",
                "reason": f"conflicting_duplicate_node:{key[1]}",
            })
    nodes = list(unique_nodes.values())
    events.sort(key=lambda row: (
        str(row.get("symbol", "")),
        str(row.get("cycle_start_utc", "")),
        str(row.get("peak_utc", "")),
        str(row.get("event_id", "")),
    ))
    nodes.sort(key=lambda row: (str(row["event_id"]), int(row["node_index"])))
    failures.sort(key=lambda row: (str(row["symbol"]), str(row.get("block_start_utc", "")), str(row["stage"]), str(row["reason"])))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(args.output_dir / "events.csv", events, ["event_id", "symbol", "analysis_status", "labels"])
    _write_csv(args.output_dir / "nodes.csv", nodes, ["event_id", "node_index", "node_type", "occurrence_time_utc", "confirmed_time_utc", "price", "direction", "source"])
    _write_csv(args.output_dir / "failures.csv", failures, ["event_id", "symbol", "block_start_utc", "stage", "reason"])
    manifest = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "symbols": sorted(selected) if selected is not None else "all",
        "config": asdict(config),
        "workers": args.workers,
        "duckdb_threads": args.duckdb_threads,
        "candidate_count": len(candidates),
        "candidate_block_count": len(blocks),
        "event_count": len(events),
        "node_count": len(nodes),
        "failure_count": len(failures),
        "read_only_archive": True,
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
