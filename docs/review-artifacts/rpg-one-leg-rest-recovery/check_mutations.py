"""Isolated assertion mutations; never edit source files or production."""
import importlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

TEST = "tests/unit/test_t43_one_leg_recovery.py"
MUTATIONS = {
    "missing_leg_not_replaced": (
        "project_mai_tai.strategy_core.schwab_1m_v2", "SchwabV2Strategy", "_cw_v2_resting_track",
        "if not state.resting_active or recover_accounts:", "if not state.resting_active:",
        TEST + "::test_t43_recorded_refusal_next_bar_replaces_only_absent_leg",
    ),
    "sibling_not_excluded": (
        "project_mai_tai.strategy_core.schwab_1m_v2", "SchwabV2Strategy", "_queue_resting_place",
        "if recover_accounts is not None:", "if False:",
        TEST + "::test_t43_recorded_refusal_next_bar_replaces_only_absent_leg",
    ),
    "generation_not_bound": (
        "project_mai_tai.strategy_core.schwab_1m_v2", "SchwabV2Strategy", "_rpg_refused_legs",
        'generation in {\n                    old.get("metadata", {}).get("rpg_resting_generation"), replacement_generation,\n                }',
        "True",
        TEST + "::test_t43_uncertain_or_filled_leg_is_never_recovered[new_generation]",
    ),
    "refresh_not_requested": (
        "project_mai_tai.oms.atr_reprice_runtime", "AtrRepriceRuntimeMixin", "_rpg_fresh_open_refusal",
        'if refusal != "rpg_stale_strategy_authorization":', "if True:",
        TEST + "::test_t43_stale_pre_wire_gets_new_v2_authorization_not_an_age_waiver",
    ),
    "age_guard_removed": (
        "project_mai_tai.oms.atr_reprice_runtime", "AtrRepriceRuntimeMixin", "_rpg_open_refusal",
        'not 0 <= self._rpg_now().timestamp() - auth.get("at", 0) <= 1.0', "False",
        TEST + "::test_t43_stale_pre_wire_gets_new_v2_authorization_not_an_age_waiver",
    ),
    "price_identity_removed": (
        "project_mai_tai.oms.atr_reprice_runtime", "AtrRepriceRuntimeMixin", "_rpg_open_refusal",
        "not prices_match or ", "",
        "tests/unit/test_rpgstuck1.py::test_r_t8_canonical_real_difference_is_still_refused",
    ),
    "local_order_called_rejected": (
        "project_mai_tai.oms.atr_reprice_runtime", "AtrRepriceRuntimeMixin", "_rpg_abort_open",
        'order.status = "aborted"', 'order.status = "rejected"',
        TEST + "::test_t43_unreadable_reauthorization_is_audited_client_abort_never_venue_reject",
    ),
    "local_intent_called_rejected": (
        "project_mai_tai.oms.atr_reprice_runtime", "AtrRepriceRuntimeMixin", "_rpg_abort_open",
        'intent.status = "aborted"', 'intent.status = "rejected"',
        TEST + "::test_t43_unreadable_reauthorization_is_audited_client_abort_never_venue_reject",
    ),
    "collision_not_rechecked_after_wait": (
        "project_mai_tai.oms.atr_reprice_runtime", "AtrRepriceRuntimeMixin", "_rpg_fresh_open_refusal",
        'if collision:\n            return collision', "if False:\n            return collision",
        TEST + "::test_t43_reauthorization_rechecks_orb_ownership_after_releasing_shared_lock",
    ),
    "abort_crash_recovery_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", "HandoffJournal", "reconcile_feedback",
        'elif order.status == "aborted":', "elif False:",
        TEST + "::test_t43_committed_abort_recovers_interrupted_dispatch_only_with_exact_no_wire_proof[complete]",
    ),
    "abort_audit_client_source_not_required": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "local_rpg_abort_proof",
        'BrokerOrderEvent.event_source == "client",', "",
        TEST + "::test_t43_committed_abort_recovers_interrupted_dispatch_only_with_exact_no_wire_proof[audit_source]",
    ),
    "abort_generation_not_bound": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "local_rpg_abort_proof",
        'or any(md.get(key)', 'or False and any(md.get(key)',
        TEST + "::test_t43_committed_abort_recovers_interrupted_dispatch_only_with_exact_no_wire_proof[foreign_generation]",
    ),
    "abort_broker_identity_ignored": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "local_rpg_abort_proof",
        'or order.broker_order_id or filled is not None', 'or filled is not None',
        TEST + "::test_t43_committed_abort_recovers_interrupted_dispatch_only_with_exact_no_wire_proof[broker_id]",
    ),
    "abort_origin_not_proven": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "local_rpg_abort_proof",
        'or md.get("refusal_origin") != "client_abort"', "",
        TEST + "::test_t43_committed_abort_recovers_interrupted_dispatch_only_with_exact_no_wire_proof[unknown_origin]",
    ),
    "abort_still_counted_inflight": (
        "project_mai_tai.services.schwab_1m_v2_bot", "SchwabV2BotService", "_fetch_position_maps",
        'if orders and all(local_rpg_abort_proof(session, order) for order in orders):',
        'if False:',
        TEST + "::test_t43_unreadable_reauthorization_is_audited_client_abort_never_venue_reject",
    ),
}


def mutated_run(name):
    module_name, class_name, function, old, new, selector = MUTATIONS[name]
    module = importlib.import_module(module_name)
    owner = getattr(module, class_name) if class_name else module
    original = getattr(owner, function)
    source = textwrap.dedent(inspect.getsource(original))
    if old not in source:
        raise ValueError(f"mutation target missing: {name}")
    namespace = {}
    exec(compile(source.replace(old, new, 1), f"<mutation:{name}>", "exec"), original.__globals__, namespace)
    setattr(owner, function, namespace[function])
    if class_name is None:
        for reference in ("project_mai_tai.services.schwab_1m_v2_bot", "project_mai_tai.oms.mirror_fresh_price"):
            reader = importlib.import_module(reference)
            if getattr(reader, function, None) is original:
                setattr(reader, function, namespace[function])
    return pytest.main(["-q", selector])


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--mutation":
        return mutated_run(sys.argv[2])
    directory = Path("/tmp/codex-t43-mutations")
    directory.mkdir(exist_ok=True)
    results = []
    for name in MUTATIONS:
        result = subprocess.run([sys.executable, __file__, "--mutation", name],
                                capture_output=True, text=True, timeout=40)
        raw = directory / (name + ".log")
        raw.write_text(result.stdout + result.stderr)
        assertion_red = result.returncode == 1 and "AssertionError" in result.stdout
        results.append({"mutation": name, "assertion_red": assertion_red, "rc": result.returncode, "raw": str(raw)})
    print(json.dumps(results, indent=2))
    return 0 if all(row["assertion_red"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
