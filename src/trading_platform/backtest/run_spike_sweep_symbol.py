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
from trading_platform.backtest.strategy_definition import (
    SharedFeatureProvider,
    aggregate_shared_metric_retention,
    normalize_shared_metric_specs,
)
from trading_platform.backtest.run_spike_short import (
    SpikeBacktestSettings,
    create_spike_engine,
    load_metrics_series,
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


@dataclass(frozen=True)
class SharedProviderGroup:
    representative: SymbolRunPlan
    provider: SharedFeatureProvider


def _export_research_trades(plan: SymbolRunPlan) -> None:
    """研究模式：把策略内的逐笔成交明细（含退出原因/MAE）导出到 run 目录。"""
    records = getattr(plan.engine.strategy, "drain_trade_records", None)
    if records is None:
        return
    rows = records()
    if not rows:
        return
    import csv as _csv

    path = plan.settings.output_path / "research_trades.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = _csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _event_matches_plan(event: Event, plan: SymbolRunPlan) -> bool:
    if isinstance(event, Bar1s):
        return (
            plan.settings.requires_bar1s
            and event.timestamp >= plan.settings.load_start_ms
        )
    return (
        event.close_time >= plan.settings.load_start_ms
        and event.interval in plan.settings.required_kline_intervals
    )


def _build_shared_provider_groups(
    plans: list[SymbolRunPlan],
) -> tuple[SharedProviderGroup, ...]:
    buckets: dict[tuple[object, int, tuple[str, ...], bool], list[SymbolRunPlan]] = {}
    for plan in plans:
        definition = plan.settings.strategy_definition
        factory = getattr(definition, "shared_feature_provider", None)
        requirements = definition.data_requirements
        if factory is None:
            if hasattr(requirements, "shared_metrics") and requirements.shared_metrics:
                raise ValueError(
                    f"strategy {definition.name} declares shared metrics without "
                    "shared_feature_provider"
                )
            continue
        try:
            shared_metrics = requirements.shared_metrics
        except AttributeError as error:
            raise ValueError(
                f"strategy {definition.name} has a shared_feature_provider but "
                "does not declare data_requirements.shared_metrics"
            ) from error
        if not (requirements.shared_features or shared_metrics):
            continue
        key = (
            factory,
            plan.settings.load_start_ms,
            plan.settings.required_kline_intervals,
            plan.settings.requires_bar1s,
        )
        buckets.setdefault(key, []).append(plan)

    groups = []
    for bucket in buckets.values():
        shared_features = frozenset().union(*(
            plan.settings.strategy_definition.data_requirements.shared_features
            for plan in bucket
        ))
        metric_consumers = tuple(
            (metric, plan.settings)
            for plan in bucket
            for metric in normalize_shared_metric_specs(
                plan.settings.strategy_definition.data_requirements.shared_metrics
            )
        )
        shared_metrics = normalize_shared_metric_specs(
            metric for metric, _settings in metric_consumers
        )
        provider_factory = getattr(
            bucket[0].settings.strategy_definition,
            "shared_feature_provider",
        )
        retention_values = []
        for plan in bucket:
            plan_definition = plan.settings.strategy_definition
            try:
                resolver = (
                    plan_definition.data_requirements.resolve_retention_minutes
                )
            except AttributeError as error:
                raise ValueError(
                    f"strategy {plan_definition.name} does not declare a "
                    "retention resolver"
                ) from error
            retention_values.append(resolver(plan.settings))
        retention = max(retention_values)
        if shared_metrics:
            retention = max(
                retention,
                max(
                    aggregate_shared_metric_retention(
                        metric_consumers,
                    ).values()
                ),
            )
        provider_kwargs = {
            "shared_features": shared_features,
            "retained_1m_minutes": retention,
        }
        if shared_metrics:
            provider_kwargs["shared_metrics"] = shared_metrics
        provider = provider_factory(**provider_kwargs)
        for plan in bucket:
            provider.bind(plan.engine.strategy)
        groups.append(SharedProviderGroup(bucket[0], provider))
    return tuple(groups)


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
    requires_bar1s = any(plan.settings.requires_bar1s for plan in plans)
    feature_requirements = [
        getattr(
            plan.settings.strategy_definition.data_requirements,
            "bar1s_feature_columns",
            None,
        )
        for plan in plans
    ]
    bar1s_feature_columns = (
        None
        if any(requirement is None for requirement in feature_requirements)
        else frozenset().union(*feature_requirements)
    )
    loader = BacktestDataLoader(
        duckdb_path=settings.duckdb_path,
        symbols=[args.symbol],
        start_ms=load_start_ms,
        end_ms=settings.end_ms,
        require_aggtrades=requires_bar1s,
        required_kline_intervals=required_intervals,
        archive_index_path=args.archive_index,
        bar1s_time_shift_ms=settings.bar1s_time_shift_ms,
        bar1s_feature_columns=bar1s_feature_columns,
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
    provider_groups = _build_shared_provider_groups(plans)
    for event in events:
        shared_event_count += 1
        for group in provider_groups:
            if _event_matches_plan(event, group.representative):
                group.provider.process_event(event)
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
            if getattr(plan.args, "research", False):
                _export_research_trades(plan)
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
    metrics_cache: dict[
        tuple[str, str], list[tuple[int, float, float]]
    ] = {}
    for item in task.get("runs", []):
        args = parse_run_args(item["arguments"])
        settings = resolve_settings(args)
        preloaded_metrics_series = None
        if settings.strategy_definition.data_requirements.metrics_5m:
            metrics_key = (str(args.metrics_root), args.symbol)
            if metrics_key not in metrics_cache:
                metrics_cache[metrics_key] = load_metrics_series(
                    args.metrics_root, args.symbol
                )
            preloaded_metrics_series = metrics_cache[metrics_key]
        engine = (
            create_spike_engine(
                args,
                settings,
                events=(),
                preloaded_metrics_series=preloaded_metrics_series,
            )
            if preloaded_metrics_series is not None
            else create_spike_engine(args, settings, events=())
        )
        plan = SymbolRunPlan(
            run_id=str(item["run_id"]),
            args=args,
            settings=settings,
            engine=engine,
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
