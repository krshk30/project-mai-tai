"""F mixed admission controls using real shared closure APIs and controlled broker evidence."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition, TradeIntent
from project_mai_tai.oms import buy_submission_journal as journal
from tests.unit.test_clearwait1_never_sent_consumer import assess_covered, closures, covered_request, produce_book
from tests.unit.test_clearwait1_runtime_caller import poll, runtime
from tests.unit.test_clearwait1_session_rollover import ACCOUNTS, PRIMARY, WEBULL, ms
from tests.unit.test_clearwait1_session_rollover import db as rollover_db
from tests.unit.test_clearwait1_unbound import CONFIGURED_IDS, NOW
from tests.integration.test_cancel_terminal_runtime import sdk as controlled_sdk

db, sdk = rollover_db, controlled_sdk
pytestmark = pytest.mark.usefixtures("sdk")


def terminal_buy(database, req, guard, account, *, status="cancelled", fault=None, bound=False, order_generation=True):
    """Committed SDK answer identity/order/latest broker event; never a historical replay."""
    coid = f"controlled-terminal-{account}"
    broker_id = f"controlled-broker-{account}"
    token_id = uuid4()
    with database[1]() as session:
        session.get(BrokerAccount, database[2][account]).external_account_id = CONFIGURED_IDS[account]
        order = BrokerOrder(strategy_id=database[3], broker_account_id=database[2][account],
            symbol=req.symbol, side="buy", client_order_id=coid, broker_order_id=broker_id,
            status=status, order_type="limit", time_in_force="day", quantity=1,
            payload={"fanout_segment_id": str(req.opportunity_id)} if order_generation else {})
        session.add(order)
        session.flush()
        event_at = NOW if fault != "pre_token_event" else datetime.fromtimestamp((ms(NOW) - 2000) / 1000, UTC)
        if fault != "missing_event":
            session.add(BrokerOrderEvent(order_id=order.id, event_type=status, event_at=event_at,
                event_source="local" if fault == "local_event" else "broker", payload={}))
        session.add(journal.BuySubmissionToken(id=token_id, process_id=guard.process_id,
            account_name=account, account_id=CONFIGURED_IDS[account], symbol=req.symbol,
            client_order_id=coid, generation=str(req.opportunity_id),
            opportunity_started_at_ms=ms(NOW) - 2000, created_at_ms=ms(NOW) - 1500,
            state="submitting" if fault == "crash" else "reported_ambiguous", wire_kind="submit",
            answers=[] if fault == "lost_id" else [{"origin": "broker", "client_order_id": coid,
                "broker_order_id": "wrong-broker" if fault == "wrong_broker_id" else broker_id}]))
        if status == "filled" or fault == "partial_fill_without_owner":
            session.add(Fill(order_id=order.id, strategy_id=database[3], broker_account_id=database[2][account],
                symbol=req.symbol, side="buy", quantity=1, price=1, filled_at=NOW))
            if fault not in {"missing_owner", "partial_fill_without_owner"}:
                session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account,
                    symbol=req.symbol, entry_order_id=order.id, entry_price=1, original_quantity=1,
                    current_quantity=1 if fault == "open_owner" else 0,
                    status="open" if fault == "open_owner" else "closed"))
        if bound:
            intent = session.scalar(select(TradeIntent).where(
                TradeIntent.broker_account_id == database[2][account], TradeIntent.intent_type == "cancel"))
            intent.payload = {**intent.payload, "metadata": {**intent.payload["metadata"],
                "target_client_order_id": "unproven-target" if fault == "wrong_target" else coid}}
            intent.updated_at = NOW
        session.commit()
    return token_id


def token_states(database):
    with database[1]() as session:
        return [t.state for t in session.scalars(select(journal.BuySubmissionToken))]


@pytest.mark.parametrize("account", [PRIMARY, WEBULL, "both"])
@pytest.mark.parametrize("status", ["cancelled", "rejected", "expired", "filled"])
@pytest.mark.parametrize("order_generation", [True, False])
@pytest.mark.asyncio
async def test_mixed_terminal_and_never_sent_actual_journal_clears(db, monkeypatch, account, status, order_generation):
    req, guard = await covered_request(db, monkeypatch)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    names = ACCOUNTS if account == "both" else {account}
    for name in names:
        terminal_buy(db, req, guard, name, status=status, bound=True, order_generation=order_generation)
    await produce_book(bot, db, req)
    await poll(bot)
    assert not db[0].restore() and not strat._removed_wait_requests
    assert len(closures(db)) == 2 and len(calls) == 1
    assert token_states(db) == ["broker_terminal"] * len(names)
    proof, = db[0].restore_terminal_proofs()
    assert len(proof.closed_owned_rows) == (len(names) if status == "filled" else 0)


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("fault", ["missing_event", "local_event", "pre_token_event", "crash", "lost_id",
    "wrong_broker_id", "wrong_target", "missing_owner", "open_owner", "partial_fill_without_owner"])
@pytest.mark.asyncio
async def test_terminal_unknown_rolls_back_mixed_closures_and_token_state(db, monkeypatch, account, fault):
    req, guard = await covered_request(db, monkeypatch)
    terminal_buy(db, req, guard, account,
        status="filled" if fault in {"missing_owner", "open_owner"} else "cancelled", fault=fault, bound=True)
    assert not assess_covered(db, req).clear
    assert not closures(db) and db[0].restore() == {"DKI": req}
    assert token_states(db) == ["submitting" if fault == "crash" else "reported_ambiguous"]


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.asyncio
async def test_terminal_filled_token_without_order_marker_still_requires_owner(db, monkeypatch, account):
    req, guard = await covered_request(db, monkeypatch)
    terminal_buy(db, req, guard, account, status="filled", fault="missing_owner", order_generation=False)
    assert not assess_covered(db, req).clear
    assert not closures(db) and token_states(db) == ["reported_ambiguous"]


@pytest.mark.parametrize("failure", ["memory_cas", "commit", "stale_book", "assessment_bound", "new_generation"])
@pytest.mark.asyncio
async def test_positive_terminal_witness_cannot_escape_failed_f_transaction(db, monkeypatch, failure):
    req, guard = await covered_request(db, monkeypatch)
    terminal_buy(db, req, guard, PRIMARY, bound=True)
    if failure == "new_generation":
        newer = replace(req, opportunity_id=req.opportunity_id + 1)
        db[0].record(newer, True)
        assert not assess_covered(db, req).clear
    elif failure == "memory_cas":
        observations = iter((True, False))
        assert not assess_covered(db, req, publication_current=lambda _req: next(observations)).clear
    elif failure == "commit":
        def fail_commit(_session):
            raise RuntimeError("controlled mixed CLEAR commit failure")
        with monkeypatch.context() as fault:
            fault.setattr(Session, "commit", fail_commit)
            with pytest.raises(RuntimeError, match="controlled mixed CLEAR commit failure"):
                assess_covered(db, req)
    elif failure == "assessment_bound":
        assert not assess_covered(db, req, minimum_book_started_at_ms=ms(NOW) + 1).clear
    else:
        from project_mai_tai.cancel_terminal_proof import CompleteWorkingBook
        book = CompleteWorkingBook(WEBULL, CONFIGURED_IDS[WEBULL], ms(NOW) - 15001,
            ms(NOW) - 15001, True, "all_working", (), "broker")
        assert not assess_covered(db, req, books={WEBULL: book}).clear
    assert not closures(db) and token_states(db) == ["reported_ambiguous"]
    assert db[0].restore() == {"DKI": newer if failure == "new_generation" else req}
