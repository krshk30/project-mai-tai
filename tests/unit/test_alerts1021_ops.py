"""Ops-only controls. Authored for main's serial executor; no provider calls."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


maintenance = load("alerts1021_maintenance", "ops/health/planned_maintenance.py")
d6 = load("alerts1021_d6", "ops/health/fanout_outcome_acceptance_cron.py")
scanner = load("alerts1021_scanner", "ops/health/scanner_capture_check.py")
seed = load("alerts1021_seed", "scripts/seed_exposure_detector.py")
replay = load("alerts1021_replay", "ops/health/replay_alerts1021.py")


def marker(tmp_path, **changes):
    data = {"started_at": 1000, "expires_at": 1600,
            "services": ["oms"], "reason": "approved install"}
    data.update(changes)
    path = tmp_path / "marker.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("now", [999, 1600, 2000])
def test_maintenance_not_active_outside_exact_ttl(tmp_path, now):
    assert not maintenance.active_services(marker(tmp_path), now)


@pytest.mark.parametrize("changes", [
    {"expires_at": 99999}, {"services": ["unknown"]}, {"services": []}, {"reason": ""},
])
def test_bad_maintenance_marker_never_suppresses(tmp_path, changes):
    assert not maintenance.active_services(marker(tmp_path, **changes), 1100)


def test_malformed_or_writable_marker_never_suppresses(tmp_path):
    path = marker(tmp_path)
    path.chmod(0o666)
    assert not maintenance.active_services(path, 1100)
    path.chmod(0o600)
    path.write_text("not json")
    assert not maintenance.active_services(path, 1100)


def test_only_scoped_stop_and_heartbeat_suppressed(tmp_path):
    services = maintenance.active_services(marker(tmp_path), 1100)
    assert maintenance.planned_finding("fleet-runtime:service-runtime:oms:inactive", services)
    assert maintenance.planned_finding("fleet-runtime:oms-heartbeat", services)
    for fingerprint in ["live-money:stops-armed", "monitor-error",
                        "fleet-runtime:service-runtime:strategy:inactive",
                        "fleet-runtime:service-runtime:oms:restart-storm",
                        "fleet-runtime:service-runtime:oms:massive-1008"]:
        assert not maintenance.planned_finding(fingerprint, services)


def test_maintenance_filter_one_start_end_and_expiry_restores_original(tmp_path):
    path, state = marker(tmp_path), tmp_path / "maintenance.state"
    findings = ["fleet-runtime:service-runtime:oms:inactive|stopped", "monitor-error|unknown"]
    assert maintenance.filter_findings(path, state, 1100, findings) == findings[1:]
    assert maintenance.filter_findings(path, state, 1200, findings) == findings[1:]
    assert maintenance.filter_findings(path, state, 1600, findings) == findings
    assert maintenance.filter_findings(path, state, 1700, findings) == findings
    log = state.with_suffix(".log").read_text()
    assert log.count("maintenance started") == log.count("maintenance ended") == 1


def metric_lines(pair="FAIL", fill="UNEXERCISED", duplicate="PASS", refused="PASS"):
    return [f"metric=paired_legs verdict={pair} paired_legs=0 usable=4 of 4",
            f"metric=fill_rate verdict={fill} mirror=3/15 schwab=0/0",
            f"metric=duplicate_legs verdict={duplicate} duplicate_legs=0 of 4",
            f"metric=refused_exits verdict={refused} post_exit_episodes=7"]


def test_d6_pair_failure_is_actionable_even_with_unexercised_fill():
    lines = metric_lines()
    valid, _ = d6._denominator_contract(lines)
    assert valid
    assert d6.actionable_nonpass("UNEXERCISED", lines, valid)
    assert d6.actionable_nonpass("FAIL", lines, valid)


@pytest.mark.parametrize("metric", ["fill", "duplicate", "refused"])
@pytest.mark.parametrize("verdict", ["FAIL", "COULD_NOT_TELL"])
def test_unexercised_cannot_hide_actual_failed_or_unknown_metric(metric, verdict):
    assert d6.actionable_nonpass("UNEXERCISED", metric_lines(**{metric: verdict}), True)


def test_unexercised_bad_denominator_remains_actionable():
    assert d6.actionable_nonpass("UNEXERCISED", metric_lines(), False)


def test_empty_capture_still_pages_without_baseline():
    from datetime import datetime

    observation = scanner.DatabaseObservation(0, 0, "-", -1, 0)
    verdict = scanner.classify(observation, now_et=datetime.fromisoformat("2026-10-21T08:00:00-04:00"),
                               feed_age_minutes=0)
    assert verdict.exit_code == 1
    assert "reason=empty_capture" in verdict.line


def test_positive_capture_without_baseline_remains_unknown_in_status():
    from datetime import datetime

    observation = scanner.DatabaseObservation(8470, 7, "2026-10-21 07:47:07", 16393, 1)
    verdict = scanner.classify(observation, now_et=datetime.fromisoformat("2026-10-21T08:00:03-04:00"),
                               feed_age_minutes=0)
    assert verdict.status == "COULD_NOT_TELL" and verdict.exit_code == 2
    assert "reason=baseline_population_insufficient" in verdict.line


def test_seed_absence_never_proves_untruncated():
    assert seed.seed_was_untruncated("OLOX", 250, []) is None
    assert seed.seed_was_untruncated("OLOX", 250, ["db-seed: OTHER hydrated 250 bars"]) is None


def test_seed_positive_truncation_is_status_only():
    lines = ["[V2-DB-SEED-GAP] OLOX dropped 249 of 250 seed bars",
             "schwab_1m_v2 db-seed: OLOX hydrated 1 bars"]
    assert seed.seed_was_untruncated("OLOX", 250, lines) is False


def test_seed_positive_full_hydration_is_required_for_untruncated():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    at = int(now.timestamp() * 1_000_000)
    line = f"receipt_ts={at} db-seed: OLOX hydrated 250 bars"
    assert seed.seed_was_untruncated("OLOX", 250, [line], newest_bar=now, now=now) is True
    assert seed.seed_was_untruncated("OLOX", 250, [line.replace("250", "200")], newest_bar=now, now=now) is None
    assert seed.seed_was_untruncated("OLOX", 250, [line]) is None


def test_seed_stale_receipt_or_newer_db_window_cannot_page():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    old = now - timedelta(minutes=5)
    line = f"receipt_ts={int(old.timestamp() * 1_000_000)} db-seed: OLOX hydrated 250 bars"
    assert seed.seed_was_untruncated("OLOX", 250, [line], newest_bar=old, now=now) is None
    assert seed.seed_was_untruncated("OLOX", 250, [line], newest_bar=now, now=old) is None


def test_new_full_seed_attempt_is_not_hidden_by_old_successful_truncation():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    lines = ["[V2-DB-SEED-GAP] OLOX dropped 249 of 250 seed bars",
             "db-seed: OLOX hydrated 1 bars",
             f"receipt_ts={int(now.timestamp() * 1_000_000)} db-seed: OLOX hydrated 250 bars"]
    assert seed.seed_was_untruncated("OLOX", 250, lines, newest_bar=now, now=now) is True


def test_d6_unexercised_is_reevaluated_without_notification_or_repeated_summary(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    window = SimpleNamespace(session_date="2026-10-20", since=now - timedelta(days=1), until=now)
    monkeypatch.setattr(d6, "completed_session_window", lambda _now: window)
    calls, notices = [], []

    def run_report(**_kwargs):
        calls.append(1)
        lines = metric_lines(pair="UNEXERCISED")
        lines[0] = "metric=paired_legs verdict=UNEXERCISED paired_legs=0 usable=0 of 0"
        return SimpleNamespace(verdict="UNEXERCISED", exit_code=3, lines=lines)

    for _ in range(2):
        result = d6.run_once(now=now, acceptance=SimpleNamespace(run_report=run_report),
                             out_dir=tmp_path, notify=lambda *args: notices.append(args) or True)
        assert result.exit_code == 3
    assert len(calls) == 2 and not notices
    assert (tmp_path / "history.log").read_text().count("[D6-OUTCOME-ACCEPTANCE-NONPASS]") == 1
    status = (tmp_path / "STATUS.txt").read_text()
    assert "verdict=UNEXERCISED" in status and "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" not in status

    failed = SimpleNamespace(run_report=lambda **_kwargs: SimpleNamespace(
        verdict="UNEXERCISED", exit_code=3,
        lines=metric_lines(pair="UNEXERCISED", duplicate="FAIL")))
    d6.run_once(now=now, acceptance=failed, out_dir=tmp_path,
                notify=lambda *args: notices.append(args) or True)
    assert len(notices) == 1


def test_packet_replay_sha_refuses_modified_input_before_classification(tmp_path):
    first = next(iter(replay.PACKETS))
    (tmp_path / first).write_text("{}\n")
    with pytest.raises(ValueError, match="packet SHA mismatch"):
        replay.replay(tmp_path)


@pytest.mark.parametrize(("code", "line", "should_notify"), [
    (2, "SCANNER_CAPTURE COULD_NOT_TELL rows=8470 reason=baseline_population_insufficient", False),
    (1, "SCANNER_CAPTURE LOW_VOLUME rows=0 reason=empty_capture", True),
    (2, "SCANNER_CAPTURE COULD_NOT_TELL database_read=FAILED", True),
])
def test_scanner_wrapper_routes_actual_status_without_network(tmp_path, code, line, should_notify):
    import os
    import shlex
    import subprocess

    check = tmp_path / "check.sh"
    check.write_text(f"printf '%s\\n' {shlex.quote(line)}\nexit {code}\n")
    config = tmp_path / "env"
    config.write_text("# controlled empty env\n")
    curl = tmp_path / "curl.sh"
    curl.write_text(f"#!/bin/bash\nprintf 'NOTIFIED\\n' >> {shlex.quote(str(tmp_path / 'notices'))}\n")
    curl.chmod(0o755)
    result = subprocess.run(["bash", str(ROOT / "ops/health/scanner_capture_verify_cron.sh")],
        capture_output=True, text=True, check=False, env={
            **os.environ, "SCANNER_CAPTURE_CHECK": str(check), "SCANNER_CAPTURE_PYTHON": "/bin/bash",
            "SCANNER_CAPTURE_ENV_FILE": str(config), "SCANNER_CAPTURE_LOG": str(tmp_path / "cron.log"),
            "SCANNER_CAPTURE_CURL": str(curl),
        })
    assert result.returncode == code
    assert (tmp_path / "notices").exists() is should_notify
    assert line in (tmp_path / "STATUS.txt").read_text()
