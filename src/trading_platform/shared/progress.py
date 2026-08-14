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
        self._started_at = time.monotonic()
        self._last_update = 0.0
        self._last_progress_pct = 0
        self._running: dict[str, float] = {}
        self._samples: list[float] = []
        self._completed: deque[CompletedItem] = deque(maxlen=self._max_completed)
        self._console: Console | None = None
        self._live: Live | None = None
        self._closed = False

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._started_at

    @property
    def eta_s(self) -> float | None:
        if self._total is None:
            return None
        if len(self._samples) < self._min_eta_samples:
            return None
        remaining = self._total - self._done
        if remaining <= 0:
            return 0.0
        avg_unit = sum(self._samples) / len(self._samples)
        return avg_unit * remaining

    def start(self, *, detail: str | None = None) -> None:
        with self._lock:
            if not self._tty:
                extras = f" {detail}" if detail else ""
                print(
                    f"event=start task={self._title}"
                    f" total={self._total if self._total is not None else 'unknown'}"
                    f"{extras}",
                    file=self._stream,
                    flush=True,
                )
            else:
                self._console = Console(file=self._stream, highlight=False)
                self._live = Live(
                    self._render(),
                    console=self._console,
                    refresh_per_second=4,
                    transient=False,
                    screen=False,
                )
                self._live.start()

    def task_start(self, name: str) -> None:
        with self._lock:
            self._running[name] = time.monotonic()

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
        with self._lock:
            if not self._tty:
                print(
                    f"event=error task={self._title} message={message}",
                    file=self._stream,
                    flush=True,
                )
            else:
                print(f"error: {message}", file=self._stream, flush=True)

    def close(self, *, status: str = "ok", detail: str | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._live is not None:
                self._live.update(self._render(), refresh=True)
                self._live.stop()
                self._live = None
            if self._tty:
                return
            extras = f" {detail}" if detail else ""
            print(
                f"event=complete task={self._title} status={status}"
                f" done={self._done}"
                f" total={self._total if self._total is not None else 'unknown'}"
                f" elapsed_s={self.elapsed_s:.0f}{extras}",
                file=self._stream,
                flush=True,
            )

    def _refresh(self) -> None:
        if self._tty:
            now = time.monotonic()
            if self._live is not None and now - self._last_update >= 0.2:
                self._live.update(self._render(), refresh=True)
                self._last_update = now
        else:
            self._emit_plain_progress()

    def _emit_plain_progress(self) -> None:
        if self._quiet or self._total is None or self._total <= 0:
            return
        percent = int(round(self._done / self._total * 100))
        step = self._progress_step
        while self._last_progress_pct + step <= percent:
            self._last_progress_pct += step
            threshold = min(self._last_progress_pct, 100)
            running = ",".join(sorted(self._running)) or "-"
            eta = self.eta_s
            eta_label = (
                f" eta_s={eta:.0f}" if eta is not None else " eta=collecting"
            )
            print(
                f"event=progress task={self._title} done={self._done}"
                f" total={self._total} percent={threshold}"
                f" elapsed_s={self.elapsed_s:.0f}{eta_label}"
                f" running={running}",
                file=self._stream,
                flush=True,
            )

    def _render(self) -> Group:
        elapsed = _format_duration(self.elapsed_s)
        running_block: list[Text] = []
        if self._running:
            ordered = sorted(self._running.items(), key=lambda item: item[1])
            for name, started in ordered:
                running_block.append(
                    Text(f"  {name}") + Text("  Elapsed ")
                    + Text(_format_duration(time.monotonic() - started))
                )
        running_panel = Panel(
            Group(*running_block) if running_block else Text("  (none)"),
            title=f"Running ({len(self._running)})",
            border_style="cyan",
            padding=(0, 1),
        )
        completed_block: list[Text] = []
        for item in self._completed:
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
        if self._total is not None and self._total > 0:
            ratio = self._done / self._total
            bar = _format_bar(ratio)
            percent = f"{ratio:.1%}"
            progress_header = Text(
                f"  {bar} {percent}  {self._done}/{self._total}"
            )
            eta = self.eta_s
            if eta is None:
                eta_text = (
                    f"ETA collecting samples "
                    f"({len(self._samples)}/{self._min_eta_samples})"
                )
                total_text = "Est. total --:--:--"
            else:
                eta_text = f"ETA {_format_duration(eta)}"
                total_text = f"Est. total {_format_duration(self.elapsed_s + eta)}"
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
