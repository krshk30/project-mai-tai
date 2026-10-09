"""F emitter to actual OMS assessment worker, physical read and journal consumer."""

import asyncio
from datetime import UTC, datetime
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.unbound_cancel_book import JOURNAL_KEY
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import ATRSellObservation
from tests.integration.test_cancel_terminal_runtime import sdk as controlled_sdk
from tests.unit.test_clearwait1_never_sent_consumer import closures, covered_request
from tests.unit.test_clearwait1_runtime_caller import emitters, poll, runtime
from tests.unit.test_clearwait1_session_rollover import WEBULL, ms
from tests.unit.test_clearwait1_session_rollover import db as rollover_db
from tests.unit.test_clearwait1_unbound import NOW

db, sdk = rollover_db, controlled_sdk
pytestmark = pytest.mark.usefixtures("sdk")


def receipt_state(database):
    with database[1]() as session:
        rows = list(session.scalars(select(TradeIntent)))
        revisions = {r.id: (r.created_at, r.updated_at, r.status) for r in rows}
        book = next(r.payload[JOURNAL_KEY]["book"] for r in rows
                    if r.broker_account_id == database[2][WEBULL])
        return revisions, book


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["request_raised", "boot", "fresh_sell"])
async def test_actual_assessment_transport_physically_refetches_inside_sharing_window(db, monkeypatch, trigger):
    purpose = "retry_exhausted" if trigger == "fresh_sell" else "scanner_removal"
    req, guard = await covered_request(db, monkeypatch, purpose=purpose, receipts=False)
    bot, strat, _, calls = runtime(db, monkeypatch, req=req, adapter=guard.cancel_terminal_delegate)
    redis = emitters(bot, db)
    oms = OmsRiskService(settings=Settings(_env_file=None, oms_adapter="simulated", broker_default_provider="webull",
        strategy_schwab_1m_v2_account_name=req.account_names[0],
        strategy_schwab_1m_v2_broker_provider="schwab", strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name=WEBULL, orb_broker_account_name="unused"), session_factory=db[1],
        broker_adapter=guard, redis_client=SimpleNamespace())
    published = []

    async def publish(event):
        published.append(event)

    async def noop(*args):
        pass

    monkeypatch.setattr(oms, "_publish_order_event", publish)
    monkeypatch.setattr(oms, "_evaluate_risk", lambda event: (True, "controlled"))
    monkeypatch.setattr(oms, "_reconcile_after_intent", noop)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(strat._now_ms() / 1000, tz or UTC)

    monkeypatch.setattr("project_mai_tai.oms.service.datetime", Clock)
    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.datetime", Clock)
    monkeypatch.setattr("project_mai_tai.db.models.datetime", Clock)
    original_xadd = redis.xadd

    async def routed_xadd(stream, fields, **kwargs):
        result = await original_xadd(stream, fields, **kwargs)
        if json.loads(fields["data"]).get("event_type") == "v2_cancel_terminal_assessment":
            await asyncio.wait_for(oms._handle_stream_message(fields), .05)
        return result

    redis.xadd = routed_xadd
    for account in req.account_names:
        source = TradeIntentEvent(event_id=uuid4(), source_service="schwab-1m-v2",
            payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=account,
                symbol=req.symbol, side="buy", intent_type="cancel", quantity=1,
                reason="controlled cancellation barrier", metadata={
                    "clearwait_purpose": req.purpose, "clearwait_removal_token": req.token,
                    "clearwait_opportunity_id": str(req.opportunity_id),
                    "clearwait_buy_only": "true", "fanout_segment_id": str(req.opportunity_id),
                    "reason": "retry_budget_exhausted" if purpose == "retry_exhausted" else "watchlist-removed"}))
        await oms.process_trade_intent(source)
    await asyncio.gather(*oms.__dict__.get("_cancel_feedback_tasks", set()))
    await asyncio.gather(*oms.__dict__.get("_cancel_terminal_tasks", {}).values())
    assert len(published) == 2
    original_revisions, original_book = receipt_state(db)
    assert len(calls) == 1 and original_book["started_at_ms"] == ms(NOW)
    if trigger == "fresh_sell":
        await poll(bot)
        await asyncio.gather(*oms.__dict__.get("_cancel_terminal_assessment_tasks", set()))
        await asyncio.gather(*oms.__dict__.get("_cancel_terminal_tasks", {}).values())
        assert len(calls) == 1 and strat._removed_wait_requests == {"DKI": req}

    # A fresh explicit wake must bypass a still-shareable old physical book.
    strat._now_ms = lambda: ms(NOW) + 1
    leaf = bot._removed_wait_adapter._adapter_for_account(WEBULL)
    client = leaf._get_client()
    response = client.get_response
    entered, release = threading.Event(), threading.Event()

    def physical_refetch(request):
        entered.set()
        assert release.wait(5)
        return response(request)

    client.get_response = physical_refetch
    leaf._get_client = lambda: client
    try:
        if trigger == "boot":
            await bot._configure_removed_wait_store()
        elif trigger == "fresh_sell":
            bar_ms = strat._now_ms()
            pending = [ATRSellObservation("DKI", bar_ms, 1, 2, f"atr-sell:DKI:{bar_ms}")]
            monkeypatch.setattr(strat, "pending_atr_sell_observations", lambda: tuple(pending))
            monkeypatch.setattr(strat, "acknowledge_atr_sell_observation", lambda _id: pending.clear())
            await bot._drain_atr_sell_observations()
            assert len(redis.observations) == 1 and not pending
        await poll(bot)
        assert await asyncio.to_thread(entered.wait, 2), "assessment failed to acquire a new physical book"
        revisions, book = receipt_state(db)
        assert revisions == original_revisions and book == original_book
        assert db[0].restore() == {"DKI": req} and strat._removed_wait_requests == {"DKI": req}
        assert len(calls) == 1 and not redis.events
        assert not strat._pending_intents and not strat._pending_webull_direct_intents
        signal = redis.assessments[-1]
        assert signal["trigger"] == trigger and signal["assessment_at_ms"] == ms(NOW) + 1
        assert signal["request"] == req.payload(active=True)
    finally:
        release.set()
        await asyncio.gather(*oms.__dict__.get("_cancel_terminal_assessment_tasks", set()))
        await asyncio.gather(*oms.__dict__.get("_cancel_terminal_tasks", {}).values())

    revisions, new_book = receipt_state(db)
    assert revisions == original_revisions and new_book["started_at_ms"] == ms(NOW) + 1
    assert len(calls) == 2
    await poll(bot)
    assert strat._removed_wait_terminal_proofs[0].observed_at_ms == ms(NOW) + 1
    assert len(closures(db)) == 2
    assert db[0].restore() == ({"DKI": req} if trigger == "fresh_sell" else {})
    signal_count = len(redis.assessments)
    await poll(bot)
    assert len(redis.assessments) == signal_count and len(calls) == 2 and not redis.events
    await oms._drain_cancel_terminal_evidence()
