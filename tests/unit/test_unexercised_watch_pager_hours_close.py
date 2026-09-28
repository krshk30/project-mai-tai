"""PAGER1/INCCLOSE controls against the installed watch's real entry points."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get("WATCH_SOURCE_PATH", ROOT / "ops/health/unexercised_watch.py"))
SPEC = importlib.util.spec_from_file_location("unexercised_watch_pager1", SOURCE)
watch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watch
SPEC.loader.exec_module(watch)
ET = ZoneInfo("America/New_York")
BENF_ID = "c7dd4f76-6304-4197-be4d-ad48990e8346"
DAIC_ID = "c82c2dc0-f197-4351-8780-3d4f6719a644"


def et_at(day: str, hour: int, minute: int) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=ET)


@pytest.mark.parametrize("day", ["2026-09-28", "2026-11-02"])
@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(6, 59, False), (7, 0, True), (19, 59, True), (20, 0, True), (20, 1, False)],
)
def test_pager_window_is_et_in_edt_and_est(day, hour, minute, expected):
    assert watch._in_pager_hours(et_at(day, hour, minute)) is expected


def test_weekend_is_not_a_pager_day():
    assert not watch._in_pager_hours(et_at("2026-11-01", 12, 0))


@pytest.mark.parametrize("inc1", [False, True])
def test_cron_guard_refuses_outside_window_before_read_or_state_write(
    tmp_path, monkeypatch, inc1
):
    monkeypatch.setattr(watch, "_watch_now", lambda: et_at("2026-09-28", 20, 1))
    monkeypatch.setattr(
        watch, "_run_inc1_pager", lambda **_kw: pytest.fail("INC1 ran outside the window")
    )
    monkeypatch.setattr(watch, "CONDITIONS", {"X": lambda: pytest.fail("watch read outside")})
    state, status = tmp_path / "state.json", tmp_path / "STATUS.txt"
    args = ["--cron", "--state", str(state), "--status", str(status)]
    if inc1:
        args.append("--inc1")
    assert watch.main(args) == 0
    assert not state.exists()
    assert not status.exists()


def test_cron_guard_allows_the_2000_minute_for_both_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "_watch_now", lambda: et_at("2026-09-28", 20, 0))
    called = []
    monkeypatch.setattr(watch, "_run_inc1_pager", lambda **_kw: called.append("inc1") or 0)
    monkeypatch.setattr(watch, "CONDITIONS", {})
    assert watch.main(["--cron", "--inc1"]) == 0
    assert watch.main([
        "--cron", "--state", str(tmp_path / "s.json"),
        "--status", str(tmp_path / "STATUS.txt"), "--no-page",
    ]) == 0
    assert called == ["inc1"]
    assert (tmp_path / "STATUS.txt").exists()


def incident(source="oms_v2_webull_uncovered_share", incident_id=BENF_ID):
    return {
        "id": incident_id,
        "source": source,
        "account": "live:orb" if source != "schwab_opening_policy_reject" else "live:schwab_1m_v2",
        "symbol": "BENF" if source != "oms_webull_protect_handle_lost" else "DAIC",
        "managed_row_id": "4d92fb21-4367-45e0-9269-b21fdcdac159",
        "session_date": "2026-09-28",
        "title": "incident",
    }


def facts(now, symbol="BENF", quantity="0", row_status="closed", open_rows=0):
    return {
        "account": "live:orb", "symbol": symbol, "quantity": quantity,
        "source_updated_at": now.isoformat(), "updated_at": now.isoformat(),
        "managed_row_status": row_status, "managed_row_quantity": 0,
        "managed_row_account": "live:orb", "managed_row_symbol": symbol,
        "open_managed_rows": open_rows,
    }


def run_inc1(tmp_path, monkeypatch, row, observed, now):
    pages = []
    closes = []
    monkeypatch.setattr(watch, "_inc1_open_incidents", lambda: [row])
    monkeypatch.setattr(watch, "_inc1_resolution_facts", lambda _incident: observed)
    monkeypatch.setattr(
        watch, "_inc1_commit_close",
        lambda candidate, reason, _now: closes.append((candidate["id"], reason)) or True,
    )
    monkeypatch.setattr(watch, "page", lambda title, body: pages.append((title, body)) or True)
    state, status = tmp_path / "inc1.json", tmp_path / "INC1_STATUS.txt"
    rc = watch._run_inc1_pager_unlocked(
        state_path=state, status_path=status, no_page=False, now=now
    )
    return rc, state, status.read_text(), pages, closes


def test_benf_closed_exact_row_and_fresh_flat_broker_auto_closes(tmp_path, monkeypatch, capsys):
    now = et_at("2026-09-28", 19, 30)
    rc, state, status, pages, closes = run_inc1(
        tmp_path, monkeypatch, incident(), facts(now), now
    )
    assert rc == 0
    assert closes == [(BENF_ID, "exact_row_closed_and_broker_flat")]
    assert pages == []
    assert "verdict=NO_OPEN_INCIDENT open=0" in status
    assert "health=GREEN" in status
    assert f"[INC1-CLOSE] id={BENF_ID}" in capsys.readouterr().out
    assert json.loads(state.read_text())[BENF_ID]["closed"] is True

    monkeypatch.setattr(watch, "_inc1_open_incidents", lambda: [])
    assert watch._run_inc1_pager_unlocked(
        state_path=state, status_path=tmp_path / "INC1_STATUS.txt", no_page=False, now=now
    ) == 0
    assert pages == []
    assert json.loads(state.read_text())[BENF_ID]["closed"] is True


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"quantity": "1"}, "broker_position_still_held"),
        ({"managed_row_status": "open"}, "exact_managed_row_not_closed"),
        ({"source_updated_at": et_at("2026-09-28", 19, 30) - timedelta(minutes=3)},
         "broker_position_read_stale"),
    ],
)
def test_unresolved_or_stale_benf_stays_open(tmp_path, monkeypatch, changes, reason):
    now = et_at("2026-09-28", 19, 30)
    observed = facts(now)
    observed.update(changes)
    rc, _state, status, pages, closes = run_inc1(
        tmp_path, monkeypatch, incident(), observed, now
    )
    assert rc == 0
    assert closes == []
    assert len(pages) == 1
    assert "verdict=OPEN_UNCOVERED open=1" in status
    assert "health=OPEN" in status
    assert reason in status


def test_unreadable_resolution_db_stays_open_with_reason(tmp_path, monkeypatch):
    row = incident()
    now = et_at("2026-09-28", 19, 30)
    pages = []
    monkeypatch.setattr(watch, "_inc1_open_incidents", lambda: [row])
    monkeypatch.setattr(
        watch, "_inc1_resolution_facts", lambda _row: (_ for _ in ()).throw(RuntimeError("DB down"))
    )
    monkeypatch.setattr(watch, "_inc1_commit_close", lambda *_: pytest.fail("closed without read"))
    monkeypatch.setattr(watch, "page", lambda title, _body: pages.append(title) or True)
    status = tmp_path / "STATUS.txt"
    watch._run_inc1_pager_unlocked(
        state_path=tmp_path / "state.json", status_path=status, no_page=False, now=now
    )
    assert "verdict=OPEN_UNCOVERED" in status.read_text()
    assert "COULD_NOT_TELL:RuntimeError:DB down" in status.read_text()
    assert len(pages) == 1


def test_no_page_is_a_read_only_close_dry_run(tmp_path, monkeypatch):
    row = incident()
    now = et_at("2026-09-28", 19, 30)
    monkeypatch.setattr(watch, "_inc1_open_incidents", lambda: [row])
    monkeypatch.setattr(watch, "_inc1_resolution_facts", lambda _row: facts(now))
    monkeypatch.setattr(watch, "_inc1_commit_close", lambda *_: pytest.fail("dry run wrote DB"))
    monkeypatch.setattr(watch, "page", lambda *_: pytest.fail("dry run paged"))
    status = tmp_path / "STATUS.txt"
    assert watch._run_inc1_pager_unlocked(
        state_path=tmp_path / "state.json", status_path=status, no_page=True, now=now
    ) == 1
    assert "open=1" in status.read_text()
    assert "dry_run_no_close" in status.read_text()


def test_changed_evidence_at_commit_does_not_close_or_suppress_page(tmp_path, monkeypatch):
    row = incident()
    now = et_at("2026-09-28", 19, 30)
    pages = []
    monkeypatch.setattr(watch, "_inc1_open_incidents", lambda: [row])
    monkeypatch.setattr(watch, "_inc1_resolution_facts", lambda _row: facts(now))
    monkeypatch.setattr(watch, "_inc1_commit_close", lambda *_: False)
    monkeypatch.setattr(watch, "page", lambda title, _body: pages.append(title) or True)
    status = tmp_path / "STATUS.txt"
    assert watch._run_inc1_pager_unlocked(
        state_path=tmp_path / "state.json", status_path=status, no_page=False, now=now
    ) == 0
    assert "verdict=OPEN_UNCOVERED" in status.read_text()
    assert "evidence_changed_before_close" in status.read_text()
    assert len(pages) == 1


def test_policy_reject_waits_for_session_end_and_zero_webull_exposure():
    row = incident("schwab_opening_policy_reject")
    before = et_at("2026-09-28", 19, 59)
    after = et_at("2026-09-28", 20, 0)
    assert watch._inc1_resolution_decision(row, facts(before), before) == (
        None, "session_not_ended"
    )
    assert watch._inc1_resolution_decision(row, facts(after, open_rows=1), after) == (
        None, "webull_managed_position_still_open_or_unreadable"
    )
    assert watch._inc1_resolution_decision(row, facts(after), after)[0] == (
        "session_ended_no_webull_exposure"
    )


def test_wrong_incident_account_never_auto_closes():
    row = incident()
    row["account"] = "live:schwab_1m_v2"
    now = et_at("2026-09-28", 19, 30)
    assert watch._inc1_resolution_decision(row, facts(now), now) == (
        None, "incident_account_mismatched"
    )
    with pytest.raises(ValueError, match="account mismatched"):
        watch._inc1_commit_close(row, "exact_row_closed_and_broker_flat", now)


def test_hdl1_closes_only_when_broker_flat_and_no_open_row(tmp_path, monkeypatch):
    row = incident("oms_webull_protect_handle_lost", DAIC_ID)
    now = et_at("2026-09-28", 19, 30)
    rc, _state, status, pages, closes = run_inc1(
        tmp_path, monkeypatch, row, facts(now, symbol="DAIC"), now
    )
    assert rc == 0 and not pages
    assert closes == [(DAIC_ID, "hdl1_no_position_or_open_row")]
    assert "verdict=NO_OPEN_INCIDENT" in status


def test_unresolved_hdl1_is_visible_but_not_added_to_inc1_urgent_route(tmp_path, monkeypatch):
    row = incident("oms_webull_protect_handle_lost", DAIC_ID)
    now = et_at("2026-09-28", 19, 30)
    rc, _state, status, pages, closes = run_inc1(
        tmp_path, monkeypatch, row, facts(now, symbol="DAIC", quantity="1"), now
    )
    assert rc == 0 and not pages and not closes
    assert "verdict=OPEN_AUX_INCIDENT" in status
    assert "health=OPEN" in status
    assert "broker_position_still_held" in status


def test_closing_update_rechecks_all_resolution_facts(monkeypatch):
    sql = []
    monkeypatch.setattr(watch, "_psql", lambda statement: sql.append(statement) or [BENF_ID])
    assert watch._inc1_commit_close(
        incident(), "exact_row_closed_and_broker_flat", et_at("2026-09-28", 19, 30)
    )
    statement = sql[0]
    assert "p.quantity = 0" in statement
    assert "p.source_updated_at between" in statement
    assert "m.status = 'closed' and m.current_quantity = 0" in statement
    assert "i.payload->>'managed_row_id'" in statement
    assert "inc1_auto_close" in statement
    assert "exact_managed_row_closed_quantity_zero" in statement


def test_policy_closing_update_rechecks_session_end_and_no_open_row(monkeypatch):
    sql = []
    row = incident("schwab_opening_policy_reject")
    monkeypatch.setattr(watch, "_psql", lambda statement: sql.append(statement) or [BENF_ID])
    assert watch._inc1_commit_close(row, "session_ended_no_webull_exposure", et_at("2026-09-28", 20, 0))
    assert "m.status = 'open'" in sql[0]
    assert "timezone('America/New_York'" in sql[0]
    assert "time '20:00'" in sql[0]


def test_installer_pins_both_all_day_cron_lines_to_one_copy():
    script = ROOT / "ops/health/install_unexercised_watch.sh"
    result = subprocess.run(["bash", str(script), "--print-cron"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if "watch.py --cron" in line]
    assert len(lines) == 2
    assert lines[0].startswith("*/15 * * * * ")
    assert lines[1].startswith("* * * * * ")
    assert "--inc1" in lines[1]
    assert all("sha256sum /home/trader/unexercised_watch/watch.py" in line for line in lines)
    assert lines[0].split('" = "')[1].split('"')[0] == lines[1].split('" = "')[1].split('"')[0]


def _install_fixture(tmp_path):
    source = tmp_path / "repo/ops/health/unexercised_watch.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(SOURCE.read_bytes())
    target = tmp_path / "installed/watch.py"
    target.parent.mkdir()
    target.write_text("# old installed watch\n")
    old_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    cron_state = tmp_path / "root-crontab"
    cron_state.write_text(
        "# unrelated\n3 * * * * /usr/local/bin/keep-me\n"
        "# BEGIN project-mai-tai unexercised-condition watch "
        "(claude-1 2026-09-08, operator GO)\n"
        f"*/15 11-21 * * 1-5 [ \"$(sha256sum {target})\" = \"{old_sha}\" ] && watch\n"
        "# END project-mai-tai unexercised-condition watch\n"
        "# BEGIN project-mai-tai INC1 uncovered-position pager (#928)\n"
        f"* 11-21 * * 1-5 [ \"$(sha256sum {target})\" = \"{old_sha}\" ] && watch --inc1\n"
        "# END project-mai-tai INC1 uncovered-position pager\n"
    )
    fake_crontab = tmp_path / "fake-crontab"
    fake_crontab.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "if [[ $1 == -l ]]; then /bin/cat \"$PAGER_CRON_STATE\"; exit 0; fi\n"
        "if [[ ${PAGER_CRON_REJECT:-0} == 1 ]]; then exit 1; fi\n"
        "/bin/cp \"$1\" \"$PAGER_CRON_STATE\"\n"
    )
    fake_crontab.chmod(0o755)
    env = dict(
        os.environ,
        PAGER_INSTALL_TEST_MODE="1",
        PAGER_INSTALL_REPO_ROOT=str(tmp_path / "repo"),
        PAGER_INSTALL_TARGET=str(target),
        PAGER_INSTALL_CRONTAB_BIN=str(fake_crontab),
        PAGER_CRON_STATE=str(cron_state),
    )
    return source, target, cron_state, env


def test_installer_replaces_both_guards_together_and_preserves_unrelated_cron(tmp_path):
    _source, target, cron_state, env = _install_fixture(tmp_path)
    script = ROOT / "ops/health/install_unexercised_watch.sh"
    first = subprocess.run(["bash", str(script), "--install"], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    installed_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    cron = cron_state.read_text()
    assert "3 * * * * /usr/local/bin/keep-me" in cron
    assert cron.count(installed_sha) == 2
    assert len([line for line in cron.splitlines() if "watch.py --cron" in line]) == 2
    assert "*/15 * * * *" in cron and "* * * * *" in cron
    second = subprocess.run(["bash", str(script), "--install"], env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert cron_state.read_text() == cron


def test_installer_restores_old_copy_when_crontab_rejects_update(tmp_path):
    source, target, cron_state, env = _install_fixture(tmp_path)
    script = ROOT / "ops/health/install_unexercised_watch.sh"
    original_watch = target.read_bytes()
    original_cron = cron_state.read_bytes()
    source.write_text(source.read_text() + "\n# changed review source\n")
    env["PAGER_CRON_REJECT"] = "1"
    result = subprocess.run(["bash", str(script), "--install"], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert target.read_bytes() == original_watch
    assert cron_state.read_bytes() == original_cron


def test_installer_refuses_incomplete_cron_block_before_copying(tmp_path):
    _source, target, cron_state, env = _install_fixture(tmp_path)
    script = ROOT / "ops/health/install_unexercised_watch.sh"
    original_watch = target.read_bytes()
    cron_state.write_text(
        cron_state.read_text().replace(
            "# END project-mai-tai INC1 uncovered-position pager\n", ""
        )
    )
    original_cron = cron_state.read_bytes()
    result = subprocess.run(["bash", str(script), "--install"], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "missing or duplicate cron marker" in result.stderr
    assert target.read_bytes() == original_watch
    assert cron_state.read_bytes() == original_cron
