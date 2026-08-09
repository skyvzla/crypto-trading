#!/usr/bin/env python3
"""单交易对参数扇出回测入口。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from trading_platform.backtest.engine import BacktestEngine, Event
from trading_platform.backtest.loader import BacktestDataLoader
from trading_platform.backtest.run_spike_short import (
    SpikeBacktestSettings,
    create_spike_engine,
    parse_args as parse_run_args,
    resolve_settings,
    save_backtest_result,
)
from trading_platform.shared.events import Bar1s, Kline


@dataclass(frozen=True)
class SymbolRunPlan:
    run_id: str
    args: argparse.Namespace
    settings: SpikeBacktestSettings
    engine: BacktestEngine


def _event_matches_plan(event: Event, plan: SymbolRunPlan) -> bool:
    if isinstance(event, Bar1s):
        return event.timestamp >= plan.settings.load_start_ms
    return (
        event.close_time >= plan.settings.load_start_ms
        and event.interval in plan.settings.required_kline_intervals
    )


def _run_shift_group(plans: list[SymbolRunPlan]) -> set[str]:
    first = plans[0]
    args = first.args
    settings = first.settings
    load_start_ms = min(plan.settings.load_start_ms for plan in plans)
    required_intervals = sorted({
        interval
        for plan in plans
        for interval in plan.settings.required_kline_intervals
    })
    loader = BacktestDataLoader(
        data_dir=settings.data_dir,
        symbols=[args.symbol],
        start_ms=load_start_ms,
        end_ms=settings.end_ms,
        require_aggtrades=True,
        required_kline_intervals=required_intervals,
        duckdb_path=args.duckdb_path,
        archive_index_path=args.archive_index,
        bar1s_time_shift_ms=settings.bar1s_time_shift_ms,
    )
    events = loader.iter_all(
        chunk_hours=args.chunk_hours,
        fetch_batch_size=args.fetch_batch_size,
        duckdb_memory_limit=args.duckdb_memory_limit,
        duckdb_threads=args.duckdb_threads,
    )
    counts = {plan.run_id: 0 for plan in plans}
    failed: set[str] = set()
    shared_event_count = 0
    for event in events:
        shared_event_count += 1
        for plan in plans:
            if plan.run_id in failed or not _event_matches_plan(event, plan):
                continue
            try:
                plan.engine.process_event(event)
                counts[plan.run_id] += 1
            except Exception as error:
                failed.add(plan.run_id)
                print(
                    f"Error: {plan.run_id} 处理行情失败：{error}",
                    file=sys.stderr,
                    flush=True,
                )
        if shared_event_count % 100_000 == 0:
            print(
                f"{args.symbol} 已读取 {shared_event_count} 个共享行情事件，"
                f"参数实例={len(plans)}",
                flush=True,
            )

    for plan in plans:
        if plan.run_id in failed:
            continue
        if counts[plan.run_id] == 0:
            print(
                f"Error: {plan.run_id} 在请求区间内没有行情事件",
                file=sys.stderr,
                flush=True,
            )
            failed.add(plan.run_id)
            continue
        try:
            result = plan.engine.finish()
            save_backtest_result(result, plan.settings.output_path)
        except Exception as error:
            failed.add(plan.run_id)
            print(
                f"Error: {plan.run_id} 保存结果失败：{error}",
                file=sys.stderr,
                flush=True,
            )
    print(
        f"{args.symbol} 行情读取完成：共享事件={shared_event_count}，"
        f"参数实例={len(plans)}，失败={len(failed)}",
        flush=True,
    )
    return failed


def run_symbol_task(task: dict[str, Any]) -> int:
    plans_by_shift: dict[int, list[SymbolRunPlan]] = {}
    for item in task.get("runs", []):
        args = parse_run_args(item["arguments"])
        settings = resolve_settings(args)
        plan = SymbolRunPlan(
            run_id=str(item["run_id"]),
            args=args,
            settings=settings,
            engine=create_spike_engine(args, settings, events=()),
        )
        plans_by_shift.setdefault(settings.bar1s_time_shift_ms, []).append(plan)
    if not plans_by_shift:
        raise ValueError("symbol task must contain at least one run")

    failed: set[str] = set()
    for plans in plans_by_shift.values():
        failed.update(_run_shift_group(plans))
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单交易对多参数共享行情回测")
    parser.add_argument("--task", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        task = json.loads(args.task.read_text(encoding="utf-8"))
        return run_symbol_task(task)
    except KeyboardInterrupt:
        print("交易对回测已停止。", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
