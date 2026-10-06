"""[codex] Baseline/mutant assertion classification; no checkout/production edits."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys


PURE = "project_mai_tai.operator_holdings"
CENSUS = "project_mai_tai.operator_holding_evidence"
RECON = "project_mai_tai.reconciliation.service"
GATE = "project_mai_tai.deploy_preflight"
PURE_TESTS = "tests/unit/test_operator_holdings.py"
GATE_TESTS = "tests/unit/test_operator_holdings_gate.py::"
CASES = {
    "order_count": (PURE, [("session_orders == 0 and session_fills == 0", "session_fills == 0")], PURE_TESTS),
    "fill_count": (PURE, [("session_orders == 0 and session_fills == 0", "session_orders == 0")], PURE_TESTS),
    "pending": (PURE, [("or pending_intents != 0", "or False")], PURE_TESTS),
    "sell": (PURE, [("or unowned_sells != 0", "or False")], PURE_TESTS),
    "fresh": (PURE, [("source_fresh is not True", "False")], PURE_TESTS),
    "complete": (PURE, [("or activity_complete is not True", "or False")], PURE_TESTS),
    "managed": (PURE, [("or managed_quantity != 0", "or False")], PURE_TESTS),
    "virtual": (PURE, [("or virtual_quantity != 0", "or False")], PURE_TESTS),
    "ledger": (PURE, [("or net_fill_balance != 0", "or False")], PURE_TESTS),
    "date": (PURE, [('.date() == date(2026, 10, 6)', '.date() != date(1999, 1, 1)')], PURE_TESTS),
    "account": (PURE, [('and account_name == "live:schwab_1m_v2"', "and True")], PURE_TESTS),
    "symbol": (PURE, [('and symbol == "IPDN"', "and True")], PURE_TESTS),
    "quantity": (PURE, [('and broker_quantity == Decimal("1000")', "and True")], PURE_TESTS),
    "open_virtual": (PURE, [("or open_virtual_rows != 0", "or False")], PURE_TESTS),
    "open_managed": (PURE, [("or open_managed_rows != 0", "or False")], PURE_TESTS),
    "pending_orders": (PURE, [("or pending_orders != 0", "or False")], PURE_TESTS),
    "collected_orders": (CENSUS, [("data.session_orders += 1", "data.session_orders += 0")],
        GATE_TESTS + "test_no_submitted_timestamp_still_counts_refused_order"),
    "collected_fills": (CENSUS, [("data.session_fills += 1", "data.session_fills += 0")],
        GATE_TESTS + "test_roundtrip_counts_not_net_zero_and_exact_ipdn_exception"),
    "collected_managed_zero": (CENSUS, [("data.open_managed_rows += 1", "data.open_managed_rows += 0")],
        GATE_TESTS + "test_open_rows_even_zero_quantities_block_general_and_exact[managed_zero]"),
    "collected_virtual_zero": (CENSUS, [("row.quantity != 0 or row.opened_at is not None", "row.quantity != 0")],
        GATE_TESTS + "test_open_rows_even_zero_quantities_block_general_and_exact[virtual_zero_open]"),
    "collected_pending_order": (CENSUS, [("data.pending_orders += 1", "data.pending_orders += 0")],
        GATE_TESTS + "test_old_pending_buy_is_not_zero_activity_permission[order]"),
    "collected_pending_intent": (CENSUS, [("data.pending_intents += 1", "data.pending_intents += 0")],
        GATE_TESTS + "test_old_pending_buy_is_not_zero_activity_permission[intent]"),
    "direct_complete": (CENSUS, [("or source.complete is not True", "or False")],
        GATE_TESTS + "test_gate_rejects_stale_unknown_partial_or_unbound_proofs[direct_incomplete]"),
    "database_complete": (CENSUS, [("self.database_complete is not True", "False")],
        GATE_TESTS + "test_gate_rejects_stale_unknown_partial_or_unbound_proofs[db_incomplete]"),
    "direct_fresh": (CENSUS, [("and 0 <= (now - value).total_seconds() <= POSITION_PROOF_MAX_AGE_SECONDS", "and True")],
        GATE_TESTS + "test_gate_rejects_stale_unknown_partial_or_unbound_proofs[direct_stale]"),
    "account_inventory": (CENSUS, [("or set(direct) != set(expected)", "or False")],
        GATE_TESTS + "test_gate_rejects_stale_unknown_partial_or_unbound_proofs[missing_account]"),
    "caller_ignores_proof_guards": (GATE, [("proof_failures = operator_holdings_proof.failures(now)", "proof_failures = []")],
        GATE_TESTS + "test_gate_rejects_stale_unknown_partial_or_unbound_proofs[direct_stale]"),
    "historical_waives_unowned_sell": (RECON, [("and not evidence.unowned_sells:", ":")],
        GATE_TESTS + "test_ignored_historical_pair_never_waives_current_unowned_sell"),
    "inconsistent_books_quantity": (CENSUS, [("or self.virtual_quantity != 0 or self.managed_quantity != 0", "or False")],
        GATE_TESTS + "test_flat_direct_read_cannot_hide_inconsistent_or_unknown_bot_evidence"),
    "bad_census_count": (CENSUS, [("type(v) is int and v >= 0", "True")],
        GATE_TESTS + "test_flat_direct_read_cannot_hide_inconsistent_or_unknown_bot_evidence"),
    # Compound census loss: scalar and per-strategy ledgers are independent fences.
    "discard_all_historical_ownership": (CENSUS, [
        ("data.net_fill_balance += balance", "data.net_fill_balance += Decimal(0)"),
        ("data.strategy_balances.get(row.strategy_id, Decimal(\"0\")) + balance",
         "data.strategy_balances.get(row.strategy_id, Decimal(\"0\")) + Decimal(0)"),
    ], GATE_TESTS + "test_tiny_or_cross_strategy_historical_ownership_never_ages_or_nets_out"),
}


def run_child(name, mode, receipt):
    import pytest
    from _pytest.outcomes import Failed

    module_name, edits, target = CASES[name]
    if mode == "mutant":
        module = importlib.import_module(module_name)
        source = Path(module.__file__).read_text()
        for old, new in edits:
            if source.count(old) != 1:
                raise RuntimeError("mutation needle not unique: " + name)
            source = source.replace(old, new)
        # Compile complete modules/classes, retaining lexical cells and real module identity.
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    reports = []

    class Classifier:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            report = (yield).get_result()
            exception = call.excinfo.value if call.excinfo else None
            reports.append({"nodeid": item.nodeid, "when": report.when, "outcome": report.outcome,
                "exception_type": type(exception).__name__ if exception else None,
                "assertion_failure": isinstance(exception, (AssertionError, Failed)),
                "failure": str(report.longrepr) if report.failed else None})

    code = pytest.main(["-q", "-p", "no:cacheprovider", "--tb=short", target], plugins=[Classifier()])
    receipt.write_text(json.dumps({"exit_code": int(code), "reports": reports}, indent=2))
    return code


def main():
    if len(sys.argv) == 5 and sys.argv[1] == "--child":
        return run_child(sys.argv[2], sys.argv[3], Path(sys.argv[4]))
    selected = set(sys.argv[2].split(",")) if len(sys.argv) == 3 and sys.argv[1] == "--only" else set(CASES)
    if not selected or selected - CASES.keys():
        raise ValueError("unknown mutation selection")
    directory = Path(os.environ.get("OPERATOR_HOLDINGS_MUTATIONS", "/tmp/operator-holdings-semantic-controls"))
    directory.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[3]
    results = []
    for name, (_, _, target) in CASES.items():
        if name not in selected:
            continue
        runs = {}
        for mode in ("baseline", "mutant"):
            receipt = directory / f"{name}-{mode}.json"
            if receipt.exists():
                receipt.unlink()
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", name, mode, str(receipt)],
                cwd=root, env={**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True, timeout=120)
            (directory / f"{name}-{mode}.log").write_text(child.stdout + child.stderr)
            data = json.loads(receipt.read_text()) if receipt.exists() else {"reports": []}
            calls = [r for r in data["reports"] if r["when"] == "call"]
            bad = [r for r in data["reports"] if r["outcome"] == "failed"]
            assertions = [r for r in bad if r["when"] == "call" and r["assertion_failure"]]
            errors = [r for r in bad if r not in assertions]
            runs[mode] = {"exit_code": child.returncode, "receipt": str(receipt),
                "baseline_pass": child.returncode == 0 and bool(calls) and all(r["outcome"] == "passed" for r in calls),
                "assertion_failures": assertions, "non_assertion_errors": errors,
                "harness_error": not receipt.exists() or child.returncode not in (0, 1),
                "stderr_tail": child.stderr[-1500:]}
        mutant = runs["mutant"]
        killed = (runs["baseline"]["baseline_pass"] and mutant["exit_code"] == 1
            and bool(mutant["assertion_failures"]) and not mutant["non_assertion_errors"]
            and not mutant["harness_error"])
        results.append({"mutation": name, "target": target, "semantic_assertion_kill": killed, "runs": runs})
    print(json.dumps({"scope": "read-only isolated controls; no tracked mutation edits",
        "mutations": results, "all_semantic_assertion_kills": all(r["semantic_assertion_kill"] for r in results)}, indent=2))
    return not all(r["semantic_assertion_kill"] for r in results)


if __name__ == "__main__":
    raise SystemExit(main())
