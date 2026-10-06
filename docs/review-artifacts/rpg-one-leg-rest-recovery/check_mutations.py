"""Isolated assertion mutations; never edit source files or production."""
import importlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

TEST = "tests/unit/test_t43_one_leg_recovery.py"
CHAIN_TEST = "tests/unit/test_t43_olox_cancel_chain.py"
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
        TEST + "::test_t43_committed_abort_recovers_interrupted_dispatch_only_with_exact_no_wire_proof[unknown_order_and_audit_origin]",
    ),
    "abort_still_counted_inflight": (
        "project_mai_tai.services.schwab_1m_v2_bot", "SchwabV2BotService", "_fetch_position_maps",
        'if orders and all(local_rpg_abort_proof(session, order) for order in orders):',
        'if False:',
        TEST + "::test_t43_unreadable_reauthorization_is_audited_client_abort_never_venue_reject",
    ),
    "ordinary_abort_no_wire_marker_ignored": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "local_rpg_abort_proof",
        'or md.get("rpg_local_abort_no_wire") != "true"', "",
        TEST + "::test_t43_ordinary_open_local_abort_is_terminal_only_for_its_new_audited_intent[False]",
    ),
    "ordinary_abort_event_id_missing": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "local_rpg_abort_proof",
        'or not md.get("rpg_abort_event_id")', "",
        TEST + "::test_t43_ordinary_open_local_abort_is_terminal_only_for_its_new_audited_intent[no_event_id]",
    ),
    "broker_refusal_crash_not_repairable": (
        "project_mai_tai.oms.atr_reprice_handoff", "HandoffJournal", "reconcile_feedback",
        'elif order.status == "rejected" and (proof := broker_rpg_rejection_proof(session, order, job)):',
        'elif False:',
        TEST + "::test_t43_committed_broker_rejection_repairs_only_absent_leg_after_crash[complete-schwab]",
    ),
    "broker_audit_origin_not_required": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "broker_rpg_rejection_proof",
        'BrokerOrderEvent.event_source == "broker",', "",
        TEST + "::test_t43_committed_broker_rejection_repairs_only_absent_leg_after_crash[audit_source-schwab]",
    ),
    "broker_refusal_generation_not_bound": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "broker_rpg_rejection_proof",
        'or any(not md.get(key)', 'or False and any(not md.get(key)',
        TEST + "::test_t43_committed_broker_rejection_repairs_only_absent_leg_after_crash[foreign_generation-schwab]",
    ),
    "aborted_dashboard_reader_removed": (
        "project_mai_tai.services.control_plane", None, "_build_failed_action_rows",
        '"aborted", ', "",
        "tests/unit/test_control_plane.py::test_failed_action_rows_show_audited_local_abort",
    ),
    "aborted_acceptance_reader_removed": (
        "tests.unit.test_fanout_identity_acceptance", "tool", "evaluate",
        "item.status in TERMINAL_STATUSES", 'item.status in (TERMINAL_STATUSES - {"aborted"})',
        "tests/unit/test_fanout_identity_acceptance.py::test_audited_aborted_attempt_counts_as_terminal_without_a_fill",
    ),
    "proven_generation_latch_not_retired": (
        "project_mai_tai.strategy_core.schwab_1m_v2", "SchwabV2Strategy", "rpg_handoff_authorization",
        'state.resting_schwab_generation = ""', "pass",
        CHAIN_TEST + "::test_recorded_1013_placed_1015_cancel_refusal_releases_only_primary_1016",
    ),
    "cancel_status_alone_releases": (
        "project_mai_tai.oms.atr_reprice_handoff", "HandoffJournal", "reconcile_feedback",
        "if at is None:", "if False:",
        CHAIN_TEST + "::test_cancel_transition_requires_exact_durable_zero_proof[missing_successor]",
    ),
    "cancel_generation_proof_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "cancelled_rpg_replacement_proof",
        'and all(old.get("metadata", {}).get(key) == md[key] for key in keys)', "",
        CHAIN_TEST + "::test_cancel_transition_requires_exact_durable_zero_proof[generation]",
    ),
    "cancel_recorded_zero_proof_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "cancelled_rpg_replacement_proof",
        'and proof.get("clear_recorded") is True', "",
        CHAIN_TEST + "::test_cancel_transition_requires_exact_durable_zero_proof[unrecorded]",
    ),
    "cancel_quantity_identity_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "cancelled_rpg_replacement_proof",
        "and old_quantity.is_finite() and old_quantity == quantity", "",
        CHAIN_TEST + "::test_cancel_transition_requires_exact_durable_zero_proof[quantity]",
    ),
    "cancel_old_clear_proof_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "cancelled_rpg_replacement_proof",
        "old_buy_proven_clear(proof)", "True",
        CHAIN_TEST + "::test_cancel_transition_requires_exact_durable_zero_proof[no_rebuy]",
    ),
    "placed_inactive_buy_owner_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "rpg_buy_owned",
        'return include_placed', 'return False',
        CHAIN_TEST + "::test_unproven_replacement_cannot_buy_with_inactive_or_different_generation[cancelled-]",
    ),
    "placed_foreign_generation_buy_owner_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "rpg_buy_owned",
        'return include_placed', 'return False',
        CHAIN_TEST + "::test_unproven_replacement_cannot_buy_with_inactive_or_different_generation[cancelled-CONTROLLED-newer-generation]",
    ),
    "terminal_projection_proof_gate_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "rpg_buy_owned",
        '(job.get("replacement") and not replacement_terminal_zero(job))', "False",
        CHAIN_TEST + "::test_status_only_terminal_projection_without_proof_still_owns_buy",
    ),
    "unproven_terminal_latch_cleared": (
        "project_mai_tai.strategy_core.schwab_1m_v2", "SchwabV2Strategy", "rpg_handoff_authorization",
        'and not replacement_terminal_zero(job)', "and False",
        CHAIN_TEST + "::test_status_only_terminal_projection_without_proof_still_owns_buy",
    ),
    "healthy_placed_cancel_blocked": (
        "project_mai_tai.strategy_core.schwab_1m_v2", "SchwabV2Strategy", "_rpg_leg_owned",
        "include_placed=False", "include_placed=True",
        CHAIN_TEST + "::test_placed_buy_ownership_does_not_prevent_serial_reprice_cancel",
    ),
    "filled_same_slot_buy_owner_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "rpg_buy_owned",
        '(slot is None or slot == job["slot"])', "False",
        CHAIN_TEST + "::test_unproven_replacement_cannot_buy_with_inactive_or_different_generation[partially_filled-]",
    ),
    "filled_first_incorrectly_consumes_reclaim": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "rpg_buy_owned",
        'and (slot is None or slot == job["slot"])', "",
        CHAIN_TEST + "::test_filled_first_slot_does_not_consume_the_independent_reclaim_slot",
    ),
    "feedback_same_phase_proof_revision_lost": (
        "project_mai_tai.strategy_core.schwab_1m_v2", "SchwabV2Strategy", "rpg_handoff_authorization",
        '(token, job["phase"], job.get("revision", 0), replacement_generation,\n'
        '                    json.dumps(job.get("replacement_terminal_report", {}), sort_keys=True),\n'
        '                    job.get("no_rebuy"), job.get("replacement_filled"))',
        '(token, job["phase"])',
        CHAIN_TEST + "::test_same_phase_new_revision_zero_proof_retires_latch_once",
    ),
    "filled_identity_check_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", "HandoffJournal", "reconcile_feedback",
        'if not replacement_order_matches(session, order, job["replacement"]):', 'if False:',
        CHAIN_TEST + "::test_invalid_filled_identity_cannot_adopt_or_consume[generation]",
    ),
    "zero_commit_proof_recheck_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", "HandoffJournal", "change",
        'if changes.get("replacement_terminal_report"):', 'if False:',
        CHAIN_TEST + "::test_zero_proof_cas_rechecks_fill_and_identity_at_commit",
    ),
    "zero_commit_report_fill_check_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", "HandoffJournal", "change",
        'if replacement_has_fills(session, order) or replacement_unknown_fill_report(session, order):',
        'if replacement_unknown_fill_report(session, order):',
        CHAIN_TEST + "::test_zero_proof_cas_rechecks_fill_and_identity_at_commit[fill_report]",
    ),
    "legacy_commit_identity_recheck_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", "HandoffJournal", "change",
        'if _expected_intent is not None:', 'if False:',
        CHAIN_TEST + "::test_legacy_zero_publication_revalidates_audit_under_cas[generation]",
    ),
    "legacy_abort_origin_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "legacy_prewire_abort_report",
        'or not ((payload.get("refusal_origin")', 'or False and not ((payload.get("refusal_origin")',
        CHAIN_TEST + "::test_legacy_audit_requires_exact_identity_and_positive_no_wire[origin]",
    ),
    "legacy_abort_quantity_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "legacy_prewire_abort_report",
        'or intent.quantity != quantity', '',
        CHAIN_TEST + "::test_legacy_audit_requires_exact_identity_and_positive_no_wire[quantity]",
    ),
    "legacy_abort_generation_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "legacy_prewire_abort_report",
        'or any(not request.get("metadata", {}).get(key)', 'or False and any(not request.get("metadata", {}).get(key)',
        CHAIN_TEST + "::test_legacy_audit_requires_exact_identity_and_positive_no_wire[generation]",
    ),
    "legacy_dispatch_absence_only_gate_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "legacy_prewire_abort_report",
        'or session.scalar(select(BrokerOrder.id)', 'or False and session.scalar(select(BrokerOrder.id)',
        CHAIN_TEST + "::test_legacy_zero_publication_revalidates_audit_under_cas[order_exists]",
    ),
    "expired_explicit_zero_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "cancelled_rpg_replacement_proof",
        'evidence.get("rpg_terminal_filled_quantity") == "0"', 'True',
        CHAIN_TEST + "::test_expired_exact_broker_report_requires_explicit_zero[None]",
    ),
    "proof_edge_dedup_removed": (
        "project_mai_tai.oms.atr_reprice_runtime", "AtrRepriceRuntimeMixin", "_rpg_advance",
        'and proof_edge and job.get("replacement_proof_edge") != proof_edge', 'and proof_edge',
        CHAIN_TEST + "::test_proof_only_replacement_read_is_bounded_by_evidence_edge_and_never_submits[unknown]",
    ),
    "legacy_refused_reconciliation_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "replacement_needs_reconciliation",
        ', "refused", "expired"', '',
        'tests/unit/test_rpgstuck1_later_startup.py::test_sckt_legacy_same_phase_later_exact_proof_releases_only_zero_not_filled',
    ),
    "canonical_report_positive_quantity_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", None, "replacement_has_fills",
        'payload.get("filled_quantity")', 'None',
        CHAIN_TEST + "::test_canonical_report_quantity_cannot_be_hidden_by_later_zero[1]",
    ),
    "canonical_unknown_initial_gate_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", "HandoffJournal", "reconcile_feedback",
        'elif replacement_unknown_fill_report(session, order):', 'elif False:',
        CHAIN_TEST + "::test_canonical_report_quantity_cannot_be_hidden_by_later_zero[NaN]",
    ),
    "canonical_unknown_commit_gate_removed": (
        "project_mai_tai.oms.atr_reprice_handoff", "HandoffJournal", "change",
        ' or replacement_unknown_fill_report(session, order)', '',
        CHAIN_TEST + "::test_zero_proof_cas_rechecks_fill_and_identity_at_commit[canonical_unknown]",
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
    if class_name == "tool":
        class ReaderBinding:
            def pytest_collection_modifyitems(self, items):
                bound = set()
                for item in items:
                    reader = getattr(item.module, "tool", None)
                    if reader is None or reader in bound or Path(reader.__file__) != Path(owner.__file__):
                        continue
                    bound.add(reader)
                    namespace = {}
                    actual = getattr(reader, function)
                    exec(compile(source.replace(old, new, 1), f"<mutation:{name}>", "exec"),
                         actual.__globals__, namespace)
                    setattr(reader, function, namespace[function])
                assert bound, "mutation did not bind the collected test's actual file reader"

        return pytest.main(["-q", selector], plugins=[ReaderBinding()])
    namespace = {}
    exec(compile(source.replace(old, new, 1), f"<mutation:{name}>", "exec"), original.__globals__, namespace)
    setattr(owner, function, namespace[function])
    if class_name is None:
        for reference in ("project_mai_tai.services.schwab_1m_v2_bot", "project_mai_tai.oms.mirror_fresh_price",
                          "project_mai_tai.strategy_core.schwab_1m_v2", "project_mai_tai.oms.atr_reprice_runtime"):
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
