"""Recorded-price v2 reprice/NFQ composition, with an explicit broker simulator.

The fixture supplies real symbols, times, entry levels, and published quotes.
Liquid SHORT bar state, the subsequent 1% trail decline, and delivery to the OMS
cache are controlled counterfactual inputs, not historical strategy/OMS facts.
Only SimulatedBrokerAdapter supplies execution reports; none are venue evidence.

This extends the shared-seam replay through both the production RPG runtime
handoff and tonight's legacy OFF path. NFQ remains ON and OMS retains its loaded
ON flag. Missing production seams fail collection rather than silently skip.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from sqlalchemy import select

from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.models import BrokerOrder, Fill
from project_mai_tai.events import TradeIntentEvent
from project_mai_tai.fanout_outcome_consumer import FanoutOutcomeJournal
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.oms import service as oms
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    SchwabV2IntentEmitter,
    SchwabV2Strategy,
)
from tests.unit.test_nfq1_mirror_fresh_price import install_shape_adapter
from tests.unit.test_oms_webull_mirror_deferred_resubmit import (
    ACCOUNT,
    _FakeRedis,
    _integrated_service,
    _session_factory,
)


CASES = [
    case for case in json.loads(
        (Path(__file__).parents[1] / "fixtures/nfq1_recorded_cases.json").read_text()
    )["cases"]
    if Decimal(case["quotes"]["before"][0]["ask_price"])
    < Decimal(case["record"]["payload"]["stop_price"])
]


def _retry_events(service):
    return [
        event for stream, data in service.redis.entries
        if stream.endswith("strategy-intents") and data.get("event_type") == "trade_intent"
        and (event := TradeIntentEvent.model_validate(data)).payload.metadata.get("nfq_retry_token")
    ]


async def _emit_lifecycle(strategy, emitter, clock, intent_type):
    primary = strategy.drain_pending_intents()
    mirrors = strategy.drain_webull_direct_intents()
    assert [draft.intent_type for draft in primary] == [intent_type]
    assert [draft.intent_type for draft in mirrors] == [intent_type]
    if intent_type == "open":
        assert primary[0].metadata["stop_price"] == mirrors[0].metadata["stop_price"]
    await emitter.emit(mirrors[0])
    event = TradeIntentEvent.model_validate(emitter.redis.entries[-1][1])
    # Project only the real emitter's envelope clock onto this historical case.
    # All lifecycle metadata, generations, and IDs come from production code.
    event.produced_at = clock[0]
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff_enabled", [True, False], ids=["handoff_on", "tonight_handoff_off"])
@pytest.mark.parametrize(
    "case", CASES,
    ids=lambda case: f'{case["record"]["symbol"]}-{case["record"]["submitted_at"][:16]}',
)
async def test_shared_v2_reprice_retires_queued_nfq_generation_exactly_once(monkeypatch, case, handoff_enabled):
    factory = _session_factory()
    service, simulated_adapter = _integrated_service(factory, enabled=True, nfq_enabled=True)
    assert isinstance(simulated_adapter, SimulatedBrokerAdapter)
    row, quote = case["record"], case["quotes"]["before"][0]
    recorded = row["payload"]
    clock = [datetime.fromisoformat(row["submitted_at"])]
    monkeypatch.setattr(oms, "utcnow", lambda: clock[0])
    is_regular = oms._is_regular_market_session
    monkeypatch.setattr(oms, "_is_regular_market_session", lambda now=None: is_regular(now or clock[0]))
    monkeypatch.setattr(oms.OmsRiskService, "_market_is_fillable", lambda self, now=None: True)
    # Exercise the real wire-shape guard before the simulator's acceptance/fill.
    install_shape_adapter(simulated_adapter, clock)

    settings = service.settings.model_copy(update={
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_atr_reprice_handoff_enabled": handoff_enabled,
        "strategy_schwab_1m_v2_pm_print_ask_confirm_enabled": True,
        "strategy_schwab_1m_v2_pm_flip_wait_enabled": False,
        "strategy_schwab_1m_v2_pm_rest_reprice_enabled": False,
        "strategy_schwab_1m_v2_gap_hold_enabled": True,
        "oms_v2_webull_mirror_fresh_price_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_quote_max_age_ms": 10_000,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_reprice_pct": 0.5,
        "strategy_schwab_1m_v2_flip_owned_first_entry_enabled": False,
        "strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct": float(recorded["resting_offset_pct"]),
        "strategy_schwab_1m_v2_entry_window_start_hour_et": 9,
        "strategy_schwab_1m_v2_entry_window_start_minute_et": 30,
        "strategy_schwab_1m_v2_entry_window_end_hour_et": 16,
        "strategy_schwab_1m_v2_entry_window_end_minute_et": 0,
    })
    if not handoff_enabled:
        settings = settings.model_copy(update={
            "strategy_schwab_1m_v2_account_name": "live:schwab_1m_v2",
            "strategy_schwab_1m_v2_entry_notional_usd": 600,
            "strategy_schwab_1m_v2_webull_entry_notional_usd": 300,
            "strategy_schwab_1m_v2_entry_window_start_hour_et": 7,
            "strategy_schwab_1m_v2_entry_window_start_minute_et": 0,
            "strategy_schwab_1m_v2_entry_window_end_hour_et": 15,
            "strategy_schwab_1m_v2_entry_window_end_minute_et": 45,
        })
    # Tonight restarts only v2; the existing OMS retains its loaded ON setting.
    service.settings = settings.model_copy(update={"strategy_schwab_1m_v2_atr_reprice_handoff_enabled": True})
    strategy = SchwabV2Strategy(settings)
    monkeypatch.setattr(strategy, "_now_ms", lambda: int(clock[0].timestamp() * 1000))
    for method_name in ("_resting_in_window", "_resting_session_is_eh", "_entry_window_closed_for_session"):
        original = getattr(strategy, method_name)
        monkeypatch.setattr(strategy, method_name, lambda now=None, fn=original: fn(now or clock[0]))

    state = strategy.watchlist_state(row["symbol"])
    state.atr_state = "short"
    state.atr_state_age = strategy._resting_min_short_bars
    state.atr_trail = float(recorded["cw_flip_level"])
    state.fanout_segment_id = int(recorded["fanout_segment_id"])
    state.atr_short_flip_bar_ts = state.fanout_segment_id
    # Explicit eligibility scaffolding: these OHLCV values are NOT a recorded bar.
    state.bars.append(OHLCVBar(
        timestamp_ms=strategy._now_ms() - 60_000,
        open=float(quote["bid_price"]), high=float(quote["ask_price"]),
        low=float(quote["bid_price"]), close=float(quote["bid_price"]),
        volume=strategy._atr_vol_floor + 1,
    ))
    state.last_quote = Quote(
        row["symbol"], float(quote["bid_price"]), float(quote["ask_price"]),
        float(quote["bid_price"]), int(datetime.fromisoformat(quote["event_ts"]).timestamp() * 1000),
    )
    FanoutSegmentIdentityStore(factory).record(
        state.symbol, state.fanout_segment_id, True,
        "controlled composition replay of recorded segment", now=clock[0],
    )
    emitter = SchwabV2IntentEmitter(settings, _FakeRedis(), ACCOUNT)
    journal = FanoutOutcomeJournal(factory)

    strategy._cw_v2_resting_track(state, None)
    original = await _emit_lifecycle(strategy, emitter, clock, "open")
    original_md = original.payload.metadata
    assert original_md["cw_flip_level"] == recorded["cw_flip_level"]
    # The fixture serialized the trail to four decimals, losing its internal
    # precision. CYCU's recomputed offset stop differs by one last-place unit.
    assert abs(Decimal(original_md["stop_price"]) - Decimal(recorded["stop_price"])) <= Decimal("0.0001")
    assert original_md["fanout_slot_id"] == recorded["fanout_slot_id"]
    slot = original_md["fanout_slot_id"]
    generation = original_md["webull_mirror_generation_id"]
    await service._handle_stream_message({"data": original.model_dump_json()})
    assert simulated_adapter.requests == []
    assert service._nfq_holds[slot].phase == "held"
    journal.bootstrap(
        strategy.apply_fanout_outcome,
        active_segments={state.symbol: state.fanout_segment_id}, now=clock[0],
    )
    assert state.fanout_claim_outcome == "held"
    assert state.webull_resting_active

    # Publication time is deliberately projected into the test cache. The fixture
    # cannot prove historical OMS receipt (particularly for the 16:00 CYCU case).
    def deliver_recorded_quote():
        service._latest_quotes_by_symbol[state.symbol] = {
            "ask": Decimal(quote["ask_price"]),
            "received_at": datetime.fromisoformat(quote["event_ts"]),
        }

    deliver_recorded_quote()
    await service._evaluate_nfq_holds(state.symbol)
    old_retry, = _retry_events(service)
    assert service._nfq_holds[slot].phase == "queued"
    assert old_retry.payload.metadata["webull_mirror_generation_id"] == generation
    assert simulated_adapter.requests == []

    # Real v2 trail movement cancels; ownership depends on v2's hand-off flag.
    state.atr_trail *= 0.99
    strategy._cw_v2_resting_track(state, None)
    cancel = await _emit_lifecycle(strategy, emitter, clock, "cancel")
    assert cancel.payload.metadata["reason"] == "reprice"
    assert cancel.payload.metadata["webull_mirror_generation_id"] == generation
    await service._handle_stream_message({"data": cancel.model_dump_json()})
    journal.poll(strategy.apply_fanout_outcome)
    assert slot not in service._nfq_holds
    assert not state.fanout_webull_claimed
    assert simulated_adapter.requests == []

    if not handoff_enabled:
        assert "atr_reprice" not in cancel.payload.metadata
        assert not HandoffJournal(factory).jobs()
        assert not strategy._rpg_entry_owned(state)
        service._latest_quotes_by_symbol.clear()
        strategy._cw_v2_resting_track(state, None)
        replacement = await _emit_lifecycle(strategy, emitter, clock, "open")
        replacement_md = replacement.payload.metadata
        assert "atr_reprice_token" not in replacement_md
        assert replacement_md["fanout_slot_id"] == slot
        assert replacement_md["webull_mirror_generation_id"] != generation
        await service._handle_stream_message({"data": replacement.model_dump_json()})
        assert service._nfq_holds[slot].phase == "held"
        clock[0] += timedelta(seconds=1)
        service._latest_quotes_by_symbol[state.symbol] = {
            "ask": Decimal(quote["ask_price"]), "received_at": clock[0],
        }
        await service._evaluate_nfq_holds(state.symbol)
        new_retry = _retry_events(service)[1]
        assert new_retry.payload.metadata["nfq_retry_token"] != old_retry.payload.metadata["nfq_retry_token"]
        for retry in (old_retry, new_retry, new_retry, old_retry):
            await service._handle_stream_message({"data": retry.model_dump_json()})
        request, = simulated_adapter.requests
        assert request.metadata["webull_mirror_generation_id"] == replacement_md["webull_mirror_generation_id"]
        assert request.intent_type == "open"
        assert Decimal(request.metadata["entry_notional_target_usd"]) == Decimal(300)
        assert request.quantity > 1  # Current dollar sizing, not the amount-zero fixture.
        assert service._nfq_holds == {}
        assert not HandoffJournal(factory).jobs()
        with factory() as session:
            order, = session.scalars(select(BrokerOrder)).all()
            fill, = session.scalars(select(Fill)).all()
            assert order.client_order_id == request.client_order_id
            assert fill.order_id == order.id
        return

    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings = strategy, settings
    bot.session_factory, bot.redis = factory, service.redis
    handoffs = HandoffJournal(factory)
    token, job = handoffs.jobs()[0]
    assert job["local_no_wire"]  # This old generation NEVER reached a broker.

    async def current_feedback():
        await bot._rpg_handoff_pass()
        for _, data in list(service.redis.entries):
            if data.get("event_type") == "atr_reprice_tick":
                await service._handle_stream_message({"data": json.dumps(data)})

    service._latest_quotes_by_symbol.clear()
    await current_feedback()
    job = handoffs.read(token)
    assert job["phase"] == "price_wait"
    replacement_md = job["replacement"]["metadata"]
    assert replacement_md["fanout_slot_id"] == slot
    assert replacement_md["fanout_segment_id"] == original_md["fanout_segment_id"]
    assert replacement_md["webull_mirror_generation_id"] != generation
    assert Decimal(replacement_md["stop_price"]) < Decimal(original_md["stop_price"])
    journal.poll(strategy.apply_fanout_outcome)
    assert state.fanout_claim_outcome == "held"

    # Controlled later delivery of the recorded price, with a NEW receipt/quote
    # timestamp. This is not evidence of a historical OMS cache observation.
    clock[0] += timedelta(seconds=1)
    state.last_quote.quote_time_ms = strategy._now_ms()
    service._latest_quotes_by_symbol[state.symbol] = {
        "ask": Decimal(quote["ask_price"]), "received_at": clock[0],
    }
    await service._evaluate_nfq_holds(state.symbol)
    new_retry = _retry_events(service)[1]
    assert new_retry.payload.metadata["nfq_retry_token"] != old_retry.payload.metadata["nfq_retry_token"]
    for retry in (old_retry, new_retry, old_retry):
        await service._handle_stream_message({"data": retry.model_dump_json()})
    assert simulated_adapter.requests == []  # Neither queued token can bypass RPG.

    await current_feedback()
    for retry in (old_retry, new_retry, new_retry):
        await service._handle_stream_message({"data": retry.model_dump_json()})
    strategy._cw_v2_resting_track(state, None)
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_direct_intents() == []
    assert len(emitter.redis.entries) == 2  # original and real cancel; RPG owns replacement
    assert service._nfq_holds == {}
    request, = simulated_adapter.requests
    assert request.intent_type == "open"
    assert request.metadata["webull_mirror_generation_id"] == replacement_md["webull_mirror_generation_id"]
    assert request.metadata["stop_price"] == replacement_md["stop_price"]
    assert request.client_order_id == handoffs.read(token)["replacement"]["client_order_id"]
    with factory() as session:
        order, = session.scalars(select(BrokerOrder)).all()
        fill, = session.scalars(select(Fill)).all()
        assert order.client_order_id == request.client_order_id
        assert order.broker_order_id.startswith("sim-order-")
        assert fill.order_id == order.id
