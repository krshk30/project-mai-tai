"""Isolated catalog acceptance; no production reads or runner adoption."""
import json
from pathlib import Path

import pytest

from ops.health import expected_flags_check as flags
from test_install_plan_contract import PLAN, candidate_file

CATALOG = Path(__file__).with_name("expected_numeric.retry-zero.json")
NAME = "strategy_schwab_1m_v2_retry_one_max_retries"
KEY = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_RETRY_ONE_MAX_RETRIES"


def retry_entry():
    return next(row for row in flags.load_numeric_catalog(CATALOG) if row["name"] == NAME)


def test_numeric_catalog_preserves_exact_c21_entries_adds_only_explicit_zero():
    baseline = json.loads(candidate_file("ops/health/expected_numeric.json"))
    artifact = json.loads(CATALOG.read_text())
    assert artifact["schema_version"] == baseline["schema_version"] == 1
    assert artifact["settings"][:-1] == baseline["settings"]
    assert len(artifact["settings"]) == 6
    entry = retry_entry()
    assert entry["expected"] == 0 and type(entry["expected"]) is int
    assert entry["owning_service"] == "schwab-1m-v2"
    assert entry["also_check_services"] == ["oms"]
    assert entry["require_process_env"] is True
    assert sum(1 + len(row.get("also_check_services", []))
               for row in artifact["settings"]) == 10
    bool_rows = json.loads(candidate_file("ops/health/expected_flags.json"))["flags"]
    assert sum(1 + len(row.get("also_check_services", [])) for row in bool_rows) + 10 == 153


def test_explicit_zero_both_owners_pass_without_default():
    seen = []

    def read(service):
        seen.append(service)
        return flags.ServiceEnvironment(1234, {KEY: "0"}, frozenset(), None)

    rc, lines = flags.audit([retry_entry()], read)
    assert rc == 0 and seen == ["schwab-1m-v2", "oms"]
    assert all("source=env:" in line for line in lines[:-1])
    assert "checked=2/2" in lines[-1]


@pytest.mark.parametrize("service", ["schwab-1m-v2", "oms"])
@pytest.mark.parametrize("value", [None, "1", "-1", "bad", ""])
def test_each_owner_missing_nonzero_or_malformed_blocks(service, value):
    def read(owner):
        env = {} if owner == service and value is None else {KEY: value if owner == service else "0"}
        return flags.ServiceEnvironment(1234, env, frozenset(), None)

    rc, lines = flags.audit([retry_entry()], read)
    assert rc != 0
    assert any(f"service={service}" in line and not line.startswith("PASS") for line in lines)


@pytest.mark.parametrize("service", ["schwab-1m-v2", "oms"])
def test_each_owner_read_failure_blocks(service):
    def read(owner):
        if owner == service:
            raise OSError("controlled unreadable process")
        return flags.ServiceEnvironment(1234, {KEY: "0"}, frozenset(), None)

    rc, lines = flags.audit([retry_entry()], read)
    assert rc == 2
    assert any(line.startswith("UNKNOWN") and f"service={service}" in line for line in lines)


def test_plan_accepts_zero_and_keeps_display_outside_restart_authority():
    document = PLAN.read_text()
    assert "RETRY_ONE_ENABLED=true" in document and "RETRY_ONE_MAX_RETRIES=0" in document
    assert "Literal runner integration is LOCALLY TESTED" in document
    assert "FLAGGATE153" in document and "151/153 rc2 UNKNOWN2" in document
    assert "five active items" in document and "#1100 is **SOURCE INCLUDED / ACTIVATION PENDING**" in document
    assert "mergeCommit=4805ddc81184c76b4d5cef5c483c809edb666fe6" in document
    assert "37513775601 attempt2 is SUCCESS" in document
    assert "reload=False" in document and "no\nExecReload" in document
    assert "control must remain untouched" in document
