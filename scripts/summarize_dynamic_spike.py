#!/usr/bin/env python3
"""Summarize dynamic-spike trigger/order/position results.

Only consumes CSVs produced by the existing backtest and PnL scripts.  It
does not infer fills or alter any strategy behavior.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path


def _as_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    value = value.strip()
    if value.endswith("%"):
        value = value[:-1]
        return float(value) / 100
    return float(value)


def _is_allowed(row: dict[str, str]) -> bool:
    return row.get("order_result") not in {"", "rejected_origin_floor"}


def _has_fill(row: dict[str, str]) -> bool:
    try:
        return int(row.get("filled_tier_count", "0") or 0) > 0
    except ValueError:
        return any(row.get(f"tier{n}_status") == "filled" for n in (1, 2, 3))


def summarize_file(path: Path) -> dict:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    candidate = len(rows)
    allowed = [row for row in rows if _is_allowed(row)]
    filled = [row for row in allowed if _has_fill(row)]
    positions = [
        row
        for row in filled
        if row.get("exit_status") not in {"", "no_position", None}
        and _as_float(row.get("net_return")) is not None
    ]
    returns = [_as_float(row.get("net_return")) for row in positions]
    returns = [value for value in returns if value is not None and math.isfinite(value)]
    wins = sum(value > 0 for value in returns)
    losses = sum(value < 0 for value in returns)
    breakeven = sum(value == 0 for value in returns)
    positive = sum(value for value in returns if value > 0)
    negative = sum(value for value in returns if value < 0)
    profit_factor = math.inf if negative == 0 and positive > 0 else (positive / abs(negative) if negative else None)
    symbol = path.name.split("_dynamic_trigger_orders", 1)[0]
    return {
        "symbol": symbol,
        "file": str(path),
        "candidate_trigger_count": candidate,
        "allowed_order_count": len(allowed),
        "any_fill_count": len(filled),
        "position_trade_count": len(positions),
        "win_count": wins,
        "loss_count": losses,
        "breakeven_count": breakeven,
        "win_rate": wins / len(positions) if positions else None,
        "profit_factor": profit_factor,
        "no_loss": losses == 0 and bool(positions),
        "sum_net_return": sum(returns),
        "equal_weight_average_return": sum(returns) / len(returns) if returns else None,
        "sum_positive_return": positive,
        "sum_negative_return": negative,
        "order_result_counts": dict(Counter(row.get("order_result", "") for row in rows)),
        "exit_status_counts": dict(Counter(row.get("exit_status", "") for row in rows)),
    }


def aggregate(per_symbol: list[dict], files: list[Path]) -> dict:
    keys = (
        "candidate_trigger_count",
        "allowed_order_count",
        "any_fill_count",
        "position_trade_count",
        "win_count",
        "loss_count",
        "breakeven_count",
    )
    result = {key: sum(item[key] for item in per_symbol) for key in keys}
    positive = sum(item["sum_positive_return"] for item in per_symbol)
    negative = sum(item["sum_negative_return"] for item in per_symbol)
    trades = result["position_trade_count"]
    wins = result["win_count"]
    result.update(
        {
            "symbol": "__ALL__",
            "files_found": len(files),
            "symbols_with_results": len(per_symbol),
            "win_rate": wins / trades if trades else None,
            "profit_factor": math.inf if negative == 0 and positive > 0 else (positive / abs(negative) if negative else None),
            "no_loss": result["loss_count"] == 0 and trades > 0,
            "sum_net_return": positive + negative,
            "equal_weight_average_return": (positive + negative) / trades if trades else None,
            "sum_positive_return": positive,
            "sum_negative_return": negative,
        }
    )
    return result


def _json_safe(value):
    if isinstance(value, float) and math.isinf(value):
        return "Infinity"
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path("reports/dynamic"))
    parser.add_argument("--pattern", default="*_dynamic_trigger_orders*_with_pnl.csv")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    files = sorted(args.input_dir.glob(args.pattern))
    per_symbol = [summarize_file(path) for path in files]
    report = {"files": [str(path) for path in files], "per_symbol": per_symbol, "aggregate": aggregate(per_symbol, files)}
    output = args.output or args.input_dir / "summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(_json_safe(report), ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps(_json_safe(report["aggregate"]), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
