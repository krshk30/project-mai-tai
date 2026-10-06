"""Recorded manager shape is not a fabricated signal/old-process receipt."""
import copy
from datetime import timedelta
import json
from pathlib import Path

import pytest
import release_policy as policy
import attended
from test_attended_release import NOW, row47_call, row47_proof


def test_recorded_manager_schema_parses_but_alone_cannot_clear_stop():
    fixture = json.loads(Path(__file__).with_name("row47_prior_schema.json").read_text())
    before, after, _ = row47_proof()
    before["InvocationID"] = fixture["manager_rows"][0]["INVOCATION_ID"]
    with pytest.raises(policy.Stop, match="CancelledError"):
        policy.row47(before, after, fixture["manager_rows"], policy.moment("2026-09-30T23:30:00Z"),
                     policy.moment("2026-09-30T23:32:00Z"), intended=True,
                     unit="project-mai-tai-orb-schwab.service")


def test_prior_manager_timestamp_never_current_attempt_clearance():
    fixture = json.loads(Path(__file__).with_name("row47_prior_schema.json").read_text())
    before, after, rows = row47_proof()
    rows[-1] = fixture["manager_rows"][0]
    with pytest.raises(policy.Stop, match="outside intended stop"):
        row47_call(before, after, rows)


@pytest.mark.parametrize("index,key,value", [(0, "_SYSTEMD_INVOCATION_ID", "f"*32),
    (3, "INVOCATION_ID", "f"*32), (3, "UNIT", "project-mai-tai-oms.service"),
    (4, "JOB_TYPE", "restart"), (4, "INVOCATION_ID", "f"*32),
    (3, "MESSAGE", "Main process exited, code=exited, status=1/FAILURE"),
    (0, "MESSAGE", "no SIGTERM observed"), (0, "MESSAGE", "SIGTERM received by another PID")])
def test_current_row47_invocation_job_and_exact_signal_required(index, key, value):
    before, after, rows = row47_proof()
    rows[index][key] = value
    with pytest.raises(policy.Stop):
        row47_call(before, after, rows)


def test_any_later_start_or_sigkill_or_out_of_window_stops():
    for message in ("[ORB-SCHWAB] mode=LIVE", "SIGKILL received", "Exception: controlled error"):
        before, after, rows = row47_proof()
        extra = copy.deepcopy(rows[0])
        extra["MESSAGE"] = message
        rows.append(extra)
        with pytest.raises(policy.Stop):
            row47_call(before, after, rows)
    before, after, rows = row47_proof()
    rows[0]["__REALTIME_TIMESTAMP"] = str(int((NOW-timedelta(seconds=1)).timestamp()*1e6))
    with pytest.raises(policy.Stop):
        row47_call(before, after, rows)


def test_changed_old_process_refused_before_systemctl_stop(monkeypatch, tmp_path):
    before, _, _ = row47_proof()
    fx = attended.Real(tmp_path, {}, tmp_path)
    fx.before = {"orb-schwab": before}
    changed = dict(before, MainPID=before["MainPID"] + 1)
    monkeypatch.setattr(fx, "fleet", lambda: {"orb-schwab": changed})
    calls = []
    monkeypatch.setattr(fx, "command", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(policy.Stop, match="changed before stop"):
        fx.action("stop", "orb-schwab")
    assert calls == []
