"""Real journal/F transaction controls; synthetic coverage, never historical proof."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4
from decimal import Decimal
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, OmsManagedPosition, TradeIntent
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms import buy_submission_journal as journal
from project_mai_tai.oms.cancel_terminal import acquire_cancel_terminal_evidence
from project_mai_tai.v2_removed_wait import RemovedWait
from project_mai_tai.cancel_terminal_proof import CompleteWorkingBook
from tests.unit.test_clearwait1_runtime_caller import poll, runtime
from tests.unit.test_clearwait1_runtime_caller import barriers, emitters
from tests.unit.test_clearwait1_runtime_caller import sdk as controlled_sdk
from tests.unit.test_clearwait1_session_rollover import ACCOUNTS, PRIMARY, WEBULL, ms, seed
from tests.unit.test_clearwait1_session_rollover import db as rollover_db
from tests.unit.test_clearwait1_unbound import CONFIGURED_IDS, NOW, controlled_routing
from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft

db = rollover_db
sdk = controlled_sdk
pytestmark = pytest.mark.usefixtures("sdk")


@pytest.mark.asyncio
async def test_concurrent_offloop_reads_have_independent_connections(db):
    barrier = threading.Barrier(2)
    def read():
        with db[1]() as session:
            connection = session.connection().connection.dbapi_connection
            rows = list(session.scalars(select(BrokerAccount.id)))
            barrier.wait(2)
            return id(connection), len(rows)
    first, second = await asyncio.wait_for(asyncio.gather(
        asyncio.to_thread(read), asyncio.to_thread(read)), 3)
    assert first[0] != second[0] and first[1] == second[1] == 2


async def covered_request(database, monkeypatch, *, purpose="scanner_removal", guard=None, receipts=True):
    """Actual startup epoch API, original durable bind, controlled terminal cancel receipt."""
    epoch = ms(NOW) - 3000
    monkeypatch.setattr(journal, "now_ms", lambda: epoch)
    guard = guard or journal.DurableBuyAdapter(controlled_routing(), database[1])
    await guard.start()
    req = RemovedWait("DKI", ms(NOW) + 1000000, "postcoverage-exact-request",
                      ms(NOW) - 1000, (PRIMARY, WEBULL), purpose)
    FanoutSegmentIdentityStore(database[1]).record(req.symbol, req.opportunity_id, True,
        "flip_owned_opportunity_v2_bind", now=datetime.fromtimestamp((epoch + 1000) / 1000, UTC))
    seed(database, req, receipts=False)
    with database[1]() as session:
        session.execute(update(BrokerAccount).values(external_account_id=None))
        for name in req.account_names if receipts else ():
            session.add(TradeIntent(strategy_id=database[3], broker_account_id=database[2][name],
                symbol=req.symbol, side="buy", intent_type="cancel", quantity=0,
                reason="scanner removal cancellation barrier", status="rejected", created_at=NOW, updated_at=NOW,
                payload={"event_id": str(uuid4()), "reason": "scanner removal cancellation barrier",
                    "source_service": "schwab-1m-v2", "metadata": {
                    "clearwait_purpose": req.purpose,
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


async def produce_book(bot, database, req):
    """Actual post-feedback producer on controlled upstream receipts, not live/history evidence."""
    with database[1]() as session:
        intent_ids = list(session.scalars(select(TradeIntent.id).where(
            TradeIntent.broker_account_id == database[2][WEBULL],
            TradeIntent.payload["metadata"]["clearwait_removal_token"].as_string() == req.token)))
    await acquire_cancel_terminal_evidence(database[1], bot._removed_wait_adapter, intent_ids)


def assess_covered(database, req, **kwargs):
    books = kwargs.pop("books", {WEBULL: CompleteWorkingBook(WEBULL, CONFIGURED_IDS[WEBULL],
        ms(NOW), ms(NOW), True, "all_working", (), "broker")})
    return database[0].retire_unbound((req,), ACCOUNTS, books=books, publication_closed={req: True},
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
async def test_actual_caller_postcoverage_dki_requires_fresh_webull_book(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    await produce_book(bot, db, req)
    await poll(bot)
    assert not strat._removed_wait_requests and not db[0].restore()
    assert len(calls) == 1 and calls[0][0] == WEBULL and len(closures(db)) == 2
    await poll(bot)
    assert len(calls) == 1 and not strat._pending_intents


@pytest.mark.asyncio
async def test_postcoverage_generic_cancel_publication_gap_then_exact_receipt_clear(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    await produce_book(bot, db, req)
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
    assert len(closures(db)) == 2 and len(calls) == 1 and calls[0][0] == WEBULL


@pytest.mark.asyncio
async def test_same_session_retry_exhausted_witness_keeps_owner_request(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch, purpose="retry_exhausted")
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    await produce_book(bot, db, req)
    await poll(bot)
    assert strat._removed_wait_requests == {"DKI": req} and db[0].restore() == {"DKI": req}
    assert len(closures(db)) == 2 and len(calls) == 1
    await poll(bot)
    assert len(calls) == 1


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
            row.updated_at = NOW
            flag_modified(row, "updated_at")
        session.commit()
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    # Complete the independent book witness so a missing book cannot mask a DB guard regression.
    await produce_book(bot, db, req)
    await poll(bot)
    assert strat._removed_wait_requests == {"DKI": req} and db[0].restore() == {"DKI": req}
    assert closures(db) == [] and len(calls) == 1


@pytest.mark.asyncio
async def test_memory_changes_during_proof_roll_back_admission_and_request_together(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    count = 0
    def current(candidate):
        nonlocal count
        count += 1
        return count == 1
    proof = assess_covered(db, req, publication_current=current)
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


@pytest.mark.parametrize("side,held", [("buy", True), ("sell", False), ("unknown", True)])
@pytest.mark.asyncio
async def test_actual_webull_foreign_order_checked_even_with_both_never_sent_witnesses(db, monkeypatch, side, held):
    req, _ = await covered_request(db, monkeypatch)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req, side=side, venue=WEBULL)
    await produce_book(bot, db, req)
    await poll(bot)
    assert bool(db[0].restore()) is held and bool(strat._removed_wait_requests) is held
    assert len(calls) == 1 and len(closures(db)) == (0 if held else 2)
    await poll(bot)
    assert len(calls) == 1


@pytest.mark.parametrize("case", ["missing", "incomplete", "stale", "pre_request", "foreign_account"])
@pytest.mark.asyncio
async def test_durable_witness_does_not_replace_authoritative_fresh_book(db, monkeypatch, case):
    req, _ = await covered_request(db, monkeypatch)
    book = CompleteWorkingBook(WEBULL, CONFIGURED_IDS[WEBULL], ms(NOW), ms(NOW), True, "all_working", (), "broker")
    if case == "missing":
        book = None
    elif case == "incomplete":
        book = replace(book, complete=False)
    elif case == "stale":
        book = replace(book, started_at_ms=ms(NOW) - 15001)
    elif case == "pre_request":
        book = replace(book, started_at_ms=req.requested_at_ms - 1)
    else:
        book = replace(book, account_id="not-configured")
    assert not assess_covered(db, req, books={WEBULL: book}).clear
    assert closures(db) == [] and db[0].restore() == {"DKI": req}


@pytest.mark.asyncio
async def test_journal_arrival_wakes_pending_request_without_consumer_http(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    await poll(bot)
    assert db[0].restore() == {"DKI": req} and calls == []
    await poll(bot)
    assert calls == [] and closures(db) == []
    await produce_book(bot, db, req)
    assert len(calls) == 1
    await poll(bot)
    assert not db[0].restore() and not strat._removed_wait_requests and len(calls) == 1


@pytest.mark.asyncio
async def test_explicit_reassessment_cannot_freshen_original_book_timestamp(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch, purpose="retry_exhausted")
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    await produce_book(bot, db, req)
    await poll(bot)
    first = strat._removed_wait_terminal_proofs[0]
    strat._now_ms = lambda: ms(NOW) + 10000
    strat.__dict__.setdefault("_removed_wait_evidence_wakes", set()).add(req)
    await poll(bot)
    assert strat._removed_wait_terminal_proofs[0].observed_at_ms == first.observed_at_ms == ms(NOW)
    strat._now_ms = lambda: ms(NOW) + 16000
    strat.__dict__.setdefault("_removed_wait_evidence_wakes", set()).add(req)
    await poll(bot)
    assert strat._removed_wait_requests == {"DKI": req} and len(calls) == 1
    assert strat._removed_wait_terminal_proofs[0].observed_at_ms == ms(NOW)


@pytest.mark.parametrize("evidence", ["fresh", "missing", "stale", "precoverage"])
@pytest.mark.asyncio
async def test_postcoverage_boot_consumes_only_original_fresh_journal(db, monkeypatch, evidence):
    req, guard = await covered_request(db, monkeypatch)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    if evidence != "missing":
        await produce_book(bot, db, req)
    if evidence == "stale":
        strat._now_ms = lambda: ms(NOW) + 15001
    if evidence == "precoverage":
        with db[1]() as session:
            session.get(journal.BuyCoverageEpoch,
                (guard.process_id, CONFIGURED_IDS[PRIMARY])).started_at_ms = ms(NOW)
            session.commit()
    await bot._configure_removed_wait_store()
    held = evidence != "fresh"
    assert bool(strat._removed_wait_requests) is held and bool(db[0].restore()) is held
    assert len(closures(db)) == (0 if held else 2)
    assert len(calls) == (0 if evidence == "missing" else 1)
    if not held:
        assert not strat._pending_intents and not strat._pending_webull_direct_intents
        assert strat._removed_wait_terminal_proofs[0].observed_at_ms == ms(NOW)


@pytest.mark.asyncio
async def test_04_reset_does_not_turn_stale_book_into_current_session_coverage(db, monkeypatch):
    from datetime import timedelta
    from project_mai_tai.fanout_segment_store import current_session_anchor
    import project_mai_tai.services.schwab_1m_v2_bot as service_module
    req, _ = await covered_request(db, monkeypatch, purpose="retry_exhausted")
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    await produce_book(bot, db, req)
    await poll(bot)
    next_anchor = current_session_anchor(NOW) + timedelta(days=1)
    strat._now_ms = lambda: ms(next_anchor)
    monkeypatch.setattr(service_module, "current_session_anchor", lambda: next_anchor)
    retire = type(db[0]).retire_prior_sessions
    monkeypatch.setattr(type(db[0]), "retire_prior_sessions",
        lambda self, *args, **kwargs: retire(self, *args, **{**kwargs, "now": next_anchor}))
    await poll(bot)
    assert strat._removed_wait_requests == {"DKI": req} and db[0].restore() == {"DKI": req}
    assert len(calls) == 1 and len(closures(db)) == 2
    assert strat._removed_wait_terminal_proofs[0].observed_at_ms == ms(NOW)
    assert bot._removed_wait_roll_anchor == next_anchor


@pytest.mark.parametrize("field", ["token", "opportunity_id", "requested_at_ms", "purpose", "account_names"])
@pytest.mark.asyncio
async def test_exact_active_request_change_rolls_back_both_admissions(db, monkeypatch, field):
    req, _ = await covered_request(db, monkeypatch)
    values = {"token": "new-request-token", "opportunity_id": req.opportunity_id + 1,
        "requested_at_ms": req.requested_at_ms + 1, "purpose": "retry_exhausted",
        "account_names": (PRIMARY,)}
    newer = replace(req, **{field: values[field]})
    db[0].record(newer, True)
    assert not assess_covered(db, req).clear
    assert closures(db) == [] and db[0].restore() == {"DKI": newer}
    assert not db[0].restore_terminal_proofs()


@pytest.mark.asyncio
async def test_clear_commit_failure_rolls_back_request_and_both_admissions(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    def fail_commit(_session):
        raise RuntimeError("controlled F CLEAR commit failure")
    with monkeypatch.context() as fault:
        fault.setattr(Session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="controlled F CLEAR commit failure"):
            assess_covered(db, req)
    assert closures(db) == [] and db[0].restore() == {"DKI": req}
    assert not db[0].restore_terminal_proofs()


@pytest.fixture
def receipt_clock():
    # Mapper hooks survive compiled-default caching after earlier test inserts.
    def pin_receipt(_mapper, _connection, intent):
        intent.created_at = intent.updated_at = NOW
        flag_modified(intent, "updated_at")
    for name in ("before_insert", "before_update"):
        event.listen(TradeIntent, name, pin_receipt)
    yield
    for name in ("before_insert", "before_update"):
        event.remove(TradeIntent, name, pin_receipt)


@pytest.mark.asyncio
async def test_real_emitter_oms_after_feedback_journal_and_consumer_clear(db, monkeypatch, receipt_clock):
    from project_mai_tai.oms.service import OmsRiskService
    from project_mai_tai import settings
    import project_mai_tai.oms.service as oms_module
    # Controlled fixed clock; no recorded row or production clock is changed.
    monkeypatch.setattr(oms_module, "utcnow", lambda: NOW)
    req = RemovedWait("DKI", ms(NOW) + 1000000, "postcoverage-exact-request", ms(NOW) - 1000, (PRIMARY, WEBULL))
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    monkeypatch.setattr(journal, "now_ms", lambda: ms(NOW) - 3000)
    oms = OmsRiskService(settings.Settings(_env_file=None, oms_adapter="simulated",
        broker_default_provider="schwab", strategy_schwab_1m_v2_broker_provider="schwab",
        strategy_schwab_1m_v2_account_name=PRIMARY, strategy_schwab_1m_v2_webull_account_name=WEBULL,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True),
        session_factory=db[1], redis_client=SimpleNamespace(), broker_adapter=bot._removed_wait_adapter)
    await oms.broker_adapter.start()
    await covered_request(db, monkeypatch, guard=oms.broker_adapter, receipts=False)
    published = []
    async def publish(envelope):
        published.append(envelope)
    monkeypatch.setattr(oms, "_publish_order_event", publish)
    monkeypatch.setattr(oms, "_evaluate_risk", lambda _event: (True, "controlled cancellation only"))
    monkeypatch.setattr(oms, "_reconcile_after_intent", AsyncMock())
    leaf = bot._removed_wait_adapter._adapter_for_account(WEBULL)
    client = leaf._get_client()
    response = client.get_response
    def after_feedback(request):
        assert any(e.payload.broker_account_name == WEBULL for e in published)
        return response(request)
    client.get_response = after_feedback
    leaf._get_client = lambda: client
    redis = emitters(bot, db)
    async def actual_xadd(_stream, fields, **_kwargs):
        import json
        from project_mai_tai.events import TradeIntentEvent
        if json.loads(fields["data"]).get("event_type") == "v2_cancel_terminal_assessment":
            redis.assessments.append(json.loads(fields["data"]))
            return "controlled-assessment-id"
        envelope = TradeIntentEvent.model_validate_json(fields["data"])
        redis.events.append(envelope)
        assert envelope.payload.metadata["clearwait_purpose"] == req.purpose
        assert await oms.process_trade_intent(envelope)
        return "controlled-redis-id"
    redis.xadd = actual_xadd
    await barriers(bot, strat, req)
    await oms._drain_cancel_terminal_evidence()
    with db[1]() as session:
        rows = list(session.scalars(select(TradeIntent)))
        assert len(rows) == 2 and all(r.payload["metadata"]["buy_submission_process_id"] == str(oms.broker_adapter.process_id) for r in rows)
        from project_mai_tai.oms.unbound_cancel_book import JOURNAL_KEY
        actual_book = next(r for r in rows if r.broker_account_id == db[2][WEBULL])
        assert JOURNAL_KEY in actual_book.payload, (calls, actual_book.payload)
    await poll(bot)
    assert not db[0].restore() and not strat._removed_wait_requests
    assert len(calls) == 1 and len(closures(db)) == 2
