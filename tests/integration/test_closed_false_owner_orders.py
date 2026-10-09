"""Recorded false-entry facts on current runtime; broker answers are controlled.

No historical token or cancel proof is fabricated: the prospective replay uses
the real pre-wire writer before each controlled adapter submission.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, OmsManagedPosition
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload, TradeIntentEvent, TradeIntentPayload
from project_mai_tai.falseflip1_runtime import FalseFlipStore, classify_managed_entries, record_bar
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms import buy_submission_journal as journal
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2IntentEmitter
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipRecord
from tests.integration import test_closed_owner_cancel_terminal as contract
from tests.integration.test_cancel_terminal_runtime import sessions  # noqa: F401
from tests.unit.test_clearwait1_runtime_caller import poll
from tests.unit.test_clearwait1_session_rollover import service
from tests.unit.test_falseflip1_runtime import FLYE, controlled_proof, runtime
from tests.unit.test_flye_bound_owner_target_close import PRIMARY, WEBULL
from tests.unit.test_flye_unbound_owner_release import controlled_routing

Transport, drain_workers = contract.Transport, contract.drain_workers
db, sdk, snapshot_timestamp = contract.db, contract.sdk, contract.snapshot_timestamp


CASES = {
    "FLYE": {"opportunity": controlled_proof(FLYE[0]).identity.opportunity_id, "segment": 1791466440000,
        "fill_ms": 1791468041000, "price": "3.05", "bar_ms": 1791468000000,
        "observed_ms": 1791468062000, "close": "2.7901", "trail": "3.006326",
        "exit_ms": 1791468081000, "high": 3.10, "low": 2.74},
    # Read-only VPS managed entry classifications and V2-ATR-PROBE, Oct9 17:30ET.
    "VEEA": {"opportunity": 1791564122190, "segment": 1791563880000,
        "fill_ms": 1791569791000, "price": "5.63", "bar_ms": 1791569760000,
        "observed_ms": 1791569822327, "close": "5.52", "trail": "5.56773693106012",
        "exit_ms": 1791569912000, "high": 5.63, "low": 5.49},
}


@pytest.mark.asyncio
@pytest.mark.usefixtures("sdk")
@pytest.mark.parametrize("symbol", ["FLYE", "VEEA"])
@pytest.mark.parametrize("pm", [False, True], ids=["rth", "pm"])
async def test_recorded_false_close_current_token_runtime_restores_actual_order(
    db, monkeypatch, snapshot_timestamp, symbol, pm,
):
    store, factory, ids, _ = db
    facts = dict(CASES[symbol])
    if pm:
        # Recorded RTH facts shifted to AM for the explicitly synthetic PM control.
        shift = (2 if symbol == "FLYE" else 6) * 3_600_000
        for key in ("opportunity", "segment", "fill_ms", "bar_ms", "observed_ms", "exit_ms"):
            facts[key] -= shift
    strategy, _, _, _, _ = runtime(pm=pm, installed_policy=True)
    settings = strategy.settings.model_copy(update={"environment": "test",
        "broker_default_provider": "schwab", "strategy_schwab_1m_v2_broker_provider": "schwab",
        "orb_broker_account_name": "unused", "oms_v2_exit_management_enabled": True,
        "oms_cancel_verify_enabled": False})
    strategy.settings = settings
    clock = [facts["opportunity"] - 120_000]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock[0] / 1000, tz or UTC)

    class StorageClock(Clock):
        last = None

        @classmethod
        def now(cls, tz=None):
            value = super().now(tz)
            cls.last = value if cls.last is None else max(value, cls.last + timedelta(microseconds=1))
            return cls.last

    for path in ("project_mai_tai.oms.service.datetime", "project_mai_tai.events.datetime",
                 "project_mai_tai.db.models.datetime", "project_mai_tai.services.schwab_1m_v2_bot.datetime",
                 "project_mai_tai.fanout_segment_store.datetime",
                 "project_mai_tai.falseflip1_runtime.datetime"):
        monkeypatch.setattr(path, Clock)
    monkeypatch.setattr(journal, "now_ms", lambda: clock[0])
    monkeypatch.setattr(broker, "now_ms", lambda: clock[0])
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: Clock.now(UTC))
    monkeypatch.setattr("project_mai_tai.oms.store.utcnow", lambda: Clock.now(UTC))
    snapshot_timestamp(StorageClock.now)
    routing = controlled_routing()
    oms = OmsRiskService(settings, SimpleNamespace(xadd=AsyncMock(return_value="controlled-feedback")),
        session_factory=factory, broker_adapter=routing)
    monkeypatch.setattr(oms, "_evaluate_risk", lambda _event: (True, "controlled risk admission"))
    monkeypatch.setattr(oms, "_market_is_fillable", lambda *_args: True)
    monkeypatch.setattr(oms, "_reconcile_after_intent", AsyncMock())
    await oms.broker_adapter.start()
    identities = FanoutSegmentIdentityStore(factory)
    identities.record(symbol, facts["opportunity"], True, "flip_owned_opportunity_v2_bind",
        now=Clock.now(UTC) + timedelta(seconds=60))
    with factory() as session:
        for name, ident in ids.items():
            session.get(BrokerAccount, ident).external_account_id = (
                "TEST-SCHWAB" if name == PRIMARY else "TEST-WEBULL")
        session.commit()
    leaf = routing._adapter_for_account(WEBULL)
    leaf._get_client = lambda: SimpleNamespace(_auto_retry=False, get_response=lambda _request:
        SimpleNamespace(status_code=200, body={"hasNext": False, "orders": []}))
    leaf._body = lambda response: response.body
    leaf._query_budget = SimpleNamespace(claim=lambda *_args, **_kwargs: None)

    async def fill(order):
        return [ExecutionReport("filled", order.client_order_id,
            broker_order_id=f"controlled-{order.broker_account_name}-{order.side}",
            broker_fill_id=f"controlled-fill-{order.broker_account_name}-{order.side}",
            symbol=symbol, side=order.side, quantity=order.quantity, filled_quantity=order.quantity,
            fill_price=Decimal(facts["price"]), origin="broker", reported_at=Clock.now(UTC))]

    for account in ids:
        routing._adapter_for_account(account).submit_order = fill
    for side in ("buy", "sell"):
        clock[0] = facts["fill_ms"] if side == "buy" else facts["exit_ms"]
        for account in ids:
            quantity = Decimal(1)
            if side == "sell":
                with factory() as session:
                    quantity = session.scalar(select(OmsManagedPosition.current_quantity).where(
                        OmsManagedPosition.symbol == symbol,
                        OmsManagedPosition.broker_account_name == account))
            await oms.process_trade_intent(TradeIntentEvent(source_service="recorded-case-current-runtime",
                produced_at=Clock.now(UTC), payload=TradeIntentPayload(strategy_code="schwab_1m_v2",
                    broker_account_name=account, symbol=symbol, side=side,
                    intent_type="open" if side == "buy" else "close", quantity=quantity,
                    reason="controlled false-entry close", metadata={
                        "fanout_segment_id": str(facts["opportunity"]),
                        "fanout_slot": "resting", "fanout_slot_id": fanout_slot_id(
                            strategy_code="schwab_1m_v2", symbol=symbol,
                            segment_id=facts["opportunity"], slot="resting"),
                        "reference_price": facts["price"], "entry_size_price": facts["price"]})))
    await oms._falseflip_database_work(record_bar, factory, {"symbol": symbol,
        "bar_ms": facts["bar_ms"], "observed_at_ms": facts["observed_ms"],
        "close": facts["close"], "trail": facts["trail"], "state": "short", "phase": "live"})
    await oms._falseflip_database_work(classify_managed_entries, factory)
    with factory() as session:
        rows = list(session.scalars(select(OmsManagedPosition).where(OmsManagedPosition.symbol == symbol)))
        assert len(rows) == 2 and all(row.status == "closed" for row in rows)
        assert all(row.entry_classification["classification"] == "FALSE_FLIP" for row in rows)
        record = FlipEntryOwnershipRecord(symbol, facts["opportunity"], "consumed", 0, 0,
            tuple(ids), {row.broker_account_name: str(row.id) for row in rows},
            {row.broker_account_name: facts["fill_ms"] for row in rows}, facts["segment"], 0)
    strategy._now_ms = lambda: clock[0]
    strategy.configure_flip_entry_ownership(lambda *_: None, restored={symbol: record},
        active_segments={symbol: record.opportunity_id}, retry_budget_persist=lambda *_: None,
        restored_retry_budgets={symbol: (facts["segment"], 1)})
    strategy.configure_removed_wait(store.record, restored={}, readable=True)
    strategy.configure_fanout_identity_persistence(lambda sym, generation, active, reason:
        identities.record(sym, generation, active, reason, now=StorageClock.now(UTC)))
    state = strategy.watchlist_state(symbol)
    state.atr_trail, state.atr_state = float(facts["trail"]), "short"
    state.atr_short_flip_bar_ts = facts["segment"]
    state.bars.append(OHLCVBar(facts["bar_ms"], float(facts["close"]),
        facts["high"], facts["low"], float(facts["close"]), 87207))
    bot = service(strategy, store)
    bot._removed_wait_adapter = routing
    bot._falseflip_store = FalseFlipStore(factory)
    transport = Transport(oms)
    bot.intent_emitter = SchwabV2IntentEmitter(settings, transport, PRIMARY)
    bot.webull_intent_emitter = SchwabV2IntentEmitter(settings, transport, WEBULL)
    bot._last_quote_by_symbol = {}
    for _ in range(4):
        strategy.apply_flip_position_book(bot._fetch_flip_position_book())
        await bot._falseflip_poll()
        await bot._drain_direct_strategy_intents()
        await drain_workers(oms)
        await poll(bot)
        await drain_workers(oms)
    strategy.apply_flip_position_book(bot._fetch_flip_position_book())
    assert not strategy._removed_wait_requests and state.flip_owner_phase == "idle"
    assert strategy._falseflip_effective_closes(state, 1) == 0
    clock[0] += 60_000
    state.bars.append(replace(state.bars[-1], timestamp_ms=clock[0] - 60_000))
    strategy.apply_flip_position_book(bot._fetch_flip_position_book())
    await oms._handle_quote_tick_event(QuoteTickEvent(source_service="controlled-replay-quote",
        produced_at=Clock.now(UTC), payload=QuoteTickPayload(symbol=symbol,
            bid_price=Decimal(facts["close"]), ask_price=Decimal(facts["close"]))))
    strategy._falseflip_restoring_track(state)
    assert state.resting_active
    drafts = strategy.drain_pending_intents() + strategy.drain_webull_direct_intents()
    if pm:
        # The PM price cross uses the existing software-rest dispatch path.
        from project_mai_tai.market_data.schwab_v2_rest_client import Quote
        trigger = strategy._active_resting_trigger(state)
        quote = Quote(symbol, trigger, trigger, trigger, clock[0], trade_time_ms=clock[0])
        bot._last_quote_by_symbol[symbol] = quote
        await oms._handle_quote_tick_event(QuoteTickEvent(source_service="controlled-replay-cross",
            produced_at=Clock.now(UTC), payload=QuoteTickPayload(symbol=symbol,
                bid_price=Decimal(str(trigger)), ask_price=Decimal(str(trigger)))))
        draft = strategy._eh_resting_cross_check(state, quote)
        assert draft is not None
        drafts = [draft, *strategy.drain_webull_fanout_intents()]
    assert len(drafts) == 2 and all(draft.side == "buy" and draft.intent_type == "open" for draft in drafts)
    for account, draft in zip((PRIMARY, WEBULL), drafts, strict=True):
        async def accept(order):
            return [ExecutionReport("accepted", order.client_order_id,
                broker_order_id="controlled-rest-" + order.broker_account_name,
                symbol=symbol, quantity=order.quantity, origin="broker", reported_at=Clock.now(UTC))]
        routing._adapter_for_account(account).submit_order = accept
        emitter = bot.intent_emitter if account == PRIMARY else bot.webull_intent_emitter
        assert bot._apply_extended_hours_routing(draft, Clock.now(UTC))
        await emitter.emit(draft)
        print(json.dumps({"case": symbol + " false close", "session": "pm" if pm else "rth",
            "side": draft.side, "account": account, "time": Clock.now(UTC).isoformat(),
            "price": draft.metadata.get("stop_price", draft.metadata.get("limit_price")),
            "evidence": "recorded entry/bar facts; current pre-wire runtime; controlled broker"}))
    with factory() as session:
        placed = list(session.scalars(select(BrokerOrder).where(
            BrokerOrder.symbol == symbol, BrokerOrder.status == "accepted")))
        assert len(placed) == 2 and all(order.side == "buy" for order in placed)
    await oms._drain_cancel_terminal_evidence()
