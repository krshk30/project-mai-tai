"""Controlled Option 2 store witnesses; never historical broker-book receipts."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from project_mai_tai.cancel_terminal_proof import BookOrder, CompleteWorkingBook
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter
from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, DashboardSnapshot, OmsManagedPosition, TradeIntent,
)
from tests.unit.test_clearwait1_session_rollover import (
    ACCOUNTS, PRIMARY, WEBULL, ms, recorded_request, request, seed, service, strategy,
)
from tests.unit.test_clearwait1_session_rollover import db as rollover_db
from project_mai_tai.v2_removed_wait import configured_removed_wait_bindings

db = rollover_db
NOW = datetime(2026, 10, 8, 13, 41, tzinfo=UTC)
CONFIGURED_IDS = {PRIMARY: "controlled-schwab-config-hash", WEBULL: "controlled-webull-config-id"}


def controlled_routing():
    """Real adapter/routing types with explicit controlled config, no credentials or HTTP."""
    schwab = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    schwab.accounts_by_name = {PRIMARY: SchwabAccountConfig(account_hash=CONFIGURED_IDS[PRIMARY])}
    webull = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
    webull.accounts_by_name = {WEBULL: WebullAccountConfig(account_id=CONFIGURED_IDS[WEBULL])}
    return RoutingBrokerAdapter(default_provider="schwab",
        provider_by_account={PRIMARY: "schwab", WEBULL: "webull"},
        factories_by_provider={"schwab": lambda: schwab, "webull": lambda: webull})


def configured_control(database, req, account, case):
    """Nullable box-shaped rows; positive books/drain remain controlled assumptions."""
    seed(database, req, receipts=False)
    expected = configured_removed_wait_bindings(controlled_routing(), req.account_names)
    books = {name: CompleteWorkingBook(name, CONFIGURED_IDS[name], ms(NOW), ms(NOW),
        True, "all_working", (), "broker") for name in req.account_names}
    with database[1]() as session:
        session.execute(update(BrokerAccount).values(external_account_id=None))
        if case in {"retained_match", "retained_mismatch"}:
            session.execute(update(BrokerAccount).where(BrokerAccount.name == account).values(
                external_account_id=CONFIGURED_IDS[account] if case == "retained_match" else "other-id"))
        elif case == "provider_mismatch":
            session.execute(update(BrokerAccount).where(BrokerAccount.name == account).values(provider="other"))
        session.commit()
        before = {a.name: (a.provider, a.external_account_id) for a in session.scalars(select(BrokerAccount))}
    if case == "missing_binding":
        expected.pop(account)
    elif case == "configured_provider_mismatch":
        expected[account] = ("webull" if account == PRIMARY else "schwab", CONFIGURED_IDS[account])
    elif case == "book_identity_mismatch":
        books[account] = replace(books[account], account_id="response-cannot-adopt-this-id")
    elif case == "missing_config":
        expected = None
    proof, = database[0].retire_unbound((req,), ACCOUNTS, books=books,
        publication_closed={req: True}, expected_bindings=expected, now=NOW)
    with database[1]() as session:
        assert {a.name: (a.provider, a.external_account_id) for a in session.scalars(select(BrokerAccount))} == before
    return proof


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("case", ["null", "retained_match", "retained_mismatch", "provider_mismatch",
                                 "configured_provider_mismatch", "missing_binding", "book_identity_mismatch",
                                 "missing_config"])
def test_configured_binding_not_book_or_nullable_db_identity(db, account, case):
    proof = configured_control(db, recorded_request("DKI"), account, case)
    assert proof.clear is (case in {"null", "retained_match"})


def test_routing_binding_refuses_missing_duplicate_or_untyped_accounts():
    for names in ((PRIMARY, "absent"), (PRIMARY, PRIMARY)):
        with pytest.raises((ValueError, RuntimeError)):
            configured_removed_wait_bindings(controlled_routing(), names)
    with pytest.raises(ValueError):
        configured_removed_wait_bindings(SimpleNamespace(accounts_by_name={PRIMARY:
            SimpleNamespace(account_hash="untyped")}), (PRIMARY,))


def controlled_books(database, at=NOW):
    with database[1]() as session:
        return {a.name: CompleteWorkingBook(a.name, a.external_account_id, ms(at), ms(at),
                True, "all_working", (), "broker") for a in session.scalars(select(BrokerAccount))}


def assess(database, req, *, books=None, drained=True, now=NOW):
    return database[0].retire_unbound((req,), ACCOUNTS,
        books=controlled_books(database) if books is None else books,
        publication_closed={req: drained}, now=now)[0]


def test_controlled_recorded_dki_identity_terminal_not_historical_book_claim(db):
    req = recorded_request("DKI")
    seed(db, req, receipts=False)
    proof = assess(db, req)
    assert proof.clear and proof.reason == "unbound_symbol_terminal"
    assert not db[0].restore() and not proof.closed_owned_rows
    with db[1]() as session:
        latest = session.scalar(select(DashboardSnapshot).order_by(DashboardSnapshot.created_at.desc()))
        assert latest.payload["token"] == req.token and latest.payload["active"] is False


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("kind", ["book_buy", "book_unknown", "order", "pending", "cancel", "managed"])
def test_either_account_positive_buy_or_unknown_fence_keeps(db, account, kind):
    req = recorded_request("DKI")
    seed(db, req, receipts=False)
    books = controlled_books(db)
    with db[1]() as session:
        if kind.startswith("book_"):
            books[account] = replace(books[account], orders=(BookOrder("foreign-buy", "DKI",
                "working", "buy" if kind == "book_buy" else "unknown"),))
        elif kind == "order":
            session.add(BrokerOrder(strategy_id=db[3], broker_account_id=db[2][account], symbol="DKI",
                side="buy", order_type="limit", time_in_force="day", quantity=1,
                status="accepted", client_order_id=str(uuid4())))
        elif kind == "managed":
            session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account,
                symbol="DKI", entry_price=1, original_quantity=1, current_quantity=1, status="open"))
        else:
            session.add(TradeIntent(strategy_id=db[3], broker_account_id=db[2][account], symbol="DKI",
                side="buy", intent_type="cancel" if kind == "cancel" else "open", quantity=1,
                reason="controlled unresolved", status="submitting"))
        session.commit()
    assert not assess(db, req, books=books).clear and db[0].restore() == {"DKI": req}


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
def test_operator_positions_and_sell_orders_do_not_block_unbound(db, account):
    req = recorded_request("DKI")
    seed(db, req, receipts=False)
    books = controlled_books(db)
    books[account] = replace(books[account], orders=(BookOrder("operator-sell", "DKI", "working", "sell"),))
    with db[1]() as session:
        session.add(AccountPosition(broker_account_id=db[2][account], symbol="DKI", quantity=1000))
        session.add(BrokerOrder(strategy_id=db[3], broker_account_id=db[2][account], symbol="DKI",
            side="sell", order_type="limit", time_in_force="day", quantity=1000,
            status="accepted", client_order_id="operator-sell"))
        session.commit()
    assert assess(db, req, books=books).clear


@pytest.mark.parametrize("case", ["drain_unknown", "book_missing", "book_stale", "book_pre_request", "token_changed"])
def test_incomplete_scope_never_commits_terminal(db, case):
    req = recorded_request("DKI")
    seed(db, req, receipts=False)
    books = controlled_books(db)
    if case == "book_missing":
        books[PRIMARY] = None
    elif case == "book_stale":
        books[PRIMARY] = replace(books[PRIMARY], started_at_ms=ms(NOW) - 15001)
    elif case == "book_pre_request":
        books[PRIMARY] = replace(books[PRIMARY], started_at_ms=req.requested_at_ms - 1)
    elif case == "token_changed":
        db[0].record(replace(req, token="newer"), True)
    proof = assess(db, req, books=books, drained=case != "drain_unknown")
    assert not proof.clear and db[0].restore()


def test_same_day_retry_terminal_witness_does_not_retire_request(db):
    req = replace(recorded_request("DKI"), purpose="retry_exhausted")
    seed(db, req, receipts=False)
    proof = assess(db, req)
    assert proof.clear and proof.reason == "unbound_symbol_terminal"
    assert db[0].restore() == {"DKI": req}
    assert assess(db, req).clear


def test_restore_witness_preserves_observation_and_new_token_supersedes(db):
    req = recorded_request("DKI")
    seed(db, req, receipts=False)
    proof = assess(db, req)
    assert db[0].restore_terminal_proofs() == (proof,)
    db[0].record(replace(req, token="newer"), True)
    assert not db[0].restore_terminal_proofs()


def test_owned_witness_is_exact_opportunity_account_and_row(db):
    req = replace(request("retry_exhausted"), opportunity_id=ms(NOW), requested_at_ms=ms(NOW))
    seed(db, req, receipts=False)
    with db[1]() as session:
        entry = BrokerOrder(strategy_id=db[3], broker_account_id=db[2][PRIMARY], symbol="DKI",
            side="buy", order_type="limit", time_in_force="day", quantity=100, status="filled",
            client_order_id="controlled-owned-entry", payload={"fanout_segment_id": str(req.opportunity_id)})
        session.add(entry)
        session.flush()
        managed = OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=PRIMARY,
            symbol="DKI", entry_order_id=entry.id, entry_price=1, original_quantity=100,
            current_quantity=0, status="closed")
        session.add(managed)
        session.flush()
        ident = str(managed.id)
        session.commit()
    proof = assess(db, req)
    assert proof.clear and proof.closed_owned_rows == ((PRIMARY, ident),)
    assert db[0].restore_terminal_proofs() == (proof,) and db[0].restore() == {"DKI": req}
    db[0].record(req, False)
    assert not db[0].restore() and db[0].restore_terminal_proofs() == (proof,)
    db[0].record(replace(req, token="newer"), True)
    with pytest.raises(ValueError, match="active removal request changed"):
        db[0].record(req, False)


@pytest.mark.asyncio
async def test_real_boot_delivers_witness_without_rejuvenation_or_duplicate_barrier(db):
    req = replace(recorded_request("DKI"), purpose="retry_exhausted")
    seed(db, req, receipts=False)
    proof = assess(db, req)
    strat = strategy(req)
    strat._now_ms = lambda: ms(NOW) + 16000
    bot = service(strat, db[0])
    await bot._configure_removed_wait_store()
    assert strat._removed_wait_requests == {"DKI": req}
    assert strat._removed_wait_terminal_proofs == (proof,)
    assert strat._removed_wait_terminal_proofs[0].observed_at_ms == ms(NOW)
    assert not strat._pending_intents and not strat._pending_webull_direct_intents
    assert strat._removed_wait_gate_closed("DKI")


@pytest.mark.parametrize("side", ["buy", ""])
def test_pending_cancel_empty_side_is_not_filtered_away(db, side):
    req = recorded_request("DKI")
    seed(db, req, receipts=False)
    with db[1]() as session:
        session.add(TradeIntent(strategy_id=db[3], broker_account_id=db[2][WEBULL], symbol="DKI",
            side=side, intent_type="cancel", quantity=1, reason="controlled unanswered", status="pending"))
        session.commit()
    assert not assess(db, req).clear


@pytest.mark.parametrize("elapsed", [0, 1, 15, 16])
def test_unbound_book_exact_clock_boundary(db, elapsed):
    req = recorded_request("DKI")
    seed(db, req, receipts=False)
    proof = assess(db, req, now=NOW + timedelta(seconds=elapsed))
    assert proof.clear is (elapsed <= 15)
