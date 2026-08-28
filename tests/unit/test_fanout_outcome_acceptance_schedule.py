from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from project_mai_tai.strategy_core.time_utils import EASTERN_TZ


OPS = Path(__file__).resolve().parents[2] / "ops" / "health"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cron = _load("fanout_outcome_acceptance_cron", OPS / "fanout_outcome_acceptance_cron.py")
acceptance = _load("scheduled_fanout_outcome_acceptance", OPS / "fanout_outcome_acceptance.py")


def _pass_report():
    return SimpleNamespace(
        exit_code=0,
        verdict="PASS",
        lines=(
            "metric=paired_legs verdict=PASS paired_legs=1 usable=1 of 1",
            "metric=fill_rate verdict=PASS mirror=1/10 schwab=1/10 gap_pp=0.0",
            "metric=duplicate_legs verdict=PASS duplicate_legs=0 of 1",
            "metric=refused_exits verdict=PASS refused_exits=0 post_exit_episodes=1",
            "verdict=PASS",
        ),
    )


def test_installed_loader_executes_the_real_acceptance_module() -> None:
    loaded = cron._load_acceptance(OPS / "fanout_outcome_acceptance.py")

    assert loaded.SQL == acceptance.SQL
    assert callable(loaded.run_report)


def test_runner_help_starts_without_the_application_package_on_sys_path() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(OPS / "fanout_outcome_acceptance_cron.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_completed_window_matches_the_controls_calendar_day_not_the_old_entry_slice() -> None:
    window = cron.completed_session_window(
        datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ)
    )

    assert window.session_date == "2026-08-28"
    assert window.since.isoformat() == "2026-08-28T00:00:00-04:00"
    assert window.until.isoformat() == "2026-08-29T00:00:00-04:00"
    # The paired/fill/refusal controls use midnight ET boundaries. This assertion kills the old
    # 07:00-16:00 scheduler slice even though both windows name the same trading date. The duplicate
    # control intentionally retains its published incident interval.
    assert "2026-08-21 00:00 America/New_York" in acceptance.SQL
    assert window.since.timetz().replace(tzinfo=None) == datetime.min.time()
    assert window.until.timetz().replace(tzinfo=None) == datetime.min.time()


def test_completed_window_skips_weekend_and_full_closure_holiday() -> None:
    tuesday_after_midnight = cron.completed_session_window(
        datetime(2026, 9, 1, 0, 17, tzinfo=EASTERN_TZ)
    )
    labor_day_after_close = cron.completed_session_window(
        datetime(2026, 9, 8, 0, 17, tzinfo=EASTERN_TZ)
    )

    assert tuesday_after_midnight.session_date == "2026-08-31"
    assert labor_day_after_close.session_date == "2026-09-04"


def test_naive_clock_is_refused_instead_of_selecting_an_implicit_timezone() -> None:
    with pytest.raises(ValueError, match="explicit timezone"):
        cron.completed_session_window(datetime(2026, 8, 28, 17, 0))


def test_new_window_clears_yesterdays_success_before_acceptance_import(tmp_path) -> None:
    status_path = tmp_path / "STATUS.txt"
    status_path.write_text(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-08-27 verdict=PASS\n",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError):
        cron.run_once(
            now=datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ),
            acceptance=None,
            acceptance_path=tmp_path / "missing-check.py",
            out_dir=tmp_path,
            notify=lambda _title, _body: True,
        )

    status = status_path.read_text(encoding="utf-8")
    assert "[D6-OUTCOME-ACCEPTANCE-STARTED] session=2026-08-28" in status
    assert "verdict=IN_PROGRESS success_marker=absent" in status
    assert "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" not in status
    history = (tmp_path / "history.log").read_text(encoding="utf-8")
    assert "session=pending_calendar" in history
    assert "[D6-OUTCOME-ACCEPTANCE-STARTED] session=2026-08-28" in history


def test_calendar_import_failure_cannot_leave_yesterdays_success(monkeypatch, tmp_path) -> None:
    status_path = tmp_path / "STATUS.txt"
    status_path.write_text(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-08-27 verdict=PASS\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cron,
        "completed_session_window",
        lambda _now: (_ for _ in ()).throw(ImportError("stale production venv")),
    )

    with pytest.raises(ImportError, match="stale production venv"):
        cron.run_once(
            now=datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ),
            acceptance=None,
            acceptance_path=tmp_path / "check.py",
            out_dir=tmp_path,
            notify=lambda _title, _body: True,
        )

    status = status_path.read_text(encoding="utf-8")
    assert "session=pending_calendar" in status
    assert "verdict=IN_PROGRESS success_marker=absent" in status
    assert "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" not in status


def test_atomic_status_write_flushes_contents_before_replace(monkeypatch, tmp_path) -> None:
    flushed = []
    monkeypatch.setattr(cron.os, "fsync", lambda fd: flushed.append(fd))

    cron._atomic_write(tmp_path / "STATUS.txt", "durable\n")

    assert flushed
    assert (tmp_path / "STATUS.txt").read_text(encoding="utf-8") == "durable\n"


def test_notification_treats_http_429_as_failure(monkeypatch) -> None:
    captured = {}

    def fake_curl(command, **kwargs):
        captured["command"] = command
        # This behaves like curl: an HTTP response is exit 0 without --fail-with-body, but an
        # HTTP 429 is exit 22 with it. Removing the flag therefore makes this control fail.
        return subprocess.CompletedProcess(
            command,
            22 if "--fail-with-body" in command else 0,
            stdout="rate limited",
            stderr="",
        )

    monkeypatch.setattr(cron.subprocess, "run", fake_curl)

    assert cron.send_notification("D6 test", "body") is False
    assert "--fail-with-body" in captured["command"]


def test_selected_session_dates_reach_the_real_sql_over_psql_stdin(
    monkeypatch, tmp_path
) -> None:
    captured = {}
    header = "kind,c1,c2,c3,c4,c5,c6,c7,c8,c9\n"

    def fake_psql(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=header, stderr="")

    notifications = []
    monkeypatch.setattr(acceptance.subprocess, "run", fake_psql)
    result = cron.run_once(
        now=datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ),
        acceptance=acceptance,
        out_dir=tmp_path,
        notify=lambda title, body: notifications.append((title, body)) or True,
    )

    assert result.exit_code == acceptance.COULD_NOT_TELL
    command = captured["command"]
    assert "window_since=2026-08-28T00:00:00-04:00" in command
    assert "window_until=2026-08-29T00:00:00-04:00" in command
    assert command[-2:] == ["-f", "-"]
    assert captured["input"] == acceptance.SQL
    sql = captured["input"]
    assert ":'window_since'" in sql and ":'window_until'" in sql
    exit_episode_sql = sql.split("target_exit_episodes AS (", 1)[1].split("),\nrows AS (", 1)[0]
    assert "CROSS JOIN target_bounds" in exit_episode_sql
    assert "sfbo.first_fill_at >= b.since_at" in exit_episode_sql
    assert "sfbo.first_fill_at < b.until_at" in exit_episode_sql
    assert notifications and "verdict=COULD_NOT_TELL" in notifications[0][1]


def test_pass_writes_success_marker_with_all_denominators_and_deduplicates(tmp_path) -> None:
    fake = SimpleNamespace(run_report=lambda **_kwargs: _pass_report())
    notifications = []
    now = datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ)

    first = cron.run_once(
        now=now,
        acceptance=fake,
        out_dir=tmp_path,
        notify=lambda title, body: notifications.append((title, body)) or True,
    )
    second = cron.run_once(
        now=now,
        acceptance=fake,
        out_dir=tmp_path,
        notify=lambda title, body: notifications.append((title, body)) or True,
    )

    status = (tmp_path / "STATUS.txt").read_text(encoding="utf-8")
    assert first.exit_code == 0
    assert "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" in status
    assert "denominators=present" in status
    assert "usable=1 of 1" in status
    assert "mirror=1/10 schwab=1/10" in status
    assert "duplicate_legs=0 of 1" in status
    assert "post_exit_episodes=1" in status
    assert notifications == []
    assert "reason=already_reported denominator=one completed ET calendar-day session" in second.lines[0]


def test_nonpass_notifies_and_never_emits_the_success_marker(tmp_path) -> None:
    report = _pass_report()
    report = SimpleNamespace(exit_code=1, verdict="FAIL", lines=report.lines[:-1] + ("verdict=FAIL",))
    fake = SimpleNamespace(run_report=lambda **_kwargs: report)
    notifications = []

    result = cron.run_once(
        now=datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ),
        acceptance=fake,
        out_dir=tmp_path,
        notify=lambda title, body: notifications.append((title, body)) or True,
    )

    assert result.exit_code == 1
    assert len(notifications) == 1
    status = (tmp_path / "STATUS.txt").read_text(encoding="utf-8")
    assert "[D6-OUTCOME-ACCEPTANCE-NONPASS]" in status
    assert "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" not in status


def test_prior_nonpass_is_rerun_instead_of_skipped_as_success(tmp_path) -> None:
    now = datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ)
    failed = _pass_report()
    failed = SimpleNamespace(exit_code=1, verdict="FAIL", lines=failed.lines[:-1] + ("verdict=FAIL",))
    first = cron.run_once(
        now=now,
        acceptance=SimpleNamespace(run_report=lambda **_kwargs: failed),
        out_dir=tmp_path,
        notify=lambda _title, _body: True,
    )
    calls = []
    second = cron.run_once(
        now=now,
        acceptance=SimpleNamespace(run_report=lambda **_kwargs: calls.append(1) or _pass_report()),
        out_dir=tmp_path,
        notify=lambda _title, _body: True,
    )

    assert first.exit_code == 1
    assert calls == [1]
    assert second.exit_code == 0
    assert "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" in (
        tmp_path / "STATUS.txt"
    ).read_text(encoding="utf-8")


def test_a_pass_missing_a_denominator_fails_closed_and_notifies(tmp_path) -> None:
    report = _pass_report()
    bad_lines = tuple(line for line in report.lines if not line.startswith("metric=refused_exits "))
    fake = SimpleNamespace(
        run_report=lambda **_kwargs: SimpleNamespace(
            exit_code=0,
            verdict="PASS",
            lines=bad_lines,
        )
    )
    notifications = []

    result = cron.run_once(
        now=datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ),
        acceptance=fake,
        out_dir=tmp_path,
        notify=lambda title, body: notifications.append((title, body)) or True,
    )

    assert result.exit_code == cron.COULD_NOT_TELL
    assert notifications
    assert "denominators=invalid" in (tmp_path / "STATUS.txt").read_text(encoding="utf-8")


def test_failed_notification_is_visible_and_session_remains_retryable(tmp_path) -> None:
    report = _pass_report()
    report = SimpleNamespace(exit_code=1, verdict="FAIL", lines=report.lines[:-1] + ("verdict=FAIL",))
    fake = SimpleNamespace(run_report=lambda **_kwargs: report)

    result = cron.run_once(
        now=datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ),
        acceptance=fake,
        out_dir=tmp_path,
        notify=lambda _title, _body: False,
    )

    assert result.exit_code == cron.COULD_NOT_TELL
    assert not (tmp_path / "last_attempted_session.txt").exists()
    status = (tmp_path / "STATUS.txt").read_text(encoding="utf-8")
    assert "notification=FAILED session_not_marked=1" in status
    assert "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" not in status


def test_installer_is_root_only_and_manages_one_repo_owned_cron_block() -> None:
    installer = (OPS / "install_fanout_outcome_acceptance.sh").read_text(encoding="utf-8")

    assert 'echo "REFUSED: run as root"' in installer
    assert 'source_check="$repo_root/ops/health/fanout_outcome_acceptance.py"' in installer
    assert 'source_cron="$repo_root/ops/health/fanout_outcome_acceptance_cron.py"' in installer
    assert 'cron_line="17 4,5,6 * * 2-6 ' in installer
    assert "malformed existing D6 cron block" in installer
    assert 'verify_runtime "$python_bin" "$target_cron"' in installer


def test_installer_executes_help_with_the_selected_runtime(tmp_path) -> None:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    assert bash is not None
    installer = (OPS / "install_fanout_outcome_acceptance.sh").as_posix()
    fake_python = tmp_path / "python"
    fake_cron = tmp_path / "cron.py"
    calls = tmp_path / "calls.txt"
    fake_cron.write_text("# target\n", encoding="utf-8")
    fake_python.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > '{calls.as_posix()}'\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    command = (
        f"MAI_TAI_INSTALLER_LIB_ONLY=1 source '{installer}'; "
        f"verify_runtime '{fake_python.as_posix()}' '{fake_cron.as_posix()}'"
    )

    result = subprocess.run([bash, "-lc", command], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert calls.read_text(encoding="utf-8").strip() == f"{fake_cron.as_posix()} --help"
