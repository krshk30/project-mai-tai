"""Local journal contract controls; hosted integration tests supply real PostgreSQL."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import threading
from uuid import uuid4
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter
from project_mai_tai.cancel_terminal_proof import (
    BookOrder, CompleteWorkingBook, UnboundCancelFences, UnboundCancelRequest, evaluate_unbound_cancel_terminal,
)
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore, current_session_anchor
from project_mai_tai.oms import buy_submission_journal as journal

NOW = 1_791_559_801_000
GENERATION = str(NOW + 1_000_000)  # Deliberately not the bind's observed time.


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'buy_token_test.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    factory.threads = []
    event.listen(engine, "before_cursor_execute", lambda *args: factory.threads.append(threading.get_ident()))
    monkeypatch.setattr(journal, "now_ms", lambda: NOW)
    yield factory
    engine.dispose()


def adapter(sessions):
    leaf = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    leaf.accounts_by_name = {"schwab": SchwabAccountConfig(account_hash="actual-hash")}
    return leaf, journal.DurableBuyAdapter(leaf, sessions)


@pytest.mark.asyncio
async def test_routed_simulator_keeps_existing_behavior_without_physical_coverage(sessions):
    calls = []
    async def simulated(req):
        calls.append(req)
        return []
    leaf = SimpleNamespace(submit_order=simulated)
    routed = RoutingBrokerAdapter(default_provider="simulated",
        provider_by_account={"schwab": "simulated"}, factories_by_provider={"simulated": lambda: leaf})
    guard = journal.DurableBuyAdapter(routed, sessions)
    await guard.start()
    await guard.submit_order(request())
    assert len(calls) == 1
    with sessions() as session:
        assert not list(session.scalars(select(journal.BuyCoverageEpoch)))
        assert not list(session.scalars(select(journal.BuySubmissionToken)))


def request(**metadata):
    return OrderRequest("exact-coid", "schwab", "schwab_1m_v2", "DKI", "buy", "open",
        Decimal(1), "ENTRY", {"fanout_segment_id": GENERATION, **metadata})


def bind(sessions, at=NOW - 100, reason="segment_bind"):
    FanoutSegmentIdentityStore(sessions).record("DKI", int(GENERATION), True, reason,
        now=datetime.fromtimestamp(at / 1000, UTC))


def scope(guard, **changes):
    return replace(journal.NeverSentScope("schwab", "actual-hash", "DKI", GENERATION,
        NOW - 100, "request-id", "request-token", NOW - 50, guard.process_id,
        current_session_anchor(datetime.fromtimestamp(NOW / 1000, UTC)).isoformat()), **changes)


@pytest.mark.parametrize("kind", ["submit", "replace"])
@pytest.mark.asyncio
async def test_unique_attempt_committed_before_every_buy_wire(sessions, kind):
    leaf, guard = adapter(sessions)
    seen = []
    async def wire(req, *args):
        with sessions() as independent:
            rows = list(independent.scalars(select(journal.BuySubmissionToken)))
            assert len(rows) == len(seen) + 1
            assert rows[-1].state == "submitting" and rows[-1].client_order_id == req.client_order_id
            seen.append(rows[-1].id)
        answer = ExecutionReport("accepted", req.client_order_id, broker_order_id=None)
        return [answer] if kind == "submit" else answer
    leaf.submit_order = wire
    leaf.replace_bracket_order = wire
    sessions.threads.clear()
    for _ in range(2):
        if kind == "submit":
            await guard.submit_order(request())
        else:
            await guard.replace_bracket_order(request(), "old-broker-id")
    assert len(set(seen)) == 2
    with sessions() as independent:
        assert all(row.state == "reported_ambiguous" for row in independent.scalars(select(journal.BuySubmissionToken)))
    assert any(t != threading.get_ident() for t in sessions.threads)


@pytest.mark.asyncio
async def test_commit_failure_or_cancelled_precommit_never_calls_adapter(sessions, monkeypatch):
    leaf, guard = adapter(sessions)
    calls = []
    async def wire(req):
        calls.append(req)
        return []
    leaf.submit_order = wire
    def fail(*args):
        raise RuntimeError("controlled commit failure")
    monkeypatch.setattr(guard, "_prepare", fail)
    with pytest.raises(RuntimeError, match="commit failure"):
        await guard.submit_order(request())
    assert calls == []


@pytest.mark.parametrize("answer", [None, "timeout", "599", "rejected", "filled"])
@pytest.mark.asyncio
async def test_uncertain_or_reported_answer_does_not_clear_token(sessions, answer):
    leaf, guard = adapter(sessions)
    async def wire(req):
        if answer == "timeout":
            raise TimeoutError("after send")
        if answer is None:
            return []
        return [ExecutionReport("filled" if answer == "filled" else "rejected", req.client_order_id,
            origin="broker", reason=answer, metadata={"http_status": "599"} if answer == "599" else {})]
    leaf.submit_order = wire
    if answer == "timeout":
        with pytest.raises(TimeoutError):
            await guard.submit_order(request())
    else:
        await guard.submit_order(request())
    with sessions() as independent:
        token = independent.scalar(select(journal.BuySubmissionToken))
        assert token.state in {"submitting", "reported_ambiguous"}


@pytest.mark.parametrize("case", ["positive", "old_opportunity", "missing_bind", "restart_only", "old_then_restart", "wrong_start"])
@pytest.mark.asyncio
async def test_coverage_epoch_uses_original_observed_bind_not_numeric_id_or_restart(sessions, case):
    _leaf, guard = adapter(sessions)
    await guard.ensure_coverage("schwab")
    with sessions() as session:
        session.get(journal.BuyCoverageEpoch, (guard.process_id, "actual-hash")).started_at_ms = NOW - 200
        session.commit()
    if case != "missing_bind":
        bind(sessions, at=NOW - 300 if case in {"old_opportunity", "old_then_restart"} else NOW - 100,
             reason="restart_restore" if case == "restart_only" else "segment_bind")
    if case == "old_then_restart":
        bind(sessions, NOW - 20, "restart_restore")
    target = scope(guard, opportunity_started_at_ms=NOW - 300 if case in {"old_opportunity", "old_then_restart"}
                   else NOW - 90 if case == "wrong_start" else NOW - 100)
    with sessions() as session:
        result = journal.close_never_sent_admission(session, target, observed_at_ms=NOW)
        assert result.never_sent is (case == "positive")
        session.rollback()


@pytest.mark.asyncio
async def test_admission_closure_rejects_new_stale_or_missing_generation_wire(sessions):
    leaf, guard = adapter(sessions)
    await guard.ensure_coverage("schwab")
    bind(sessions)
    with sessions() as session:
        session.get(journal.BuyCoverageEpoch, (guard.process_id, "actual-hash")).started_at_ms = NOW - 200
        session.commit()
    with sessions() as session:
        result = journal.close_never_sent_admission(session, scope(guard), observed_at_ms=NOW)
        assert result.never_sent
        session.commit()
    async def deny(*args):
        pytest.fail("closed generation crossed the adapter")
    leaf.submit_order = deny
    for req in (request(), replace(request(), metadata={})):
        with pytest.raises(journal.BuyAdmissionClosed):
            await guard.submit_order(req)


@pytest.mark.asyncio
async def test_failed_consumer_cas_rolls_back_admission_closure(sessions):
    _leaf, guard = adapter(sessions)
    await guard.ensure_coverage("schwab")
    bind(sessions)
    with sessions() as session:
        session.get(journal.BuyCoverageEpoch, (guard.process_id, "actual-hash")).started_at_ms = NOW - 200
        session.commit()
    with sessions() as session:
        assert journal.close_never_sent_admission(session, scope(guard), observed_at_ms=NOW).never_sent
        session.rollback()
    with sessions() as independent:
        assert not list(independent.scalars(select(journal.BuyAdmissionClosure)))


@pytest.mark.asyncio
async def test_unresolved_older_token_blocks_even_new_postcoverage_opportunity(sessions):
    _leaf, guard = adapter(sessions)
    await guard.ensure_coverage("schwab")
    bind(sessions)
    with sessions() as session:
        session.get(journal.BuyCoverageEpoch, (guard.process_id, "actual-hash")).started_at_ms = NOW - 200
        session.add(journal.BuySubmissionToken(id=uuid4(), process_id=uuid4(), account_id="actual-hash",
            account_name="schwab", symbol="DKI", client_order_id="older", generation="older",
            opportunity_started_at_ms=NOW - 1000, created_at_ms=NOW - 1000,
            state="reported_ambiguous", wire_kind="replace", answers=[]))
        session.commit()
    with sessions() as session:
        assert not journal.close_never_sent_admission(session, scope(guard), observed_at_ms=NOW).never_sent


def test_original_missing_or_cross_session_bind_is_unknown(sessions):
    bind(sessions, NOW - 86_400_000)
    bind(sessions, NOW - 100, "restart_restore")
    with sessions() as session:
        assert journal.opportunity_start_ms(session, "DKI", GENERATION, NOW) == 0
        assert journal.opportunity_start_ms(session, "FOREIGN", GENERATION, NOW) == 0
        assert session.scalar(select(DashboardSnapshot)) is not None


@pytest.mark.asyncio
async def test_canonical_two_account_never_sent_uses_actual_journal_not_fake_books(sessions, monkeypatch):
    schwab, _guard = adapter(sessions)
    webull = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
    webull.accounts_by_name = {"webull": WebullAccountConfig(account_id="actual-webull")}
    routed = RoutingBrokerAdapter(default_provider="schwab",
        provider_by_account={"schwab": "schwab", "webull": "webull"},
        factories_by_provider={"schwab": lambda: schwab, "webull": lambda: webull})
    guard = journal.DurableBuyAdapter(routed, sessions)
    monkeypatch.setattr(journal, "now_ms", lambda: NOW - 200)
    await guard.start()
    bind(sessions)
    monkeypatch.setattr(journal, "now_ms", lambda: NOW)
    sc = scope(guard)
    wb = replace(sc, account_name="webull", account_id="actual-webull")
    req = UnboundCancelRequest("DKI", sc.request_id, sc.request_token, sc.generation, "retry_exhausted",
        sc.requested_at_ms, {"schwab": sc.account_id, "webull": wb.account_id},
        {"schwab": "schwab", "webull": "webull"}, sc.session_key,
        sc.opportunity_started_at_ms, {name: str(guard.process_id) for name in ("schwab", "webull")})
    fences = UnboundCancelFences(req, True, True, True, True)
    book = CompleteWorkingBook("webull", "actual-webull", NOW - 25, NOW - 10, True,
        "all_working", (), "broker")
    books = {"webull": book}
    with sessions() as session:
        witnesses = {s.account_name: journal.close_never_sent_admission(session, s, observed_at_ms=NOW)
                     for s in (sc, wb)}
        assert not evaluate_unbound_cancel_terminal(req, {}, fences=fences,
            never_sent_witnesses=witnesses, now_ms=NOW).terminal
        assert evaluate_unbound_cancel_terminal(req, books, fences=fences,
            never_sent_witnesses=witnesses, now_ms=NOW).terminal
        # Operator orders need not have an OMS token. Their book still governs.
        for bad in (None, replace(book, complete=False), replace(book, started_at_ms=NOW - 100),
                    replace(book, account_id="foreign"),
                    replace(book, orders=(BookOrder("manual", "DKI", "working", "buy"),)),
                    replace(book, orders=(BookOrder("manual", "DKI", "working", "unknown"),))):
            assert not evaluate_unbound_cancel_terminal(req, {"webull": bad}, fences=fences,
                never_sent_witnesses=witnesses, now_ms=NOW).terminal
        assert evaluate_unbound_cancel_terminal(req, {"webull": replace(book,
            orders=(BookOrder("manual-sell", "DKI", "working", "sell"),))}, fences=fences,
            never_sent_witnesses=witnesses, now_ms=NOW).terminal
        for changed in (replace(req, token="other"), replace(req, generation="other"),
                        replace(req, session_key="yesterday"), replace(req, opportunity_started_at_ms=NOW - 300)):
            assert not evaluate_unbound_cancel_terminal(changed, books,
                fences=replace(fences, request=changed), never_sent_witnesses=witnesses, now_ms=NOW).terminal
        assert not evaluate_unbound_cancel_terminal(req, books, fences=fences,
            never_sent_witnesses={"schwab": witnesses["schwab"]}, now_ms=NOW).terminal
        session.commit()
