"""Collect on either branch; exercise only the combined RPG1 + NFQ1 tree.

Uses recorded AMOD stale-hold inputs and a simulated OMS broker. This covers the
first-entry quote wait, NOT the unfinished cancel/readback/re-place handoff.
"""
from datetime import UTC, datetime
from decimal import Decimal

import pytest

pytest.importorskip(
    "project_mai_tai.oms.mirror_fresh_price",
    reason="Composition requires NFQ1 PR #1086 in addition to RPG1 PR #1085",
)

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.fanout_outcome_consumer import FanoutOutcomeJournal
from project_mai_tai.oms import service as oms
from tests.unit.test_rpg1_pending_first_quote import setup_wait, fresh
from tests.unit.test_nfq1_mirror_fresh_price import queued_events, seed_segment, set_quote
from tests.unit.test_oms_webull_mirror_deferred_resubmit import _integrated_service, _session_factory


def as_event(draft, account, now):
    return TradeIntentEvent(source_service="schwab-1m-v2", produced_at=now,
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=account,
            symbol=draft.symbol, side=draft.side, intent_type=draft.intent_type,
            quantity=draft.quantity, reason=draft.reason, metadata=dict(draft.metadata)))


async def composed_wait(monkeypatch):
    strategy, state, milliseconds = setup_wait(
        monkeypatch, strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
    )
    factory = _session_factory()
    service, adapter = _integrated_service(factory, enabled=True, nfq_enabled=True)
    service.settings.strategy_schwab_1m_v2_entry_window_end_hour_et = 15
    service.settings.strategy_schwab_1m_v2_entry_window_end_minute_et = 45
    clock = [datetime.fromtimestamp(milliseconds[0] / 1000, UTC)]
    monkeypatch.setattr(oms, "utcnow", lambda: clock[0])
    monkeypatch.setattr(oms, "_is_regular_market_session", lambda now=None: True)
    monkeypatch.setattr(oms.OmsRiskService, "_market_is_fillable", lambda self, now=None: True)
    assert len(strategy._pending_first_rest_quotes) == 1
    assert not adapter.requests
    primary = fresh(strategy, milliseconds)
    clock[0] = datetime.fromtimestamp(milliseconds[0] / 1000, UTC)
    mirrors = strategy.drain_webull_direct_intents()
    assert len(primary) == len(mirrors) == 1
    event = as_event(mirrors[0], "live:orb", clock[0])
    assert event.payload.metadata["fanout_slot_id"] == primary[0].metadata["fanout_slot_id"]
    seed_segment(factory, event, clock)
    result = await service.process_trade_intent(event)
    assert result[-1].payload.reason == "webull_mirror_no_fresh_quote_held"
    assert not adapter.requests
    journal = FanoutOutcomeJournal(factory)
    journal.bootstrap(strategy.apply_fanout_outcome,
                      active_segments={state.symbol: state.fanout_segment_id}, now=clock[0])
    assert state.webull_resting_active
    assert state.fanout_claim_outcome == "held"
    return strategy, state, milliseconds, service, adapter, event, clock, journal


@pytest.mark.asyncio
@pytest.mark.parametrize("simulated_ending", ["accepted", "filled"])
async def test_rpg_quote_then_nfq_quote_submits_one_mirror_without_new_bar(monkeypatch, simulated_ending):
    strategy, state, milliseconds, service, adapter, event, clock, journal = await composed_wait(monkeypatch)
    if simulated_ending == "accepted":
        async def simulated_working(request):
            _, _, _, _, refusal = WebullBrokerAdapter._apply_resting_mirror_market_shape(
                request=request, order_type="STOP_LIMIT",
                wire_limit=Decimal(request.metadata["limit_price"]),
                wire_stop=Decimal(request.metadata["stop_price"]), metadata={}, now=clock[0])
            assert refusal is None
            adapter.requests.append(request)
            return [ExecutionReport(event_type="accepted", origin="broker",
                client_order_id=request.client_order_id, broker_order_id="simulated-not-real",
                symbol=request.symbol, side="buy", intent_type="open", quantity=request.quantity,
                metadata=dict(request.metadata))]
        monkeypatch.setattr(adapter, "submit_order", simulated_working)
    bars = len(state.bars)
    set_quote(service, event, clock)
    for _ in range(5):
        assert fresh(strategy, milliseconds) == []
        assert not strategy._pending_webull_direct_intents
        await service._evaluate_nfq_holds(state.symbol)
    retries = queued_events(service)
    assert len(retries) == 1
    assert not adapter.requests  # Tick callback queues; only the serial lane sends.
    await service._handle_stream_message({"data": retries[0].model_dump_json()})
    await service._handle_stream_message({"data": retries[0].model_dump_json()})
    journal.poll(strategy.apply_fanout_outcome)
    assert len(adapter.requests) == 1
    assert len(state.bars) == bars
    assert state.webull_resting_active
    if simulated_ending == "accepted":
        assert state.resting_active and state.fanout_claim_outcome == "held"
    else:
        assert not state.resting_active
        assert state.fanout_claim_outcome == "filled"


@pytest.mark.asyncio
async def test_nfq_window_giveup_tells_waiting_strategy_only_webull_is_released(monkeypatch):
    strategy, state, _, service, adapter, _, clock, journal = await composed_wait(monkeypatch)
    clock[0] = clock[0].replace(hour=19, minute=45)
    await service._evaluate_nfq_holds()
    journal.poll(strategy.apply_fanout_outcome)
    assert not adapter.requests
    assert not state.webull_resting_active
    assert not state.fanout_webull_claimed
    assert state.resting_active and state.resting_is_broker_order


@pytest.mark.asyncio
async def test_rpg_bound_cancel_prevents_already_queued_nfq_mirror(monkeypatch):
    strategy, state, _, service, adapter, event, clock, _ = await composed_wait(monkeypatch)
    set_quote(service, event, clock)
    await service._evaluate_nfq_holds(state.symbol)
    retry = queued_events(service)[0]
    strategy._queue_resting_cancel(state, reason="reprice")
    cancels = strategy.drain_webull_direct_intents()
    assert len(cancels) == 1 and cancels[0].intent_type == "cancel"
    service._nfq_observe_intent(as_event(cancels[0], "live:orb", clock[0]))
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert not adapter.requests
    assert not service._nfq_holds
