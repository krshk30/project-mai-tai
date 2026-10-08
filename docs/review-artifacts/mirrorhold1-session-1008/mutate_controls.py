"""Four isolated mutations; never rewrite tracked production source."""
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
        replace(M, "_mirrorhold_dispatch", "self.logger.warning(", "self.logger.info(")
    elif name == "exact_slot_retirement_guard_removed":
        replace(M, "_mirrorhold_gate", 'and md.get("fanout_slot_id") == identity(event)[4]\n', '')
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

    rc = pytest.main(["-q", "-p", "no:cacheprovider", "--tb=short", TEST + CASES[name]], plugins=[Classifier()])
    path.write_text(json.dumps({"exit_code": int(rc), "reports": reports}, indent=2))
    return int(rc)


def main():
    if len(sys.argv) == 5 and sys.argv[1] == "--child":
        return child(sys.argv[2], sys.argv[3], Path(sys.argv[4]))
    receipts = Path("/tmp/mirrorholdG-mutants")
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
