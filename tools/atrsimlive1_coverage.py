"""C1 14:11 supplied-envelope binding; never native market completeness.

The three continuity witnesses are externally reviewed retained operands. This
reader byte-binds them; it does not infer continuous capture from print density.
"""

from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
MAX_PROOF_BYTES = 2 * 1024 * 1024


def load(path, expected_sha):
    from atrsimlive1_runtime import decode
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_PROOF_BYTES + 1)
    if len(raw) > MAX_PROOF_BYTES or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("supplied_envelope_receipt_hash_or_cap")
    value = decode(raw)
    if (value.get("schema") != "ATRSIMLIVE1_REVIEWED_ENVELOPES_V1"
            or value.get("ruling") != "C1_20261028_1411_SUPPLIED_WATCHED_ENVELOPE"
            or not isinstance(value.get("windows"), list) or len(value["windows"]) > 4096):
        raise ValueError("reviewed_supplied_envelope_receipt_required")
    return value


def verify(window, *, symbol, day, trades, asof_ns, nanos, read_proof):
    from atrsimlive1_collect import digest
    if (window.get("symbol") != symbol or window.get("trading_day") != day
            or window.get("native_market_complete") is not False
            or window.get("review_status") != "ACCEPTED_SUPPLIED_ENVELOPE"):
        raise ValueError("exact_reviewed_window_required")
    start, end = nanos(window["start"]), nanos(window["end"])
    if not 0 < start < end <= asof_ns:
        raise ValueError("bounded_observed_envelope_required")
    if (not isinstance(read_proof, dict) or read_proof.get("provider") != "massive"
            or read_proof.get("table") != "market_capture_trades"
            or read_proof.get("symbol") != symbol or read_proof.get("full_read") is not True
            or read_proof.get("truncated") is not False
            or nanos(read_proof["start"]) > start or nanos(read_proof["end"]) < end
            or type(read_proof.get("row_count")) is not int or read_proof["row_count"] != len(trades)
            or read_proof.get("rows_sha256") != digest(trades)):
        raise ValueError("actual_full_read_certificate_required")
    witnesses = window.get("continuity_witnesses")
    if not isinstance(witnesses, dict) or set(witnesses) != {"watch", "subscription", "capture"}:
        raise ValueError("three_independent_continuity_operands_required")
    for kind, reference in witnesses.items():
        if not isinstance(reference, dict) or not reference.get("pointer"):
            raise ValueError("continuity_source_reference_required")
        path = Path(reference["artifact"])
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("retained_continuity_file_required")
        with path.open("rb") as stream:
            raw = stream.read(MAX_PROOF_BYTES + 1)
        if (len(raw) > MAX_PROOF_BYTES
                or hashlib.sha256(raw).hexdigest() != reference.get("sha256")):
            raise ValueError("continuity_witness_bytes_changed:" + kind)
    rows = [row for row in trades if start <= nanos(row["event_ts"]) < end]
    if not rows or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("nonempty_unique_supplied_scope_required")
    rows.sort(key=lambda row: (nanos(row["event_ts"]), row["id"]))
    first, last = nanos(rows[0]["event_ts"]), nanos(rows[-1]["event_ts"])
    gap = max((nanos(right["event_ts"]) - nanos(left["event_ts"])
               for left, right in zip(rows, rows[1:])), default=0)
    metrics = dict(first_print_id=rows[0]["id"], last_print_id=rows[-1]["id"],
                   first_print_ns=first, last_print_ns=last, max_interprint_gap_ns=gap,
                   row_count=len(rows), rows_sha256=digest(rows), full_read=True, truncated=False)
    # Main may additionally bind a previously captured exact census. A fresh
    # one-pass qualifier can instead measure it here after complete pagination.
    expected = window.get("expected_census")
    if expected is not None and json.dumps(expected, sort_keys=True) != json.dumps(metrics, sort_keys=True):
        raise ValueError("supplied_scope_actual_census_mismatch")
    # No arbitrary gap threshold. The reviewed capture/watch witnesses decide
    # the scope; the measured actual gap remains visible, even when large.
    left = datetime.fromisoformat(window["start"].replace("Z", "+00:00")).astimezone(ET)
    right = datetime.fromisoformat(window["end"].replace("Z", "+00:00")).astimezone(ET)
    note = (f"coverage: watched window {left:%H:%M}–{right:%H:%M} ET; "
            "outside-window and native/market completeness UNKNOWN")
    return start, end, rows, note, metrics
