"""Real service callback methods + private journal. Main emitter tested separately."""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select

from project_mai_tai.brokerdecides1 import AdmissionMemory
from project_mai_tai.brokerdecides1_entry import prepare_send
from project_mai_tai.brokerdecides1_prewire import PublicationBoundary
from project_mai_tai.brokerdecides1_prewire_authority import SIDECAR, write_fresh_attempt
from project_mai_tai.brokerdecides1_publication_callbacks import PublicationCallbacks
from project_mai_tai.brokerdecides1_retirement import load_terminal_tokens
from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from tests.unit.test_brokerdecides1_entry_hooks import ACCOUNTS
from tests.unit.test_brokerdecides1_prewire_protocol import case


def service_case(case):
    # Actual constructor, L12 off so no sender/default transport is loaded.
    service = SchwabV2BotService(Settings(strategy_schwab_1m_v2_brokerdecides1_enabled=False))
    service.strategy, service.settings = case.strategy, case.strategy.settings
    service.session_factory = case.factory
    service.strategy._removed_wait_accounts = lambda: ACCOUNTS
    draft = replace(case.draft, metadata={**case.draft.metadata,
        "clearwait_dispatch_token": "service-fresh-token"})
    capability = write_fresh_attempt(case.factory, {
        "schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": "WORX",
        "opportunity_id": "100", "account_names": list(ACCOUNTS), "account_name": ACCOUNTS[0],
        "kind": "attempt", "attempt_token": "service-fresh-token"})
    setattr(draft, SIDECAR, capability)
    return service, draft


def test_actual_service_factory_callback_commits_then_releases_exact_memory(case):
    service, draft = service_case(case)
    boundary = service._brokerdecides1_publication_boundary(draft, ACCOUNTS[0])
    prepare_send(service.strategy, draft, ACCOUNTS[0])
    proof = boundary.refuse("controlled_serialization_refusal", 1300)
    result = asyncio.run(service._brokerdecides1_prewire_refusal(boundary, proof))
    assert result == "JOURNAL_COMMITTED_EXACT_RESERVATION_RELEASED"
    assert not service.strategy._brokerdecides1_memory.reservation_tokens()
    with case.factory() as session:
        assert load_terminal_tokens(session, ACCOUNTS, {"service-fresh-token"}) == {("WORX", "service-fresh-token")}


def test_actual_service_factory_repeated_emit_cannot_rebind_same_capability(case):
    service, draft = service_case(case)
    first = service._brokerdecides1_publication_boundary(draft, ACCOUNTS[0])
    with pytest.raises(ValueError, match="consumed_or_mismatched"):
        service._brokerdecides1_publication_boundary(draft, ACCOUNTS[0])
    assert first.phase == "BEFORE_XADD"


@pytest.mark.parametrize("failure", ["cancel", "storage"])
def test_callback_failure_or_cancellation_retains_exact_ram(case, failure):
    service, draft = service_case(case)
    boundary = service._brokerdecides1_publication_boundary(draft, ACCOUNTS[0])
    prepare_send(service.strategy, draft, ACCOUNTS[0])
    proof = boundary.refuse("controlled", 1300)

    async def unavailable(*args):
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise ConnectionError("controlled storage failure")

    service._brokerdecides1_publication_callbacks.run_sync = unavailable
    with pytest.raises((asyncio.CancelledError, ConnectionError)):
        asyncio.run(service._brokerdecides1_prewire_refusal(boundary, proof))
    assert service.strategy._brokerdecides1_memory.reservation_tokens() == {"service-fresh-token"}


def test_callback_memory_replacement_is_not_cleared_after_commit(case):
    service, draft = service_case(case)
    boundary = service._brokerdecides1_publication_boundary(draft, ACCOUNTS[0])
    prepare_send(service.strategy, draft, ACCOUNTS[0])
    original = service.strategy._brokerdecides1_memory
    proof = boundary.refuse("controlled", 1300)
    replacement = AdmissionMemory()
    replacement.reserve("WORX", "service-fresh-token")

    async def changed_memory(function, *args):
        result = function(*args)
        service.strategy._brokerdecides1_memory = replacement
        return result

    service._brokerdecides1_publication_callbacks.run_sync = changed_memory
    assert asyncio.run(service._brokerdecides1_prewire_refusal(boundary, proof)) == "JOURNAL_COMMITTED_MEMORY_REPLACED"
    assert original.reservation_tokens() == replacement.reservation_tokens() == {"service-fresh-token"}


def test_callback_rejects_another_adapter_even_with_same_accounts(case):
    service, draft = service_case(case)
    owner = PublicationCallbacks(service)
    boundary = owner.bind(draft, ACCOUNTS[0])
    proof = boundary.refuse("controlled", 1300)
    with pytest.raises(ValueError, match="scope_UNKNOWN"):
        asyncio.run(PublicationCallbacks(service).refused(boundary, proof))


@pytest.mark.parametrize("missing", ["flag", "storage"])
def test_callback_factory_failclosed_without_profile_or_storage(case, missing):
    service, draft = service_case(case)
    if missing == "flag":
        service.settings.strategy_schwab_1m_v2_brokerdecides1_enabled = False
        assert service._brokerdecides1_publication_boundary(draft, ACCOUNTS[0]) is None
        return
    else:
        service.session_factory = None
    with pytest.raises(ValueError, match="storage_UNKNOWN"):
        service._brokerdecides1_publication_boundary(draft, ACCOUNTS[0])


def test_actual_strategy_tracker_carries_capability_only_outside_metadata(case):
    strategy = case.strategy
    strategy._removed_wait_accounts = lambda: ACCOUNTS
    strategy._cancelack_dispatch_generation = 0
    strategy._cancelack_obligations = {}
    strategy._record_removed_wait_dispatch = lambda *a, **k: SchwabV2Strategy._record_removed_wait_dispatch(strategy, *a, **k)
    assert SchwabV2Strategy.track_removed_wait_dispatch(strategy, case.draft, ACCOUNTS[0])
    capability = getattr(case.draft, SIDECAR)
    assert capability.attempt_id != case.boundary._capability.attempt_id
    assert SIDECAR not in case.draft.metadata
    boundary = PublicationBoundary(case.draft, ACCOUNTS[0], ACCOUNTS)
    assert boundary.identity.token == case.draft.metadata["clearwait_dispatch_token"]
    with case.factory() as session:
        row = session.get(DashboardSnapshot, capability.attempt_id)
        assert row.payload["prewire_authority_nonce"] == capability.nonce


def test_missing_writer_capability_does_not_reuse_draft_old_sidecar(case):
    strategy = case.strategy
    strategy._removed_wait_accounts = lambda: ACCOUNTS
    strategy._cancelack_dispatch_generation = 0
    strategy._cancelack_obligations = {}
    strategy._removed_wait_dispatch_persist = lambda payload: None
    strategy._record_removed_wait_dispatch = lambda *a, **k: SchwabV2Strategy._record_removed_wait_dispatch(strategy, *a, **k)
    assert not SchwabV2Strategy.track_removed_wait_dispatch(strategy, case.draft, ACCOUNTS[0])
    assert getattr(case.draft, SIDECAR) is None
