"""Adversarial fresh-journal authority controls. No native/provider claims."""

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from project_mai_tai.brokerdecides1_entry import prepare_send
from project_mai_tai.brokerdecides1_prewire import PublicationBoundary, commit_refusal
from project_mai_tai.brokerdecides1_prewire_authority import SIDECAR, write_fresh_attempt
from project_mai_tai.brokerdecides1_retirement import load_terminal_tokens
from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.v2_removed_wait import RemovedWaitStore
from tests.unit.test_brokerdecides1_entry_hooks import ACCOUNTS
from tests.unit.test_brokerdecides1_prewire_protocol import case


def payload(token="new-controlled-token"):
    return {"schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": "WORX",
        "opportunity_id": "100", "account_names": list(ACCOUNTS), "account_name": ACCOUNTS[0],
        "kind": "attempt", "attempt_token": token}


@pytest.mark.parametrize("copy_mode", ["same_draft", "shallow_copy", "deep_copy"])
def test_a_xadd_unknown_b_refusal_same_token_can_never_retire(case, copy_mode):
    prepare_send(case.strategy, case.draft, ACCOUNTS[0])

    async def send():
        raise ConnectionError("controlled unknown ACK before OMS arrival")

    with pytest.raises(ConnectionError):
        asyncio.run(case.boundary.invoke_xadd(send))
    draft = case.draft if copy_mode == "same_draft" else (
        copy.copy(case.draft) if copy_mode == "shallow_copy" else copy.deepcopy(case.draft))
    with pytest.raises(ValueError, match="consumed_or_mismatched"):
        PublicationBoundary(draft, ACCOUNTS[0], ACCOUNTS)
    assert case.boundary.phase == "XADD_INVOKED_UNKNOWN"
    assert case.strategy._brokerdecides1_memory.reservation_tokens() == {"persisted-attempt"}
    with case.factory() as session:
        assert "WORX" in local_evidence(session, ACCOUNTS)[0]
        assert not load_terminal_tokens(session, ACCOUNTS, {"persisted-attempt"})


@pytest.mark.parametrize("stage", ["bound_before_send", "invoked_unknown", "refused"])
def test_second_boundary_cannot_claim_same_fresh_capability_at_any_stage(case, stage):
    if stage == "invoked_unknown":
        async def send():
            raise ConnectionError()
        with pytest.raises(ConnectionError):
            asyncio.run(case.boundary.invoke_xadd(send))
    elif stage == "refused":
        case.boundary.refuse("controlled", 1300)
    with pytest.raises(ValueError, match="consumed_or_mismatched"):
        PublicationBoundary(case.draft, ACCOUNTS[0], ACCOUNTS)


@pytest.mark.parametrize("restore_kind", ["metadata", "db_payload", "dataclass_replace"])
def test_restart_or_reconstructed_draft_cannot_acquire_no_wire_authority(case, restore_kind):
    restored = replace(case.draft, metadata=dict(case.draft.metadata))
    if restore_kind == "db_payload":
        with case.factory() as session:
            row = session.scalar(select(DashboardSnapshot))
            restored.metadata["clearwait_dispatch_token"] = row.payload["attempt_token"]
            # Even copying the persisted nonce is DATA, never a live capability.
            restored.metadata["copied_journal_nonce"] = row.payload["prewire_authority_nonce"]
    with pytest.raises(ValueError, match="capability_missing"):
        PublicationBoundary(restored, ACCOUNTS[0], ACCOUNTS)
    with case.factory() as session:
        assert "WORX" in local_evidence(session, ACCOUNTS)[0]


def test_new_writer_refuses_existing_unknown_token_without_order_lookup(case):
    with pytest.raises(ValueError, match="token_already_exists"):
        write_fresh_attempt(case.factory, payload("persisted-attempt"))
    with case.factory() as session:
        assert "WORX" in local_evidence(session, ACCOUNTS)[0]


def test_new_writer_refuses_legacy_random_pk_unknown_token(case):
    with case.factory() as session:
        session.add(DashboardSnapshot(id=uuid4(), snapshot_type="v2_wait_dispatch", payload=payload("legacy-unknown")))
        session.commit()
    with pytest.raises(ValueError, match="token_already_exists"):
        write_fresh_attempt(case.factory, payload("legacy-unknown"))


@pytest.mark.parametrize("accounts", [[ACCOUNTS[0]], list(ACCOUNTS)])
def test_flag_off_legacy_writer_keeps_payload_and_never_returns_capability(case, accounts):
    data = {**payload("legacy-single-or-dual"), "account_names": accounts}
    assert RemovedWaitStore(case.factory).record_dispatch(data) is None
    with case.factory() as session:
        row = session.scalar(select(DashboardSnapshot).where(
            DashboardSnapshot.payload["attempt_token"].as_string() == "legacy-single-or-dual"))
        assert row.payload == data
    with pytest.raises(ValueError, match="token_already_exists"):
        write_fresh_attempt(case.factory, payload("legacy-single-or-dual"))


def test_new_writer_token_primary_key_is_stable_and_not_reissued(case):
    capability = write_fresh_attempt(case.factory, payload())
    expected = uuid5(NAMESPACE_URL, "project-mai-tai/prewire-attempt/v1/new-controlled-token")
    assert capability.attempt_id == expected
    with pytest.raises(ValueError, match="token_already_exists"):
        write_fresh_attempt(case.factory, payload())
    assert copy.copy(capability) is capability and copy.deepcopy(capability) is capability
    with pytest.raises(TypeError, match="cannot_be_serialized"):
        import pickle
        pickle.dumps(capability)


def test_concurrent_new_writers_same_token_issue_at_most_one_capability(case):
    barrier = Barrier(2)

    def factory():
        session = case.factory()
        scalar = session.scalar
        waited = False

        def simultaneous(query, *args, **kwargs):
            nonlocal waited
            result = scalar(query, *args, **kwargs)
            if not waited:
                waited = True
                barrier.wait(timeout=5)
            return result

        session.scalar = simultaneous
        return session

    def writer():
        try:
            return write_fresh_attempt(factory, payload("concurrent-new-token"))
        except (ValueError, IntegrityError, OperationalError):
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: writer(), range(2)))
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize("failure", ["commit_ack", "readback"])
def test_missing_fresh_commit_reply_or_readback_never_returns_authority(case, failure):
    calls = []

    def factory():
        calls.append(True)
        if failure == "readback" and len(calls) == 2:
            raise ConnectionError("controlled fresh readback unavailable")
        session = case.factory()
        if failure == "commit_ack":
            commit = session.commit

            def unknown():
                commit()
                raise ConnectionError("controlled fresh commit ACK lost")

            session.commit = unknown
        return session

    with pytest.raises(ConnectionError):
        write_fresh_attempt(factory, payload("fresh-ack-unknown"))
    with pytest.raises(ValueError, match="token_already_exists"):
        write_fresh_attempt(case.factory, payload("fresh-ack-unknown"))
    restored = replace(case.draft, metadata={**case.draft.metadata,
        "clearwait_dispatch_token": "fresh-ack-unknown"})
    with pytest.raises(ValueError, match="capability_missing"):
        PublicationBoundary(restored, ACCOUNTS[0], ACCOUNTS)
