from io import StringIO

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
    assert "event=start task=backtest total=240 runs=240" in text
    assert "event=complete task=backtest status=ok done=0 total=240" in text


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


def test_progress_bar_format():
    assert _format_bar(0.5, width=10) == "[#####-----]"
    assert _format_bar(0.0, width=10) == "[----------]"
    assert _format_bar(1.0, width=10) == "[##########]"
    assert _format_bar(1.5, width=10) == "[##########]"
