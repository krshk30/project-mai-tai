"""CONTROLLED lock/journal + state tests. No PG, role, schema or copy acceptance.

The imported nonce fixture explicitly creates/cleans only its fresh private
directory; its sole dependency is builtin tmp_path, not another module fixture.
Independent Main approval callbacks below are UNIT doubles, not actual proof.
"""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from tests.unit.test_l12_nonce_restore_caller import nonce as nonce, new_lease
from tools import l12_nonce_copy_lease as module
from tools.l12_main_nonce_target import EXECUTION_BINDING_VERSION, MainNonceTarget


def inputs(target, *, operation="boot", marker_sha="1" * 64):
    binding = dict(version=EXECUTION_BINDING_VERSION,
        target_capability_sha256=target.reviewed_sha256,
        creation_source_head=target.values["source_head"], consumer_source_head="d" * 40,
        consumer_root=str(Path(__file__).resolve().parents[2]),
        consumer_inventory_sha256="2" * 64, review_sha256="3" * 64)
    context = dict(version="L12_POSTCOPY_PRIVATE_STAGE_V1", operation=operation,
        target_capability_sha256=target.reviewed_sha256, claim_id="CONTROLLED-FIRST-STAGE",
        copy_result_sha256="4" * 64, copy_lease_sha256=marker_sha,
        copy_closure_sha256="5" * 64, execution_review_sha256="6" * 64,
        consumer_binding_sha256=sha256(module.canonical(binding)).hexdigest(),
        source_inventory_sha256="2" * 64, expires_ns=module._WALL_NS() + 60000000000,
        maximum_sessions=2)
    return context, binding


def stage(target, context, binding, *, guard=None):
    return module.NonceCopyLease(target, manifest_sha256="d" * 64, metadata_sha256="e" * 64,
        schema_review_sha256="f" * 64, writer_exclusion_receipt_sha256="1" * 64,
        source_guard=lambda observed: observed is target, operation=context["operation"],
        execution_binding=binding, postcopy_context=context,
        postcopy_guard=guard if guard is not None else lambda observed, proof: observed is target)


def consumed_copy(target):
    copy = new_lease(target, "restore")
    raw, digest = copy.raw, copy.receipt_sha256
    copy.close()
    return raw, digest


@pytest.mark.parametrize("operation", ["boot", "manual_stop", "mirror"])
def test_fresh_explicit_stage_preserves_consumed_restore_and_has_own_journal(nonce, operation):
    raw, digest = consumed_copy(nonce)
    context, binding = inputs(nonce, operation=operation, marker_sha=digest)
    lease = stage(nonce, context, binding)
    try:
        assert lease.path.name == operation + "-offline-writer.lock"
        assert lease.file_receipt["exact_readback"] is True
        assert lease.context["source_head"] == nonce.values["source_head"]
        assert lease.context["execution_head"] == "d" * 40
        assert not lease.postcopy_started
        lease.begin_postcopy()
        assert lease.require_postcopy(operation) is True
    finally:
        lease.close()
    assert (Path(nonce.values["root"]) / "restore-offline-writer.lock").read_bytes() == raw
    with pytest.raises(FileExistsError):
        stage(nonce, context, binding)
    with pytest.raises(FileExistsError):
        new_lease(nonce, "restore")


def test_consumed_copy_marker_alone_is_not_independent_closure_proof(nonce):
    _, digest = consumed_copy(nonce)
    context, binding = inputs(nonce, marker_sha=digest)
    with pytest.raises(ValueError, match="independent_Main_copy_closure"):
        stage(nonce, context, binding, guard=lambda *args: False)
    assert not (Path(nonce.values["root"]) / "boot-offline-writer.lock").exists()


def test_missing_original_restore_marker_cannot_be_treated_as_completed_copy(nonce):
    context, binding = inputs(nonce)
    with pytest.raises(FileNotFoundError):
        stage(nonce, context, binding)


def test_shared_gate_blocks_postcopy_while_copy_is_still_active(nonce):
    original = new_lease(nonce, "restore")
    context, binding = inputs(nonce, marker_sha=original.receipt_sha256)
    try:
        with pytest.raises(BlockingIOError):
            stage(nonce, context, binding)
    finally:
        original.close()


def test_changed_original_marker_hash_refuses_before_stage_creation(nonce):
    consumed_copy(nonce)
    context, binding = inputs(nonce, marker_sha="9" * 64)
    with pytest.raises(ValueError, match="original_consumed_copy_marker_bytes_changed"):
        stage(nonce, context, binding)


@pytest.mark.parametrize("change", [
    {"operation": "retry"}, {"claim_id": "../restore"}, {"expires_ns": 0},
    {"maximum_sessions": 0}, {"maximum_sessions": True}, {"maximum_sessions": -1},
    {"copy_result_sha256": "0" * 64}, {"copy_closure_sha256": "unknown"},
    {"source_inventory_sha256": "9" * 64}, {"consumer_binding_sha256": "9" * 64},
    {"target_capability_sha256": "9" * 64}, {"extra": True},
])
def test_context_changes_refuse_in_pure_validation(nonce, change):
    context, binding = inputs(nonce)
    context.update(change)
    with pytest.raises(ValueError, match="exact_unexpired_postcopy_claim"):
        module.validate_postcopy_context(context, operation="boot", target_sha256=nonce.reviewed_sha256,
            execution_binding=binding, observed_ns=1)


def test_postcopy_context_cannot_relabel_old_construct_or_restore(nonce):
    context, binding = inputs(nonce)
    for operation in ("construct", "restore"):
        with pytest.raises(ValueError, match="postcopy_proof_cannot_relabel"):
            module.NonceCopyLease(nonce, manifest_sha256="d" * 64, metadata_sha256="e" * 64,
                schema_review_sha256="f" * 64, writer_exclusion_receipt_sha256="1" * 64,
                source_guard=lambda target: True, operation=operation,
                execution_binding=binding, postcopy_context=context, postcopy_guard=lambda *args: True)


def test_operation_cannot_be_reentered_or_reused_for_ddl_or_restore(nonce):
    _, digest = consumed_copy(nonce)
    context, binding = inputs(nonce, marker_sha=digest)
    lease = stage(nonce, context, binding)
    try:
        with pytest.raises(ValueError, match="exact_started_postcopy_operation"):
            lease.require_postcopy("boot")
        lease.begin_postcopy()
        with pytest.raises(ValueError, match="one_postcopy_stage_attempt"):
            lease.begin_postcopy()
        with pytest.raises(ValueError, match="onechain_attempt"):
            lease.begin_ddl()
        with pytest.raises(ValueError, match="postcopy_stage_is_not_restore"):
            lease(None, SimpleNamespace())
        with pytest.raises(ValueError, match="exact_started_postcopy_operation"):
            lease.require_postcopy("manual_stop")
    finally:
        lease.close()


def test_later_independent_review_refusal_stops_held_stage(nonce):
    _, digest = consumed_copy(nonce)
    context, binding = inputs(nonce, marker_sha=digest)
    allowed = [True]
    lease = stage(nonce, context, binding, guard=lambda *args: allowed[0])
    try:
        lease.begin_postcopy()
        allowed[0] = False
        with pytest.raises(ValueError, match="independent_evidence_changed"):
            lease.require_postcopy("boot")
    finally:
        lease.close()


def test_expiry_uses_captured_real_clock_not_retimed_module_attribute(nonce, monkeypatch):
    _, digest = consumed_copy(nonce)
    context, binding = inputs(nonce, marker_sha=digest)
    lease = stage(nonce, context, binding)
    try:
        lease.begin_postcopy()
        monkeypatch.setattr(module, "_WALL_NS", lambda: context["expires_ns"])
        with pytest.raises(ValueError, match="postcopy_claim_expired"):
            lease.require_postcopy("boot")
    finally:
        lease.close()


def test_session_census_budget_counts_failure_and_refuses_growth(nonce, monkeypatch):
    _, digest = consumed_copy(nonce)
    context, binding = inputs(nonce, marker_sha=digest)
    lease = stage(nonce, context, binding)
    monkeypatch.setattr(MainNonceTarget, "verify_session", lambda *args: None)
    try:
        lease.begin_postcopy()
        for _ in range(2):
            with pytest.raises(ValueError, match="other_nonce_client_backend"):
                lease.verify_session(SimpleNamespace(scalar=lambda statement: 1))
        with pytest.raises(ValueError, match="session_budget_exhausted"):
            lease.verify_session(SimpleNamespace(scalar=lambda statement: pytest.fail("no census after exhausted budget")))
        assert lease.postcopy_sessions == 2
    finally:
        lease.close()


def test_input_mutation_does_not_change_held_context(nonce):
    _, digest = consumed_copy(nonce)
    context, binding = inputs(nonce, marker_sha=digest)
    before = deepcopy(context)
    lease = stage(nonce, context, binding)
    try:
        context["claim_id"] = "mutated"
        assert lease.postcopy_context == before
        assert lease.context["postcopy"] == before
    finally:
        lease.close()


@pytest.mark.parametrize("change", ["mode", "symlink", "hardlink"])
def test_original_copy_marker_unsafe_identity_is_not_adopted(nonce, change):
    _, digest = consumed_copy(nonce)
    marker = Path(nonce.values["root"]) / "restore-offline-writer.lock"
    if change == "mode":
        marker.chmod(0o644)
    elif change == "symlink":
        original = marker.with_name("controlled-original")
        marker.rename(original)
        marker.symlink_to(original)
    else:
        os.link(marker, marker.with_name("controlled-second-link"))
    context, binding = inputs(nonce, marker_sha=digest)
    with pytest.raises((ValueError, OSError)):
        stage(nonce, context, binding)


def test_distinct_postcopy_stages_cannot_overlap_via_separate_stage_paths(nonce):
    _, digest = consumed_copy(nonce)
    first, binding = inputs(nonce, marker_sha=digest)
    second, second_binding = inputs(nonce, operation="manual_stop", marker_sha=digest)
    lease = stage(nonce, first, binding)
    try:
        lease.begin_postcopy()
        with pytest.raises(BlockingIOError):
            stage(nonce, second, second_binding)
    finally:
        lease.close()


def test_postcopy_cannot_bind_original_restore_inputs(nonce):
    _, digest = consumed_copy(nonce)
    context, binding = inputs(nonce, marker_sha=digest)
    lease = stage(nonce, context, binding)
    try:
        lease.begin_postcopy()
        with pytest.raises(ValueError, match="nonce_lease_input_binding_changed"):
            lease.bind_inputs(plan=None, schema_review=None, authority=None, binding=None)
    finally:
        lease.close()


def test_terminal_actual_result_record_is_non_GO_and_prevents_later_sessions(nonce):
    _, digest = consumed_copy(nonce)
    context, binding = inputs(nonce, marker_sha=digest)
    lease = stage(nonce, context, binding)
    try:
        lease.begin_postcopy()
        lease.finish_postcopy(outcome="OBSERVED_NOT_QUALIFICATION", result_sha256="8" * 64)
        assert lease.postcopy_finished is True
        assert lease.journal.last_receipt["exact_readback"] is True
        with pytest.raises(ValueError, match="exact_started_postcopy_operation"):
            lease.require_postcopy("boot")
        with pytest.raises(ValueError, match="exact_started_postcopy_operation"):
            lease.finish_postcopy(outcome="FAILED", result_sha256="9" * 64)
    finally:
        lease.close()


def test_lease_never_mints_GO_from_a_result_hash(nonce):
    _, digest = consumed_copy(nonce)
    context, binding = inputs(nonce, marker_sha=digest)
    lease = stage(nonce, context, binding)
    try:
        lease.begin_postcopy()
        with pytest.raises(ValueError, match="non_GO_outcome"):
            lease.finish_postcopy(outcome="GO", result_sha256="8" * 64)
        assert lease.postcopy_finished is False
    finally:
        lease.close()
