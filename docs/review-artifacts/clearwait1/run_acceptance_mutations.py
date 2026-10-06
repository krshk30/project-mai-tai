"""Run isolated in-memory guard mutations against the recorded acceptance controls.

No source files, production connections or other worktrees are modified.
"""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "src/project_mai_tai/v2_removed_wait.py"
TESTS = ROOT / "tests/unit/test_clearwait1_removed_wait.py"


def load_tests():
    spec = importlib.util.spec_from_file_location("clearwait1_controls", TESTS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def mutant(name, reason, *, settle=False):
    tree = ast.parse(SOURCE.read_text())
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        returns = [n for n in node.body if isinstance(n, ast.Return)]
        if any(isinstance(n.value, ast.Call) and n.value.args
               and isinstance(n.value.args[0], ast.Constant)
               and n.value.args[0].value == reason for n in returns):
            node.test = ast.Constant(False)
            matches.append(node.lineno)
    if not settle and len(matches) != 1:
        raise RuntimeError(f"mutation {name} expected one guard, got {matches}")
    module = ModuleType("clearwait1_mutant_" + name)
    sys.modules[module.__name__] = module
    exec(compile(ast.fix_missing_locations(tree), str(SOURCE), "exec"), module.__dict__)
    if settle:
        module.SETTLE_MS = 0
    return module, matches


def main():
    tests = load_tests()
    cases = [
        ("all_account_receipts", "waiting_for_all_cancel_receipts", "test_acceptance_faults_fail_closed", ("missing_receipt",)),
        ("unknown_cancel", "cancel_unknown_or_refused", "test_acceptance_faults_fail_closed", ("unknown_cancel",)),
        ("terminal_order", "opening_order_not_terminal", "test_acceptance_faults_fail_closed", ("pending_order",)),
        ("position_guard", "position_stays_managed", "test_acceptance_faults_fail_closed", ("position",)),
        ("owner_position_identity", "filled_or_ambiguous_owner", "test_acceptance_faults_fail_closed", ("position_id",)),
        ("no_wire_evidence", "no_order_is_not_no_wire", "test_acceptance_faults_fail_closed", ("generic_absence",)),
        ("nfq_terminal", "mirror_retry_not_retired", "test_acceptance_faults_fail_closed", ("nfq_hold",)),
        ("xhg_unknown_ticket", "replacement_ticket_not_terminal", "test_recorded_xhg_unknown_ticket_overrules_rejected_order", ()),
        ("aifa_settle_window", "", "test_recorded_aifa_latest_cancel_not_first_cancel_controls_settle", ()),
    ]
    original = tests.assess_removed_wait
    results = []
    for name, reason, test_name, args in cases:
        test = getattr(tests, test_name)
        test(*args)
        module, lines = mutant(name, reason, settle=name == "aifa_settle_window")
        tests.assess_removed_wait = module.assess_removed_wait
        try:
            test(*args)
        except AssertionError:
            outcome = "KILLED"
        except Exception as exc:
            outcome = "HARNESS_ERROR:" + type(exc).__name__
        else:
            outcome = "SURVIVED"
        finally:
            tests.assess_removed_wait = original
        results.append({"mutation": name, "guard_lines": lines, "test": test_name,
                        "arguments": args, "outcome": outcome})
    print(json.dumps({"source": str(SOURCE.relative_to(ROOT)),
                      "method": "AST guard disabling in isolated memory; baseline control first",
                      "results": results}, indent=2))
    return 0 if all(r["outcome"] == "KILLED" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
