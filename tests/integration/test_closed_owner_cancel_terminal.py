"""Synthetic postcoverage FLYE through OMS fills, assessment transport and F/I CAS.

Controlled broker/Redis endpoints are not retained historical or live evidence.
"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, OmsManagedPosition, Strategy, TradeIntent
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload, TradeIntentEvent, TradeIntentPayload
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore, current_session_anchor
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.falseflip1_runtime import FalseFlipStore, classify_managed_entries, record_bar
from project_mai_tai.oms import buy_submission_journal as journal
from project_mai_tai.oms.cancel_feedback import feedback_published
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.unbound_cancel_book import JOURNAL_KEY
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2IntentEmitter
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipStore
from tests.integration.test_cancel_terminal_runtime import sdk as controlled_sdk
from tests.integration.test_cancel_terminal_runtime import sessions as pg_sessions
from tests.unit.test_clearwait1_runtime_caller import poll
from tests.unit.test_clearwait1_session_rollover import service
from tests.unit.test_flye_bound_owner_target_close import FLYE, PRIMARY, WEBULL, confirm_controlled_next_entry, replay, sell
from tests.unit.test_flye_prewire_consumer_contract import synthetic_opportunity
from tests.integration.test_flye_bound_owner_postgres import controlled_unbound_db
from tests.unit.test_flye_prewire_runtime_caller import at
from tests.unit.test_flye_unbound_owner_release import controlled_routing

sdk, sessions = controlled_sdk, pg_sessions


@pytest.fixture
def db(sessions):
    return controlled_unbound_db(sessions)


@pytest.fixture
def snapshot_timestamp(request):
    def bind(now):
        def timestamp(_mapper, _connection, row):
            if row.created_at is None:
                row.created_at = now(UTC)
        event.listen(DashboardSnapshot, "before_insert", timestamp)
        request.addfinalizer(lambda: event.remove(DashboardSnapshot, "before_insert", timestamp))
    return bind


class Transport:
    def __init__(self, oms):
        self.oms, self.events, self.assessments, self.observations = oms, [], [], []

    async def xadd(self, _stream, fields, **_kwargs):
        payload = json.loads(fields["data"])
        kind = payload.get("event_type")
        if kind == "v2_cancel_terminal_assessment":
            self.assessments.append(payload)
            await self.oms._handle_stream_message(fields)
        elif kind == "v2_atr_sell_observation":
            self.observations.append(payload)
        else:
            event = TradeIntentEvent.model_validate_json(fields["data"])
            self.events.append(event)
            await self.oms.process_trade_intent(event)
        return "controlled-transport-id"


async def drain_workers(oms):
    # Keep the runtime open across the explicit fresh-SELL assessment.
    await asyncio.gather(*oms.__dict__.get("_cancel_feedback_tasks", set()))
    await asyncio.gather(*oms.__dict__.get("_cancel_terminal_assessment_tasks", set()))
    await asyncio.gather(*oms.__dict__.get("_cancel_terminal_tasks", {}).values())
    assert not getattr(oms, "_cancel_terminal_closing", False)


def snapshot(sessions):
    with sessions() as session:
        receipts = list(session.scalars(select(TradeIntent).where(TradeIntent.intent_type == "cancel")))
        revisions = {r.id: (r.created_at, r.updated_at, r.status) for r in receipts}
        physical = [r.payload[JOURNAL_KEY]["book"] for r in receipts if JOURNAL_KEY in r.payload]
        tokens = [(t.generation, t.state) for t in session.scalars(select(journal.BuySubmissionToken))]
        closures = list(session.scalars(select(journal.BuyAdmissionClosure)))
        return revisions, physical, tokens, closures


@pytest.mark.asyncio
@pytest.mark.usefixtures("sdk")
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("case", ["terminal_target", "recorded_target", "terminal_stop", "operator_sell", "working_schwab",
    "working_webull", "unknown_side", "open_owned", "unknown_rows", "same_real_segment", "nullable_binding",
    "conflicting_binding"])
async def test_actual_terminal_transport_closed_owner_after_sell(db, monkeypatch, snapshot_timestamp, pm, account, case):
    store, sessions, ids, strategy_id = db
    strategy, state, record, clock, _ = (replay(account=account, pm=pm) if case == "recorded_target"
                                        else synthetic_opportunity(account, pm))
    settings = strategy.settings.model_copy(update={"environment": "test",
        "broker_default_provider": "schwab", "orb_broker_account_name": "unused",
        "strategy_schwab_1m_v2_broker_provider": "schwab",
        "strategy_schwab_1m_v2_dual_broker_fanout_enabled": True,
        "oms_v2_exit_management_enabled": True, "oms_cancel_verify_enabled": False})
    if case == "recorded_target":
        settings = settings.model_copy(update={
            "strategy_schwab_1m_v2_resting_buy_round_up_enabled": True,
            "strategy_schwab_1m_v2_retry_one_enabled": True,
            "strategy_schwab_1m_v2_retry_one_max_retries": 0,
            "strategy_schwab_1m_v2_atr_reprice_handoff_enabled": False,
            "strategy_schwab_1m_v2_false_flip_enabled": True,
            "strategy_schwab_1m_v2_gap_line_carry_enabled": True,
            "oms_v2_webull_mirror_retained_hold_enabled": True,
            "strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct": 0.5,
        })
    strategy.settings, strategy._removed_wait_persist = settings, store.record
    if case == "recorded_target":
        strategy._resting_trigger_offset_pct = settings.strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct
        assert strategy._retry_one_enabled and strategy._retry_one_max_retries == 0
        strategy.configure_fanout_identity_persistence(lambda symbol, segment, active, reason:
            FanoutSegmentIdentityStore(sessions).record(symbol, segment, active, reason, now=at(clock[0])))
    routing = controlled_routing()
    feedback = SimpleNamespace(xadd=AsyncMock(return_value="controlled-feedback-accepted"))
    oms = OmsRiskService(settings, feedback, session_factory=sessions, broker_adapter=routing)
    monkeypatch.setattr(oms, "_evaluate_risk", lambda _event: (True, "controlled risk admission"))
    monkeypatch.setattr(oms, "_market_is_fillable", lambda *_args: True)
    monkeypatch.setattr(oms, "_reconcile_after_intent", AsyncMock())
    monkeypatch.setattr(journal, "now_ms", lambda: clock[0])
    monkeypatch.setattr(broker, "now_ms", lambda: clock[0])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0] / 1000, tz or UTC)

    class StorageClock(Clock):
        last = None

        @classmethod
        def now(cls, tz=None):
            value = super().now(tz)
            # Transactions advance even while the physical-book clock stays in one millisecond.
            cls.last = value if cls.last is None else max(value, cls.last + timedelta(microseconds=1))
            return cls.last

    monkeypatch.setattr("project_mai_tai.oms.service.datetime", Clock)
    monkeypatch.setattr("project_mai_tai.events.datetime", Clock)
    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.datetime", Clock)
    monkeypatch.setattr("project_mai_tai.db.models.datetime", Clock)
    snapshot_timestamp(StorageClock.now)
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: at(clock[0]))
    monkeypatch.setattr("project_mai_tai.oms.store.utcnow", lambda: at(clock[0]))
    wires, reads = [], []
    primary = routing._adapter_for_account(PRIMARY)
    primary._authorized_request_json = AsyncMock(side_effect=AssertionError("Schwab book forbidden"))
    leaf = routing._adapter_for_account(WEBULL)

    def physical_book(request):
        assert request.values == {"account_id": "TEST-WEBULL", "page_size": 100}
        reads.append(clock[0])
        side = "SELL" if case == "operator_sell" else "UNKNOWN" if case == "unknown_side" else "BUY"
        orders = [{"client_order_id": "controlled-foreign", "order_id": "foreign-wire",
            "symbol": "FLYE", "order_status": "SUBMITTED", "side": side}]
        return SimpleNamespace(status_code=200, body={"hasNext": False,
            "orders": orders if case in {"working_webull", "operator_sell", "unknown_side"} else []})

    leaf._get_client = lambda: SimpleNamespace(_auto_retry=False, get_response=physical_book)
    leaf._body = lambda response: response.body
    leaf._query_budget = SimpleNamespace(claim=lambda *_args, **_kwargs: None)

    async def wire(request):
        if request.side == "buy":
            with sessions() as independent:
                token = independent.scalar(select(journal.BuySubmissionToken).where(
                    journal.BuySubmissionToken.client_order_id == request.client_order_id))
                assert token.state == "submitting" and token.client_order_id == request.client_order_id
                assert token.generation == request.metadata["fanout_segment_id"]
                epoch = independent.get(journal.BuyCoverageEpoch, (token.process_id, token.account_id))
                assert epoch is not None and epoch.account_name == token.account_name == account
                assert epoch.started_at_ms < token.opportunity_started_at_ms
                assert token.account_id == ("TEST-WEBULL" if account == WEBULL else "TEST-SCHWAB")
        wires.append(request)
        return [ExecutionReport("filled", request.client_order_id, broker_order_id="controlled-" + request.side,
            broker_fill_id="fill-" + request.side, symbol=request.symbol, quantity=request.quantity,
            filled_quantity=request.quantity, fill_price=Decimal("2.34"), origin="broker",
            reported_at=at(clock[0]))]

    routing._adapter_for_account(account).submit_order = wire
    bind_ms = clock[0] - 60_000
    if case == "recorded_target":
        bind_ms = record.position_entry_ms[account] - 2
    clock[0] = bind_ms - 1
    await oms.broker_adapter.start()
    FanoutSegmentIdentityStore(sessions).record("FLYE", record.opportunity_id, True,
        "flip_owned_opportunity_v2_bind", now=at(bind_ms))
    clock[0] = bind_ms + 2
    if case != "nullable_binding":
        with sessions() as session:
            for name in (PRIMARY, WEBULL):
                session.get(BrokerAccount, ids[name]).external_account_id = (
                    "CONFLICTING-ACCOUNT" if case == "conflicting_binding" and name == account
                    else "TEST-WEBULL" if name == WEBULL else "TEST-SCHWAB")
            session.commit()
    for side in ("buy", "sell"):
        if side == "sell" and case == "open_owned":
            break
        if case == "recorded_target" and side == "sell":
            clock[0] = int(datetime.fromisoformat(FLYE["target_fill_utc"]).timestamp() * 1000)
        event = TradeIntentEvent(source_service="controlled-entry-close", produced_at=at(clock[0]), payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name=account, symbol="FLYE", side=side,
            intent_type="open" if side == "buy" else "close",
            quantity=Decimal(280) if side == "buy" else wires[0].quantity,
            reason="oms_v2_managed_exit:CW_HARD_STOP" if case == "terminal_stop" else "synthetic target close",
            metadata={"fanout_segment_id": str(record.opportunity_id),
                "fanout_slot_id": fanout_slot_id(strategy_code="schwab_1m_v2", symbol="FLYE",
                    segment_id=record.opportunity_id, slot="resting"), "reference_price": "2.34",
                "entry_size_price": "2.34", "order_type": "oco_exit"
                if side == "sell" and case != "terminal_stop" else "market"}))
        await oms.process_trade_intent(event)
        clock[0] += 1
    if case == "recorded_target":
        # Retained 16:58:02.801 UTC probe for the 12:57 ET entry bar.
        record_bar(sessions, {"symbol": "FLYE", "bar_ms": 1791478620000,
            "observed_at_ms": 1791478682801, "close": "2.135000",
            "trail": "1.882676", "state": "long", "phase": "live"}, now=at(clock[0]))
        assert classify_managed_entries(sessions, now=at(clock[0])) == 1
        strategy.configure_falseflip(FalseFlipStore(sessions).restore(now=at(clock[0])), readable=True)
    with sessions() as session:
        row = session.scalar(select(OmsManagedPosition))
        assert row is not None and row.entry_order_id is not None
        if case == "recorded_target":
            assert row.entry_classification["classification"] == "REAL_FLIP"
        assert row.current_quantity == (wires[0].quantity if case == "open_owned" else 0)
        entry = session.get(BrokerOrder, row.entry_order_id)
        assert entry.status == "filled" and entry.payload["fanout_segment_id"] == str(record.opportunity_id)
        assert session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == entry.id)).event_source == "broker"
        assert len(session.scalars(select(Fill)).all()) == (1 if case == "open_owned" else 2)
        record = replace(record, position_ids={account: str(row.id)}, position_entry_ms={account: bind_ms + 2})
        state.flip_owner_position_ids, state.flip_owner_position_entry_ms = dict(record.position_ids), dict(record.position_entry_ms)
        if case in {"working_schwab", "operator_sell"}:
            foreign = Strategy(code="synthetic_foreign", name="unrelated working order")
            session.add(foreign)
            session.flush()
            venue = WEBULL if case == "operator_sell" else PRIMARY
            session.add(BrokerOrder(strategy_id=foreign.id, broker_account_id=ids[venue], symbol="FLYE",
                side="sell" if case == "operator_sell" else "buy", order_type="limit", time_in_force="day", quantity=1000,
                status="accepted", client_order_id="controlled-foreign"))
            if case == "operator_sell":
                position = session.scalar(select(AccountPosition).where(
                    AccountPosition.broker_account_id == ids[WEBULL], AccountPosition.symbol == "FLYE"))
                if position is None:
                    session.add(AccountPosition(broker_account_id=ids[WEBULL], symbol="FLYE", quantity=1000))
                else:
                    position.quantity = 1000
            session.commit()

    owner_store = FlipEntryOwnershipStore(sessions)
    strategy._flip_owner_persist = lambda owner, active, reason: owner_store.record(
        owner, active=active, reason=reason, now=StorageClock.now(UTC))
    strategy._retry_one_budget_persist = lambda symbol, segment, count: owner_store.record_retry_budget(
        symbol, segment, count, now=StorageClock.now(UTC))
    bot = service(strategy, store)

    def owner_book(*, unreadable=False):
        actual = bot._fetch_flip_position_book()
        assert actual.readable
        if case != "open_owned":
            assert {c.managed_row_id for c in actual.closes_by_symbol["FLYE"]} == set(record.position_ids.values())
        strategy.apply_flip_position_book(replace(actual, readable=False) if unreadable else actual)

    clock[0] = FLYE["fresh_sell_poll_ms"] - 60_000
    if case == "open_owned":
        # Negative wake before the real owner poll: the DB-held leg must veto clearance.
        strategy._cancel_retry_leftovers(state)
    owner_book()
    req = strategy._removed_wait_requests["FLYE"]
    bot._removed_wait_roll_anchor = current_session_anchor(at(clock[0]))
    bot._removed_wait_adapter = routing
    transport = Transport(oms)
    bot.intent_emitter = SchwabV2IntentEmitter(settings, transport, broker_account_name=PRIMARY)
    bot.webull_intent_emitter = SchwabV2IntentEmitter(settings, transport, broker_account_name=WEBULL)
    assert not reads
    await bot._drain_direct_strategy_intents()
    await drain_workers(oms)
    assert feedback.xadd.await_count > 0
    with sessions() as session:
        receipts = list(session.scalars(select(TradeIntent).where(TradeIntent.intent_type == "cancel")))
        assert len(receipts) == 2 and all(feedback_published(row) for row in receipts)
    assert len(transport.events) == 2 and all(e.payload.intent_type == "cancel" for e in transport.events)
    await poll(bot)
    await drain_workers(oms)
    await poll(bot)
    assert state.flip_owner_phase != "idle" and not strategy._strict_first_rest_admitted(state, slot="first")
    before, _, _, _ = snapshot(sessions)

    if case != "same_real_segment":
        clock[0] = FLYE["fresh_sell_poll_ms"]
        owner_book()
        sell(strategy, state, clock)
        assert strategy._observe_atr_sell(state, {"flip": "SELL", "observation_phase": "live"}) is not None
        assert owner_store.restore_retry_budgets(now=at(clock[0]))["FLYE"] == (state.retry_one_segment_id, 0)
        assert owner_store.restore_active(now=at(clock[0]))["FLYE"].phase == "awaiting_close"
        await bot._drain_atr_sell_observations()
        assert transport.observations and bot._removed_wait_assessment_triggers[req][0] == "fresh_sell"
    if case == "operator_sell":
        state.position_qty = state.position_qty_held = 1000
    owner_book(unreadable=case == "unknown_rows")
    await poll(bot)
    await drain_workers(oms)
    await poll(bot)
    owner_book(unreadable=case == "unknown_rows")
    revisions, books, tokens, closures = snapshot(sessions)
    assert revisions == before
    primary._authorized_request_json.assert_not_awaited()
    released = case in {"terminal_target", "recorded_target", "terminal_stop", "operator_sell", "nullable_binding"}
    assert (state.flip_owner_phase == "idle") is released
    assert (not store.restore()) is released
    assert strategy._strict_first_rest_admitted(state, slot="first") is (released and case != "operator_sell")
    assert len(wires) == (1 if case == "open_owned" else 2)
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
    if case == "operator_sell":
        assert state.position_qty == state.position_qty_held == 1000
        with sessions() as session:
            assert session.scalar(select(AccountPosition.quantity).where(
                AccountPosition.broker_account_id == ids[WEBULL], AccountPosition.symbol == "FLYE")) == 1000
        strategy._cw_v2_resting_track(state, {"state": "short", "trail": 2.558685})
        assert not state.resting_active
        assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
    if released:
        assert tokens == [(str(record.opportunity_id), "broker_terminal")] and len(closures) == 2
        assert books[0]["started_at_ms"] == clock[0] and reads[-1] == clock[0]
        assert transport.assessments[-1]["trigger"] == "fresh_sell"
        proof, = store.restore_terminal_proofs()
        assert proof.request == req and set(proof.closed_owned_rows) == set(record.position_ids.items())
        assert not owner_store.restore_active(now=at(clock[0]))
        if case != "operator_sell":
            if case == "recorded_target":
                from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
                # Recorded owner/SELL boundary; following venue/quote/clean-bar
                # inputs are controlled counterfactuals, not historical fills.
                clock[0] += 120_000
                owner_book()
                state.bars.append(OHLCVBar(clock[0] - 60_000, 2.28, 2.35, 2.27, 2.285, 149257))
                state.atr_state = "short"
                state.atr_state_age = 3
                await oms._handle_quote_tick_event(QuoteTickEvent(source_service="controlled-replay-quote",
                    produced_at=at(clock[0]), payload=QuoteTickPayload(symbol="FLYE",
                        bid_price=Decimal("2.39"), ask_price=Decimal("2.40"))))
                strategy._cw_v2_resting_track(state, {"state": "short", "trail": 2.558685})
            else:
                strategy._queue_resting_place(state, 2.558685, slot="first")
            assert state.resting_active
            if not pm:
                for venue, drafts in ((PRIMARY, strategy.drain_pending_intents()),
                                      (WEBULL, strategy.drain_webull_direct_intents())):
                    draft, = drafts
                    assert draft.intent_type == "open" and draft.side == "buy"
                    if case == "recorded_target":
                        print(json.dumps({"case": "FLYE 10-08 14:10", "side": draft.side,
                            "account": venue, "time": at(clock[0]).isoformat(),
                            "price": draft.metadata.get("stop_price"),
                            "limit": draft.metadata.get("limit_price"),
                            "order_result": "CONTROLLED ACCEPTED (verified below)",
                            "evidence": "recorded boundary; controlled post-deploy broker replay"}))
                        async def accept(order_request):
                            return [ExecutionReport("accepted", order_request.client_order_id,
                                broker_order_id="replayed-next-" + order_request.broker_account_name,
                                symbol=order_request.symbol, side=order_request.side,
                                quantity=order_request.quantity, metadata=order_request.metadata,
                                origin="broker", reported_at=at(clock[0]))]
                        routing._adapter_for_account(venue).submit_order = accept
                        emitter = bot.intent_emitter if venue == PRIMARY else bot.webull_intent_emitter
                        await emitter.emit(draft)
                if case == "recorded_target":
                    with sessions() as session:
                        resting = session.scalars(select(BrokerOrder).where(
                            BrokerOrder.symbol == "FLYE", BrokerOrder.status == "accepted")).all()
                        assert len(resting) == 2 and {r.broker_account_id for r in resting} == set(ids.values())
                        assert all(r.side == "buy" and r.order_type == "STOP_LIMIT" for r in resting), [
                            (r.side, r.order_type, r.payload) for r in resting]
            confirm_controlled_next_entry(strategy, state, clock)
            assert not strategy._strict_first_rest_admitted(state, slot="first")
    elif case in {"working_schwab", "working_webull", "unknown_side", "open_owned", "conflicting_binding"}:
        assert not closures and tokens == [(str(record.opportunity_id), "reported_ambiguous")]
    if case == "same_real_segment":
        assert state.flip_owner_phase == "bound" and store.restore() == {"FLYE": req}
        assert state.retry_one_segment_id == record.retry_segment_id
    await oms._drain_cancel_terminal_evidence()
