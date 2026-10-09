"""Synthetic postcoverage caller -> actual OMS BUY; not historical chart clearance."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrder, TradeIntent
from project_mai_tai.events import TradeIntentEvent
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms import buy_submission_journal as journal
from project_mai_tai.oms.cancel_feedback import feedback_published
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2IntentEmitter, TradeIntentDraft
from tests.integration.test_cancel_terminal_runtime import sdk as controlled_sdk
from tests.integration.test_clearwait1_postgres import pg_db as controlled_db
from tests.integration.test_clearwait1_postgres import postgres_factory as controlled_factory
from tests.unit import test_clearwait1_never_sent_consumer as covered
from tests.unit import test_clearwait1_runtime_caller as caller
from tests.unit.test_clearwait1_session_rollover import PRIMARY, WEBULL, ms

db, sdk, postgres_factory = controlled_db, controlled_sdk, controlled_factory


class Transport:
    def __init__(self, oms):
        self.oms, self.events, self.assessments = oms, [], []

    async def xadd(self, _stream, fields, **_kwargs):
        payload = json.loads(fields["data"])
        if payload.get("event_type") == "v2_cancel_terminal_assessment":
            self.assessments.append(payload)
            await self.oms._handle_stream_message(fields)
        else:
            event = TradeIntentEvent.model_validate_json(fields["data"])
            self.events.append(event)
            await self.oms.process_trade_intent(event)
        return "controlled-accepted"


async def settle(oms):
    await asyncio.gather(*oms.__dict__.get("_cancel_feedback_tasks", set()))
    await asyncio.gather(*oms.__dict__.get("_cancel_terminal_assessment_tasks", set()))
    await asyncio.gather(*oms.__dict__.get("_cancel_terminal_tasks", {}).values())


@pytest.mark.asyncio
@pytest.mark.usefixtures("sdk")
@pytest.mark.parametrize("pm", [True, False], ids=["pm", "rth"])
async def test_synthetic_dki_caller_clear_then_actual_both_account_buy(db, monkeypatch, pm):
    at = datetime(2026, 10, 8, 11, 45, tzinfo=UTC) if pm else covered.NOW
    monkeypatch.setattr(covered, "NOW", at)
    monkeypatch.setattr(caller, "NOW", at)
    req, guard = await covered.covered_request(db, monkeypatch, receipts=False)
    bot, strategy, _, reads = caller.runtime(db, monkeypatch, req=req,
        adapter=guard.cancel_terminal_delegate)
    clock = [ms(at)]
    strategy._now_ms = lambda: clock[0]
    monkeypatch.setattr(journal, "now_ms", lambda: clock[0])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0] / 1000, tz or UTC)

    monkeypatch.setattr("project_mai_tai.oms.service.datetime", Clock)
    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.datetime", Clock)
    monkeypatch.setattr("project_mai_tai.db.models.datetime", Clock)
    feedback = SimpleNamespace(xadd=AsyncMock(return_value="controlled-normal-feedback"))
    settings = Settings(_env_file=None, oms_adapter="simulated", broker_default_provider="schwab",
        strategy_schwab_1m_v2_account_name=PRIMARY, strategy_schwab_1m_v2_broker_provider="schwab",
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_account_name=WEBULL, orb_broker_account_name="unused")
    oms = OmsRiskService(settings, feedback, session_factory=db[1], broker_adapter=guard)
    monkeypatch.setattr(oms, "_evaluate_risk", lambda _event: (True, "controlled risk inputs"))
    monkeypatch.setattr(oms, "_market_is_fillable", lambda *_args: True)
    monkeypatch.setattr(oms, "_reconcile_after_intent", AsyncMock())
    transport = Transport(oms)
    bot.intent_emitter = SchwabV2IntentEmitter(bot.settings, transport, broker_account_name=PRIMARY)
    bot.webull_intent_emitter = SchwabV2IntentEmitter(bot.settings, transport, broker_account_name=WEBULL)
    wires = []

    async def wire(request):
        assert request.side == "buy" and request.intent_type == "open"
        with db[1]() as independent:
            token = independent.scalar(select(journal.BuySubmissionToken).where(
                journal.BuySubmissionToken.client_order_id == request.client_order_id))
            assert token is not None and token.state == "submitting"
            assert token.account_name == request.broker_account_name
            assert token.generation == request.metadata["fanout_segment_id"]
            epoch = independent.get(journal.BuyCoverageEpoch, (token.process_id, token.account_id))
            assert epoch is not None and epoch.started_at_ms < token.opportunity_started_at_ms
        wires.append(request)
        return [ExecutionReport("accepted", request.client_order_id,
            broker_order_id="controlled-" + request.broker_account_name, symbol="DKI",
            quantity=request.quantity, origin="broker")]

    for account in (PRIMARY, WEBULL):
        leaf = guard.cancel_terminal_delegate._adapter_for_account(account)
        leaf.settings = settings
        leaf.submit_order = wire
    blocked = TradeIntentDraft("DKI", "buy", "open", Decimal(1), "synthetic pending BUY", {})
    assert not await bot._emit_removal_tracked(bot.intent_emitter, blocked)
    strategy._queue_removed_wait_barriers(strategy.watchlist_state("DKI"), req)
    await caller.poll(bot)
    assert not reads and not wires and db[0].restore() == {"DKI": req}
    try:
        await bot._drain_direct_strategy_intents()
        await settle(oms)
        assert len(reads) == 1 and len(transport.events) == 2
        with db[1]() as session:
            receipts = list(session.scalars(select(TradeIntent)))
            assert len(receipts) == 2 and all(feedback_published(row) for row in receipts)
            revisions = {row.id: (row.created_at, row.updated_at, row.status) for row in receipts}
        clock[0] += 1
        await caller.poll(bot)
        await settle(oms)
        await caller.poll(bot)
        assert len(reads) == 2 and not db[0].restore() and not strategy._removed_wait_requests
        assert not wires and len(covered.closures(db)) == 2
        with db[1]() as session:
            assert {row.id: (row.created_at, row.updated_at, row.status)
                    for row in session.scalars(select(TradeIntent))} == revisions
        clock[0] += 1
        identities = FanoutSegmentIdentityStore(db[1])
        strategy.configure_fanout_identity_persistence(lambda symbol, generation, active, reason:
            identities.record(symbol, generation, active, reason, now=Clock.now(UTC)))
        strategy.configure_removed_wait(db[0].record, restored=db[0].restore(), readable=True,
            dispatch_persist=db[0].record_dispatch, terminal_proofs=db[0].restore_terminal_proofs())
        generation = strategy._ensure_flip_owner_opportunity(strategy.watchlist_state("DKI"))
        assert generation == clock[0]
        draft = TradeIntentDraft("DKI", "buy", "open", Decimal(1), "synthetic caller BUY", {
            "fanout_segment_id": str(generation), "order_type": "limit", "limit_price": "4.79",
            "reference_price": "4.79", "entry_size_price": "4.79", "session": "AM" if pm else "RTH"})
        for emitter in (bot.intent_emitter, bot.webull_intent_emitter):
            assert await bot._emit_removal_tracked(emitter, draft)
        assert {request.broker_account_name for request in wires} == {PRIMARY, WEBULL}
        with db[1]() as session:
            orders = list(session.scalars(select(BrokerOrder)))
            assert len(orders) == 2 and all(order.status == "accepted" for order in orders)
            assert {order.broker_account_id for order in orders} == set(db[2].values())
            tokens = list(session.scalars(select(journal.BuySubmissionToken)))
            assert len(tokens) == 2 and all(token.generation == str(generation) for token in tokens)
            assert all(token.state == "reported_ambiguous" for token in tokens)
    finally:
        await oms._drain_cancel_terminal_evidence()
