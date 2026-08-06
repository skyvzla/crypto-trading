#!/usr/bin/env python3
"""Batch runner for the unchanged dynamic-spike backtest scripts.

This wrapper only orchestrates ``backtest_dynamic_spike.py`` and
``append_dynamic_spike_pnl.py``.  Strategy logic remains in those scripts.
Each worker handles one symbol at a time, which keeps DuckDB read connections
short-lived and bounds memory usage.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
BACKTEST = ROOT / "scripts" / "backtest_dynamic_spike.py"
APPEND_PNL = ROOT / "scripts" / "append_dynamic_spike_pnl.py"


def _read_symbols(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.DictReader(handle)
        symbols = []
        for row in rows:
            symbol = (row.get("symbol") or "").strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    return symbols


def _run_one(symbol: str, args: argparse.Namespace) -> dict:
    out = args.output_dir / f"{symbol}_dynamic_trigger_orders.csv"
    pnl_out = out.with_name(f"{out.stem}_with_pnl.csv")
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(ROOT / "src")
            + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
            "SYMBOL": symbol,
            "START": args.start,
            "END": args.end,
            "OUT": str(out),
        }
    )
    started = time.monotonic()
    commands = [
        [sys.executable, str(BACKTEST)],
        [sys.executable, str(APPEND_PNL)],
    ]
    append_env = dict(env, CSV=str(out))
    logs = []
    try:
        for index, (command, command_env) in enumerate(((commands[0], env), (commands[1], append_env))):
            # The unchanged PnL script assumes at least one trigger row.  An
            # empty, valid backtest is still a useful result, so preserve an
            # empty PnL artifact instead of treating it as a failed symbol.
            if index == 1 and out.exists() and _csv_has_no_rows(out):
                pnl_out.write_bytes(out.read_bytes())
                break
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=command_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=args.timeout,
                check=False,
            )
            logs.append(completed.stdout[-4000:])
            if completed.returncode:
                return {
                    "symbol": symbol,
                    "status": "failed",
                    "returncode": completed.returncode,
                    "output": "\n".join(logs),
                    "seconds": round(time.monotonic() - started, 2),
                }
        if not pnl_out.exists():
            return {
                "symbol": symbol,
                "status": "failed",
                "returncode": 1,
                "output": f"missing output: {pnl_out}",
                "seconds": round(time.monotonic() - started, 2),
            }
        return {
            "symbol": symbol,
            "status": "ok",
            "rows_file": str(pnl_out),
            "seconds": round(time.monotonic() - started, 2),
            "output": "\n".join(logs),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "symbol": symbol,
            "status": "timeout",
            "returncode": 124,
            "output": str(exc),
            "seconds": round(time.monotonic() - started, 2),
        }
    except Exception as exc:  # keep one bad symbol from hiding other results
        return {
            "symbol": symbol,
            "status": "failed",
            "returncode": 1,
            "output": repr(exc),
            "seconds": round(time.monotonic() - started, 2),
        }


def _csv_has_no_rows(path: Path) -> bool:
    """Return true when a backtest CSV has no data rows (header-only/empty)."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        next(reader, None)  # header (the original script may emit no header)
        return next(reader, None) is None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=Path, default=ROOT / "reports" / "active_alt100_current.csv")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports" / "dynamic")
    parser.add_argument("--start", default="2026-07-01T00:00:00+00:00")
    parser.add_argument("--end", default="2026-08-01T00:00:00+00:00")
    parser.add_argument("--workers", type=int, default=2, help="并发币种数；建议 2-4，避免内存峰值过高")
    parser.add_argument("--timeout", type=int, default=3600, help="单个脚本超时秒数")
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args()
    datetime.fromisoformat(args.start)
    datetime.fromisoformat(args.end)
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    return args


def main() -> int:
    args = parse_args()
    symbols = _read_symbols(args.symbols)
    if not symbols:
        raise SystemExit(f"no symbols found in {args.symbols}")
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_run_one, symbol, args): symbol for symbol in symbols}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"{result['symbol']}: {result['status']} ({result['seconds']}s)", flush=True)
    results.sort(key=lambda item: item["symbol"])
    manifest = args.manifest or args.output_dir / "batch_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbols_file": str(args.symbols),
        "start": args.start,
        "end": args.end,
        "workers": args.workers,
        "results": results,
        "ok": sum(item["status"] == "ok" for item in results),
        "failed": sum(item["status"] != "ok" for item in results),
    }
    manifest.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    print(f"manifest={manifest} ok={payload['ok']} failed={payload['failed']}")
    return 0 if payload["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
