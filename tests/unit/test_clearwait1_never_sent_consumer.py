"""Real journal/F transaction controls; synthetic coverage, never historical proof."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, OmsManagedPosition, TradeIntent
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms import buy_submission_journal as journal
from project_mai_tai.v2_removed_wait import RemovedWait
from tests.unit.test_clearwait1_runtime_caller import poll, runtime
from tests.unit.test_clearwait1_runtime_caller import emitters
from tests.unit.test_clearwait1_session_rollover import ACCOUNTS, PRIMARY, WEBULL, ms, seed
from tests.unit.test_clearwait1_session_rollover import db as rollover_db
from tests.unit.test_clearwait1_unbound import CONFIGURED_IDS, NOW, controlled_routing
from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft

db = rollover_db


async def covered_request(database, monkeypatch, *, purpose="scanner_removal"):
    """Actual startup epoch API, original durable bind, controlled terminal cancel receipt."""
    epoch = ms(NOW) - 3000
    monkeypatch.setattr(journal, "now_ms", lambda: epoch)
    guard = journal.DurableBuyAdapter(controlled_routing(), database[1])
    await guard.start()
    req = RemovedWait("DKI", ms(NOW) + 1000000, "postcoverage-exact-request",
                      ms(NOW) - 1000, (PRIMARY, WEBULL), purpose)
    FanoutSegmentIdentityStore(database[1]).record(req.symbol, req.opportunity_id, True,
        "flip_owned_opportunity_v2_bind", now=datetime.fromtimestamp((epoch + 1000) / 1000, UTC))
    seed(database, req, receipts=False)
    with database[1]() as session:
        session.execute(update(BrokerAccount).values(external_account_id=None))
        for name in req.account_names:
            session.add(TradeIntent(strategy_id=database[3], broker_account_id=database[2][name],
                symbol=req.symbol, side="buy", intent_type="cancel", quantity=0,
                reason="controlled cancel feedback", status="rejected", created_at=NOW, updated_at=NOW,
                payload={"source_service": "schwab-1m-v2", "metadata": {
                    "clearwait_removal_token": req.token,
                    "clearwait_opportunity_id": str(req.opportunity_id),
                    "clearwait_buy_only": "true", "fanout_segment_id": str(req.opportunity_id),
                    "reason": "retry_budget_exhausted" if purpose == "retry_exhausted" else "watchlist-removed",
                    "buy_submission_process_id": str(guard.process_id)}}))
        session.commit()
    return req, guard


def closures(database):
    with database[1]() as session:
        return list(session.scalars(select(journal.BuyAdmissionClosure)))


def assess_covered(database, req, **kwargs):
    return database[0].retire_unbound((req,), ACCOUNTS, books={}, publication_closed={req: True},
        expected_bindings={PRIMARY: ("schwab", CONFIGURED_IDS[PRIMARY]), WEBULL: ("webull", CONFIGURED_IDS[WEBULL])},
        now=NOW, **kwargs)[0]


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.asyncio
async def test_positive_coverage_operator_position_and_sell_do_not_block(db, monkeypatch, account):
    from project_mai_tai.db.models import AccountPosition
    req, _ = await covered_request(db, monkeypatch)
    with db[1]() as session:
        session.add(AccountPosition(broker_account_id=db[2][account], symbol="DKI", quantity=1000))
        session.add(BrokerOrder(strategy_id=db[3], broker_account_id=db[2][account], symbol="DKI",
            side="sell", order_type="limit", time_in_force="day", quantity=1000,
            status="accepted", client_order_id="operator-sell"))
        session.commit()
    assert assess_covered(db, req).clear and len(closures(db)) == 2


@pytest.mark.asyncio
async def test_actual_caller_postcoverage_dki_closes_both_admissions_without_http(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    await poll(bot)
    assert not strat._removed_wait_requests and not db[0].restore()
    assert calls == [] and len(closures(db)) == 2
    await poll(bot)
    assert calls == [] and not strat._pending_intents


@pytest.mark.asyncio
async def test_postcoverage_generic_cancel_publication_gap_then_exact_receipt_clear(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    redis = emitters(bot, db)
    persist = redis.persist
    redis.persist = lambda _event: None
    draft = TradeIntentDraft("DKI", "buy", "cancel", Decimal(100), "untagged protective cancel", {})
    strat._pending_intents.append(draft)
    await asyncio.wait_for(bot._drain_direct_strategy_intents(), 2)
    missing = redis.events[-1]
    assert not missing.payload.metadata.get("clearwait_removal_token")
    await poll(bot)
    assert db[0].restore() == {"DKI": req} and closures(db) == []
    assert not await bot._emit_removal_tracked(bot.intent_emitter, replace(draft, intent_type="open"))
    redis.persist = persist
    persist(missing)
    await poll(bot)
    assert not db[0].restore() and not strat._removed_wait_requests
    assert len(closures(db)) == 2 and calls == []


@pytest.mark.asyncio
async def test_same_session_retry_exhausted_witness_keeps_owner_request(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch, purpose="retry_exhausted")
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    await poll(bot)
    assert strat._removed_wait_requests == {"DKI": req} and db[0].restore() == {"DKI": req}
    assert len(closures(db)) == 2 and calls == []
    await poll(bot)
    assert calls == []


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("case", ["working", "pending", "cancel", "managed", "closed_owner", "token", "reported", "terminal_token", "old_unresolved", "stamp", "precoverage"])
@pytest.mark.asyncio
async def test_real_journal_or_existing_db_unknown_rolls_back_all_closures(db, monkeypatch, account, case):
    req, guard = await covered_request(db, monkeypatch)
    with db[1]() as session:
        if case == "working":
            session.add(BrokerOrder(strategy_id=db[3], broker_account_id=db[2][account], symbol="DKI",
                side="buy", order_type="limit", time_in_force="day", quantity=1,
                status="accepted", client_order_id=str(uuid4())))
        elif case in {"pending", "cancel"}:
            session.add(TradeIntent(strategy_id=db[3], broker_account_id=db[2][account], symbol="DKI",
                side="" if case == "cancel" else "buy", intent_type="cancel" if case == "cancel" else "open",
                quantity=1, reason="controlled unknown", status="submitting"))
        elif case == "managed":
            session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account,
                symbol="DKI", entry_price=1, original_quantity=1, current_quantity=1, status="open"))
        elif case == "closed_owner":
            entry = BrokerOrder(strategy_id=db[3], broker_account_id=db[2][account], symbol="DKI",
                side="buy", order_type="limit", time_in_force="day", quantity=1,
                status="filled", client_order_id="controlled-filled-entry",
                payload={"fanout_segment_id": str(req.opportunity_id)})
            session.add(entry)
            session.flush()
            session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=account,
                symbol="DKI", entry_order_id=entry.id, entry_price=1, original_quantity=1,
                current_quantity=0, status="closed"))
        elif case in {"token", "reported", "terminal_token", "old_unresolved"}:
            session.add(journal.BuySubmissionToken(process_id=guard.process_id,
                account_id=CONFIGURED_IDS[account], account_name=account, symbol="DKI", client_order_id="exact-buy",
                generation=str(req.opportunity_id), opportunity_started_at_ms=ms(NOW) - 2000,
                created_at_ms=ms(NOW) - (10000 if case == "old_unresolved" else 1500),
                state="reported_ambiguous" if case == "reported" else "broker_terminal" if case == "terminal_token" else "submitting",
                wire_kind="submit", answers=[]))
        elif case == "precoverage":
            session.get(journal.BuyCoverageEpoch, (guard.process_id, CONFIGURED_IDS[account])).started_at_ms = ms(NOW)
        else:
            row = session.scalar(select(TradeIntent).where(TradeIntent.broker_account_id == db[2][account]))
            row.payload = {**row.payload, "metadata": {**row.payload["metadata"], "buy_submission_process_id": str(uuid4())}}
        session.commit()
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    await poll(bot)
    assert strat._removed_wait_requests == {"DKI": req} and db[0].restore() == {"DKI": req}
    assert closures(db) == [] and calls == []


@pytest.mark.asyncio
async def test_memory_changes_during_proof_roll_back_admission_and_request_together(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    count = 0
    def current(candidate):
        nonlocal count
        count += 1
        return count == 1
    proof, = db[0].retire_unbound((req,), ACCOUNTS, books={}, publication_closed={req: True},
        expected_bindings={PRIMARY: ("schwab", CONFIGURED_IDS[PRIMARY]), WEBULL: ("webull", CONFIGURED_IDS[WEBULL])},
        publication_current=current, now=NOW)
    assert not proof.clear and closures(db) == [] and db[0].restore() == {"DKI": req}


@pytest.mark.asyncio
async def test_legacy_zero_id_is_not_rejuvenated_by_new_coverage(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    legacy = replace(req, opportunity_id=0)
    db[0].record(legacy, True)
    bot, strat, _, calls = runtime(db, monkeypatch, req=legacy)
    await poll(bot)
    assert strat._removed_wait_requests == {"DKI": legacy}
    assert closures(db) == [] and calls == []
