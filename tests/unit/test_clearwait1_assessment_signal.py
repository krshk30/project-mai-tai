"""Explicit F assessment signals, no age retries, fabricated cancels or freshened books."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import TradeIntent
from project_mai_tai.oms.unbound_cancel_book import JOURNAL_KEY
from project_mai_tai.strategy_core.schwab_1m_v2 import ATRSellObservation
from tests.unit.test_clearwait1_never_sent_consumer import closures, covered_request, produce_book
from tests.unit.test_clearwait1_runtime_caller import emitters, poll, runtime
from tests.unit.test_clearwait1_session_rollover import PRIMARY, WEBULL, ms
from tests.unit.test_clearwait1_session_rollover import db as rollover_db
from tests.unit.test_clearwait1_unbound import NOW
from tests.integration.test_cancel_terminal_runtime import sdk as controlled_sdk

db, sdk = rollover_db, controlled_sdk
pytestmark = pytest.mark.usefixtures("sdk")


@pytest.mark.asyncio
async def test_request_signal_after_exact_receipts_then_actual_journal_clear(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    redis = emitters(bot, db)
    await poll(bot)
    payload, = redis.assessments
    assert payload["event_type"] == "v2_cancel_terminal_assessment" and payload["schema_version"] == 1
    assert payload["source_service"] == "schwab-1m-v2" and payload["request"] == req.payload(active=True)
    assert payload["assessment_at_ms"] == ms(NOW) and payload["trigger"] == "request_raised"
    UUID(payload["assessment_id"])
    with db[1]() as session:
        rows = list(session.scalars(select(TradeIntent)))
        assert {r["intent_id"] for r in payload["receipts"]} == {str(r.id) for r in rows}
        assert {r["account_name"] for r in payload["receipts"]} == {PRIMARY, WEBULL}
        assert all(r["status"] == "rejected" and r["updated_at"] == NOW.isoformat() for r in payload["receipts"])
    assert not calls and not redis.events and not closures(db)
    await produce_book(bot, db, req)
    await poll(bot)
    assert not strat._removed_wait_requests and len(calls) == 1 and len(closures(db)) == 2
    assert len(redis.assessments) == 1 and not redis.events


@pytest.mark.asyncio
async def test_pending_signal_is_deduplicated_without_age_retry(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    redis = emitters(bot, db)
    for elapsed in (0, 1000, 15001, 30000):
        strat._now_ms = lambda: ms(NOW) + elapsed
        await poll(bot)
    assert len(redis.assessments) == 1 and calls == [] and closures(db) == []
    assert db[0].restore() == {"DKI": req} and not redis.events


@pytest.mark.asyncio
async def test_boot_requests_resampling_without_recancelling_or_refreshing_old_book(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    redis = emitters(bot, db)
    await produce_book(bot, db, req)
    strat._now_ms = lambda: ms(NOW) + 16000
    await bot._configure_removed_wait_store()
    assert db[0].restore() == {"DKI": req} and not closures(db)
    assert not strat._pending_intents and not strat._pending_webull_direct_intents
    payload, = redis.assessments
    assert payload["trigger"] == "boot" and payload["assessment_at_ms"] == ms(NOW) + 16000
    for _ in range(3):
        await poll(bot)
    with db[1]() as session:
        receipt = session.scalar(select(TradeIntent).where(TradeIntent.broker_account_id == db[2][WEBULL]))
        assert receipt.payload[JOURNAL_KEY]["book"]["started_at_ms"] == ms(NOW)
    assert len(calls) == 1 and len(redis.assessments) == 1 and not redis.events


@pytest.mark.asyncio
async def test_new_receipt_revision_wakes_signal_but_journal_payload_does_not(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch, purpose="retry_exhausted")
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    redis = emitters(bot, db)
    await poll(bot)
    await produce_book(bot, db, req)
    await poll(bot)
    assert len(redis.assessments) == 1
    # Controlled new feedback revision, preserving the old book's actual observation.
    strat._now_ms = lambda: ms(NOW) + 1
    with db[1]() as session:
        receipt = session.scalar(select(TradeIntent).where(TradeIntent.broker_account_id == db[2][WEBULL]))
        receipt.updated_at = NOW + timedelta(microseconds=1)
        session.commit()
    await poll(bot)
    assert len(redis.assessments) == 2 and len(calls) == 1
    assert any(r["updated_at"] == (NOW + timedelta(microseconds=1)).isoformat()
               for r in redis.assessments[-1]["receipts"])
    assert strat._removed_wait_terminal_proofs[0].observed_at_ms == ms(NOW)


@pytest.mark.asyncio
async def test_changed_active_request_cannot_emit_old_assessment(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    bot, _, _, calls = runtime(db, monkeypatch, req=req)
    redis = emitters(bot, db)
    newer = replace(req, opportunity_id=req.opportunity_id + 1)
    db[0].record(newer, True)
    await poll(bot)
    assert not redis.assessments and calls == [] and not closures(db)
    assert db[0].restore() == {"DKI": newer}


@pytest.mark.asyncio
async def test_signal_delivery_failure_keeps_bound_until_explicit_new_wake(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    redis = emitters(bot, db)
    await produce_book(bot, db, req)
    strat._now_ms = lambda: ms(NOW) + 1
    deliveries = []
    async def fail_signal(payload):
        deliveries.append(payload)
        raise RuntimeError("controlled signal delivery failure")
    original = bot.intent_emitter.emit_cancel_terminal_assessment
    monkeypatch.setattr(bot.intent_emitter, "emit_cancel_terminal_assessment", fail_signal)
    await poll(bot)
    await poll(bot)
    assert len(deliveries) == 1 and not closures(db) and db[0].restore() == {"DKI": req}
    monkeypatch.setattr(bot.intent_emitter, "emit_cancel_terminal_assessment", original)
    strat.__dict__.setdefault("_removed_wait_evidence_wakes", set()).add(req)
    await poll(bot)
    assert len(redis.assessments) == 1 and redis.assessments[0]["trigger"] == "after_feedback"
    assert len(calls) == 1 and not closures(db)


@pytest.mark.asyncio
async def test_fresh_sell_publication_triggers_exact_request_resampling(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch, purpose="retry_exhausted")
    bot, strat, _, calls = runtime(db, monkeypatch, req=req)
    redis = emitters(bot, db)
    await produce_book(bot, db, req)
    await poll(bot)
    strat._now_ms = lambda: ms(NOW) + 1
    bar_ms = int(datetime.now(UTC).timestamp() * 1000)
    pending = [ATRSellObservation("DKI", bar_ms, 1, 2, f"atr-sell:DKI:{bar_ms}")]
    monkeypatch.setattr(strat, "pending_atr_sell_observations", lambda: tuple(pending))
    monkeypatch.setattr(strat, "acknowledge_atr_sell_observation", lambda _decision: pending.clear())
    await bot._drain_atr_sell_observations()
    assert len(redis.observations) == 1 and bot._removed_wait_assessment_triggers[req][0] == "fresh_sell"
    await poll(bot)
    assert len(redis.assessments) == 2 and redis.assessments[-1]["trigger"] == "fresh_sell"
    assert redis.assessments[-1]["assessment_at_ms"] == ms(NOW) + 1 and len(calls) == 1


@pytest.mark.asyncio
async def test_late_assessment_trigger_change_rolls_back_both_admissions(db, monkeypatch):
    req, _ = await covered_request(db, monkeypatch)
    bot, _, _, calls = runtime(db, monkeypatch, req=req)
    emitters(bot, db)
    await produce_book(bot, db, req)
    original = db[0].retire_unbound
    def changed_trigger(*args, **kwargs):
        callback = kwargs["publication_current"]
        count = 0
        def current(candidate):
            nonlocal count
            count += 1
            if count == 2:
                bot._removed_wait_assessment_triggers[req] = ("fresh_sell", "new-explicit-decision")
            return callback(candidate)
        return original(*args, **{**kwargs, "publication_current": current})
    monkeypatch.setattr(db[0], "retire_unbound", changed_trigger)
    await poll(bot)
    assert not closures(db) and db[0].restore() == {"DKI": req} and len(calls) == 1
