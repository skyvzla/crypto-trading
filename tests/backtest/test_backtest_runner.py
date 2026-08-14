import argparse
import logging

import pytest

from trading_platform.backtest import runner


def _args(tmp_path):
    return argparse.Namespace(
        strategy="minimal",
        symbols=["BTCUSDT"],
        start="2026-07-01",
        end="2026-07-02",
        duckdb_path="history.duckdb",
        output=str(tmp_path / "output"),
        maker_fee=0.0002,
        taker_fee=0.0004,
        account_id="backtest",
        total_notional=None,
        warmup_hours=None,
        limit_fill_fraction=1.0,
        exchange_info=None,
        chunk_hours=24.0,
        fetch_batch_size=10_000,
        duckdb_memory_limit=None,
        duckdb_threads=1,
        log_level="INFO",
        log_file=None,
    )


def test_runner_closes_dashboard_when_interrupted(tmp_path, monkeypatch):
    class RecordingDashboard:
        def __init__(self, **kwargs):
            self.started = []
            self.failed = []
            self.closed = []

        def start(self, **kwargs):
            pass

        def task_start(self, name):
            self.started.append(name)

        def task_failed(self, name, *, increment=1):
            self.failed.append((name, increment))

        def close(self, *, status="ok", detail=None):
            self.closed.append((status, detail))

    class FakeLoader:
        def __init__(self, **kwargs):
            pass

        def iter_all(self, **kwargs):
            return iter([object()])

    class InterruptedEngine:
        def __init__(self, **kwargs):
            pass

        def run(self):
            raise KeyboardInterrupt

    dashboard = RecordingDashboard()
    root_logger = logging.getLogger()
    original_level = root_logger.level
    original_handlers = set(root_logger.handlers)
    monkeypatch.setattr(runner, "parse_args", lambda: _args(tmp_path))
    monkeypatch.setattr(runner, "TaskDashboard", lambda **kwargs: dashboard)
    monkeypatch.setattr(runner, "BacktestDataLoader", FakeLoader)
    monkeypatch.setattr(runner, "load_strategy", lambda *args, **kwargs: object())
    monkeypatch.setattr(runner, "BacktestEngine", InterruptedEngine)

    try:
        with pytest.raises(KeyboardInterrupt):
            runner.main()
    finally:
        root_logger.setLevel(original_level)
        for handler in list(root_logger.handlers):
            if handler not in original_handlers:
                root_logger.removeHandler(handler)
                handler.close()

    assert dashboard.started == ["minimal BTCUSDT"]
    assert dashboard.failed == [("minimal BTCUSDT", 1)]
    assert dashboard.closed == [("interrupted", None)]


def test_runner_writes_debug_records_to_log_file_at_info_console_level(
    tmp_path, monkeypatch
):
    class RecordingDashboard:
        def start(self, **kwargs):
            pass

        def task_start(self, name):
            pass

        def task_done(self, name, status):
            pass

        def close(self, **kwargs):
            pass

    class FakeLoader:
        def __init__(self, **kwargs):
            pass

        def iter_all(self, **kwargs):
            return iter([object()])

    class Result:
        virtual_time_end = runner.parse_date("2026-07-02")

    class FakeEngine:
        def __init__(self, **kwargs):
            pass

        def run(self):
            logging.getLogger("trading_platform.backtest.engine").debug(
                "engine debug record"
            )
            return Result()

    class FakeAnalyzer:
        def __init__(self, result):
            pass

        def analyze(self):
            return {
                "orders": {
                    "total": 0, "filled": 0, "cancelled": 0,
                    "expired": 0, "fill_rate": 0.0,
                },
                "positions": {
                    "total": 0, "open": 0, "closed": 0,
                    "profitable": 0, "loss": 0, "win_rate": 0.0,
                },
                "pnl": {
                    "net_pnl": 0.0, "total_unrealized": 0.0,
                    "total_profit": 0.0, "total_loss": 0.0,
                    "total_commission": 0.0, "profit_factor": 0.0,
                    "max_drawdown": 0.0, "sharpe_ratio": 0.0,
                },
            }

        def save_results(self, parent, name):
            pass

    root_logger = logging.getLogger()
    original_level = root_logger.level
    original_handler_levels = {
        handler: handler.level for handler in root_logger.handlers
    }
    original_handlers = set(root_logger.handlers)
    existing_log_handler = logging.FileHandler(tmp_path / "existing.log")
    existing_log_handler.setLevel(logging.ERROR)
    root_logger.addHandler(existing_log_handler)
    monkeypatch.setattr(runner, "parse_args", lambda: _args(tmp_path))
    monkeypatch.setattr(
        runner, "TaskDashboard", lambda **kwargs: RecordingDashboard()
    )
    monkeypatch.setattr(runner, "BacktestDataLoader", FakeLoader)
    monkeypatch.setattr(runner, "load_strategy", lambda *args, **kwargs: object())
    monkeypatch.setattr(runner, "BacktestEngine", FakeEngine)
    monkeypatch.setattr(runner, "ResultAnalyzer", FakeAnalyzer)

    try:
        runner.main()
        assert existing_log_handler.level == logging.ERROR
        assert root_logger.level == original_level
        assert {
            handler: handler.level for handler in original_handler_levels
        } == original_handler_levels
    finally:
        root_logger.setLevel(original_level)
        for handler, level in original_handler_levels.items():
            handler.setLevel(level)
        for handler in list(root_logger.handlers):
            if handler not in original_handlers:
                root_logger.removeHandler(handler)
                handler.close()

    log_file = tmp_path / "output" / "backtest.log"
    assert "engine debug record" in log_file.read_text(encoding="utf-8")


def test_runner_exits_cleanly_when_log_file_cannot_be_created(
    tmp_path, monkeypatch
):
    root_logger = logging.getLogger()
    original_level = root_logger.level
    original_handlers = set(root_logger.handlers)
    monkeypatch.setattr(runner, "parse_args", lambda: _args(tmp_path))
    monkeypatch.setattr(
        runner,
        "TaskDashboard",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("dashboard must not start without a log file")
        ),
    )
    monkeypatch.setattr(
        runner.logging,
        "FileHandler",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )

    try:
        with pytest.raises(SystemExit) as error:
            runner.main()
    finally:
        root_logger.setLevel(original_level)
        for handler in list(root_logger.handlers):
            if handler not in original_handlers:
                root_logger.removeHandler(handler)
                handler.close()

    assert error.value.code == 1


def test_runner_marks_dashboard_failed_for_rule_loading_error(
    tmp_path, monkeypatch
):
    class RecordingDashboard:
        def __init__(self):
            self.started = []
            self.failed = []
            self.closed = []

        def start(self, **kwargs):
            pass

        def task_start(self, name):
            self.started.append(name)

        def task_failed(self, name, *, increment=1):
            self.failed.append((name, increment))

        def close(self, *, status="ok", detail=None):
            self.closed.append((status, detail))

    class FakeLoader:
        def __init__(self, **kwargs):
            pass

        def iter_all(self, **kwargs):
            return iter([object()])

    dashboard = RecordingDashboard()
    root_logger = logging.getLogger()
    original_level = root_logger.level
    original_handlers = set(root_logger.handlers)
    monkeypatch.setattr(runner, "parse_args", lambda: _args(tmp_path))
    monkeypatch.setattr(runner, "TaskDashboard", lambda **kwargs: dashboard)
    monkeypatch.setattr(runner, "BacktestDataLoader", FakeLoader)
    monkeypatch.setattr(runner, "load_strategy", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        runner,
        "load_symbol_rules",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("bad rules")),
    )

    try:
        with pytest.raises(SystemExit) as error:
            runner.main()
    finally:
        root_logger.setLevel(original_level)
        for handler in list(root_logger.handlers):
            if handler not in original_handlers:
                root_logger.removeHandler(handler)
                handler.close()

    assert error.value.code == 1
    assert dashboard.started == ["minimal BTCUSDT"]
    assert dashboard.failed == [("minimal BTCUSDT", 1)]
    assert dashboard.closed == [("failed", None)]


def test_runner_restores_spike_strategy_configuration_hint(
    tmp_path, monkeypatch, caplog
):
    class RecordingDashboard:
        def start(self, **kwargs):
            pass

        def task_start(self, name):
            pass

        def task_failed(self, name, *, increment=1):
            pass

        def close(self, **kwargs):
            pass

    class FakeLoader:
        def __init__(self, **kwargs):
            pass

        def iter_all(self, **kwargs):
            return iter([object()])

    args = _args(tmp_path)
    args.strategy = "spike"
    root_logger = logging.getLogger()
    original_level = root_logger.level
    original_handlers = set(root_logger.handlers)
    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(
        runner, "TaskDashboard", lambda **kwargs: RecordingDashboard()
    )
    monkeypatch.setattr(runner, "BacktestDataLoader", FakeLoader)
    monkeypatch.setattr(
        runner,
        "load_strategy",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("Spike strategy requires a positive --total-notional")
        ),
    )

    try:
        with pytest.raises(SystemExit) as error:
            runner.main()
    finally:
        root_logger.setLevel(original_level)
        for handler in list(root_logger.handlers):
            if handler not in original_handlers:
                root_logger.removeHandler(handler)
                handler.close()

    assert error.value.code == 1
    assert "Spike 策略必须提供至少一个币种和正数" in caplog.text
    assert "--total-notional" in caplog.text
