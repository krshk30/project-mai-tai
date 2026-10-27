"""Private controlled sources, not native-book/production/cadence evidence."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.brokerdecides1 import AdmissionMemory
from project_mai_tai.brokerdecides1_entry import entry_decision, prepare_send
from project_mai_tai.brokerdecides1_prewire import PublicationBoundary, commit_refusal
from project_mai_tai.brokerdecides1_prewire_authority import SIDECAR
from project_mai_tai.brokerdecides1_prewire_proof import valid_prewire_ids
from project_mai_tai.brokerdecides1_retirement import load_terminal_tokens, unresolved_query, valid_retirement
from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    BrokerAccount, BrokerDecidesDispatchEvidence as Evidence,
    BrokerDecidesDispatchRetirement as Retirement, BrokerOrder, DashboardSnapshot, Strategy, TradeIntent,
)
from project_mai_tai.v2_removed_wait import RemovedWaitStore
from tests.unit.test_brokerdecides1_entry_hooks import ACCOUNTS, fixture


@pytest.fixture
def case(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'private-publish.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        session.add_all([BrokerAccount(name=n, provider=p, environment="live")
            for n, p in zip(ACCOUNTS, ("schwab", "webull"), strict=True)])
        session.add(Strategy(code="schwab_1m_v2", name="v2"))
        session.commit()
    strategy, state, draft = fixture()
    # Actual record_dispatch transaction, not a fake successful persistence hook.
    store = RemovedWaitStore(factory, prewire_authority=True)
    strategy._removed_wait_dispatch_persist = store.record_dispatch
    capability = store.record_dispatch({"schema_version": 1, "strategy_code": "schwab_1m_v2",
        "brokerdecides_exception": True,
        "kind": "attempt", "attempt_token": "persisted-attempt", "symbol": "WORX",
        "opportunity_id": "100", "account_names": list(ACCOUNTS), "account_name": ACCOUNTS[0]})
    setattr(draft, SIDECAR, capability)
    boundary = PublicationBoundary(draft, ACCOUNTS[0], ACCOUNTS)
    yield SimpleNamespace(engine=engine, factory=factory, strategy=strategy, state=state,
        draft=draft, boundary=boundary)
    engine.dispose()


def closed(case, *, reserved=True):
    if reserved:
        prepare_send(case.strategy, case.draft, ACCOUNTS[0])
    proof = case.boundary.refuse("controlled_serialization_refused", 1300)
    committed = commit_refusal(case.factory, proof)
    return proof, committed


def test_positive_refusal_commits_before_exact_memory_release_and_restart(case):
    prepare_send(case.strategy, case.draft, ACCOUNTS[0])
    proof = case.boundary.refuse("controlled_serialization_refused", 1300)
    assert case.strategy._brokerdecides1_memory.reservation_tokens() == {"persisted-attempt"}
    committed = commit_refusal(case.factory, proof)
    assert case.strategy._brokerdecides1_memory.reservation_tokens() == {"persisted-attempt"}
    assert case.boundary.release_after_commit(case.strategy._brokerdecides1_memory, committed)
    assert not case.boundary.release_after_commit(case.strategy._brokerdecides1_memory, committed)
    with case.factory() as session:
        assert session.scalar(select(func.count()).select_from(BrokerOrder)) == 0
        assert session.scalar(select(func.count()).select_from(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "v2_wait_dispatch")) == 1
        assert not list(session.scalars(unresolved_query(ACCOUNTS)))
        assert session.scalar(select(valid_retirement()).select_from(DashboardSnapshot).where(
            DashboardSnapshot.id == committed.attempt_id)) is True
        assert load_terminal_tokens(session, ACCOUNTS, {"persisted-attempt"}) == {("WORX", "persisted-attempt")}
        inflight, fills, terminals = local_evidence(session, ACCOUNTS)
        assert not inflight and not fills
    # A new consumer still requires its own complete native packet. No flat claim.
    restarted = AdmissionMemory()
    assert restarted.snapshot is None
    assert restarted.assess(accounts=ACCOUNTS, symbol="WORX", segment=900,
        now_ms=1300, max_age_ms=20_000).allowed is False


def test_l11_refusal_before_l12_reservation_can_close_only_its_journal(case):
    proof, committed = closed(case, reserved=False)
    assert not case.boundary.release_after_commit(case.strategy._brokerdecides1_memory, committed)
    with case.factory() as session:
        assert not list(session.scalars(unresolved_query(ACCOUNTS)))


@pytest.mark.parametrize("outcome", ["exception", "cancel", "success", "synchronous_exception"])
def test_invoked_transport_can_never_issue_refusal_or_release(case, outcome):
    prepare_send(case.strategy, case.draft, ACCOUNTS[0])
    calls = []

    async def send(*args, **kwargs):
        calls.append(True)
        if outcome == "exception":
            raise ConnectionError("unknown ACK")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return "1-0"

    def sync_fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("possibly invoked")

    async def run():
        if outcome == "success":
            assert await case.boundary.invoke_xadd(send) == "1-0"
        else:
            with pytest.raises((ConnectionError, asyncio.CancelledError, RuntimeError)):
                await case.boundary.invoke_xadd(sync_fail if outcome == "synchronous_exception" else send)

    asyncio.run(run())
    assert calls == [True]
    with pytest.raises(ValueError, match="not_proven"):
        case.boundary.refuse("not allowed after transport", 1400)
    assert case.strategy._brokerdecides1_memory.reservation_tokens() == {"persisted-attempt"}
    with case.factory() as session:
        assert "WORX" in local_evidence(session, ACCOUNTS)[0]
        assert session.scalar(select(func.count()).select_from(Retirement)) == 0


@pytest.mark.parametrize("change", ["expired", "unknown", "working", "position", "inflight", "fill"])
def test_real_final_gate_after_controlled_await_observes_changed_memory(case, change):
    assert entry_decision(case.strategy, "WORX").allowed

    async def awaited_guard():
        await asyncio.sleep(0)
        memory, snapshot = case.strategy._brokerdecides1_memory, case.strategy._brokerdecides1_memory.snapshot
        if change == "expired":
            case.strategy._now_ms = lambda: 1500  # Exact existing 500ms fixture bound.
        else:
            changes = {"generation": snapshot.generation + 1}
            if change == "unknown":
                changes["local_complete"] = False
            elif change in {"working", "position"}:
                changes["books"] = (snapshot.books[0], replace(snapshot.books[1], **{
                    "working" if change == "working" else "positions": frozenset({"WORX"})}))
            elif change == "inflight":
                changes["inflight"] = frozenset({"WORX"})
            else:
                changes["filled_segments"] = frozenset({("WORX", 900)})
            memory.apply(replace(snapshot, **changes))

    asyncio.run(awaited_guard())
    with pytest.raises(ValueError, match="brokerdecides1_hold"):
        prepare_send(case.strategy, case.draft, ACCOUNTS[0])
    assert not case.strategy._brokerdecides1_memory.reservation_tokens()
    proof = case.boundary.refuse("controlled_final_gate_refusal", 1500)
    commit_refusal(case.factory, proof)


def test_clear_final_gate_stamps_then_sends_once_but_keeps_pending_reservation(case):
    calls = []

    async def run():
        await asyncio.sleep(0)  # All guard work precedes final synchronous gate.
        case.boundary.validate_draft()
        prepare_send(case.strategy, case.draft, ACCOUNTS[0])
        payload = dict(case.draft.metadata)

        async def send(data):
            calls.append(data)
            return "1-0"

        await case.boundary.invoke_xadd(send, payload)
        with pytest.raises(ValueError, match="consumed"):
            await case.boundary.invoke_xadd(send, payload)

    asyncio.run(run())
    assert calls == [{**case.draft.metadata}]
    assert calls[0]["brokerdecides_reservation_token"] == "persisted-attempt"
    assert calls[0]["brokerdecides_line_segment_ms"] == "900"
    assert case.strategy._brokerdecides1_memory.reservation_tokens() == {"persisted-attempt"}


@pytest.mark.parametrize("change", ["attempt", "refusal", "missing_refusal", "missing_evidence",
    "extra_evidence", "account", "strategy", "duplicate_attempt", "duplicate_refusal", "new_order", "new_intent"])
def test_missing_changed_or_new_sources_reopen_positive_projection(case, change):
    proof, committed = closed(case)
    with case.factory() as session:
        attempt = session.get(DashboardSnapshot, committed.attempt_id)
        refusal = session.get(DashboardSnapshot, committed.receipt_id)
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == ACCOUNTS[0]))
        strategy = session.scalar(select(Strategy))
        if change in {"attempt", "refusal"}:
            row = attempt if change == "attempt" else refusal
            row.payload = {**row.payload, "changed": True}
        elif change == "missing_refusal":
            session.delete(refusal)
        elif change == "missing_evidence":
            session.execute(delete(Evidence).where(Evidence.attempt_id == attempt.id,
                Evidence.entity_kind == "prewire_refusal"))
        elif change == "extra_evidence":
            session.add(Evidence(attempt_id=attempt.id, entity_kind="order", entity_id=uuid4(), original_state="foreign"))
        elif change == "account":
            account.external_account_id = "changed"
        elif change == "strategy":
            strategy.code = "foreign_strategy"
        elif change in {"duplicate_attempt", "duplicate_refusal"}:
            row = attempt if change == "duplicate_attempt" else refusal
            session.add(DashboardSnapshot(snapshot_type=row.snapshot_type, payload=dict(row.payload)))
        else:
            parent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
                symbol="WORX", side="buy", intent_type="open", quantity=1, reason="controlled",
                status="filled", payload={"metadata": {"clearwait_dispatch_token": "persisted-attempt"}})
            session.add(parent)
            session.flush()
            if change == "new_order":
                session.add(BrokerOrder(intent_id=parent.id, strategy_id=strategy.id, broker_account_id=account.id,
                    client_order_id="late-order", symbol="WORX", side="buy", quantity=1,
                    order_type="limit", time_in_force="day", status="filled",
                    payload={"clearwait_dispatch_token": "persisted-attempt"}))
        session.flush()
        assert not list(session.scalars(valid_prewire_ids()))
        assert attempt.id in {row.id for row in session.scalars(unresolved_query(ACCOUNTS))}
        assert not load_terminal_tokens(session, ACCOUNTS, {"persisted-attempt"})


@pytest.mark.parametrize("mismatch", ["token", "account", "symbol", "opportunity", "accounts", "missing"])
def test_journal_mismatch_or_absence_is_not_positive_proof(case, mismatch):
    with case.factory() as session:
        row = session.scalar(select(DashboardSnapshot))
        if mismatch == "missing":
            session.delete(row)
        else:
            key = {"token": "attempt_token", "account": "account_name", "symbol": "symbol",
                   "opportunity": "opportunity_id", "accounts": "account_names"}[mismatch]
            row.payload = {**row.payload, key: ["foreign"] if mismatch == "accounts" else "foreign"}
        session.commit()
    prepare_send(case.strategy, case.draft, ACCOUNTS[0])
    proof = case.boundary.refuse("controlled", 1300)
    with pytest.raises(ValueError, match="identity_UNKNOWN"):
        commit_refusal(case.factory, proof)
    assert case.strategy._brokerdecides1_memory.reservation_tokens() == {"persisted-attempt"}


def test_wrong_receipt_cannot_release_unrelated_reservation(case):
    proof, committed = closed(case)
    memory = AdmissionMemory()
    memory.reserve("WORX", "other-token")
    memory.reserve("YMAT", "persisted-attempt")
    assert not case.boundary.release_after_commit(memory, committed)
    assert memory.reservation_tokens() == {"other-token", "persisted-attempt"}
    with pytest.raises(ValueError, match="identity_UNKNOWN"):
        case.boundary.release_after_commit(memory, replace(committed, receipt_id=uuid4()))
    with pytest.raises(ValueError, match="positive_prewire_proof_UNKNOWN"):
        commit_refusal(case.factory, replace(proof, reason="forged clone"))


def test_refused_boundary_cannot_invoke_transport_even_after_commit(case):
    closed(case)
    called = []
    with pytest.raises(ValueError, match="consumed"):
        asyncio.run(case.boundary.invoke_xadd(lambda: called.append(True)))
    assert not called


def test_commit_failure_keeps_reservation_and_original_journal(case):
    prepare_send(case.strategy, case.draft, ACCOUNTS[0])
    proof = case.boundary.refuse("controlled", 1300)

    def unavailable():
        raise ConnectionError("controlled unavailable storage")

    with pytest.raises(ConnectionError):
        commit_refusal(unavailable, proof)
    assert case.strategy._brokerdecides1_memory.reservation_tokens() == {"persisted-attempt"}
    with case.factory() as session:
        assert "WORX" in local_evidence(session, ACCOUNTS)[0]


def test_exact_receipt_retry_is_idempotent_without_refresh(case):
    proof, committed = closed(case)
    again = commit_refusal(case.factory, proof)
    assert again.attempt_id == committed.attempt_id and again.receipt_id == committed.receipt_id
    with case.factory() as session:
        assert session.scalar(select(func.count()).select_from(Retirement)) == 1
        assert session.scalar(select(func.count()).select_from(Evidence)) == 3


@pytest.mark.parametrize("stage", ["commit_reply", "readback"])
def test_commit_unknown_or_readback_failure_keeps_ram_but_restart_can_find_receipt(case, stage):
    prepare_send(case.strategy, case.draft, ACCOUNTS[0])
    proof = case.boundary.refuse("controlled", 1300)
    calls = []

    def factory():
        calls.append(True)
        if stage == "readback" and len(calls) == 2:
            raise ConnectionError("controlled readback unavailable")
        session = case.factory()
        if stage == "commit_reply":
            original_commit = session.commit

            def unknown():
                original_commit()
                raise ConnectionError("controlled commit ACK lost")

            session.commit = unknown
        return session

    with pytest.raises(ConnectionError):
        commit_refusal(factory, proof)
    assert case.strategy._brokerdecides1_memory.reservation_tokens() == {"persisted-attempt"}
    with case.factory() as session:
        assert load_terminal_tokens(session, ACCOUNTS, {"persisted-attempt"}) == {("WORX", "persisted-attempt")}


def test_posttransport_context_finalizer_exception_cannot_be_prewire(case):
    prepare_send(case.strategy, case.draft, ACCOUNTS[0])

    async def send():
        return "1-0"

    asyncio.run(case.boundary.invoke_xadd(send))
    # An outer guard can still raise after the successful transport returns.
    with pytest.raises(ValueError, match="not_proven"):
        case.boundary.refuse("controlled_outer_guard_error", 1300)
    assert case.boundary.phase == "XADD_ACKNOWLEDGED"
    assert case.strategy._brokerdecides1_memory.reservation_tokens() == {"persisted-attempt"}


def test_boundary_is_per_invocation_not_inherited_by_unrelated_tasks(case):
    async def run():
        async def unrelated():
            from project_mai_tai.brokerdecides1_prewire_authority import write_fresh_attempt

            other_draft = replace(case.draft, metadata={**case.draft.metadata,
                "clearwait_dispatch_token": "other-fresh-token"})
            capability = write_fresh_attempt(case.factory, {
                "schema_version": 1, "strategy_code": "schwab_1m_v2", "symbol": "WORX",
                "opportunity_id": "100", "account_names": list(ACCOUNTS), "account_name": ACCOUNTS[0],
                "kind": "attempt", "attempt_token": "other-fresh-token"})
            setattr(other_draft, SIDECAR, capability)
            other = PublicationBoundary(other_draft, ACCOUNTS[0], ACCOUNTS)
            proof = case.boundary.refuse("controlled", 1300)
            committed = commit_refusal(case.factory, proof)
            with pytest.raises(ValueError, match="identity_UNKNOWN"):
                other.release_after_commit(case.strategy._brokerdecides1_memory, committed)
            assert other.phase == "BEFORE_XADD"

        await asyncio.create_task(unrelated())

    asyncio.run(run())
