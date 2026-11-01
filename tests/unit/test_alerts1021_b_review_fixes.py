"""Author-only controls; original failed safety test stays byte-for-byte intact."""

import fcntl
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from project_mai_tai import phone_alert_policy as policy
from tests.unit.test_alerts1021_b_phone_policy import latch, stop_body
from tests.unit.test_alerts1021_b_replay_contract import driver, packet, page


@pytest.mark.parametrize("delivered", [False, True])
def test_lock_timeout_pages_without_consuming_event_and_later_attempt_still_sends(
    tmp_path, monkeypatch, delivered,
):
    marker, journal = tmp_path / "latch.json", tmp_path / "pages.jsonl"
    marker.write_text(json.dumps(latch()))
    journal.write_text("")
    clock, sleeps, flags = [0.0], [], []

    def sleep(seconds):
        assert 0 < seconds <= 0.01
        sleeps.append(seconds)
        clock[0] += seconds

    def busy(stream, mode):
        flags.append(mode)
        assert mode == fcntl.LOCK_EX | fcntl.LOCK_NB
        raise BlockingIOError("SYNTHETIC busy lock")

    original_flock = policy.fcntl.flock
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(policy.fcntl, "flock", busy)
    send = Mock(return_value=delivered)
    assert policy.send_stop_once("Option A paper STOP", stop_body(), send,
                                 latch_path=marker, journal=journal) is delivered
    assert flags and sleeps and clock[0] == pytest.approx(policy.STOP_LOCK_WAIT_SECONDS)
    assert policy.STOP_LOCK_WAIT_SECONDS == 0.25 < 15
    send.assert_called_once_with()
    assert journal.read_text() == ""  # even successful fallback did not consume.
    monkeypatch.setattr(policy.fcntl, "flock", original_flock)
    send.return_value = True
    assert policy.send_stop_once("Option A paper STOP", stop_body(), send,
                                 latch_path=marker, journal=journal)
    assert send.call_count == 2
    assert policy.send_stop_once("Option A paper STOP", stop_body(), send,
                                 latch_path=marker, journal=journal)
    assert send.call_count == 2


def test_lock_can_be_acquired_after_short_contention_without_waiting_full_budget(monkeypatch):
    clock, attempts = [0.0], []

    def available_after_two_tries(stream, mode):
        assert mode == fcntl.LOCK_EX | fcntl.LOCK_NB
        attempts.append(mode)
        if len(attempts) < 3:
            raise BlockingIOError("SYNTHETIC brief contention")

    monkeypatch.setattr(policy, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    monkeypatch.setattr(policy.fcntl, "flock", available_after_two_tries)
    policy._lock_stop_journal(object())
    assert len(attempts) == 3 and clock[0] == pytest.approx(0.02)


@pytest.mark.timing
def test_actual_held_local_lock_fails_open_before_watchdog_without_journal_write(tmp_path):
    import time

    marker, journal = tmp_path / "latch.json", tmp_path / "pages.jsonl"
    marker.write_text(json.dumps(latch()))
    send = Mock(return_value=True)
    with journal.open("w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        assert policy.send_stop_once("Option A paper STOP", stop_body(), send,
                                     latch_path=marker, journal=journal)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0  # actual local lock contention, not a send/I/O deadline claim.
        assert journal.read_text() == ""
    send.assert_called_once_with()


def test_g1_source_and_original_failed_safety_module_are_preserved():
    root = Path(__file__).resolve().parents[2]
    expected = {
        "ops/health/momwarn1_repair/optiona/G1/guard_rules.py":
            "00e30b549b21e5ebea0bf0e74c1fee5c99f80153d47491bdf34e795f5f3792c6",
        "tests/unit/test_alerts1021_b_emitters.py":
            "4872d25e547be6373a50e749501ea03e11633967f0e290ae6811a01a9149f623",
    }
    for path, digest in expected.items():
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == digest
    assert policy.STOP_PAGES.parent != policy.SAFETY_STOP.parent


@pytest.mark.parametrize("sender", ["repo", "deployed_g1", "unknown"])
def test_replay_only_dedupes_exact_repo_sender_not_untouched_deployed_guard(tmp_path, sender):
    rows = [page("1", "Option A paper STOP", stop_body()),
            page("2", "Option A paper STOP", stop_body())]
    path = packet(tmp_path, rows)
    evidence = []
    for row in rows:
        context = {"topic": row["topic"], "message_id": row["id"], "binding": "EXACT_RETAINED",
                   "latch_present_at_notification": True, "safety_stop_latch": latch()}
        if sender != "unknown":
            context.update(sender_binding="EXACT_RETAINED", sender_path=(
                "/home/trader/project-mai-tai/ops/health/option_a_treatment_guard.py" if sender == "repo"
                else "/home/trader/option-a-daily-guard/guard_rules.py"))
        evidence.append(context)
    raw = "".join(json.dumps(row) + "\n" for row in evidence).encode()
    (tmp_path / "evidence.jsonl").write_bytes(raw)
    manifest = json.loads(path.read_text())
    manifest["evidence"] = {"path": "evidence.jsonl", "sha256": hashlib.sha256(raw).hexdigest()}
    path.write_text(json.dumps(manifest))
    result = driver().replay(path)
    assert result["actual_messages"] == 2
    assert result["after_phone"] == (1 if sender == "repo" else 2)
    if sender != "repo":
        assert result["unknown_lookup_or_event"] == 2
        assert all(row["rule"] != "same_stop_event_already_delivered" for row in result["rows"])
