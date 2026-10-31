"""Synthetic routing controls, not an actual24h replay or phone send receipt."""

from datetime import datetime
import importlib.util
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from project_mai_tai.phone_alert_policy import (
    locked_window_notice_resolved, manual_only, phone_decision, send_stop_once,
)


NOOP = "client_abort live:orb cancel n=239 (3d-STREAK) - cancel_target_not_found\n\nFull report: /tmp/STATUS.txt"
SEED = "SEED-EXPOSURE   VERDICT   : 1 EXPOSED of 4 swept (0 boundary, 1 internal)"


@pytest.mark.parametrize("title,body", [
    ("Intent refusal class - our defect", NOOP),
    ("AMBER mai-tai readiness warnings", SEED),
    ("scanner capture CHECK UNKNOWN", "SCANNER_CAPTURE COULD_NOT_TELL database_read=FAILED"),
    ("scanner capture CHECK ERROR", "SCANNER_CAPTURE COULD_NOT_TELL checker_exit=9"),
    ("V2 poll-reenrol FIRED (good)", "VERDICT reenroll=PROVEN\nFirst path executed"),
    ("V2 first live cross 2026-10-21", "[OK] ABC first=1\nVERDICT composition=OK entered=1"),
])
def test_only_exact_approved_phone_classes_are_nonpaging(title, body):
    assert phone_decision(title, body)[0] is False


@pytest.mark.parametrize("title,body", [
    ("Intent refusal class - our defect", NOOP.replace("cancel_target_not_found", "cancel_failed_timeout")),
    ("Intent refusal class - our defect", NOOP.replace(" cancel ", " open ")),
    ("Intent refusal class - our defect", NOOP.replace("cancel_target_not_found", "cancel_target_not_found_extra")),
    ("Intent refusal class - our defect", NOOP + "\nOTHER UNKNOWN REFUSAL"),
    ("RED LIVE_MONEY_RED", "oms-order-lifecycle accepted with no order"),
    ("RED reconcile drift: ABC", "native quantity differs"),
    ("v2 BAR HOLE", "feed missing minute"),
    ("RED mai-tai service runtime: oms", "inactive"),
    ("RED mai-tai NOT READY (RED)", "group4 UNKNOWN"),
    ("RED mai-tai NOT READY (RED)", SEED),
    ("scanner capture LOW VOLUME", "SCANNER_CAPTURE LOW_VOLUME rows=0 feed_age_minutes=0"),
    ("snapshot UNKNOWN", "COULD_NOT_TELL"),
    ("scanner capture CHECK UNKNOWN", "LIVE_MONEY_RED SCANNER_CAPTURE COULD_NOT_TELL"),
    ("V2 poll-reenrol REPEATING", "VERDICT reenroll=REPEATED"),
    ("V2 first live cross 2026-10-21", "[BREACH] ABC\nVERDICT composition=BREACH"),
    ("V2 first live cross 2026-10-21", "[COULD_NOT_TELL] ABC"),
    ("RED live config drift", "LIVE_LOCKED unrelated real drift"),
    ("Option A paper STOP UNKNOWN", "failed stop"),
    ("Option A owner release UNKNOWN", "unknown owner"),
])
def test_each_real_risk_and_near_miss_still_pages(title, body):
    assert phone_decision(title, body)[0] is True


@pytest.mark.parametrize("fault", ["none", "bot_row", "partial", "failed", "missing_id", "wrong_account", "mixed"])
def test_manual_only_requires_complete_exact_account_native_lookup(fault):
    args = dict(account="live:schwab_1m_v2", native_ids=["NATIVE1"],
                requested_ids=["NATIVE1"], bot_ids=[], complete=True)
    if fault == "bot_row":
        args["bot_ids"] = ["NATIVE1"]
    elif fault == "partial":
        args["requested_ids"] = []
    elif fault == "failed":
        args["complete"] = False
    elif fault == "missing_id":
        args["native_ids"] = [""]
    elif fault == "wrong_account":
        args["account"] = ""
    elif fault == "mixed":
        args.update(native_ids=["NATIVE1", "BOT2"], requested_ids=["NATIVE1", "BOT2"], bot_ids=["BOT2"])
    assert manual_only(**args) is (fault == "none")


def latch(at="2026-10-21T16:54:01+00:00"):
    return {"at_utc": at, "session_date": "2026-10-21", "reason": "SYNTHETIC safety trigger",
            "clear_policy": "explicit human/claude-1 action only"}


def stop_body(at="2026-10-21T16:54:10+00:00", reason="SYNTHETIC trigger"):
    return json.dumps({"action": "stop_paper", "at_utc": at, "reason": reason, "systemctl_rc": 0})


def test_stop_dedupes_same_event_not_timestamp_or_reason_and_new_stop_pages(tmp_path):
    marker, journal = tmp_path / "latch.json", tmp_path / "pages.jsonl"
    marker.write_text(json.dumps(latch()))
    send = Mock(return_value=True)
    assert send_stop_once("Option A paper STOP", stop_body(), send, latch_path=marker, journal=journal)
    assert send_stop_once("Option A paper STOP", stop_body(reason="different watchdog wrapper"), send,
                          latch_path=marker, journal=journal)
    assert send.call_count == 1
    # New explicit clear/new latch identity, even inside the same25 seconds.
    marker.write_text(json.dumps(latch("2026-10-21T16:54:15+00:00")))
    assert send_stop_once("Option A paper STOP", stop_body("2026-10-21T16:54:20+00:00"), send,
                          latch_path=marker, journal=journal)
    assert send.call_count == 2
    assert len(journal.read_text().splitlines()) == 3


@pytest.mark.parametrize("fault", ["absent", "malformed", "future", "writable", "unknown_title", "bad_journal"])
def test_unknown_stop_identity_or_storage_never_suppresses(tmp_path, fault):
    marker, journal = tmp_path / "latch.json", tmp_path / "pages.jsonl"
    if fault != "absent":
        marker.write_text("bad" if fault == "malformed" else json.dumps(latch(
            "2026-10-21T16:55:00+00:00" if fault == "future" else "2026-10-21T16:54:01+00:00")))
    if fault == "writable":
        marker.chmod(0o666)
    if fault == "bad_journal":
        journal.write_text("malformed\n")
    send = Mock(return_value=True)
    title = "Option A paper STOP UNKNOWN" if fault == "unknown_title" else "Option A paper STOP"
    for _ in range(2):
        assert send_stop_once(title, stop_body(), send, latch_path=marker, journal=journal)
    assert send.call_count == 2


def test_failed_delivery_does_not_consume_stop_event(tmp_path):
    marker, journal = tmp_path / "latch.json", tmp_path / "pages.jsonl"
    marker.write_text(json.dumps(latch()))
    send = Mock(side_effect=[False, True])
    assert not send_stop_once("Option A paper STOP", stop_body(), send, latch_path=marker, journal=journal)
    assert send_stop_once("Option A paper STOP", stop_body(), send, latch_path=marker, journal=journal)
    assert send.call_count == 2


def test_filtered_low_alert_keeps_full_body_and_same_digest_count(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[2] / "ops/health/low_priority_alerts.py"
    spec = importlib.util.spec_from_file_location("alerts_b_low", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("MAI_TAI_LOW_ALERT_SPOOL", str(tmp_path))
    send = Mock(return_value=True)
    monkeypatch.setattr(module, "_send", send)
    assert module.send_low("reject-watch", "Intent refusal class - our defect", NOOP)
    send.assert_not_called()
    record = json.loads(next(tmp_path.glob("*.jsonl")).read_text())
    assert record["body"] == NOOP and record["phone"] is False
    assert "reject-watch: count=1" in module.digest_body(datetime.now(module.ET).date().isoformat(), tmp_path)


def test_approved_mirror_1545_not_a_generic_drift_suppression():
    body = ("strategy_schwab_1m_v2_entry_window_end_hour_et\n LIVE_LOCKED=16 live env='15'\n"
            "strategy_schwab_1m_v2_entry_window_end_minute_et\n LIVE_LOCKED=0 live env='45'\n"
            "2 env-set drift(s), 0 unset-and-divergent, 0 unknown key(s).")
    assert locked_window_notice_resolved("RED live config drift", body, hour=15, minute=45)
    assert not locked_window_notice_resolved("RED live config drift", body, hour=16, minute=0)
    assert not locked_window_notice_resolved("RED live config drift", body.replace("0 unknown", "1 unknown"), hour=15, minute=45)
    assert phone_decision("RED live config drift", body)[0] is True


def test_maintenance_and_original_risk_emitters_remain_unmodified_in_scope():
    root = Path(__file__).resolve().parents[2]
    # The B filter does not alter marker TTL or the original true#3/#4 senders.
    hashes = {
        "planned_maintenance.py": "a4c7b1e8718cdbb696e5ad162361367cf97c4dc7c9cbe3220efad01cce6c907b",
        "fleet_health_cron.sh": "511c9d560eee1b1a5422e65d4e85c6f078f73802dae3095151462ba42ea927e4",
        "preopen_readiness_cron.sh": "850a423ea4dcccd1bfd6362ea11eec5bfba361a5bfba95358e770a984f347b60",
        "reconcile_alert_cron.sh": "42e1d3290c8c676006582faf0b8f9b104532cf9f4b33a8bb6ec3bd81b5bac6c5",
        "bar_gap_watch_cron.sh": "d2ed789874ba7c2876fc7ce12dd1e6e87a48ea516a9c5fa08e482fbae2938521",
    }
    for path, expected in hashes.items():
        assert hashlib.sha256((root / "ops/health" / path).read_bytes()).hexdigest() == expected


def test_concurrent_same_stop_pages_once_with_delivery_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    marker, journal = tmp_path / "latch.json", tmp_path / "pages.jsonl"
    marker.write_text(json.dumps(latch()))
    delivered = []

    def send():
        delivered.append(True)
        return True

    def invoke(_):
        return send_stop_once("Option A paper STOP", stop_body(), send, latch_path=marker, journal=journal)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(invoke, range(2)))
    assert delivered == [True]
