"""Run one isolated runtime mutation at a time; never writes source files."""
import json
from pathlib import Path
import os
import subprocess
import sys

CASES = {
    "tick_sql": "unrelated_empty_outside",
    "publish_before_commit": "cache_publication",
    "rollback_publish": "cache_publication",
    "stale_revision": "older_worker_revision",
    "symbol_isolation": "unrelated_empty_outside",
    "worker_dedup": "generic_worker_dedup",
    "revision_budget": "unknown_gate_is_once",
    "retirement_phase": "durable_retirement",
    "shutdown_invalidate": "shutdown_drains",
    "hold_log": "sxtc_hold_restores",
    "sync_refresh": "broker_sync_refreshes",
    "drift_revalidate": "stale_drift_cache",
    "dirty_wakeup": "last_tick_new_committed",
    "restore_all_phases": "startup_restores_only_held_queued",
    "off_outer_owner_bypass": "flag_off_recorded_sxtc_rows",
    "off_inner_owner_bypass": "flag_off_recorded_sxtc_rows",
    "terminal_owner_leak": "each_committed_owner_phase",
    "discard_uncertain_fence": "uncertain_wire_is_serial_fence",
    "off_nfq_retained_scan": "flag_off_recorded_sxtc_rows",
    "terminal_projection_leak": "flag_off_recorded_sxtc_rows",
}

directory = Path(__file__).parent
results = []
for name, expression in CASES.items():
    output = directory / ("mutation-" + name + ".txt")
    command = [sys.executable, "-m", "pytest", "-p", "tests.unit.hotfix1_mutations",
        "tests/unit/test_hotfix1_symbol_tick_cache.py", "-q", "--tb=short", "-k", expression]
    with output.open("w") as stream:
        try:
            run = subprocess.run(command, env={**os.environ, "HOTFIX1_MUTATION": name},
                stdout=stream, stderr=subprocess.STDOUT, timeout=30)
            code = run.returncode
        except subprocess.TimeoutExpired:
            code = "TIMEOUT"
    result = {"mutation": name, "test_selector": expression, "exit": code,
        "verdict": "RED" if code == 1 else "SURVIVED" if code == 0 else "HARNESS_ERROR"}
    results.append(result)
    print(json.dumps(result), flush=True)
(directory / "mutations.json").write_text(json.dumps(results, indent=2) + "\n")
