"""
轻量任务进度仪表盘

- 交互终端（TTY）：rich Live 原地刷新，固定面板不滚屏。
- 非交互终端（Agent/CI）：仅低频输出结构化单行（start/progress/complete/error），无 ANSI。
- ETA 需要至少 ``min_eta_samples``（默认 5）个有效完成样本，避免早期样本波动。
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TextIO
from urllib.parse import quote

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_HMS_FIELDS = 60 * 60


def _format_duration(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, rem = divmod(total, _HMS_FIELDS)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_bar(ratio: float, width: int = 30) -> str:
    filled = round(min(max(ratio, 0.0), 1.0) * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _encode_text(value: str) -> str:
    return quote(value, safe="")


@dataclass
class CompletedItem:
    name: str
    status: str
    duration_s: float


class TaskDashboard:
    """
    长任务统一进度显示。

    Args:
        title: 任务标题（如 "backtest"、"market-archive"）。
        total: 总单元数；None 表示未知（只显示 Elapsed，不显示进度/ETA）。
        stream: 输出流，默认 sys.stdout；isatty() 决定 rich/plain 模式。
        min_eta_samples: ETA 所需最少有效样本数。
        max_completed: Completed 列表保留条数。
        quiet: 非 TTY 下抑制 progress 行（仅 start/complete）。
        progress_step: 非 TTY 下进度行输出百分比步长（0-100）。
    """

    def __init__(
        self,
        *,
        title: str,
        total: int | None = None,
        stream: TextIO | None = None,
        min_eta_samples: int = 5,
        max_completed: int = 3,
        quiet: bool = False,
        progress_step: int = 10,
    ) -> None:
        self._title = title
        self._total = total
        self._done = 0
        self._stream = stream or sys.stdout
        self._min_eta_samples = max(1, min_eta_samples)
        self._max_completed = max(1, max_completed)
        self._quiet = quiet
        self._progress_step = max(1, min(100, progress_step))
        self._tty = self._stream.isatty()
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._started_at = time.monotonic()
        self._last_update = 0.0
        self._last_progress_pct = 0
        self._running: dict[str, float] = {}
        self._samples: list[float] = []
        self._completed: deque[CompletedItem] = deque(maxlen=self._max_completed)
        self._console: Console | None = None
        self._live: Live | None = None
        self._started = False
        self._closed = False

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._started_at

    @property
    def eta_s(self) -> float | None:
        with self._lock:
            return self._estimate_eta(self._total, self._done, self._samples)

    def start(self, *, detail: str | None = None) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if self._closed or self._started:
                    return
            encoded_detail = f" detail={_encode_text(detail)}" if detail else ""
            if not self._tty:
                print(
                    f"event=start task={_encode_text(self._title)}"
                    f" total={self._total if self._total is not None else 'unknown'}"
                    f"{encoded_detail}",
                    file=self._stream,
                    flush=True,
                )
                with self._lock:
                    self._started = True
                return

            console = Console(file=self._stream, highlight=False)
            live = Live(
                console=console,
                refresh_per_second=4,
                transient=False,
                screen=False,
                get_renderable=self._render,
            )
            try:
                live.start()
            except Exception:
                live.stop()
                raise
            with self._lock:
                self._console = console
                self._live = live
                self._started = True

    def task_start(self, name: str) -> None:
        with self._lock:
            self._running[name] = time.monotonic()
        self._refresh()

    def task_done(
        self,
        name: str,
        status: str = "OK",
        *,
        count_as_sample: bool = True,
        increment: int = 1,
    ) -> None:
        with self._lock:
            started = self._running.pop(name, None)
            if started is not None:
                duration = time.monotonic() - started
                if count_as_sample and duration >= 0:
                    self._samples.append(duration / max(1, increment))
                self._completed.appendleft(
                    CompletedItem(name=name, status=status, duration_s=duration)
                )
            self._done += max(1, increment)
        self._refresh()

    def task_skip(self, name: str, status: str = "Skipped", *, increment: int = 1) -> None:
        self.task_done(name, status, count_as_sample=False, increment=increment)

    def task_failed(self, name: str, *, increment: int = 1) -> None:
        self.task_done(name, "Failed", count_as_sample=False, increment=increment)

    def error(self, message: str) -> None:
        with self._lifecycle_lock:
            with self._lock:
                console = self._console
            if not self._tty:
                print(
                    f"event=error task={_encode_text(self._title)}"
                    f" message={_encode_text(message)}",
                    file=self._stream,
                    flush=True,
                )
            elif console is not None:
                console.print(Text(f"error: {message}"))
            else:
                print(f"error: {message}", file=self._stream, flush=True)

    def close(self, *, status: str = "ok", detail: str | None = None) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                live = self._live
                self._live = None
                self._console = None
                done = self._done
                total = self._total
            if live is not None:
                live.stop()
        if self._tty:
            return
        encoded_detail = f" detail={_encode_text(detail)}" if detail else ""
        print(
            f"event=complete task={_encode_text(self._title)}"
            f" status={_encode_text(status)}"
            f" done={done}"
            f" total={total if total is not None else 'unknown'}"
            f" elapsed_s={self.elapsed_s:.0f}{encoded_detail}",
            file=self._stream,
            flush=True,
        )

    def _refresh(self) -> None:
        if not self._tty:
            self._emit_plain_progress()
            return

        with self._lifecycle_lock:
            with self._lock:
                now = time.monotonic()
                live = self._live
                if live is None or now - self._last_update < 0.2:
                    return
                self._last_update = now
            live.refresh()

    def _emit_plain_progress(self) -> None:
        with self._lock:
            if self._quiet or self._total is None or self._total <= 0:
                return
            percent = min(100, int(round(self._done / self._total * 100)))
            if self._last_progress_pct + self._progress_step > percent:
                return
            self._last_progress_pct = (
                percent // self._progress_step * self._progress_step
            )
            running = ",".join(quote(name, safe="") for name in sorted(self._running)) or "-"
            eta = self._estimate_eta(self._total, self._done, self._samples)
            eta_label = (
                f" eta_s={eta:.0f}" if eta is not None else " eta=collecting"
            )
            line = (
                f"event=progress task={_encode_text(self._title)} done={self._done}"
                f" total={self._total} percent={percent}"
                f" elapsed_s={self.elapsed_s:.0f}{eta_label}"
                f" running={running}"
            )
        print(line, file=self._stream, flush=True)

    def _estimate_eta(
        self,
        total: int | None,
        done: int,
        samples: list[float],
    ) -> float | None:
        if total is None or len(samples) < self._min_eta_samples:
            return None
        remaining = total - done
        if remaining <= 0:
            return 0.0
        return sum(samples) / len(samples) * remaining

    def _render(self) -> Group:
        with self._lock:
            total = self._total
            done = self._done
            samples = list(self._samples)
            running = sorted(self._running.items(), key=lambda item: item[1])
            completed = list(self._completed)
            elapsed_s = self.elapsed_s
        elapsed = _format_duration(elapsed_s)
        running_block: list[Text] = []
        if running:
            for name, started in running:
                running_block.append(
                    Text(f"  {name}") + Text("  Elapsed ")
                    + Text(_format_duration(time.monotonic() - started))
                )
        running_panel = Panel(
            Group(*running_block) if running_block else Text("  (none)"),
            title=f"Running ({len(running)})",
            border_style="cyan",
            padding=(0, 1),
        )
        completed_block: list[Text] = []
        for item in completed:
            completed_block.append(
                Text(f"  {item.name}") + Text("  ")
                + Text(item.status, style="green" if item.status == "OK" else "red")
                + Text("  " + _format_duration(item.duration_s))
            )
        completed_panel = Panel(
            Group(*completed_block) if completed_block else Text("  (none)"),
            title="Completed",
            border_style="green",
            padding=(0, 1),
        )
        if total is not None and total > 0:
            ratio = done / total
            bar = _format_bar(ratio)
            percent = f"{ratio:.1%}"
            progress_header = Text(
                f"  {bar} {percent}  {done}/{total}"
            )
            eta = self._estimate_eta(total, done, samples)
            if eta is None:
                eta_text = (
                    f"ETA collecting samples "
                    f"({len(samples)}/{self._min_eta_samples})"
                )
                total_text = "Est. total --:--:--"
            else:
                eta_text = f"ETA {_format_duration(eta)}"
                total_text = f"Est. total {_format_duration(elapsed_s + eta)}"
            progress_lines = Text() + progress_header
            progress_lines.append_text(
                Text(
                    f"\n  Elapsed {elapsed}  {eta_text}  {total_text}",
                    style="dim",
                )
            )
        else:
            progress_lines = Text(f"  Elapsed {elapsed}")
        progress_panel = Panel(
            progress_lines,
            title="Progress",
            border_style="yellow",
            padding=(0, 1),
        )
        return Group(running_panel, completed_panel, progress_panel)
