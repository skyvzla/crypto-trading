#!/usr/bin/env python3
"""单币单日的完整行情波段扫描（ZigZag 摆动分割）。

从本地 DuckDB 1m 归档读取指定币种某日的分钟线，用固定百分比阈值做
ZigZag 摆动点识别，输出完整行情波段（起止时间/方向/时长/价格），
并标记包含单根插针（spike bar）的波段。

用途：复核日线异动报告里"日内极值区间"是否覆盖完整行情段。典型反例
AKEUSDT 2026-07-27：日内 high 出现在 06:17 的插针、日内 low 出现在
06:20 的插针，原扫描只输出 downward 3 分钟，丢失了 00:35 起涨到
06:17 见顶的完整上涨段。
"""

from __future__ import annotations

import argparse
import csv
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from trading_platform.market.archive.index import (
    ARCHIVE_INDEX_FILENAME,
    load_archive_index,
    verify_archive_index_files,
)

DAY_MS = 86_400_000


def _iso(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).isoformat()


def _hour_minute(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).strftime("%H:%M")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ZigZag 摆动分割扫描单币单日完整行情波段")
    parser.add_argument("--symbol", required=True, help="币种，如 AKEUSDT")
    parser.add_argument(
        "--date",
        required=True,
        help="UTC 日期，如 2026-07-27",
    )
    parser.add_argument(
        "--archive-index",
        type=Path,
        default=Path("data/market/candles") / ARCHIVE_INDEX_FILENAME,
    )
    parser.add_argument(
        "--zigzag-percent",
        type=float,
        default=3.0,
        help="摆动确认阈值百分比，默认 3.0",
    )
    parser.add_argument(
        "--spike-threshold",
        type=float,
        default=0.15,
        help="单根 1m bar 的 (high-low)/low 超过该比例视为插针，默认 0.15",
    )
    parser.add_argument(
        "--merge-short-minutes",
        type=int,
        default=30,
        help="时长小于该值的短波段合并进相邻主波段形成周期，默认 30；0 关闭",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="可选 CSV 输出路径；缺省只打印",
    )
    return parser.parse_args()


def _load_day_bars(index_path: Path, symbol: str, date: str) -> list[tuple[int, float, float, float, float]]:
    index = load_archive_index(index_path)
    target = index[
        index["timeframe"].eq("1m") & index["symbol"].eq(symbol.upper())
    ].copy()
    if target.empty:
        raise ValueError(f"archive index has no 1m partitions for {symbol}")
    year, month = int(date[:4]), int(date[5:7])
    parts = target[
        (target["year"] == year) & (target["month"] == month)
    ].drop_duplicates("relative_path")
    if parts.empty:
        raise ValueError(f"archive index has no 1m partitions for {symbol} {date}")
    verify_archive_index_files(parts, index_path.parent)
    day_open_ms = int(
        datetime.fromisoformat(f"{date}T00:00:00+00:00").timestamp() * 1000
    )
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        rows = connection.execute(
            """
            SELECT epoch_ms(open_time), open, high, low, close
            FROM read_parquet(?, union_by_name=true)
            WHERE symbol = ?
              AND timeframe = '1m'
              AND epoch_ms(open_time) >= ?
              AND epoch_ms(open_time) < ?
            ORDER BY open_time
            """,
            [[str(index_path.parent / path) for path in parts["relative_path"]],
             symbol.upper(), day_open_ms, day_open_ms + DAY_MS],
        ).fetchall()
    finally:
        connection.close()
    return [
        (int(t), float(open_), float(high), float(low), float(close))
        for t, open_, high, low, close in rows
    ]


def _zigzag_pivots(
    bars: list[tuple[int, float, float, float, float]], percent: float
) -> list[tuple[int, float, str]]:
    """返回 (index, price, kind) 摆动点列表，kind 为 'high' 或 'low'。

    方向状态机：向上时跟踪最高点，从最高点回撤超过 percent 才确认该
    高点为摆动点并转向向下；对称处理低点。确认摆动点后新方向的极值
    延迟到下一根 bar 初始化，避免同一根 bar 内 high/low 同时满足阈值
    产生的 0 分钟镜像摆动。
    """
    if not bars:
        return []
    pivots: list[tuple[int, float, str]] = []
    direction = 0
    sw_high_idx, sw_high = 0, bars[0][2]
    sw_low_idx, sw_low = 0, bars[0][3]
    for index in range(1, len(bars)):
        _, _, high, low, _ = bars[index]
        if direction == 0:
            if high > sw_high:
                sw_high_idx, sw_high = index, high
            if low < sw_low:
                sw_low_idx, sw_low = index, low
            if low <= sw_high * (1 - percent / 100.0):
                pivots.append((sw_high_idx, sw_high, "high"))
                direction = -1
                sw_high = 0.0
                sw_low = 0.0
            elif high >= sw_low * (1 + percent / 100.0):
                pivots.append((sw_low_idx, sw_low, "low"))
                direction = 1
                sw_high = 0.0
                sw_low = 0.0
        elif direction == 1:
            if high > sw_high:
                sw_high_idx, sw_high = index, high
            elif sw_high > 0 and low <= sw_high * (1 - percent / 100.0):
                pivots.append((sw_high_idx, sw_high, "high"))
                direction = -1
                sw_low = 0.0
                sw_high = 0.0
        else:
            if sw_low == 0.0:
                sw_low_idx, sw_low = index, low
            elif low < sw_low:
                sw_low_idx, sw_low = index, low
            elif sw_low > 0 and high >= sw_low * (1 + percent / 100.0):
                pivots.append((sw_low_idx, sw_low, "low"))
                direction = 1
                sw_high = 0.0
                sw_low = 0.0
    if direction == 1 and sw_high > 0:
        pivots.append((sw_high_idx, sw_high, "high"))
    elif direction == -1 and sw_low > 0:
        pivots.append((sw_low_idx, sw_low, "low"))
    return pivots


def _contains_spike(
    bars: list[tuple[int, float, float, float, float]],
    start_index: int,
    end_index: int,
    spike_threshold: float,
) -> bool:
    return any(
        (bar[2] - bar[3]) / bar[3] > spike_threshold
        for bar in bars[start_index : end_index + 1]
    )


def _spike_direction(
    bars: list[tuple[int, float, float, float, float]],
    start_index: int,
    end_index: int,
    spike_threshold: float,
) -> str:
    """周期内插针方向：单根 bar 的 high/low 相对前一根收盘价的偏离。

    up=上涨插针（瞬间冲高，做空信号），down=暴跌插针（瞬间砸低）。
    段首 pivot bar 归前一段检测，本段从第二根 bar 开始比较。
    """
    kinds: set[str] = set()
    if end_index <= start_index:
        return "none"
    previous_close = bars[start_index][4]
    for bar in bars[start_index + 1 : end_index + 1]:
        if previous_close > 0:
            if bar[2] / previous_close - 1 > spike_threshold:
                kinds.add("up")
            elif bar[3] / previous_close - 1 < -spike_threshold:
                kinds.add("down")
        previous_close = bar[4]
    if not kinds:
        return "none"
    if len(kinds) == 2:
        return "both"
    return next(iter(kinds))


def _merge_short_segments(
    bars: list[tuple[int, float, float, float, float]],
    segments: list[tuple[int, int, str]],
    merge_minutes: int,
) -> list[tuple[int, int, str]]:
    """把时长小于 merge_minutes 的短波段合并进相邻波段。

    短波段通常由插针产生（瞬间暴涨暴跌后又回来），合并后形成完整
    的"行情周期"。迭代合并直到没有短波段；合并段方向标记为 merged，
    由调用方按段首尾收盘价决定。
    """
    merged_segments = list(segments)
    while True:
        merged = False
        for index in range(len(merged_segments)):
            start_index, end_index, _ = merged_segments[index]
            if (bars[end_index][0] - bars[start_index][0]) // 60_000 < merge_minutes:
                left = max(0, index - 1)
                right = min(len(merged_segments) - 1, index + 1)
                merged_segments[left : right + 1] = [
                    (merged_segments[left][0], merged_segments[right][1], "merged")
                ]
                merged = True
                break
        if not merged:
            break
    return merged_segments


def _build_waves(
    bars: list[tuple[int, float, float, float, float]],
    pivots: list[tuple[int, float, str]],
    spike_threshold: float,
    merge_minutes: int,
) -> list[dict[str, object]]:
    waves: list[dict[str, object]] = []
    if not pivots:
        return waves
    segments: list[tuple[int, int, str]] = []
    previous = pivots[0]
    if previous[2] == "high":
        segments.append((0, previous[0], "up"))
    else:
        segments.append((0, previous[0], "down"))
    for current in pivots[1:]:
        direction = "down" if previous[2] == "high" else "up"
        segments.append((previous[0], current[0], direction))
        previous = current
    if merge_minutes > 0:
        segments = _merge_short_segments(bars, segments, merge_minutes)
    for index, (start_index, end_index, direction) in enumerate(segments):
        merged = direction == "merged"
        if merged:
            direction = "up" if bars[end_index][4] >= bars[start_index][4] else "down"
        start_ms = bars[start_index][0]
        end_ms = bars[end_index][0]
        start_price = bars[start_index][4]
        end_price = bars[end_index][4]
        segment = bars[start_index : end_index + 1]
        high_bar = max(segment, key=lambda bar: (bar[2], bar[0]))
        low_bar = min(segment, key=lambda bar: (bar[3], bar[0]))
        waves.append({
            "wave_index": index + 1,
            "direction": direction,
            "start_utc": _iso(start_ms),
            "end_utc": _iso(end_ms),
            "duration_minutes": (end_ms - start_ms) // 60_000,
            "start_price": f"{start_price:.12g}",
            "end_price": f"{end_price:.12g}",
            "amplitude_percent": f"{(end_price - start_price) / start_price * 100:.4f}",
            "range_percent": f"{(high_bar[2] - low_bar[3]) / low_bar[3] * 100:.4f}",
            "high_price": f"{high_bar[2]:.12g}",
            "high_utc": _iso(high_bar[0]),
            "low_price": f"{low_bar[3]:.12g}",
            "low_utc": _iso(low_bar[0]),
            "contains_spike": str(
                _contains_spike(bars, start_index, end_index, spike_threshold)
            ).lower(),
            "spike_direction": _spike_direction(
                bars, start_index, end_index, spike_threshold
            ),
            "merged": str(merged).lower(),
            "status": "incomplete" if end_index == len(bars) - 1 else "confirmed",
        })
    return waves


def main() -> int:
    args = parse_args()
    if args.zigzag_percent <= 0:
        raise ValueError("zigzag-percent must be positive")
    if args.spike_threshold <= 0:
        raise ValueError("spike-threshold must be positive")
    if args.merge_short_minutes < 0:
        raise ValueError("merge-short-minutes must be non-negative")
    index_path = args.archive_index.resolve()
    bars = _load_day_bars(index_path, args.symbol, args.date)
    if not bars:
        raise ValueError(f"no 1m bars for {args.symbol} on {args.date}")
    pivots = _zigzag_pivots(bars, args.zigzag_percent)
    waves = _build_waves(
        bars, pivots, args.spike_threshold, args.merge_short_minutes
    )

    print(f"symbol={args.symbol.upper()} date={args.date} bars={len(bars)} "
          f"zigzag_percent={args.zigzag_percent} pivots={len(pivots)} "
          f"merge_short_minutes={args.merge_short_minutes}")
    print(f"{'#':>2} {'dir':<5} {'start':>6} {'end':>6} {'dur':>5} "
          f"{'start':>10} {'end':>10} {'amp%':>8} {'range%':>8} {'high':>10} {'low':>10} "
          f"{'spike':>5} {'spkdir':>6} {'merged':>6} {'status':<11}")
    for wave in waves:
        start_ms = int(datetime.fromisoformat(wave["start_utc"]).timestamp() * 1000)
        end_ms = int(datetime.fromisoformat(wave["end_utc"]).timestamp() * 1000)
        print(f"{wave['wave_index']:>2} {wave['direction']:<5} {_hour_minute(start_ms):>6} "
              f"{_hour_minute(end_ms):>6} {wave['duration_minutes']:>5} "
              f"{wave['start_price']:>10} {wave['end_price']:>10} "
              f"{wave['amplitude_percent']:>8} {wave['range_percent']:>8} "
              f"{wave['high_price']:>10} {wave['low_price']:>10} "
              f"{wave['contains_spike']:>5} {wave['spike_direction']:>6} "
              f"{wave['merged']:>6} {wave['status']:<11}")
    if not waves:
        print("no swings detected for the day")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=args.output.parent,
            prefix=f".{args.output.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(
                stream,
                fieldnames=["wave_index", "direction", "start_utc", "end_utc",
                            "duration_minutes", "start_price", "end_price",
                            "amplitude_percent", "range_percent", "high_price",
                            "high_utc", "low_price", "low_utc",
                            "contains_spike", "spike_direction", "merged", "status"],
            )
            writer.writeheader()
            writer.writerows(waves)
        temporary.replace(args.output)
        print(f"output={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())