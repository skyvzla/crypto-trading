"""Single-process guard for the backtest orchestrator."""

from __future__ import annotations

from contextlib import AbstractContextManager
import fcntl
import os
from pathlib import Path
from types import TracebackType


DEFAULT_BACKTEST_LOCK_FILE = Path("reports/.backtest.lock")
BACKTEST_LOCK_FILE_ENV = "BACKTEST_LOCK_FILE"


class BacktestAlreadyRunning(RuntimeError):
    """Raised when another backtest orchestrator owns the process lock."""


def backtest_lock_path() -> Path:
    """Return the configured lock path without creating it."""

    configured = os.environ.get(BACKTEST_LOCK_FILE_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_BACKTEST_LOCK_FILE


class BacktestProcessLock(AbstractContextManager["BacktestProcessLock"]):
    """Hold an exclusive, non-blocking OS process lock until context exit.

    The lock file itself is intentionally retained after exit; the kernel lock
    is released when its descriptor closes, so stale files do not block a later
    run after a crash.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else backtest_lock_path()
        self._file = None

    def __enter__(self) -> "BacktestProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_file.close()
            raise BacktestAlreadyRunning(
                f"backtest already running (lock: {self.path})"
            ) from error
        self._file = lock_file
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        lock_file, self._file = self._file, None
        if lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()
        return None