"""CONTROLLED private files/lease, no PG. Unit guards are not Main acceptance."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_l12_nonce_restore_caller import nonce as nonce
from tests.unit.test_l12_postcopy_lease_stages import consumed_copy, inputs, stage


def boot(nonce):
    raw, digest = consumed_copy(nonce)
    context, binding = inputs(nonce, marker_sha=digest)
    lease = stage(nonce, context, binding)
    lease.begin_postcopy()
    return lease, raw


def begin(lease, variant="EMPTY_FLAT", pre="a" * 64, guard=lambda *args: True):
    lease.begin_boot_arm(variant, preimage_sha256=pre, readback_guard=guard)


def finish(lease, variant="EMPTY_FLAT", post="b" * 64, guard=lambda *args: True):
    lease.finish_boot_arm(variant, postimage_sha256=post, result_sha256="c" * 64,
        allowed_mutations_review_sha256="d" * 64, readback_guard=guard)


def test_existing_one_lease_two_arms_requires_actual_poststate_not_original_preimage(nonce):
    lease, original = boot(nonce)
    try:
        begin(lease)
        finish(lease)
        begin(lease, "ONE_WORKING_BUY", "b" * 64)
        finish(lease, "ONE_WORKING_BUY", "e" * 64)
        assert [row["variant"] for row in lease.boot_arms] == ["EMPTY_FLAT", "ONE_WORKING_BUY"]
        assert lease.boot_arms[1]["preimage_sha256"] == lease.boot_arms[0]["postimage_sha256"]
        lease.finish_postcopy(outcome="OBSERVED_NOT_QUALIFICATION", result_sha256="f" * 64)
        assert lease.postcopy_finished and lease.journal.last_receipt["exact_readback"]
    finally:
        lease.close()
    assert (Path(nonce.values["root"]) / "restore-offline-writer.lock").read_bytes() == original


def test_arm2_cannot_reuse_original_preimage_or_reset_stage(nonce):
    lease, _ = boot(nonce)
    try:
        begin(lease)
        finish(lease)
        with pytest.raises(ValueError, match="preimage_order"):
            begin(lease, "ONE_WORKING_BUY", "a" * 64)
        assert lease.boot_arm_failed
        with pytest.raises(ValueError):
            begin(lease, "ONE_WORKING_BUY", "b" * 64)
        with pytest.raises(ValueError, match="one_postcopy_stage_attempt"):
            lease.begin_postcopy()
    finally:
        lease.close()


@pytest.mark.parametrize("variant", ["ONE_WORKING_BUY", "native_flat", "EMPTY"])
def test_wrong_first_arm_is_refused(nonce, variant):
    lease, _ = boot(nonce)
    try:
        with pytest.raises(ValueError):
            begin(lease, variant)
        assert lease.boot_arm_failed
    finally:
        lease.close()


def test_missing_independent_preimage_acceptance_is_not_zero_or_copy_permission(nonce):
    lease, _ = boot(nonce)
    try:
        with pytest.raises(ValueError):
            begin(lease, guard=lambda *args: False)
        assert not lease.boot_arms and lease.boot_arm_failed
    finally:
        lease.close()


def test_owner_or_mutation_or_duplicate_buy_failure_stops_second_arm(nonce):
    lease, _ = boot(nonce)
    try:
        begin(lease)
        finish(lease)
        begin(lease, "ONE_WORKING_BUY", "b" * 64)
        with pytest.raises(ValueError, match="owner_and_no_resend"):
            finish(lease, "ONE_WORKING_BUY", guard=lambda *args: False)
        assert len(lease.boot_arms) == 1 and lease.boot_arm_failed
        with pytest.raises(ValueError, match="incomplete_or_failed"):
            lease.finish_postcopy(outcome="OBSERVED_NOT_QUALIFICATION", result_sha256="a" * 64)
        lease.finish_postcopy(outcome="FAILED", result_sha256="a" * 64)
    finally:
        lease.close()


def test_only_one_arm_cannot_be_reported_complete(nonce):
    lease, _ = boot(nonce)
    try:
        begin(lease)
        finish(lease)
        with pytest.raises(ValueError, match="incomplete_or_failed"):
            lease.finish_postcopy(outcome="OBSERVED_NOT_QUALIFICATION", result_sha256="a" * 64)
    finally:
        lease.close()


def test_active_arm_failure_is_not_retried_or_discarded(nonce):
    lease, _ = boot(nonce)
    try:
        begin(lease)
        lease.fail_boot_arm()
        assert lease.boot_arm_active["variant"] == "EMPTY_FLAT"
        with pytest.raises(ValueError, match="exact_active"):
            lease.require_boot_arm("EMPTY_FLAT")
        with pytest.raises(ValueError):
            begin(lease)
    finally:
        lease.close()


def test_shared_session_budget_not_reset_between_arms(nonce, monkeypatch):
    from tools.l12_main_nonce_target import MainNonceTarget
    lease, _ = boot(nonce)
    monkeypatch.setattr(MainNonceTarget, "verify_session", lambda self, session: True)
    session = SimpleNamespace(scalar=lambda statement: 0)
    try:
        begin(lease)
        lease.verify_session(session)
        finish(lease)
        begin(lease, "ONE_WORKING_BUY", "b" * 64)
        lease.verify_session(session)
        assert lease.postcopy_sessions == 2
        with pytest.raises(ValueError, match="session_budget_exhausted"):
            lease.verify_session(session)
        assert lease.postcopy_sessions == 2
    finally:
        lease.close()


def test_guard_receives_exact_pre_post_result_and_fixed_mutation_review(nonce):
    lease, _ = boot(nonce)
    seen = []
    try:
        begin(lease)
        finish(lease, guard=lambda *args: seen.append(args) or True)
        target, before, post, result, review = seen[0]
        assert target is nonce and before == dict(variant="EMPTY_FLAT", preimage_sha256="a" * 64)
        assert (post, result, review) == ("b" * 64, "c" * 64, "d" * 64)
    finally:
        lease.close()


def test_zero_postimage_cannot_be_accepted_by_a_permissive_unit_guard(nonce):
    lease, _ = boot(nonce)
    try:
        begin(lease)
        with pytest.raises(ValueError, match="poststate_mutations"):
            finish(lease, post="0" * 64)
        assert lease.boot_arm_failed and not lease.boot_arms
    finally:
        lease.close()


def test_readback_primary_exception_identity_survives_and_stops_arm(nonce):
    lease, _ = boot(nonce)
    error = RuntimeError("CONTROLLED_READBACK_FAILURE")

    def fail(*args):
        raise error

    try:
        begin(lease)
        with pytest.raises(RuntimeError) as caught:
            finish(lease, guard=fail)
        assert caught.value is error and lease.boot_arm_failed
        assert lease.boot_arm_active["variant"] == "EMPTY_FLAT"
    finally:
        lease.close()


@pytest.mark.asyncio
async def test_driver_reentry_refuses_before_actor_or_provider_work():
    from tools.l12_recorded_boot_driver import RecordedBootDriver
    driver = object.__new__(RecordedBootDriver)
    driver.consumed = True
    with pytest.raises(ValueError, match="consumed_no_retry"):
        await driver.run(None, copy_guard=lambda: pytest.fail("no second run guard/call"))
