"""File-only consistency checks for Claude16:31:39's NO-KILL P4 disposition.

Never collects, connects, executes a service command or returns a native PASS.
Raw collection/provenance and final cadence acceptance belong to the reviewer.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re


MAX_RECEIPT_BYTES = 32 * 1024 * 1024
MAX_TRADES = 100_000
MAX_BATCHES = 1_000
WORKER_UNIT = "project-mai-tai-snapshot-worker.service"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, name, *, positive=False):
    require(type(value) is int and value >= (1 if positive else 0), f"invalid {name}")
    return value


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def load_receipt(path):
    with Path(path).open("rb") as source:
        raw = source.read(MAX_RECEIPT_BYTES + 1)
    require(len(raw) <= MAX_RECEIPT_BYTES, "receipt size bound exceeded")
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def validate_receipt(packet, *, expected_head, expected_unit_sha256, expected_source_files):
    """Validate supplied evidence against INDEPENDENT main/reviewer manifest pins."""
    require(isinstance(expected_head, str) and re.fullmatch(r"[0-9a-f]{40}", expected_head), "invalid expected head")
    require(sha(expected_unit_sha256), "invalid expected unit hash")
    require(bool(expected_source_files) and all(sha(value) for value in expected_source_files.values()),
            "independent source manifest required")
    require(packet["kind"] == "L13_P4_POSTINSTALL_READ_163139", "wrong P4 disposition")
    require(packet["collector"] == "claude-1" and bool(packet["collection_reference"]), "reviewer read required")
    require(packet["actions"] == "READ_ONLY_NO_KILL_NO_RESTART", "kill/restart packet is not this disposition")
    require(packet["source_head"] == expected_head, "installed head differs")
    require(packet["unit_sha256"] == expected_unit_sha256, "installed unit differs")
    require(packet["source_files_sha256"] == expected_source_files, "installed source manifest differs")
    require(bool(packet["host_boot_id"]), "host/boot identity absent")
    require(sha(packet["raw_systemd_sha256"]) and sha(packet["raw_stream_sha256"]), "raw evidence hashes absent")
    window = packet["observation"]
    start = integer(window["start_epoch_ns"], "window start", positive=True)
    end = integer(window["end_epoch_ns"], "window end", positive=True)
    require(end > start and window["complete"] is True, "incomplete observation window")
    units = packet["worker_before"], packet["worker_after"]
    for unit in units:
        require(unit["name"] == WORKER_UNIT and unit["load_state"] == "loaded"
                and unit["active_state"] == "active" and unit["sub_state"] == "running", "worker not active")
        integer(unit["main_pid"], "worker PID", positive=True)
        require(integer(unit["nrestarts"], "NRestarts") == 0, "worker restarted during accepted no-kill read")
        integer(unit["exec_start_epoch_ns"], "worker start", positive=True)
    require(units[0]["main_pid"] == units[1]["main_pid"]
            and units[0]["exec_start_epoch_ns"] == units[1]["exec_start_epoch_ns"], "worker instance changed")
    for gateway in (packet["gateway_before"], packet["gateway_after"]):
        integer(gateway["main_pid"], "gateway PID", positive=True)
        integer(gateway["exec_start_epoch_ns"], "gateway start", positive=True)
    require(packet["gateway_before"] == packet["gateway_after"], "gateway identity changed during read")
    require(units[0]["main_pid"] != packet["gateway_before"]["main_pid"], "worker shares gateway PID")
    batches = packet["snapshot_batches"]
    require(2 <= len(batches) <= MAX_BATCHES, "cadence needs actual bounded batch sequence")
    previous = None
    intervals = []
    for batch in batches:
        require(re.fullmatch(r"[0-9]+-[0-9]+", batch["stream_id"]) is not None, "bad Redis batch ID")
        stamp = integer(batch["produced_epoch_ns"], "batch time", positive=True)
        identity = tuple(int(part) for part in batch["stream_id"].split("-"))
        require(start <= stamp <= end, "batch outside read window")
        if previous is not None:
            require(identity > previous[0] and stamp > previous[1], "batch order/identity repeated")
            intervals.append(stamp - previous[1])
        previous = identity, stamp
    startup_ns = batches[0]["produced_epoch_ns"] - units[0]["exec_start_epoch_ns"]
    require(0 <= startup_ns <= 120_000_000_000, "first observed batch not within two minutes of start")
    require(bool(packet["baseline_cadence_reference"]) and sha(packet["baseline_cadence_sha256"]),
            "old cadence evidence absent; never infer unchanged from settings")
    trades = packet["trades"]
    require(0 < len(trades) <= MAX_TRADES, "bounded actual trade samples required")
    lags = []
    seen = set()
    for trade in trades:
        identity = trade["stream_id"]
        require(identity not in seen and re.fullmatch(r"[0-9]+-[0-9]+", identity) is not None,
                "duplicate/invalid trade sample")
        seen.add(identity)
        produced = integer(trade["produced_epoch_ns"], "trade produced", positive=True)
        sip = integer(trade["sip_epoch_ns"], "SIP time", positive=True)
        require(start <= produced <= end and sip <= produced, "trade outside window or negative lag")
        lags.append(produced - sip)
    p99 = sorted(lags)[math.ceil(len(lags) * .99) - 1]
    require(p99 < 300_000_000, "post-install trade p99 is not under300ms")
    return {"status": "INTERNALLY_CONSISTENT_REVIEW_REQUIRED_NOT_NATIVE_PASS",
            "trade_count": len(trades), "trade_lag_p99_ns": p99,
            "snapshot_count": len(batches), "snapshot_intervals_ns": intervals,
            "first_observed_batch_since_start_ns": startup_ns,
            "kill_to_batch": "NOT_PERFORMED: reviewer16:31:39 forbids production kill",
            "cadence": "RAW_INTERVALS_REPORTED_REVIEWER_MUST_COMPARE_WITH_OLD",
            "source_head": expected_head, "unit_sha256": expected_unit_sha256}
