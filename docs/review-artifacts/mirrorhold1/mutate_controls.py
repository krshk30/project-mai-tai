"""[codex] Isolated in-memory safety mutations; no tracked source is rewritten."""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import FunctionType


TEST = "tests/unit/test_mirrorhold1_retained_hold.py::"
CASES = {
    "queue_spends_wire_budget": "test_queue_and_duplicate_copies_spend_only_one_actual_wire",
    "reprice_resets_wire_budget": "test_three_actual_resubmissions_cap_survives_reprice_and_restart",
    "rpg_forget_deletes_durable_owner": "test_recorded_ipdn_1027_proxy_steps_survive_until_1048",
    "final_dispatch_cap_removed": "test_final_cap_guard_survives_an_accepted_fourth_wire_then_cancel",
    "reused_client_guard_removed": "test_direct_dispatch_guard_refuses_a_previously_used_client",
    "terminal_cancel_retains_permission": "test_controlled_actual_terminal_cancel_fences_queued_generation",
    "affirmative_no_wire_guard_removed": "test_real_adapter_no_wire_freshness_is_free_but_false_marker_is_not",
    "enqueue_token_fences_removed": "test_enqueue_ack_loss_invalidates_old_copy_without_losing_hold",
    "wire_evidence_guard_removed": "test_terminal_report_without_submission_evidence_does_not_release_unknown_budget",
    "cas_guard_removed": "test_stale_revision_cannot_replace_generation",
    "off_recreates_nfq_actor": "test_off_keeps_existing_owner_without_recreating_a_second_nfq_actor",
}


def replace_method(cls, name, old, new, *, count=1):
    original = getattr(cls, name)
    source = textwrap.dedent(inspect.getsource(original))
    if source.count(old) != count:
        raise AssertionError(f"mutation needle not unique: {name}: {old!r}")
    namespace = dict(original.__globals__)
    # Compiling inside a class creates the lexical cell required by zero-arg super.
    wrapper = "class _MutationCompiler:\n" + textwrap.indent(source.replace(old, new), "    ")
    exec(compile(wrapper, "<isolated-mirrorhold1-mutation>", "exec"), namespace)
    compiled = getattr(namespace["_MutationCompiler"], name)
    if compiled.__code__.co_freevars != original.__code__.co_freevars:
        raise RuntimeError(f"mutation closure mismatch: {name}")
    replacement = FunctionType(compiled.__code__, original.__globals__, name,
                               original.__defaults__, original.__closure__)
    replacement.__kwdefaults__ = original.__kwdefaults__
    replacement.__qualname__ = original.__qualname__
    setattr(cls, name, replacement)


def apply(name):
    from project_mai_tai.oms.mirror_retained_hold import MirrorRetainedHoldMixin as M
    if name == "queue_spends_wire_budget":
        replace_method(M, "_mirrorhold_evaluate", '"phase": "queued", "token": token,',
                       '"phase": "queued", "token": token, "wire_submissions": data["wire_submissions"] + 1,')
    elif name == "reprice_resets_wire_budget":
        replace_method(M, "_mirrorhold_upsert", 'data = row.payload',
                       'data = {**row.payload, "wire_submissions": 0, "wire_clients": []}')
    elif name == "rpg_forget_deletes_durable_owner":
        from sqlalchemy import select
        from project_mai_tai.db.models import DashboardSnapshot
        from project_mai_tai.oms.mirror_retained_hold import SNAPSHOT_TYPE
        from project_mai_tai.oms.service import OmsRiskService
        original = OmsRiskService._forget_webull_mirror_deferred

        def forget(self, slot_id, *, reason):
            if reason == "rpg_reauthorizes_price_wait":
                with self.session_factory() as session:
                    rows = session.scalars(select(DashboardSnapshot).where(
                        DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE)).all()
                    for row in rows:
                        if row.payload["identity"][4] == slot_id:
                            session.delete(row)
                    session.commit()
            return original(self, slot_id, reason=reason)

        OmsRiskService._forget_webull_mirror_deferred = forget
    elif name == "final_dispatch_cap_removed":
        replace_method(M, "_mirrorhold_dispatch", 'if data["wire_submissions"] >= MAX_WIRE_SUBMISSIONS:', 'if False:')
    elif name == "reused_client_guard_removed":
        replace_method(M, "_mirrorhold_dispatch", 'if client in data["wire_clients"]:', 'if False:')
    elif name == "terminal_cancel_retains_permission":
        replace_method(M, "_mirrorhold_observe_intent", '"phase": "retired",', '"phase": "held",')
    elif name == "affirmative_no_wire_guard_removed":
        replace_method(M, "_mirrorhold_reports", 'r.metadata.get("webull_local_no_wire") == "true" and not\n             r.metadata.get("webull_wire_submitted_at_utc") for r in exact',
                       'not r.metadata.get("webull_wire_submitted_at_utc") for r in exact')
    elif name == "enqueue_token_fences_removed":
        # Compound mutation: remove both independent token admission/dispatch fences.
        replace_method(M, "_mirrorhold_token_matches",
            'data["phase"] == "queued" and data["token"] == event.payload.metadata.get("mirrorhold_token")',
            'data["phase"] in {"held", "queued"}')
        replace_method(M, "_mirrorhold_dispatch",
            'or (md.get("mirrorhold_token") and (data["phase"] != "queued"\n                or data["token"] != md["mirrorhold_token"]))', '')
    elif name == "wire_evidence_guard_removed":
        replace_method(M, "_mirrorhold_reports", 'client in clients and all(', 'all(', count=3)
    elif name == "cas_guard_removed":
        replace_method(M, "_mirrorhold_write", 'if changed != 1:', 'if False:')
    elif name == "off_recreates_nfq_actor":
        replace_method(M, "_nfq_observe_reports", 'if self._mirrorhold_scope(event):', 'if False:')
    else:
        raise ValueError(name)


def child_run(name, mutate, result_path):
    import pytest
    from _pytest.outcomes import Failed

    if mutate:
        apply(name)
    reports = []

    class Classifier:
        def pytest_runtest_setup(self, item):
            if name == "rpg_forget_deletes_durable_owner":
                original_state = item.module.state

                def required_state(lane, event):
                    data = original_state(lane, event)
                    assert data is not None, "durable retained owner must survive RPG forget"
                    return data

                item.module.state = required_state

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            report = (yield).get_result()
            exception = call.excinfo.value if call.excinfo else None
            reports.append({
                "nodeid": item.nodeid, "when": report.when, "outcome": report.outcome,
                "exception_type": type(exception).__name__ if exception else None,
                "assertion_failure": isinstance(exception, (AssertionError, Failed)),
                "failure": str(report.longrepr) if report.failed else None,
            })

    code = pytest.main(["-q", "-p", "no:cacheprovider", "--tb=short",
                        TEST + CASES[name]], plugins=[Classifier()])
    result_path.write_text(json.dumps({"exit_code": int(code), "reports": reports}, indent=2))
    return code


def main():
    if len(sys.argv) == 5 and sys.argv[1] == "--child":
        return child_run(sys.argv[2], sys.argv[3] == "mutant", Path(sys.argv[4]))
    if len(sys.argv) != 1:
        raise SystemExit("usage: mutate_controls.py [--child NAME]")
    results = []
    root = Path(__file__).resolve().parents[3]
    receipt_dir = Path(os.environ.get("MIRRORHOLD_MUTATION_RECEIPTS", "/tmp/mirrorhold1-semantic-controls"))
    receipt_dir.mkdir(parents=True, exist_ok=True)
    for name, target in CASES.items():
        runs = {}
        for mode in ("baseline", "mutant"):
            receipt = receipt_dir / f"{name}-{mode}.json"
            if receipt.exists():
                receipt.unlink()
            command = [sys.executable, str(Path(__file__).resolve()), "--child", name, mode, str(receipt)]
            child = subprocess.run(command, cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "src"},
                                   capture_output=True, text=True, timeout=120)
            output = child.stdout + child.stderr
            (receipt_dir / f"{name}-{mode}.log").write_text(output)
            evidence = json.loads(receipt.read_text()) if receipt.exists() else {"reports": []}
            failures = [r for r in evidence["reports"] if r["outcome"] == "failed"]
            errors = [r for r in failures if r["when"] != "call" or not r["assertion_failure"]]
            assertions = [r for r in failures if r["when"] == "call" and r["assertion_failure"]]
            calls = [r for r in evidence["reports"] if r["when"] == "call"]
            runs[mode] = {
                "exit_code": child.returncode, "receipt": str(receipt),
                "assertion_failures": assertions, "non_assertion_errors": errors,
                "baseline_pass": child.returncode == 0 and bool(calls)
                    and all(r["outcome"] == "passed" for r in calls),
                "harness_error": not receipt.exists() or child.returncode not in (0, 1),
                "output_tail": output[-2500:],
            }
        mutant = runs["mutant"]
        killed = (runs["baseline"]["baseline_pass"] and mutant["exit_code"] == 1
                  and bool(mutant["assertion_failures"]) and not mutant["non_assertion_errors"]
                  and not mutant["harness_error"]
                  and all(r["nodeid"].startswith(TEST + target) for r in mutant["assertion_failures"]))
        results.append({"mutation": name, "target": TEST + target,
                        "semantic_assertion_kill": killed, "runs": runs})
    print(json.dumps({"scope": "controlled isolated subprocesses; tracked source unchanged",
                      "delete_owner_oracle": "explicit non-null state assertion before the existing recorded replay oracle",
                      "mutations": results,
                      "all_semantic_assertion_kills": all(r["semantic_assertion_kill"] for r in results)}, indent=2))
    return 0 if all(r["semantic_assertion_kill"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
