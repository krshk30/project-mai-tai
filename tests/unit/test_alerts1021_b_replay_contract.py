"""Synthetic packet contracts ONLY, not fabricated actual pages or reduction."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PREOPEN = "mai-tai-preopen-28806a5a97b7"
ROUTINE = "mai-tai-routine-112964cc8f26787132a29538"


def driver():
    path = ROOT / "ops/health/replay_alerts1021_b.py"
    spec = importlib.util.spec_from_file_location("alerts_b_test_replay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def packet(tmp_path, records):
    raw = "".join(json.dumps(row) + "\n" for row in records).encode()
    (tmp_path / "messages.jsonl").write_bytes(raw)
    (tmp_path / "empty.jsonl").write_bytes(b"")
    manifest = {"schema": "ALERTS1021-B-PAGES-1", "complete": True,
        "coverage_provenance": "SYNTHETIC contract fixture, not historical evidence",
        "since": "2026-10-20T18:00:00Z", "until": "2026-10-21T18:00:00Z",
        "topics": [PREOPEN, ROUTINE], "message_denominator": sum(r.get("event") == "message" for r in records),
        "messages": [{"topic": PREOPEN, "path": "messages.jsonl", "sha256": hashlib.sha256(raw).hexdigest()},
                     {"topic": ROUTINE, "path": "empty.jsonl", "sha256": hashlib.sha256(b"").hexdigest()}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def page(identity, title, body):
    return {"event": "message", "id": identity, "topic": PREOPEN, "time": 1792602000,
            "title": title, "message": body}


def test_every_message_kept_in_denominator_unknown_orphan_stop_keep_phone(tmp_path):
    rows = [{"event": "open"}, page("1", "ORPHAN ORDER RED", "SYNTHETIC unknown original native ID"),
            page("2", "Option A paper STOP", "SYNTHETIC unknown original latch"),
            page("3", "RED LIVE_MONEY_RED", "real risk fixture")]
    result = driver().replay(packet(tmp_path, rows))
    assert result["actual_messages"] == 3 and result["after_phone"] == 3
    assert result["raw_event_counts"] == {"open": 1, "message": 3}
    assert result["unknown_lookup_or_event"] == 2
    assert [row["id"] for row in result["rows"]] == ["1", "2", "3"]


def test_changed_hash_fails_instead_of_silent_partial_replay(tmp_path):
    path = packet(tmp_path, [page("1", "UNKNOWN", "SYNTHETIC")])
    (tmp_path / "messages.jsonl").write_text("\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        driver().replay(path)


def test_duplicate_message_identity_is_not_dropped_to_fit_denominator(tmp_path):
    row = page("1", "UNKNOWN", "SYNTHETIC")
    with pytest.raises(ValueError, match="denominator/unique"):
        driver().replay(packet(tmp_path, [row, row]))


def test_missing_phone_topic_refuses_complete_replay(tmp_path):
    path = packet(tmp_path, [page("1", "UNKNOWN", "SYNTHETIC")])
    manifest = json.loads(path.read_text())
    manifest["messages"].pop()
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="topic export is missing"):
        driver().replay(path)
