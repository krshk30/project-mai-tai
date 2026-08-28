from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
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


def test_completed_window_uses_today_only_after_the_1600_et_boundary() -> None:
    before = cron.completed_session_window(
        datetime(2026, 8, 28, 15, 59, tzinfo=EASTERN_TZ)
    )
    after = cron.completed_session_window(
        datetime(2026, 8, 28, 16, 1, tzinfo=EASTERN_TZ)
    )

    assert before.session_date == "2026-08-27"
    assert after.session_date == "2026-08-28"
    assert after.since.isoformat() == "2026-08-28T07:00:00-04:00"
    assert after.until.isoformat() == "2026-08-28T16:00:00-04:00"


def test_completed_window_skips_weekend_and_full_closure_holiday() -> None:
    monday_before_close = cron.completed_session_window(
        datetime(2026, 8, 31, 9, 0, tzinfo=EASTERN_TZ)
    )
    labor_day_after_close = cron.completed_session_window(
        datetime(2026, 9, 7, 17, 0, tzinfo=EASTERN_TZ)
    )

    assert monday_before_close.session_date == "2026-08-28"
    assert labor_day_after_close.session_date == "2026-09-04"


def test_naive_clock_is_refused_instead_of_selecting_an_implicit_timezone() -> None:
    with pytest.raises(ValueError, match="explicit timezone"):
        cron.completed_session_window(datetime(2026, 8, 28, 17, 0))


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
        now=datetime(2026, 8, 28, 16, 17, tzinfo=EASTERN_TZ),
        acceptance=acceptance,
        out_dir=tmp_path,
        notify=lambda title, body: notifications.append((title, body)) or True,
    )

    assert result.exit_code == acceptance.COULD_NOT_TELL
    command = captured["command"]
    assert "window_since=2026-08-28T07:00:00-04:00" in command
    assert "window_until=2026-08-28T16:00:00-04:00" in command
    assert command[-2:] == ["-f", "-"]
    assert captured["input"] == acceptance.SQL
    assert notifications and "verdict=COULD_NOT_TELL" in notifications[0][1]


def test_pass_writes_success_marker_with_all_denominators_and_deduplicates(tmp_path) -> None:
    fake = SimpleNamespace(run_report=lambda **_kwargs: _pass_report())
    notifications = []
    now = datetime(2026, 8, 28, 16, 17, tzinfo=EASTERN_TZ)

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
    assert "reason=already_reported denominator=one completed session" in second.lines[0]


def test_nonpass_notifies_and_never_emits_the_success_marker(tmp_path) -> None:
    report = _pass_report()
    report = SimpleNamespace(exit_code=1, verdict="FAIL", lines=report.lines[:-1] + ("verdict=FAIL",))
    fake = SimpleNamespace(run_report=lambda **_kwargs: report)
    notifications = []

    result = cron.run_once(
        now=datetime(2026, 8, 28, 16, 17, tzinfo=EASTERN_TZ),
        acceptance=fake,
        out_dir=tmp_path,
        notify=lambda title, body: notifications.append((title, body)) or True,
    )

    assert result.exit_code == 1
    assert len(notifications) == 1
    status = (tmp_path / "STATUS.txt").read_text(encoding="utf-8")
    assert "[D6-OUTCOME-ACCEPTANCE-NONPASS]" in status
    assert "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" not in status


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
        now=datetime(2026, 8, 28, 16, 17, tzinfo=EASTERN_TZ),
        acceptance=fake,
        out_dir=tmp_path,
        notify=lambda title, body: notifications.append((title, body)) or True,
    )

    assert result.exit_code == cron.COULD_NOT_TELL
    assert notifications
    assert "denominators=invalid" in (tmp_path / "STATUS.txt").read_text(encoding="utf-8")


def test_installer_is_root_only_and_manages_one_repo_owned_cron_block() -> None:
    installer = (OPS / "install_fanout_outcome_acceptance.sh").read_text(encoding="utf-8")

    assert 'echo "REFUSED: run as root"' in installer
    assert 'source_check="$repo_root/ops/health/fanout_outcome_acceptance.py"' in installer
    assert 'source_cron="$repo_root/ops/health/fanout_outcome_acceptance_cron.py"' in installer
    assert 'cron_line="17 20,21 * * 1-5 ' in installer
    assert "malformed existing D6 cron block" in installer
