"""Controlled nine-flag composition and effective-zero audit; no live IO."""

import json
from pathlib import Path

from ops.health.expected_flags_check import (
    ServiceEnvironment,
    audit,
    load_catalog,
    load_numeric_catalog,
)
from project_mai_tai.settings import Settings
from tests.unit.test_all_on_pm import ALL_ON


composition = {**ALL_ON, "orb_schwab_atr_entry_gate_enabled": True}
assert len(composition) == 9
settings = Settings(_env_file=None, **composition, webull_list_primary_reads_enabled=True)
assert all(getattr(settings, name) is True for name in composition)
assert settings.webull_list_primary_reads_enabled
root = Path(__file__).resolve().parents[3]
entries = load_catalog(root / "ops/health/expected_flags.json") + load_numeric_catalog(
    root / "ops/health/expected_numeric.json"
)
environments = {}
for entry in entries:
    value = composition.get(entry["name"], entry["expected"])
    for service in [entry["owning_service"], *entry.get("also_check_services", [])]:
        environments.setdefault(service, {})["MAI_TAI_" + entry["name"].upper()] = str(value).lower()
readers = []


def controlled_proc(service):
    readers.append(service)
    return ServiceEnvironment(101, environments[service], frozenset(), None)


rc, lines = audit(entries, environment_reader=controlled_proc)
assert rc == 0, lines[-1]
assert "mismatches=0 unknown=0" in lines[-1]
assert len(readers) == len(set(readers)) == 9
print(json.dumps({"controlled_not_live": True, "all_on_nine": composition,
                  "webull_list_primary_reads_enabled": True,
                  "effective_zero": lines[-1], "service_reads": len(readers)}, sort_keys=True))
