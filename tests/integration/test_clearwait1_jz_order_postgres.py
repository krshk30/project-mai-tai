"""Recorded JZ event sequence with current runtime commits and controlled venue inputs.

This is a counterfactual current-source replay, not clearance of the archived
opportunity0 row. No historical token, target, fill or complete book is invented.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.broker_adapters.webull_order_reads import shared_budget
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot, TradeIntent
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.oms import buy_submission_journal as journal
from project_mai_tai.oms.cancel_feedback import feedback_published
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar, Quote, SchwabV2IntentEmitter, SchwabV2Strategy,
)
from tests.integration.test_cancel_terminal_runtime import Client, sdk as controlled_sdk
from tests.integration.test_clearwait1_dki_order_postgres import Transport, settle
from tests.integration.test_clearwait1_postgres import pg_db as controlled_db
from tests.integration.test_falseflip1_postgres_epochs import postgres_factory  # noqa: F401
from tests.unit.test_clearwait1_jz_recorded import RAW
from tests.unit.test_clearwait1_session_rollover import PRIMARY, WEBULL, request, service, strategy
from tests.unit.test_clearwait1_unbound import controlled_routing

db, sdk = controlled_db, controlled_sdk


def epoch(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


@pytest.mark.asyncio
async def test_original_jz_event_sequence_current_coverage_clear_and_actual_two_account_buy(db, sdk, monkeypatch):
    removal_ms = int(RAW["request"]["payload"]["requested_at_ms"])
    clock = [removal_ms - 120_000]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0] / 1000, tz or UTC)
    for path in ("project_mai_tai.oms.service.datetime", "project_mai_tai.db.models.datetime",
                 "project_mai_tai.services.schwab_1m_v2_bot.datetime",
                 "project_mai_tai.fanout_segment_store.datetime",
                 "project_mai_tai.v2_flip_entry_ownership.datetime"):
        monkeypatch.setattr(path, Clock)
    monkeypatch.setattr(journal, "now_ms", lambda: clock[0])
    monkeypatch.setattr(broker, "now_ms", lambda: clock[0])
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: Clock.now(UTC))
    settings = strategy(request()).settings.model_copy(update={
        "oms_adapter": "simulated", "broker_default_provider": "schwab",
        "strategy_schwab_1m_v2_broker_provider": "schwab",
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct": .5,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_band_pct": .5,
        "strategy_schwab_1m_v2_resting_buy_round_up_enabled": True,
        "strategy_schwab_1m_v2_webull_resting_mirror_enabled": True,
        "orb_broker_account_name": "unused"})
    guard = journal.DurableBuyAdapter(controlled_routing(), db[1])
    with db[1]() as session:
        for name, account_id in db[2].items():
            account = session.get(BrokerAccount, account_id)
            account.external_account_id = broker.broker_binding(guard, name)[1]
        session.commit()
    await guard.start()
    # Both venue books/quotes below are explicit counterfactual controls. The
    # retained capture has no provider inventory and no new historical BUY fill.
    client = Client(pages=[{"has_next": False, "orders": []}, {"has_next": False, "orders": []}])
    webull = guard.cancel_terminal_delegate._adapter_for_account(WEBULL)
    webull._get_client = lambda: client
    webull._query_budget = shared_budget("controlled-JZ-replay", str(db[3]))
    feedback = SimpleNamespace(xadd=AsyncMock(return_value="controlled-normal-publication"))
    oms = OmsRiskService(settings, feedback, session_factory=db[1], broker_adapter=guard)
    monkeypatch.setattr(oms, "_evaluate_risk", lambda _event: (True, "controlled risk inputs"))
    monkeypatch.setattr(oms, "_market_is_fillable", lambda *_args: True)
    monkeypatch.setattr(oms, "_reconcile_after_intent", AsyncMock())
    transport = Transport(oms)
    bot = service(SchwabV2Strategy(settings), db[0])
    bot._removed_wait_adapter = guard.cancel_terminal_delegate
    bot.strategy._now_ms = lambda: clock[0]
    active = bot._configure_fanout_identity_store()
    bot._configure_flip_entry_ownership_store(active)
    await bot._configure_removed_wait_store()
    bot.intent_emitter = SchwabV2IntentEmitter(settings, transport, broker_account_name=PRIMARY)
    bot.webull_intent_emitter = SchwabV2IntentEmitter(settings, transport, broker_account_name=WEBULL)
    state = bot.strategy.watchlist_state("JZ")
    bot.strategy.apply_flip_position_book(bot._fetch_flip_position_book())
    # Recorded pre-removal state: software armed, flat, idle, no active REST.
    state.cw_armed = True
    state.cw_resting_taken = state.cw_reclaim_taken = True
    state.cw_entries_this_flip = 1
    assert state.fanout_segment_id == state.flip_owner_opportunity_id == 0
    clock[0] = removal_ms
    bot.strategy.release_and_drop_symbol("JZ", reason="watchlist-removed")
    req = bot.strategy._removed_wait_requests["JZ"]
    assert req.opportunity_id > 0, "new software wait must obtain a current durable identity before barriers"
    assert req.opportunity_id == removal_ms and req.token != RAW["request"]["payload"]["token"]
    with db[1]() as session:
        assert not list(session.scalars(select(journal.BuySubmissionToken)))
        binds = list(session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "v2_fanout_segment_identity")))
        assert binds, "identity must be produced by actual caller persistence"
    try:
        await bot._drain_direct_strategy_intents()
        await settle(oms)
        with db[1]() as session:
            receipts = list(session.scalars(select(TradeIntent)))
            assert len(receipts) == 2 and all(feedback_published(row) for row in receipts)
            assert all(row.payload["metadata"]["buy_submission_process_id"] == str(guard.process_id)
                       for row in receipts)
            assert not list(session.scalars(select(BrokerOrder)))
        clock[0] = epoch(RAW["readded_at"])
        bot.strategy.scanner_readded("JZ")
        sell = RAW["sell"]
        clock[0] = sell["observed_at_ms"]
        state.bars.append(OHLCVBar(sell["bar_time_ms"], float(sell["close"]), 1.24, 1.13,
                                  float(sell["close"]), 584780))
        observation = bot.strategy._observe_atr_sell(state, {"flip": "SELL", "trail": float(sell["trail"])})
        assert observation.decision_id == sell["decision_id"]
        # Observation is sent through the existing bot publisher. Transport keeps
        # its envelope visible and delegates only actual assessment/intent types.
        original_xadd = transport.xadd
        async def xadd(stream, fields, **kwargs):
            if json.loads(fields["data"]).get("event_type") == "v2_atr_sell_observation":
                return "controlled-recorded-SELL-publication"
            return await original_xadd(stream, fields, **kwargs)
        transport.xadd = xadd
        await bot._drain_atr_sell_observations()
        for _ in range(2):
            await bot._removed_wait_unbound_pass((req,), db[0], set(req.account_names))
            await settle(oms)
            bot.strategy.apply_flip_position_book(bot._fetch_flip_position_book())
        bot.strategy.apply_flip_position_book(bot._fetch_flip_position_book())
        await bot._removed_wait_poll()
        assert not db[0].restore() and not bot.strategy._removed_wait_requests
        assert len(list(transport.assessments)) == 1
        clock[0] += 1
        assert state.fanout_segment_id == state.flip_owner_opportunity_id == 0
        wires = []
        async def wire(order_request):
            with db[1]() as independent:
                token = independent.scalar(select(journal.BuySubmissionToken).where(
                    journal.BuySubmissionToken.client_order_id == order_request.client_order_id))
                assert token is not None and token.state == "submitting"
                assert token.account_name == order_request.broker_account_name
                assert token.generation == order_request.metadata["fanout_segment_id"]
                assert token.generation != str(req.opportunity_id)
                coverage = independent.get(journal.BuyCoverageEpoch, (token.process_id, token.account_id))
                assert coverage is not None and coverage.started_at_ms < token.opportunity_started_at_ms
            wires.append(order_request)
            return [ExecutionReport("accepted", order_request.client_order_id,
                broker_order_id="controlled-JZ-" + order_request.broker_account_name,
                symbol="JZ", quantity=order_request.quantity, reported_at=Clock.now(UTC), origin="broker")]
        for name in req.account_names:
            guard.cancel_terminal_delegate._adapter_for_account(name).submit_order = wire
        # Controlled fresh quote below the recorded SELL line permits the actual
        # strategy resting-order caller; no hand-authored BUY draft is injected.
        bot.strategy.on_quote("JZ", Quote("JZ", 1.16, 1.17, 1.165, clock[0]))
        await oms._handle_quote_tick_event(QuoteTickEvent(source_service="controlled-replay-quote",
            produced_at=Clock.now(UTC), payload=QuoteTickPayload(symbol="JZ",
                bid_price=Decimal("1.16"), ask_price=Decimal("1.17"))))
        bot.strategy._queue_resting_place(state, float(sell["trail"]))
        await bot._drain_direct_strategy_intents()
        assert {order.broker_account_name for order in wires} == {PRIMARY, WEBULL}
        with db[1]() as session:
            orders = list(session.scalars(select(BrokerOrder)))
            assert len(orders) == 2 and all(order.status == "accepted" for order in orders)
            assert {order.broker_account_id for order in orders} == set(db[2].values())
            tokens = list(session.scalars(select(journal.BuySubmissionToken)))
            assert len(tokens) == 2 and all(token.state == "reported_ambiguous" for token in tokens)
    finally:
        await oms._drain_cancel_terminal_evidence()
