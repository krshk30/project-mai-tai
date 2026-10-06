"""[codex] Isolated in-memory safety mutations; no tracked source is rewritten."""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap


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


def replace_method(cls, name, old, new):
    original = getattr(cls, name)
    source = textwrap.dedent(inspect.getsource(original))
    if source.count(old) != 1:
        raise AssertionError(f"mutation needle not unique: {name}: {old!r}")
    namespace = dict(original.__globals__)
    exec(compile(source.replace(old, new), "<isolated-mirrorhold1-mutation>", "exec"), namespace)
    setattr(cls, name, namespace[name])


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
        original = M._mirrorhold_reports
        source = textwrap.dedent(inspect.getsource(original))
        assert source.count('client in clients and all(') == 3
        namespace = dict(original.__globals__)
        exec(compile(source.replace('client in clients and all(', 'all('), "<isolated-wire-evidence-mutation>", "exec"), namespace)
        M._mirrorhold_reports = namespace["_mirrorhold_reports"]
    elif name == "cas_guard_removed":
        replace_method(M, "_mirrorhold_write", 'if changed != 1:', 'if False:')
    elif name == "off_recreates_nfq_actor":
        replace_method(M, "_nfq_observe_reports", 'if self._mirrorhold_scope(event):', 'if False:')
    else:
        raise ValueError(name)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        name = sys.argv[2]
        apply(name)
        import pytest
        return pytest.main(["-q", "-p", "no:cacheprovider", "--tb=short", TEST + CASES[name]])
    if len(sys.argv) != 1:
        raise SystemExit("usage: mutate_controls.py [--child NAME]")
    results = []
    root = Path(__file__).resolve().parents[3]
    for name, target in CASES.items():
        command = [sys.executable, str(Path(__file__).resolve()), "--child", name]
        child = subprocess.run(command, cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "src"},
                               capture_output=True, text=True, timeout=120)
        output = child.stdout + child.stderr
        # Pytest rc=1 is a killed control. Import/collection/needle errors do not count.
        killed = child.returncode == 1 and "FAILED " + TEST + target in output
        results.append({"mutation": name, "target": TEST + target, "exit_code": child.returncode,
                        "killed": killed, "output_tail": output[-2500:]})
    print(json.dumps({"scope": "controlled isolated subprocesses; tracked source unchanged",
                      "mutations": results, "all_killed": all(r["killed"] for r in results)}, indent=2))
    return 0 if all(r["killed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
