"""Isolated semantic mutations; never rewrite tracked production source."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
TEST = "tests/unit/test_mirrorhold1_session_duplicate_scan.py::"
CASES = {
    "session_bound_removed": "test_recorded_dispatch_replay[AIXI-48]",
    "current_pending_guard_removed": "test_current_orders_still_refuse_and_warn[pending-dispatch_uncertain]",
    "same_segment_fill_excluded": "test_integer_same_segment_survives_sql_bound",
    "exact_slot_retirement_guard_removed": "test_other_or_unreadable_slot_fill_never_retires_this_slot",
    "refusal_warning_removed": "test_session_anchor_both_clocks_and_accounts",
    "queue_session_bound_removed": "tests/unit/test_mirrorhold1_queue_wirecap.py::test_recorded_queue_restore_dispatch_replay[AIXI-48]",
    "restore_session_bound_removed": "tests/unit/test_mirrorhold1_queue_wirecap.py::test_restore_never_materializes_unrelated_old_terminal",
    "lifetime_wire_cap_restored": "tests/unit/test_mirrorhold1_queue_wirecap.py::test_recorded_flye_four_accepted_reprices_do_not_cap_next[False]",
    "price_aggressive_limit_removed": "tests/unit/test_mirrorhold1_retained_hold.py::test_three_actual_resubmissions_cap_survives_reprice_and_restart",
    "final_dispatch_cap_removed": "test_every_dispatch_refusal_is_warning_with_actual_scan_count[cap-actual_submission_cap]",
    "price_aggressive_reprice_reset": "tests/unit/test_mirrorhold1_retained_hold.py::test_three_actual_resubmissions_cap_survives_reprice_and_restart",
    "price_aggressive_history_removed": "tests/unit/test_mirrorhold1_queue_wirecap.py::test_legacy_price_aggressive_cap_is_rebuilt_and_survives_restart",
    "unknown_legacy_budget_released": "tests/unit/test_mirrorhold1_queue_wirecap.py::test_legacy_cap_without_retained_wire_proof_is_not_released",
}
spec = importlib.util.spec_from_file_location("existing_mutation_compiler", Path(__file__).parents[1] / "mirrorhold1/mutate_controls.py")
compiler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compiler)


def mutate(name):
    from project_mai_tai.oms.mirror_retained_hold import MirrorRetainedHoldMixin as M
    replace = compiler.replace_method
    if name == "session_bound_removed":
        replace(M, "_mirrorhold_dispatch", "session_bound=True,", "session_bound=False,")
    elif name == "current_pending_guard_removed":
        replace(M, "_mirrorhold_gate", 'elif order.status != "filled":', 'elif order.status not in {"filled", "pending"}:')
    elif name == "same_segment_fill_excluded":
        replace(M, "_mirrorhold_session_orders", 'cast(segment, String) == identity(event)[3], ', '')
    elif name == "refusal_warning_removed":
        replace(M, "_mirrorhold_refusal", "self.logger.warning(", "self.logger.info(")
    elif name == "exact_slot_retirement_guard_removed":
        replace(M, "_mirrorhold_gate", 'and md.get("fanout_slot_id") == identity(event)[4]\n', '')
    elif name in {"queue_session_bound_removed", "restore_session_bound_removed"}:
        method = "_mirrorhold_prepare_queue" if name.startswith("queue") else "_restore_mirrorhold"
        replace(M, method, "session_bound=True,", "session_bound=False,")
    elif name == "lifetime_wire_cap_restored":
        replace(M, "_mirrorhold_budget_exhausted", 'data.get("price_aggressive_refusals", data["wire_submissions"])', 'data["wire_submissions"]')
    elif name == "price_aggressive_limit_removed":
        replace(M, "_mirrorhold_budget_exhausted", 'return data.get("price_aggressive_refusals", data["wire_submissions"]) >= MAX_PRICE_AGGRESSIVE_REFUSALS', 'return False')
    elif name == "final_dispatch_cap_removed":
        replace(M, "_mirrorhold_dispatch", 'if self._mirrorhold_budget_exhausted(data):', 'if False:')
    elif name == "price_aggressive_reprice_reset":
        replace(M, "_mirrorhold_upsert", 'data = self._mirrorhold_upgrade_budget(session, row, event)',
                'data = {**self._mirrorhold_upgrade_budget(session, row, event), "price_aggressive_clients": [], "price_aggressive_refusals": 0}')
    elif name == "price_aggressive_history_removed":
        replace(M, "_mirrorhold_prior_wires", 'aggressive.append(order.client_order_id)', 'pass')
    elif name == "unknown_legacy_budget_released":
        replace(M, "_mirrorhold_upgrade_budget", 'if uncertain and phase not in {"retired", "filled"}:', 'if False:')
    else:
        raise ValueError(name)


def child(name, mode, path):
    import pytest
    if mode == "mutant":
        mutate(name)
    reports = []

    class Classifier:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            report = (yield).get_result()
            reports.append({"nodeid": item.nodeid, "when": report.when, "outcome": report.outcome,
                            "assertion_failure": bool(call.excinfo and isinstance(call.excinfo.value, AssertionError)),
                            "failure": str(report.longrepr) if report.failed else None})

    target = CASES[name] if "::" in CASES[name] else TEST + CASES[name]
    rc = pytest.main(["-q", "-p", "no:cacheprovider", "--tb=short", target], plugins=[Classifier()])
    path.write_text(json.dumps({"exit_code": int(rc), "reports": reports}, indent=2))
    return int(rc)


def main():
    if len(sys.argv) == 5 and sys.argv[1] == "--child":
        return child(sys.argv[2], sys.argv[3], Path(sys.argv[4]))
    receipts = Path("/tmp/mirrorholdG-queue-final-mutants")
    receipts.mkdir(exist_ok=True)
    results = {}
    for name in CASES:
        runs = {}
        for mode in ("baseline", "mutant"):
            receipt = receipts / f"{name}-{mode}.json"
            proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", name, mode, str(receipt)],
                                  cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
                                  capture_output=True, text=True, timeout=120)
            (receipts / f"{name}-{mode}.log").write_text(proc.stdout + proc.stderr)
            data = json.loads(receipt.read_text()) if receipt.exists() else {"reports": []}
            failures = [r for r in data["reports"] if r["outcome"] == "failed"]
            calls = [r for r in data["reports"] if r["when"] == "call"]
            runs[mode] = {"rc": proc.returncode, "receipt": str(receipt), "failures": failures,
                          "passed": proc.returncode == 0 and bool(calls) and all(r["outcome"] == "passed" for r in calls),
                          "assertion_red": proc.returncode == 1 and bool(failures) and
                              all(r["when"] == "call" and r["assertion_failure"] for r in failures)}
        results[name] = {"runs": runs, "killed": runs["baseline"]["passed"] and runs["mutant"]["assertion_red"]}
    print(json.dumps(results, indent=2))
    return 0 if all(r["killed"] for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
