"""Six C12124 cases from the SAME retained CSV; Main-only execution."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
RAW = Path("/Users/velkris/.codex/task-queues/analysis/OCT27-C12045-D2-SEVEN-SESSIONS-RO-FIRST/stdout.json")
RAW_SHA = "9820991545c7ac17918100bc61ea7055f0ba73ee79491dd5b71140fea60e737b"
SPEC = importlib.util.spec_from_file_location(
    "d2_c12124_report", ROOT / "ops/health/fanout_outcome_acceptance.py")
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)
WORX = "c5ef2efe-7802-4c63-862d-3d98e2ba4548"
AHT = "89adfa05-db51-4fe8-bb1c-fbce9d04cc1c"
STAK = "55b43f16-66a6-4ed3-bb79-31b9ee018534"


@pytest.fixture(scope="module")
def packet():
    try:
        RAW.lstat()
    except FileNotFoundError:
        pytest.skip(f"D2 requires local rig artifact {RAW}")
    raw = RAW.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == RAW_SHA
    data = json.loads(raw)
    assert data["complete"] is True and data["rollback"] is True
    assert data["connection_closed"] is True
    return {case["day"]: case for case in data["cases"]}


def _rows(case):
    assert hashlib.sha256(case["csv"].encode()).hexdigest() == case["csv_sha256"]
    return list(tool.parse_rows(case["csv"]))


def _evaluate(case, rows):
    return tool.evaluate(rows, since=tool.parse_instant(case["since"], "since"),
                         until=tool.parse_instant(case["until"], "until"))


def _route(rows, order):
    found = [row for row in rows if row.kind == "ROUTE_ORDER" and row.values[1] == order]
    assert len(found) == 1
    return found[0]


def _replace(rows, row, values):
    rows[rows.index(row)] = tool.RawRow(row.kind, tuple(values))


@pytest.mark.parametrize(("day", "order", "submit", "filled", "seal"), [
    ("2026-10-20", WORX, "2026-10-20T11:02:44.389660+00:00",
     "2026-10-20T11:02:44+00:00", "2026-10-20T11:02:02.719000+00:00"),
    ("2026-10-21", AHT, "2026-10-21T12:46:14.257122+00:00",
     "2026-10-21T12:46:14+00:00", "2026-10-21T11:21:02.945000+00:00"),
])
def test_named_native_second_resolution_rows(packet, day, order, submit, filled, seal):
    case = packet[day]
    rows = _rows(case)
    row = _route(rows, order)
    assert tool.parse_instant(row.values[5], "submit") == datetime.fromisoformat(submit)
    assert tool.parse_instant(row.values[6], "fill") == datetime.fromisoformat(filled)
    payload = json.loads(row.values[3])
    assert tool.parse_instant(payload["atr_country_sealed_at"], "seal") == datetime.fromisoformat(seal)
    fills = [tool._parse_fill(item) for item in rows if item.kind == "FANOUT_FILL"]
    assert tool.route_scopes(rows, fills)[(row.values[0], order)] == "NOT_APPLICABLE"
    assert _evaluate(case, rows).exit_code == tool.PASS


def test_named_stak_is_positive_other_strategy_not_missing_metadata_default(packet):
    case = packet["2026-10-22"]
    rows = _rows(case)
    row = _route(rows, STAK)
    payload = json.loads(row.values[3])
    assert payload["orb_entry"] == "true" and payload["orb_country_route"] == "true"
    permit = json.loads(payload["bot_control_permit"])
    assert permit["client_order_id"] == "orb_schwab-STAK-open-5a48637d31f9"
    assert permit["strategy_id"] == "fb750bad-0aa2-4c02-80c6-3c4b89f4ea29"
    assert row.values[6] == ""
    fills = [tool._parse_fill(item) for item in rows if item.kind == "FANOUT_FILL"]
    assert tool.route_scopes(rows, fills)[(row.values[0], STAK)] == "EXCLUDED_NON_ATR"
    report = _evaluate(case, rows)
    assert report.exit_code == tool.PASS
    assert any("excluded_non_atr_orders=1" in line for line in report.lines)


def test_submit_one_and_half_seconds_after_fill_remains_unknown(packet):
    case = packet["2026-10-20"]
    rows = _rows(case)
    row = _route(rows, WORX)
    values = list(row.values)
    filled = tool.parse_instant(values[6], "fill")
    values[5] = (filled + timedelta(seconds=1.5)).isoformat()
    _replace(rows, row, values)
    assert _evaluate(case, rows).exit_code == tool.COULD_NOT_TELL


def test_future_seal_after_raw_submit_remains_unknown_without_flooring_seal(packet):
    case = packet["2026-10-20"]
    rows = _rows(case)
    row = _route(rows, WORX)
    values = list(row.values)
    payload = json.loads(values[3])
    submitted = tool.parse_instant(values[5], "submit")
    payload["atr_country_sealed_at"] = (submitted + timedelta(microseconds=1)).isoformat()
    values[3] = json.dumps(payload)
    _replace(rows, row, values)
    assert _evaluate(case, rows).exit_code == tool.COULD_NOT_TELL


def test_atr_order_missing_metadata_stays_in_cohort_unknown_not_excluded(packet):
    case = packet["2026-10-20"]
    rows = _rows(case)
    row = _route(rows, WORX)
    values = list(row.values)
    values[3] = "{}"
    _replace(rows, row, values)
    assert len(rows) == len(_rows(case))
    fills = [tool._parse_fill(item) for item in rows if item.kind == "FANOUT_FILL"]
    assert tool.route_scopes(rows, fills)[(row.values[0], WORX)] == "UNKNOWN"
    assert _evaluate(case, rows).exit_code == tool.COULD_NOT_TELL
