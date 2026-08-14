from io import StringIO
import threading

from rich.console import Console

import trading_platform.shared.progress as progress
from trading_platform.shared.progress import TaskDashboard, _format_bar


def _captured(events):
    out = StringIO()
    dashboard = TaskDashboard(title="t", total=100, stream=out, **events["kwargs"])
    dashboard.start()
    for name, action in events["actions"]:
        getattr(dashboard, action[0])(name, *action[1:])
    dashboard.close()
    return out.getvalue()


def test_plain_output_has_no_ansi_and_start_complete_lines():
    out = StringIO()
    dashboard = TaskDashboard(title="backtest", total=240, stream=out)
    dashboard.start(detail="runs=240")
    dashboard.close(status="ok", detail="output=reports/x")

    text = out.getvalue()
    assert "\x1b[" not in text
    assert "event=start task=backtest total=240 detail=runs%3D240" in text
    complete_line = next(line for line in text.splitlines() if line.startswith("event=complete"))
    complete_fields = dict(field.split("=", 1) for field in complete_line.split())
    assert complete_fields["task"] == "backtest"
    assert complete_fields["status"] == "ok"
    assert complete_fields["done"] == "0"
    assert complete_fields["total"] == "240"
    assert complete_fields["detail"] == "output%3Dreports%2Fx"


def test_eta_requires_min_samples():
    out = StringIO()
    dashboard = TaskDashboard(title="t", total=100, stream=out, min_eta_samples=5)
    dashboard.start()
    for i in range(4):
        dashboard.task_start(f"F{i}")
        dashboard.task_done(f"F{i}", increment=25)
    dashboard.close()

    text = out.getvalue()
    progress_lines = [line for line in text.splitlines() if "event=progress" in line]
    assert progress_lines
    assert all("eta=collecting" in line for line in progress_lines)


def test_eta_shown_after_min_samples():
    out = StringIO()
    dashboard = TaskDashboard(title="t", total=100, stream=out, min_eta_samples=5)
    dashboard.start()
    for i in range(5):
        dashboard.task_start(f"F{i}")
        dashboard.task_done(f"F{i}", increment=20)
    dashboard.close()

    text = out.getvalue()
    last = [line for line in text.splitlines() if "event=progress" in line][-1]
    assert "eta_s=" in last


def test_failed_tasks_not_eta_samples():
    out = StringIO()
    dashboard = TaskDashboard(
        title="t", total=100, stream=out, min_eta_samples=5
    )
    dashboard.start()
    for i in range(5):
        dashboard.task_start(f"F{i}")
        dashboard.task_failed(f"F{i}")
    for i in range(4):
        dashboard.task_start(f"S{i}")
        dashboard.task_done(f"S{i}", increment=10)
    dashboard.close()

    text = out.getvalue()
    last = [line for line in text.splitlines() if "event=progress" in line][-1]
    assert "eta=collecting" in last


def test_skipped_tasks_not_eta_samples():
    out = StringIO()
    dashboard = TaskDashboard(title="t", total=100, stream=out, min_eta_samples=3)
    dashboard.start()
    for i in range(3):
        dashboard.task_start(f"S{i}")
        dashboard.task_skip(f"S{i}", increment=33)
    dashboard.close()

    text = out.getvalue()
    last = [line for line in text.splitlines() if "event=progress" in line][-1]
    assert "eta=collecting" in last


def test_unknown_total_has_no_progress():
    out = StringIO()
    dashboard = TaskDashboard(title="run", total=None, stream=out)
    dashboard.start()
    dashboard.task_start("BTCUSDT")
    dashboard.close()

    text = out.getvalue()
    assert "event=progress" not in text
    assert "eta" not in text


def test_completed_keeps_only_recent_max():
    out = StringIO()
    dashboard = TaskDashboard(title="t", total=10, stream=out, max_completed=3)
    dashboard.start()
    for i in range(5):
        dashboard.task_start(f"T{i}")
        dashboard.task_done(f"T{i}", count_as_sample=False, increment=2)
    dashboard.close()

    assert len(dashboard._completed) == 3
    names = [item.name for item in dashboard._completed]
    assert names == ["T4", "T3", "T2"]


def test_running_sorted_by_start_time():
    import time

    out = StringIO()
    dashboard = TaskDashboard(title="t", total=10, stream=out)
    dashboard.start()
    dashboard.task_start("Z")
    time.sleep(0.01)
    dashboard.task_start("A")
    ordered = sorted(dashboard._running.items(), key=lambda item: item[1])
    assert [name for name, _ in ordered] == ["Z", "A"]
    dashboard.close()


def test_progress_percent_uses_done_total():
    out = StringIO()
    dashboard = TaskDashboard(title="t", total=240, stream=out)
    dashboard.start()
    dashboard.task_start("BTCUSDT")
    dashboard.task_done("BTCUSDT", increment=24)
    dashboard.close()

    text = out.getvalue()
    assert "percent=10" in text
    assert "done=24 total=240" in text


def test_plain_progress_encodes_running_names_and_reports_actual_percent():
    out = StringIO()
    dashboard = TaskDashboard(title="t", total=100, stream=out)
    dashboard.start()
    dashboard.task_start("w1 BTCUSDT 1s")
    dashboard.task_start("completed")
    dashboard.task_done("completed", increment=25)
    dashboard.close()

    progress_lines = [line for line in out.getvalue().splitlines() if "event=progress" in line]
    assert len(progress_lines) == 1
    fields = dict(field.split("=", 1) for field in progress_lines[0].split())
    assert fields["done"] == "25"
    assert fields["total"] == "100"
    assert fields["percent"] == "25"
    assert fields["running"] == "w1%20BTCUSDT%201s"


def test_plain_error_encodes_message():
    out = StringIO()
    dashboard = TaskDashboard(title="t", stream=out)
    dashboard.error("network failed: retry later")

    fields = dict(field.split("=", 1) for field in out.getvalue().strip().split())
    assert fields["message"] == "network%20failed%3A%20retry%20later"


def test_plain_output_encodes_all_text_fields_and_detail():
    out = StringIO()
    dashboard = TaskDashboard(title="market archive", stream=out)
    dashboard.start(detail="output=/tmp/run dir")
    dashboard.error("network retry later")
    dashboard.close(status="not ok", detail="output=/tmp/result dir")

    lines = [dict(field.split("=", 1) for field in line.split()) for line in out.getvalue().splitlines()]
    start, error, complete = lines
    assert start["task"] == "market%20archive"
    assert start["detail"] == "output%3D%2Ftmp%2Frun%20dir"
    assert error["task"] == "market%20archive"
    assert error["message"] == "network%20retry%20later"
    assert complete["task"] == "market%20archive"
    assert complete["status"] == "not%20ok"
    assert complete["detail"] == "output%3D%2Ftmp%2Fresult%20dir"


def test_tty_error_uses_rich_console(monkeypatch):
    class TTYStream(StringIO):
        def isatty(self):
            return True

    class FakeConsole:
        instances = []

        def __init__(self, **_kwargs):
            self.messages = []
            self.__class__.instances.append(self)

        def print(self, message):
            self.messages.append(message)

    class FakeLive:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(progress, "Console", FakeConsole)
    monkeypatch.setattr(progress, "Live", FakeLive)
    stream = TTYStream()
    dashboard = TaskDashboard(title="backtest", stream=stream)
    dashboard.start()
    dashboard.error("simulated error")

    assert stream.getvalue() == ""
    assert [str(message) for message in FakeConsole.instances[-1].messages] == [
        "error: simulated error"
    ]

    dashboard.close()


def test_tty_close_during_start_stops_live(monkeypatch):
    started = threading.Event()
    close_attempted = threading.Event()
    release_start = threading.Event()

    class TTYStream(StringIO):
        def isatty(self):
            return True

    class FakeLive:
        instance = None

        def __init__(self, **_kwargs):
            self.stopped = False
            self.__class__.instance = self

        def start(self):
            started.set()
            release_start.wait(timeout=1)

        def stop(self):
            self.stopped = True

    class TrackingLock:
        def __init__(self):
            self._lock = threading.Lock()

        def __enter__(self):
            if threading.current_thread().name == "dashboard-close":
                close_attempted.set()
            self._lock.acquire()
            return self

        def __exit__(self, *_args):
            self._lock.release()

    monkeypatch.setattr(progress, "Live", FakeLive)
    dashboard = TaskDashboard(title="backtest", stream=TTYStream())
    dashboard._lifecycle_lock = TrackingLock()
    start_thread = threading.Thread(target=dashboard.start, name="dashboard-start")
    close_thread = threading.Thread(target=dashboard.close, name="dashboard-close")
    start_thread.start()
    try:
        assert started.wait(timeout=1)
        close_thread.start()
        assert close_attempted.wait(timeout=1)
    finally:
        release_start.set()
        start_thread.join(timeout=1)
        close_thread.join(timeout=1)

    assert not start_thread.is_alive()
    assert not close_thread.is_alive()
    assert dashboard._live is None
    assert FakeLive.instance.stopped


def test_start_is_idempotent(monkeypatch):
    class TTYStream(StringIO):
        def isatty(self):
            return True

    class FakeLive:
        instances = []

        def __init__(self, **_kwargs):
            self.stopped = False
            self.__class__.instances.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(progress, "Live", FakeLive)
    dashboard = TaskDashboard(title="backtest", stream=TTYStream())
    dashboard.start()
    dashboard.start()
    dashboard.close()

    assert len(FakeLive.instances) == 1
    assert FakeLive.instances[0].stopped

    plain_stream = StringIO()
    plain_dashboard = TaskDashboard(title="backtest", stream=plain_stream)
    plain_dashboard.start()
    plain_dashboard.start()
    plain_dashboard.close()
    assert plain_stream.getvalue().count("event=start") == 1


def test_tty_live_reads_current_dashboard_state(monkeypatch):
    class TTYStream(StringIO):
        def isatty(self):
            return True

    class FakeLive:
        instances = []

        def __init__(self, renderable=None, **kwargs):
            self.renderable = renderable
            self.get_renderable = kwargs.get("get_renderable")
            self.refreshed = 0
            self.__class__.instances.append(self)

        def start(self):
            pass

        def refresh(self):
            self.refreshed += 1

        def stop(self):
            pass

    monkeypatch.setattr(progress, "Live", FakeLive)
    dashboard = TaskDashboard(title="backtest", total=1, stream=TTYStream())
    dashboard.start()
    dashboard.task_start("BTCUSDT")

    live = FakeLive.instances[-1]
    assert live.get_renderable is not None
    assert live.refreshed == 1
    rendered = StringIO()
    Console(file=rendered, force_terminal=False, width=120).print(live.get_renderable())
    assert "BTCUSDT" in rendered.getvalue()

    dashboard.close()


def test_progress_bar_format():
    assert _format_bar(0.5, width=10) == "[#####-----]"
    assert _format_bar(0.0, width=10) == "[----------]"
    assert _format_bar(1.0, width=10) == "[##########]"
    assert _format_bar(1.5, width=10) == "[##########]"
