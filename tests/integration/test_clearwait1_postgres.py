"""F consumer SQL/CAS controls on the required isolated PostgreSQL CI service."""

import os
from dataclasses import replace
from datetime import UTC, datetime
from threading import get_ident
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.engine import make_url

from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, DashboardSnapshot, OmsManagedPosition, Strategy, TradeIntent,
)
from project_mai_tai.v2_removed_wait import RemovedWaitStore
from tests.integration.test_falseflip1_postgres_epochs import postgres_factory  # noqa: F401
from tests.unit.test_clearwait1_session_rollover import (
    ACCOUNTS, NOW, PRIMARY, RAW, WEBULL, ms, recorded_request, request, seed, service, strategy,
)
from tests.unit.test_clearwait1_unbound import NOW as UNBOUND_NOW, configured_control, controlled_books
from tests.integration.test_cancel_terminal_runtime import sdk as caller_sdk

sdk = caller_sdk


@pytest.fixture
def pg_db(request):
    url = make_url(os.environ["MAI_TAI_DATABASE_URL"])
    assert url.host in {"localhost", "127.0.0.1"} and url.database.endswith("_test")
    factory = request.getfixturevalue("postgres_factory")
    ids = {name: uuid4() for name in ACCOUNTS}
    strategy_id = uuid4()
    with factory() as session:
        session.add(Strategy(id=strategy_id, code="schwab_1m_v2", name="F controlled test"))
        for name, ident in ids.items():
            session.add(BrokerAccount(id=ident, name=name, external_account_id=f"controlled-{name}",
                provider="webull" if name == WEBULL else "schwab", environment="test"))
        session.commit()
    return RemovedWaitStore(factory), factory, ids, strategy_id


def controlled_terminal(db, req):
    seed(db, req)
    with db[1]() as session:
        session.execute(update(TradeIntent).values(status="cancelled",
            updated_at=datetime.fromtimestamp(req.requested_at_ms / 1000, UTC)))
        session.commit()
        assert all(row.updated_at == datetime.fromtimestamp(req.requested_at_ms / 1000, UTC)
                   for row in session.scalars(select(TradeIntent)).all())


@pytest.mark.asyncio
async def test_pg_boot_offloop_retirement_precedes_barriers(pg_db, monkeypatch):
    store, factory, _, _ = pg_db
    req = request()
    controlled_terminal(pg_db, req)
    original = RemovedWaitStore.retire_prior_sessions
    thread = get_ident()

    def retire(self, *args, **kwargs):
        assert get_ident() != thread
        return original(self, *args, **{**kwargs, "now": NOW})

    monkeypatch.setattr(RemovedWaitStore, "retire_prior_sessions", retire)
    bot = service(strategy(req), store)
    await bot._configure_removed_wait_store()
    assert not bot.strategy._removed_wait_requests and not bot.strategy._pending_intents
    with factory() as session:
        rows = session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "v2_removed_wait").order_by(
                DashboardSnapshot.created_at, DashboardSnapshot.id)).all()
        assert rows[0].payload == req.payload(active=True)
        assert rows[-1].payload["reason"] == "session_rollover" and not rows[-1].payload["active"]


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("kind", ["order", "managed", "pending"])
def test_pg_either_account_working_managed_or_pending_stays_closed(pg_db, account, kind):
    store, factory, ids, strategy_id = pg_db
    req = request()
    controlled_terminal(pg_db, req)
    with factory() as session:
        if kind == "order":
            session.add(BrokerOrder(strategy_id=strategy_id, broker_account_id=ids[account],
                symbol=req.symbol, side="buy", order_type="limit", time_in_force="day",
                quantity=1, status="accepted", client_order_id="controlled-working"))
        elif kind == "managed":
            session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account,
                symbol=req.symbol, entry_price=1, original_quantity=1, current_quantity=1, status="open"))
        else:
            session.add(TradeIntent(strategy_id=strategy_id, broker_account_id=ids[account],
                symbol=req.symbol, side="buy", intent_type="cancel", quantity=1,
                reason="controlled pending cancellation", status="pending"))
        session.commit()
    proof, = store.retire_prior_sessions((req,), ACCOUNTS, now=NOW)
    assert not proof.clear and store.restore() == {req.symbol: req}


def test_pg_latest_token_cas_and_same_day_retry_are_not_retired(pg_db):
    store, _, _, _ = pg_db
    old = request()
    controlled_terminal(pg_db, old)
    newer = replace(old, token="newer-token", opportunity_id=ms(NOW), requested_at_ms=ms(NOW))
    store.record(newer, True)
    assert not store.retire_prior_sessions((old,), ACCOUNTS, now=NOW)[0].clear
    assert store.restore() == {newer.symbol: newer}
    today = replace(newer, purpose="retry_exhausted", token="current-retry")
    store.record(today, True)
    assert not store.retire_prior_sessions((today,), ACCOUNTS, now=NOW)[0].clear
    assert store.restore() == {today.symbol: today}


def test_pg_recorded_17_transitions_missing_proof_never_synthetic_flat(pg_db):
    store, factory, _, _ = pg_db
    with factory() as session:
        for row in RAW["archive"]:
            session.add(DashboardSnapshot(id=UUID(row["id"]), snapshot_type="v2_removed_wait",
                payload=row["payload"], created_at=datetime.fromisoformat(row["created_at"])))
        session.commit()
    restored = store.restore()
    assert len(RAW["archive"]) == 17 and len(restored) == 7
    proofs = store.retire_prior_sessions(tuple(restored.values()), ACCOUNTS, now=NOW)
    assert sum(p.clear for p in proofs) == 0 and store.restore() == restored


def test_pg_recorded_dki_missing_shared_proof_stays_unknown(pg_db):
    store, _, _, _ = pg_db
    req = recorded_request("DKI")
    store.record(req, True)
    assert req.opportunity_id == 0 and req.requested_at_ms == 1791465918168
    proof, = store.proofs((req,), ACCOUNTS, now=NOW)
    assert not proof.clear and store.restore() == {"DKI": req}


def unbound_control(database, req, drained=True):
    """Real PostgreSQL/CAS with explicitly controlled books and publication witness."""
    return database[0].retire_unbound((req,), ACCOUNTS, books=controlled_books(database),
        publication_closed={req: drained}, now=UNBOUND_NOW)[0]


def test_pg_legacy_zero_id_without_coverage_stays_active(pg_db):
    req = recorded_request("DKI")
    seed(pg_db, req, receipts=False)
    proof = unbound_control(pg_db, req)
    assert not proof.clear and pg_db[0].restore() == {"DKI": req}
    assert not pg_db[0].restore_terminal_proofs()


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("case", ["null", "retained_mismatch", "provider_mismatch",
                                 "configured_provider_mismatch", "book_identity_mismatch"])
def test_pg_nullable_config_binding_and_mismatch_controls(pg_db, account, case):
    proof = configured_control(pg_db, recorded_request("DKI"), account, case)
    assert not proof.clear


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("kind", ["order", "pending", "managed"])
def test_pg_unbound_buy_and_owned_open_fences_both_accounts(pg_db, account, kind):
    req = recorded_request("DKI")
    seed(pg_db, req, receipts=False)
    with pg_db[1]() as session:
        if kind == "order":
            session.add(BrokerOrder(strategy_id=pg_db[3], broker_account_id=pg_db[2][account],
                symbol="DKI", side="buy", order_type="limit", time_in_force="day", quantity=1,
                status="accepted", client_order_id="controlled-working"))
        elif kind == "pending":
            session.add(TradeIntent(strategy_id=pg_db[3], broker_account_id=pg_db[2][account],
                symbol="DKI", side="", intent_type="cancel", quantity=1,
                reason="controlled unanswered empty-side cancel", status="pending"))
        else:
            session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account,
                symbol="DKI", entry_price=1, original_quantity=1, current_quantity=1, status="open"))
        session.commit()
    assert not unbound_control(pg_db, req).clear and pg_db[0].restore() == {"DKI": req}


def test_pg_unbound_publication_gap_not_empty_db_clear(pg_db):
    req = recorded_request("DKI")
    seed(pg_db, req, receipts=False)
    assert not unbound_control(pg_db, req, drained=False).clear
    assert pg_db[0].restore() == {"DKI": req}


def test_pg_unbound_same_day_retry_retains_active_request(pg_db):
    req = replace(recorded_request("DKI"), purpose="retry_exhausted")
    seed(pg_db, req, receipts=False)
    proof = unbound_control(pg_db, req)
    assert not proof.clear and pg_db[0].restore() == {"DKI": req}
    assert not pg_db[0].restore_terminal_proofs()


def test_pg_unrelated_closed_history_does_not_exhaust_current_proof(pg_db):
    req = recorded_request("DKI")
    seed(pg_db, req, receipts=False)
    with pg_db[1]() as session:
        session.add_all(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=PRIMARY,
            symbol="DKI", entry_price=1, original_quantity=1, current_quantity=0, status="closed")
            for _ in range(2049))
        session.commit()
    assert not unbound_control(pg_db, req).clear


@pytest.fixture
def caller_clock(monkeypatch):
    import project_mai_tai.services.schwab_1m_v2_bot as module
    from project_mai_tai.fanout_segment_store import current_session_anchor
    monkeypatch.setattr(module, "current_session_anchor", lambda: current_session_anchor(UNBOUND_NOW))


@pytest.mark.asyncio
async def test_pg_actual_postcoverage_caller_nullable_ids(pg_db, sdk, caller_clock, monkeypatch):
    from tests.unit.test_clearwait1_never_sent_consumer import test_actual_caller_postcoverage_dki_requires_fresh_webull_book
    await test_actual_caller_postcoverage_dki_requires_fresh_webull_book(pg_db, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", [PRIMARY, WEBULL])
@pytest.mark.parametrize("case", ["working", "token", "old_unresolved", "stamp"])
async def test_pg_actual_journal_unknown_holds(pg_db, caller_clock, monkeypatch, venue, case):
    from tests.unit.test_clearwait1_never_sent_consumer import test_real_journal_or_existing_db_unknown_rolls_back_all_closures
    await test_real_journal_or_existing_db_unknown_rolls_back_all_closures(pg_db, monkeypatch, venue, case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["boot", "missing_event", "quote", "raise"])
async def test_pg_actual_caller_receipts_and_bounded_acquisition(pg_db, sdk, caller_clock, monkeypatch, case):
    from tests.unit import test_clearwait1_runtime_caller as controls
    tests = {"boot": controls.test_recorded_dki_boot_zero_one_two_receipts_all_legacy_unknown,
        "missing_event": controls.test_missing_db_event_after_emit_returns_is_not_absence,
        "quote": controls.test_stalled_offloop_proof_quote_memory_only_unrelated_cancel_immediate,
        "raise": controls.test_fresh_raise_has_no_broker_get_before_cancel_feedback}
    await tests[case](pg_db, sdk, monkeypatch)


@pytest.mark.asyncio
async def test_pg_late_memory_cas_rolls_back_both_admissions(pg_db, monkeypatch):
    from tests.unit.test_clearwait1_never_sent_consumer import test_memory_changes_during_proof_roll_back_admission_and_request_together
    await test_memory_changes_during_proof_roll_back_admission_and_request_together(pg_db, monkeypatch)
