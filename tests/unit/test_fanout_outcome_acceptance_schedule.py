from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from project_mai_tai.strategy_core.time_utils import EASTERN_TZ, US_MARKET_HOLIDAYS


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
fleet_health = _load("scheduled_fleet_health_check", OPS / "fleet_health_check.py")


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


def _csv_row(kind: str, *values: object) -> tuple[str, ...]:
    padded = tuple(str(value) for value in values) + ("",) * (9 - len(values))
    return (kind, *padded)


def _csv_output(*, include_outside_window_population: bool) -> str:
    rows = [
        _csv_row("CONTROL_PAIR", 53, 16, 37, 7, 9),
        _csv_row("CONTROL_FILL", 292, 18, 368, 34, 12),
        _csv_row("CONTROL_DUP", 22, 22, "4.58"),
        _csv_row("CONTROL_REFUSED", "2026-08-24", 37, 9, 2, 28),
        _csv_row("CONTROL_REFUSED", "2026-08-25", 25, 9, 2, 16),
        _csv_row("CONTROL_REFUSED", "2026-08-26", 49, 49, 11, 0),
    ]
    if include_outside_window_population:
        segment = "1787830200000"
        slot = "resting"
        slot_id = acceptance.fanout_slot_id(
            strategy_code="schwab_1m_v2",
            symbol="YYGH",
            segment_id=segment,
            slot=slot,
        )
        # The selected window ends at 2026-08-29 00:00 ET (04:00Z). Every target row below is
        # deliberately one minute outside it. A widened target CTE leaks this otherwise-complete
        # pair and changes all four zero-denominator metrics.
        outside = "2026-08-29 04:01:00+00"
        rows.extend(
            [
                _csv_row(
                    "FANOUT_FILL", "live:orb", "wb-outside", "YYGH", outside, 2.0,
                    segment, slot, slot_id, "rth_resting_mirror",
                ),
                _csv_row(
                    "FANOUT_FILL", "live:schwab_1m_v2", "sw-outside", "YYGH", outside,
                    2.0, segment, slot, slot_id, "",
                ),
                _csv_row(
                    "MATCHED_ORDER", "live:orb", "wb-outside", "YYGH", "filled", "true",
                    "rth_resting_mirror",
                ),
                _csv_row(
                    "MATCHED_ORDER", "live:schwab_1m_v2", "sw-outside", "YYGH", "filled",
                    "true", "",
                ),
                _csv_row("EXIT_EPISODE", "sell-outside", "2026-08-29"),
            ]
        )
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("kind", *(f"c{index}" for index in range(1, 10))))
    writer.writerows(rows)
    return output.getvalue()


def _cte(sql: str, name: str, following: str) -> str:
    return sql.split(f"{name} AS (", 1)[1].split(f"),\n{following} AS (", 1)[0]


def _target_window_text_tripwire(sql: str) -> bool:
    """Cheap shape tripwire; the real window proof runs SQL in PostgreSQL.

    This helper deliberately remains diagnostic only.  It cannot enumerate every
    equivalent widening.  ``tests/integration/test_acceptance_sql_windows.py`` imports
    ``acceptance.SQL`` and asks PostgreSQL whether inside/outside populations differ.
    """
    target_bounds = [
        line.strip()
        for line in _cte(sql, "target_bounds", "fill_by_order").splitlines()
        if line.strip()
    ]
    if target_bounds != [
        "SELECT :'window_since'::timestamptz AS since_at,",
        ":'window_until'::timestamptz AS until_at",
    ]:
        return False

    # Exact tails make this a useful fast tripwire for common edits.  They are not a semantic
    # proof: derived tables and earlier UNION branches can widen a population without changing
    # these strings, which is why the integration harness is load-bearing.
    direct = {
        "target_buy_legs": (
            "target_mirror_symbols",
            [
                "CROSS JOIN target_bounds b",
                "WHERE bo.side = 'buy'",
                "AND ba.name IN ('live:orb', 'live:schwab_1m_v2')",
                "AND fbo.first_fill_at >= b.since_at AND fbo.first_fill_at < b.until_at",
                "AND (",
                "(ba.name = 'live:orb' AND bo.payload::jsonb ? 'fanout_source')",
                "OR nullif(bo.payload::jsonb->>'fanout_segment_id', '') IS NOT NULL",
                ")",
            ],
        ),
        "target_mirror_symbols": (
            "target_matched_orders",
            [
                "CROSS JOIN target_bounds b",
                "WHERE ba.name = 'live:orb'",
                "AND bo.side = 'buy'",
                "AND upper(bo.order_type::text) = 'STOP_LIMIT'",
                "AND bo.payload::jsonb->>'fanout_source' = 'rth_resting_mirror'",
                "AND bo.submitted_at >= b.since_at AND bo.submitted_at < b.until_at",
            ],
        ),
        "target_matched_orders": (
            "target_refused",
            [
                "CROSS JOIN target_bounds b",
                "WHERE bo.side = 'buy'",
                "AND upper(bo.order_type::text) = 'STOP_LIMIT'",
                "AND bo.symbol IN (SELECT symbol FROM target_mirror_symbols)",
                "AND ba.name IN ('live:orb', 'live:schwab_1m_v2')",
                "AND bo.submitted_at >= b.since_at AND bo.submitted_at < b.until_at",
            ],
        ),
        "target_refused": (
            "target_refused_classified",
            [
                "CROSS JOIN target_bounds b",
                "WHERE ba.provider = 'webull'",
                "AND bo.side = 'sell'",
                "AND e.event_type = 'rejected'",
                "AND upper(coalesce(e.payload::jsonb->>'reason', '')) LIKE",
                "'%NEW_NO_POSITION%CAN_NOT_SELL_SHORT%'",
                "AND e.event_at >= b.since_at AND e.event_at < b.until_at",
            ],
        ),
        "target_exit_episodes": (
            "rows",
            [
                "CROSS JOIN target_bounds b",
                "WHERE ba.name = 'live:orb'",
                "AND bo.side = 'sell'",
                "AND sfbo.first_fill_at >= b.since_at AND sfbo.first_fill_at < b.until_at",
            ],
        ),
    }
    for name, (following, expected_tail) in direct.items():
        lines = [
            line.strip()
            for line in _cte(sql, name, following).splitlines()
            if line.strip()
        ]
        try:
            tail_start = lines.index("CROSS JOIN target_bounds b")
        except ValueError:
            return False
        if lines[tail_start:] != expected_tail:
            return False
    classified = _cte(sql, "target_refused_classified", "target_exit_episodes")
    return "FROM target_refused r" in {line.strip() for line in classified.splitlines()}


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


def test_independent_monitor_closure_calendar_matches_application_for_2026_2027() -> None:
    assert fleet_health._FULL_CLOSURES == US_MARKET_HOLIDAYS


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
            acceptance_sha256="0" * 64,
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


def test_prebootstrap_read_failure_cannot_restore_stale_success(monkeypatch, tmp_path) -> None:
    status_path = tmp_path / "STATUS.txt"
    status_path.write_text(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-08-27 verdict=PASS\n",
        encoding="utf-8",
    )
    original_read = Path.read_text

    def fail_detached_read(path, *args, **kwargs):
        if path.name.startswith(".STATUS.txt.prior-"):
            raise PermissionError("controlled prior-status read failure")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_detached_read)
    result = cron.run_once(
        now=datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ),
        acceptance=SimpleNamespace(run_report=lambda **_kwargs: _pass_report()),
        out_dir=tmp_path,
        notify=lambda _title, _body: True,
    )

    assert result.exit_code == 0
    current = original_read(status_path, encoding="utf-8")
    assert "session=2026-08-28" in current
    assert "session=2026-08-27" not in current


def test_prebootstrap_write_failure_removes_stale_success_from_live_path(
    monkeypatch, tmp_path
) -> None:
    status_path = tmp_path / "STATUS.txt"
    stale = "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-08-27 verdict=PASS\n"
    status_path.write_text(stale, encoding="utf-8")

    def fail_write(_path, _contents):
        raise PermissionError("controlled bootstrap write failure")

    monkeypatch.setattr(cron, "_atomic_write", fail_write)
    with pytest.raises(PermissionError, match="bootstrap write failure"):
        cron.run_once(
            now=datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ),
            acceptance=SimpleNamespace(run_report=lambda **_kwargs: _pass_report()),
            out_dir=tmp_path,
            notify=lambda _title, _body: True,
        )

    assert not status_path.exists()
    detached = list(tmp_path.glob(".STATUS.txt.prior-*"))
    assert len(detached) == 1
    assert detached[0].read_text(encoding="utf-8") == stale


def test_history_rotates_to_one_bounded_predecessor(monkeypatch, tmp_path) -> None:
    history = tmp_path / "history.log"
    history.write_text("old-history", encoding="utf-8")
    monkeypatch.setattr(cron, "HISTORY_MAX_BYTES", 5)

    cron._append_history(
        history,
        now=datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ),
        contents="new-history\n",
    )

    assert history.read_text(encoding="utf-8").endswith("new-history\n")
    assert (tmp_path / "history.log.1").read_text(encoding="utf-8") == "old-history"


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


def test_textual_target_window_tripwire_matches_the_current_query_shape() -> None:
    assert _target_window_text_tripwire(acceptance.SQL)


def test_textual_tripwire_rejects_adjacent_or_true_and_a_widened_bounds_source() -> None:
    adjacent_predicate = acceptance.SQL.replace(
        "AND sfbo.first_fill_at >= b.since_at AND sfbo.first_fill_at < b.until_at",
        "AND sfbo.first_fill_at >= b.since_at AND sfbo.first_fill_at < b.until_at\n"
        "      OR TRUE",
        1,
    )
    widened_source = acceptance.SQL.replace(
        ":'window_until'::timestamptz AS until_at",
        ":'window_until'::timestamptz + interval '3650 days' AS until_at",
        1,
    )

    assert _target_window_text_tripwire(adjacent_predicate) is False
    assert _target_window_text_tripwire(widened_source) is False


def test_fake_psql_smoke_selects_different_control_legs_and_verdicts(
    monkeypatch,
) -> None:
    """Pin scheduler wiring only; PostgreSQL integration owns window semantics."""
    outputs = iter(
        [
            _csv_output(include_outside_window_population=False),
            _csv_output(include_outside_window_population=True),
        ]
    )
    submitted_sql: list[str] = []

    def fake_psql(command, **kwargs):
        submitted_sql.append(kwargs["input"])
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=next(outputs),
            stderr="",
        )

    monkeypatch.setattr(acceptance.subprocess, "run", fake_psql)
    bounded_sql = acceptance.SQL
    bounded_report = acceptance.run_report(
        since=datetime(2026, 8, 28, 0, 0, tzinfo=EASTERN_TZ),
        until=datetime(2026, 8, 29, 0, 0, tzinfo=EASTERN_TZ),
    )
    widened_sql = bounded_sql.replace(
        "AND fbo.first_fill_at >= b.since_at AND fbo.first_fill_at < b.until_at",
        "AND fbo.first_fill_at >= b.since_at AND fbo.first_fill_at < b.until_at "
        "+ interval '3650 days'",
    ).replace(
        "AND bo.submitted_at >= b.since_at AND bo.submitted_at < b.until_at",
        "AND bo.submitted_at >= b.since_at AND bo.submitted_at < b.until_at OR TRUE",
    ).replace(
        "AND e.event_at >= b.since_at AND e.event_at < b.until_at",
        "AND e.event_at >= b.since_at AND e.event_at < b.until_at OR TRUE",
    ).replace(
        "AND sfbo.first_fill_at >= b.since_at AND sfbo.first_fill_at < b.until_at",
        "AND sfbo.first_fill_at >= b.since_at AND sfbo.first_fill_at < b.until_at "
        "+ interval '3650 days'",
    )
    monkeypatch.setattr(acceptance, "SQL", widened_sql)
    widened_report = acceptance.run_report(
        since=datetime(2026, 8, 28, 0, 0, tzinfo=EASTERN_TZ),
        until=datetime(2026, 8, 29, 0, 0, tzinfo=EASTERN_TZ),
    )

    assert _target_window_text_tripwire(bounded_sql) is True
    assert _target_window_text_tripwire(widened_sql) is False
    assert submitted_sql == [bounded_sql, widened_sql]
    assert bounded_report.exit_code == acceptance.UNEXERCISED
    output = "\n".join(bounded_report.lines)
    assert "paired_legs=0 usable=0 of 0" in output
    assert "mirror=0/0 schwab=0/0" in output
    assert "duplicate_legs=0 of 0" in output
    assert "refused_exits=0 post_exit_episodes=0" in output
    assert widened_report.exit_code == acceptance.PASS
    widened_output = "\n".join(widened_report.lines)
    assert "paired_legs=1 usable=1 of 1" in widened_output
    assert "metric=fill_rate verdict=PASS" in widened_output
    assert "refused_exits=0 post_exit_episodes=1" in widened_output


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
    history = (tmp_path / "history.log").read_text(encoding="utf-8")
    assert "[D6-OUTCOME-ACCEPTANCE-SKIPPED] session=2026-08-28" in history


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


@pytest.mark.parametrize(
    ("metric", "needle", "replacement"),
    (
        ("paired_legs", "usable=", "population="),
        ("paired_legs", " of ", " / "),
        ("fill_rate", "mirror=", "webull="),
        ("fill_rate", "schwab=", "primary="),
        ("duplicate_legs", " of ", " / "),
        ("refused_exits", "post_exit_episodes=", "episodes="),
    ),
)
def test_a_pass_missing_any_denominator_field_fails_closed_and_notifies(
    tmp_path, metric: str, needle: str, replacement: str
) -> None:
    report = _pass_report()
    bad_lines = tuple(
        line.replace(needle, replacement, 1) if line.startswith(f"metric={metric} ") else line
        for line in report.lines
    )
    assert bad_lines != report.lines
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
    status = (tmp_path / "STATUS.txt").read_text(encoding="utf-8")
    assert "denominators=invalid" in status
    assert f"metric={metric} omitted its denominator" in status
    assert "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" not in status


@pytest.mark.parametrize("metric", ("paired_legs", "fill_rate", "duplicate_legs", "refused_exits"))
@pytest.mark.parametrize("match_count", (0, 2))
def test_a_pass_with_missing_or_duplicate_metric_line_fails_closed(
    tmp_path, metric: str, match_count: int
) -> None:
    report = _pass_report()
    metric_line = next(line for line in report.lines if line.startswith(f"metric={metric} "))
    remaining = tuple(line for line in report.lines if line != metric_line)
    bad_lines = remaining + ((metric_line,) * match_count)
    notifications = []

    result = cron.run_once(
        now=datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ),
        acceptance=SimpleNamespace(
            run_report=lambda **_kwargs: SimpleNamespace(
                exit_code=0,
                verdict="PASS",
                lines=bad_lines,
            )
        ),
        out_dir=tmp_path,
        notify=lambda title, body: notifications.append((title, body)) or True,
    )

    status = (tmp_path / "STATUS.txt").read_text(encoding="utf-8")
    assert result.exit_code == cron.COULD_NOT_TELL
    assert notifications
    assert f"expected one metric={metric} line, found {match_count}" in status
    assert "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" not in status


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
    history = (tmp_path / "history.log").read_text(encoding="utf-8")
    assert "notification=FAILED session_not_marked=1" in history


def test_runtime_refuses_a_stale_acceptance_artifact_before_import(tmp_path) -> None:
    check = tmp_path / "check.py"
    check.write_text("raise AssertionError('must not import stale bytes')\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="artifact SHA-256 mismatch"):
        cron.run_once(
            now=datetime(2026, 8, 29, 0, 17, tzinfo=EASTERN_TZ),
            acceptance=None,
            acceptance_path=check,
            acceptance_sha256="0" * 64,
            out_dir=tmp_path,
            notify=lambda _title, _body: True,
        )

    status = (tmp_path / "STATUS.txt").read_text(encoding="utf-8")
    assert "verdict=IN_PROGRESS success_marker=absent" in status
    assert "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" not in status


def test_installer_is_root_only_and_manages_one_repo_owned_cron_block() -> None:
    installer = (OPS / "install_fanout_outcome_acceptance.sh").read_text(encoding="utf-8")
    installer_lib = (OPS / "fanout_outcome_acceptance_install_lib.sh").read_text(
        encoding="utf-8"
    )

    assert 'echo "REFUSED: run as root"' in installer_lib
    assert 'source_check="$repo_root/ops/health/fanout_outcome_acceptance.py"' in installer
    assert 'source_cron="$repo_root/ops/health/fanout_outcome_acceptance_cron.py"' in installer
    assert 'cron_line="17 4,5,6 * * 2-6 ' in installer
    assert "--acceptance-sha256 $source_check_sha256" in installer
    assert "--out-dir $target_dir" in installer
    assert "malformed existing D6 cron block" in installer_lib
    assert "MAI_TAI_INSTALLER_LIB_ONLY" not in installer
    assert 'if ! require_root "$effective_uid"; then' in installer
    assert 'if ! verify_d6_installed_copy "$source_check" "$target_check"; then' in installer
    assert 'if ! verify_d6_installed_copy "$source_cron" "$target_cron"; then' in installer
    assert (
        'if ! verify_d6_existing_cron_block "$begin_marker" "$end_marker" "$current_cron"; then'
        in installer
    )
    assert 'if ! verify_exactly_one_d6_schedule "$cron_line" "$installed_cron"; then' in installer
    assert 'if ! verify_d6_runtime "$python_bin" "$target_cron" "$target_check" ' in installer
    assert cron.DEFAULT_OUT_DIR / "STATUS.txt" == fleet_health._D6_STATUS_PATH

    target_dir_line = next(
        line for line in installer.splitlines() if line.startswith("production_target_dir=")
    )
    shell_target_dir = Path(target_dir_line.split("=", 1)[1])
    assert shell_target_dir == cron.DEFAULT_OUT_DIR
    assert shell_target_dir == fleet_health._D6_STATUS_PATH.parent
    assert "first 00:17 ET D6 cron success" in installer


@pytest.mark.parametrize(
    ("call_prefix", "helper"),
    (
        ('if ! require_root "$effective_uid";', "require_root"),
        (
            'if ! verify_d6_installed_copy "$source_check" "$target_check";',
            "verify_d6_installed_copy",
        ),
        (
            'if ! verify_d6_installed_copy "$source_cron" "$target_cron";',
            "verify_d6_installed_copy",
        ),
        ('if ! verify_d6_runtime "$python_bin"', "verify_d6_runtime"),
        (
            'if ! verify_d6_existing_cron_block "$begin_marker"',
            "verify_d6_existing_cron_block",
        ),
        (
            'if ! verify_exactly_one_d6_schedule "$cron_line"',
            "verify_exactly_one_d6_schedule",
        ),
    ),
)
def test_each_real_installer_guard_call_refuses_even_without_errexit(
    tmp_path: Path, call_prefix: str, helper: str
) -> None:
    """Every side-effect boundary must stop explicitly, independent of ``set -e``."""
    installer_lines = (OPS / "install_fanout_outcome_acceptance.sh").read_text(
        encoding="utf-8"
    ).splitlines()
    call_index = next(
        index for index, line in enumerate(installer_lines) if line.startswith(call_prefix)
    )
    call_site = "\n".join(installer_lines[call_index : call_index + 3])
    survived = tmp_path / "survived"
    harness = tmp_path / "guard.sh"
    harness.write_text(
        "set -uo pipefail\n"
        "set +e\n"
        f"{helper}() {{ return 23; }}\n"
        "effective_uid=0\n"
        "source_check=source-check target_check=target-check\n"
        "source_cron=source-cron target_cron=target-cron\n"
        "python_bin=python target_dir=target-dir source_check_sha256=digest\n"
        "begin_marker=begin end_marker=end current_cron=current-cron\n"
        "cron_line=cron-line installed_cron=installed-cron\n"
        f"{call_site}\n"
        f"touch '{survived.as_posix()}'\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [_bash_executable(), str(harness)], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert not survived.exists()


def _bash_executable() -> str:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    assert bash is not None
    return bash


def _run_install_helper(command: str) -> subprocess.CompletedProcess[str]:
    installer_lib = (OPS / "fanout_outcome_acceptance_install_lib.sh").as_posix()
    return subprocess.run(
        [_bash_executable(), "-lc", f"source '{installer_lib}'; {command}"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_installer_library_guards_have_reachable_positive_and_negative_polarities(
    tmp_path,
) -> None:
    reviewed = tmp_path / "reviewed.py"
    exact = tmp_path / "exact.py"
    stale = tmp_path / "stale.py"
    reviewed.write_text("reviewed\n", encoding="utf-8")
    exact.write_text("reviewed\n", encoding="utf-8")
    stale.write_text("stale\n", encoding="utf-8")
    expected = "17 4,5,6 * * 2-6 python cron.py"
    empty_cron = tmp_path / "empty.cron"
    one_block = tmp_path / "one.cron"
    two_blocks = tmp_path / "two.cron"
    reversed_block = tmp_path / "reversed.cron"
    empty_cron.write_text("# unrelated\n", encoding="utf-8")
    one_block.write_text("# BEGIN D6\njob\n# END D6\n", encoding="utf-8")
    two_blocks.write_text(
        "# BEGIN D6\njob\n# END D6\n# BEGIN D6\njob\n# END D6\n", encoding="utf-8"
    )
    reversed_block.write_text("# END D6\njob\n# BEGIN D6\n", encoding="utf-8")

    assert _run_install_helper("require_root 0").returncode == 0
    assert _run_install_helper("require_root 1").returncode != 0
    assert _run_install_helper(
        f"verify_d6_installed_copy '{reviewed.as_posix()}' '{exact.as_posix()}'"
    ).returncode == 0
    assert _run_install_helper(
        f"verify_d6_installed_copy '{reviewed.as_posix()}' '{stale.as_posix()}'"
    ).returncode != 0

    # Exactly one is the accepted boundary; both adjacent counts refuse.
    assert _run_install_helper(
        f"verify_exactly_one_d6_schedule '{expected}' ''"
    ).returncode != 0
    assert _run_install_helper(
        f"verify_exactly_one_d6_schedule '{expected}' '{expected}'"
    ).returncode == 0
    assert _run_install_helper(
        f"verify_exactly_one_d6_schedule '{expected}' $'{expected}\\n{expected}'"
    ).returncode != 0

    # Zero or one well-ordered managed blocks are valid; two, reversed, or unreadable refuse.
    for path in (empty_cron, one_block):
        assert _run_install_helper(
            f"verify_d6_existing_cron_block '# BEGIN D6' '# END D6' '{path.as_posix()}'"
        ).returncode == 0
    for path in (two_blocks, reversed_block, tmp_path / "missing.cron"):
        assert _run_install_helper(
            f"verify_d6_existing_cron_block '# BEGIN D6' '# END D6' '{path.as_posix()}'"
        ).returncode != 0


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def _shell_path(path: Path) -> str:
    rendered = path.resolve().as_posix()
    if len(rendered) >= 3 and rendered[1:3] == ":/":
        return f"/{rendered[0].lower()}{rendered[2:]}"
    return rendered


def _whole_installer_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path, Path]:
    repo = tmp_path / "repo"
    health = repo / "ops" / "health"
    health.mkdir(parents=True)
    for name in (
        "install_fanout_outcome_acceptance.sh",
        "fanout_outcome_acceptance_install_lib.sh",
        "fanout_outcome_acceptance.py",
        "fanout_outcome_acceptance_cron.py",
    ):
        shutil.copy2(OPS / name, health / name)

    test_root = tmp_path / "runtime"
    test_root.mkdir()
    fake_bin = test_root / "fake-bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    cron_state = tmp_path / "root.crontab"
    _write_executable(
        fake_bin / "id",
        "#!/usr/bin/env bash\nif [[ ${1:-} == -u ]]; then echo 0; else /usr/bin/id \"$@\"; fi\n",
    )
    _write_executable(
        fake_bin / "python3",
        f"#!/usr/bin/env bash\nexec '{Path(sys.executable).as_posix()}' \"$@\"\n",
    )
    _write_executable(
        fake_bin / "install",
        "#!/usr/bin/env bash\n"
        "printf 'install %s\\n' \"$*\" >> \"$FAKE_COMMAND_LOG\"\n"
        "args=()\n"
        "while (($#)); do\n"
        "  case \"$1\" in -o|-g) shift 2 ;; *) args+=(\"$1\"); shift ;; esac\n"
        "done\n"
        "exec /usr/bin/install \"${args[@]}\"\n",
    )
    _write_executable(
        fake_bin / "sha256sum",
        "#!/usr/bin/env bash\n"
        "printf 'sha256sum %s\\n' \"$*\" >> \"$FAKE_COMMAND_LOG\"\n"
        "exec /usr/bin/sha256sum \"$@\"\n",
    )
    _write_executable(
        fake_bin / "crontab",
        "#!/usr/bin/env bash\n"
        "printf 'crontab %s\\n' \"$*\" >> \"$FAKE_COMMAND_LOG\"\n"
        "if [[ ${1:-} == -l ]]; then\n"
        "  if [[ ${FAKE_CRONTAB_READ_FAIL:-0} == 1 ]]; then\n"
        "    echo 'permission denied reading root crontab' >&2; exit 17\n"
        "  fi\n"
        "  if [[ -f $FAKE_CRONTAB_STATE ]]; then cat \"$FAKE_CRONTAB_STATE\"; exit 0; fi\n"
        "  echo 'no crontab for root' >&2; exit 1\n"
        "fi\n"
        "cp \"$1\" \"$FAKE_CRONTAB_STATE\"\n",
    )
    _write_executable(
        test_root / "python",
        f"#!/usr/bin/env bash\nexec '{Path(sys.executable).as_posix()}' \"$@\"\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": ":".join(
                (
                    _shell_path(fake_bin),
                    _shell_path(Path(sys.executable).parent),
                    "/usr/local/bin",
                    "/usr/bin",
                    "/bin",
                )
            ),
            "MAI_TAI_D6_INSTALL_TEST_ROOT": _shell_path(test_root),
            "FAKE_COMMAND_LOG": command_log.as_posix(),
            "FAKE_CRONTAB_STATE": cron_state.as_posix(),
        }
    )
    return health / "install_fanout_outcome_acceptance.sh", env, command_log, cron_state


def test_whole_installer_succeeds_with_one_managed_schedule(tmp_path) -> None:
    installer, env, command_log, cron_state = _whole_installer_fixture(tmp_path)

    result = subprocess.run(
        [_bash_executable(), str(installer)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "first 00:17 ET D6 cron success" in result.stdout
    assert "restart_required=0" in result.stdout
    assert cron_state.read_text(encoding="utf-8").count(
        "/cron.py --acceptance "
    ) == 1
    commands = command_log.read_text(encoding="utf-8")
    assert "install -d" in commands
    assert "sha256sum" in commands
    assert commands.count("crontab -l") == 2


@pytest.mark.parametrize("shell_mode", ("normal", "without-set-e", "insert-set-plus-e"))
def test_whole_installer_refuses_unreadable_crontab_without_relying_on_errexit(
    tmp_path, shell_mode: str
) -> None:
    installer, env, command_log, cron_state = _whole_installer_fixture(tmp_path)
    source = installer.read_text(encoding="utf-8")
    assert source.count("set -euo pipefail") == 1
    if shell_mode == "without-set-e":
        source = source.replace("set -euo pipefail", "set -uo pipefail", 1)
    elif shell_mode == "insert-set-plus-e":
        source = source.replace("set -euo pipefail", "set -euo pipefail\nset +e", 1)
    installer.write_text(source, encoding="utf-8")
    installer.chmod(0o755)
    env["FAKE_CRONTAB_READ_FAIL"] = "1"

    result = subprocess.run(
        [_bash_executable(), str(installer)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "REFUSED: could not read the existing root crontab" in result.stderr
    assert "restart_required=0" not in result.stdout
    assert not cron_state.exists()
    assert command_log.read_text(encoding="utf-8").count("crontab -l") == 1


def test_installer_executes_help_with_the_selected_runtime(tmp_path) -> None:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    assert bash is not None
    installer_lib = (OPS / "fanout_outcome_acceptance_install_lib.sh").as_posix()
    fake_python = tmp_path / "python"
    fake_cron = tmp_path / "cron.py"
    fake_check = tmp_path / "check.py"
    calls = tmp_path / "calls.txt"
    fake_cron.write_text("# target\n", encoding="utf-8")
    fake_check.write_text("# checked artifact\n", encoding="utf-8")
    fake_python.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > '{calls.as_posix()}'\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    command = (
        f"source '{installer_lib}'; "
        f"verify_d6_runtime '{fake_python.as_posix()}' '{fake_cron.as_posix()}' "
        f"'{fake_check.as_posix()}' '{'a' * 64}' '{tmp_path.as_posix()}'"
    )

    result = subprocess.run([bash, "-lc", command], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert calls.read_text(encoding="utf-8").strip() == (
        f"{fake_cron.as_posix()} --acceptance {fake_check.as_posix()} "
        f"--acceptance-sha256 {'a' * 64} --out-dir {tmp_path.as_posix()} "
        "--verify-artifact-only"
    )


def test_installer_runtime_probe_failure_refuses(monkeypatch, tmp_path) -> None:
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    assert bash is not None
    installer_lib = (OPS / "fanout_outcome_acceptance_install_lib.sh").as_posix()
    fake_python = tmp_path / "python"
    fake_cron = tmp_path / "cron.py"
    fake_check = tmp_path / "check.py"
    fake_cron.write_text("# target\n", encoding="utf-8")
    fake_check.write_text("# checked artifact\n", encoding="utf-8")
    fake_python.write_text("#!/usr/bin/env bash\nexit 9\n", encoding="utf-8")
    fake_python.chmod(0o755)
    command = (
        f"source '{installer_lib}'; "
        f"verify_d6_runtime '{fake_python.as_posix()}' '{fake_cron.as_posix()}' "
        f"'{fake_check.as_posix()}' '{'a' * 64}' '{tmp_path.as_posix()}'"
    )

    result = subprocess.run([bash, "-lc", command], capture_output=True, text=True, check=False)

    assert result.returncode == 9


def test_installer_call_site_cannot_swallow_a_failed_runtime_probe(tmp_path) -> None:
    """Execute the installer's real call line; adding ``|| true`` must make this fail."""
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    assert bash is not None
    installer = (OPS / "install_fanout_outcome_acceptance.sh").read_text(encoding="utf-8")
    installer_lines = installer.splitlines()
    call_index = next(
        index
        for index, line in enumerate(installer_lines)
        if line.startswith("if ! verify_d6_runtime ")
    )
    call_site = "\n".join(installer_lines[call_index : call_index + 3])
    installer_lib = (OPS / "fanout_outcome_acceptance_install_lib.sh").as_posix()
    fake_python = tmp_path / "python"
    fake_cron = tmp_path / "cron.py"
    fake_check = tmp_path / "check.py"
    survived = tmp_path / "survived"
    fake_python.write_text("#!/usr/bin/env bash\nexit 9\n", encoding="utf-8")
    fake_python.chmod(0o755)
    fake_cron.write_text("# target\n", encoding="utf-8")
    fake_check.write_text("# check\n", encoding="utf-8")
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -euo pipefail\n"
        f"source '{installer_lib}'\n"
        f"python_bin='{fake_python.as_posix()}'\n"
        f"target_cron='{fake_cron.as_posix()}'\n"
        f"target_check='{fake_check.as_posix()}'\n"
        f"source_check_sha256='{'a' * 64}'\n"
        f"target_dir='{tmp_path.as_posix()}'\n"
        f"{call_site}\n"
        f"touch '{survived.as_posix()}'\n",
        encoding="utf-8",
    )

    result = subprocess.run([bash, str(harness)], capture_output=True, text=True, check=False)

    assert result.returncode != 0
    assert not survived.exists()


def test_artifact_only_probe_accepts_exact_bytes_and_refuses_mismatch(tmp_path) -> None:
    runner = OPS / "fanout_outcome_acceptance_cron.py"
    check = tmp_path / "check.py"
    check.write_text("# reviewed bytes\n", encoding="utf-8")
    digest = hashlib.sha256(check.read_bytes()).hexdigest()
    command = [
        sys.executable,
        str(runner),
        "--acceptance",
        str(check),
        "--acceptance-sha256",
        digest,
        "--out-dir",
        str(tmp_path / "runtime-output"),
        "--verify-artifact-only",
    ]

    good = subprocess.run(command, capture_output=True, text=True, check=False)
    check.write_text("# stale replacement\n", encoding="utf-8")
    stale = subprocess.run(command, capture_output=True, text=True, check=False)

    assert good.returncode == 0
    assert "[D6-INSTALL-ARTIFACT-VERIFIED]" in good.stdout
    assert (tmp_path / "runtime-output").is_dir()
    assert list((tmp_path / "runtime-output").iterdir()) == []
    assert stale.returncode != 0
    assert "artifact SHA-256 mismatch" in stale.stderr


def test_artifact_only_probe_refuses_when_output_directory_cannot_be_written(
    monkeypatch, tmp_path, capsys
) -> None:
    check = tmp_path / "check.py"
    check.write_text("# reviewed bytes\n", encoding="utf-8")
    digest = hashlib.sha256(check.read_bytes()).hexdigest()
    monkeypatch.setattr(
        cron,
        "_atomic_write",
        lambda _path, _contents: (_ for _ in ()).throw(PermissionError("not writable")),
    )

    with pytest.raises(PermissionError, match="not writable"):
        cron.main(
            [
                "--acceptance",
                str(check),
                "--acceptance-sha256",
                digest,
                "--out-dir",
                str(tmp_path / "unwritable"),
                "--verify-artifact-only",
            ]
        )

    assert "[D6-INSTALL-ARTIFACT-VERIFIED]" not in capsys.readouterr().out
