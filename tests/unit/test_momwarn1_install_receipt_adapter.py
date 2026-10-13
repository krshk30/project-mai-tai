"""Controlled receipts only; no actual box or live-health claim."""

from copy import deepcopy
from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).parents[2] / "ops/health/momwarn1_repair/receipt_adapter.py"
SPEC = importlib.util.spec_from_file_location("momwarn_receipt_adapter", PATH)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


@pytest.fixture
def fixture():
    now = datetime(2026, 10, 13, 11, 10, tzinfo=UTC)
    paths = {"/run/health.json", "/var/log/oms.log", "/proc/12/stat"}
    sources = {"/repo/reader.py": "a" * 64, "/repo/source.py": "b" * 64}
    row = {
        "status": "READ_LIVE_2_OK", "identity": {
            "source_head": "c" * 40, "gateway_instance": "12:123", "gateway_pid": 12,
            "uid": 1000, "gid": 1000, "unit": "project-mai-tai-market-data.service",
            "environment_source": "/proc/12/environ (values not exported)",
        },
        "attempts": [{"attempt": n, "read_status": "OK", "health": {}} for n in (1, 2)],
        "permissions": [{"path": p, "readable": True} for p in paths],
        "native_sources": [{"path": p, "sha256": h} for p, h in sources.items()],
        "caller_sha256": "d" * 64, "observed_ns": int(now.timestamp() * 1e9),
        "health_verdict": "NOT_EVALUATED_NO_GATEWAY_PUBLISH_LAG",
    }
    expectations = dict(expected_head="c" * 40, expected_instance="12:123",
                        expected_uid=1000, expected_gid=1000, expected_paths=paths,
                        expected_sources=sources, expected_caller_sha256="d" * 64, now=now)
    return row, expectations


def test_success_preserves_reader_only_scope_and_raw_hash(fixture):
    row, expectations = fixture
    raw = json.dumps(row).encode()
    result = adapter.f3_receipt(raw, **expectations)
    assert result["status"] == "PASS" and result["read_live_calls"] == 2
    assert result["health_verdict"] == "NOT_EVALUATED_NO_GATEWAY_PUBLISH_LAG"
    assert result["raw_sha256"] == adapter.hashlib.sha256(raw).hexdigest()
    assert "not a gateway health verdict" in result["scope"]


@pytest.mark.parametrize("change", [
    {"uid": 0}, {"gid": 0}, {"uid": True}, {"gateway_instance": "12:124"},
    {"source_head": "e" * 40}, {"environment_source": "env-file"},
])
def test_wrong_service_identity_or_environment_refused(fixture, change):
    row, expectations = fixture
    row["identity"].update(change)
    with pytest.raises(ValueError):
        adapter.f3_receipt(json.dumps(row).encode(), **expectations)


@pytest.mark.parametrize("field,value", [
    ("status", "UNKNOWN"), ("identity_changed", True), ("attempts", []),
    ("permissions", []), ("native_sources", []), ("caller_sha256", "e" * 64),
    ("observed_ns", 0), ("observed_ns", True),
])
def test_missing_failed_or_forged_evidence_refused(fixture, field, value):
    row, expectations = fixture
    row[field] = value
    with pytest.raises(ValueError):
        adapter.f3_receipt(json.dumps(row).encode(), **expectations)


@pytest.mark.parametrize("delta", [-31, 1])
def test_stale_or_future_receipt_refused(fixture, delta):
    row, expectations = fixture
    row["observed_ns"] += delta * 1_000_000_000
    with pytest.raises(ValueError, match="stale or future"):
        adapter.f3_receipt(json.dumps(row).encode(), **expectations)


def test_midread_error_and_duplicate_paths_cannot_be_promoted(fixture):
    original, expectations = fixture
    for kind in ("exception", "unreadable", "duplicate"):
        row = deepcopy(original)
        if kind == "exception":
            row["attempts"][1]["exception_class"] = "PermissionError"
        elif kind == "unreadable":
            row["permissions"][1]["readable"] = False
        else:
            row["permissions"][1] = row["permissions"][0]
        with pytest.raises(ValueError):
            adapter.f3_receipt(json.dumps(row).encode(), **expectations)
