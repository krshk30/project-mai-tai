"""Actual RPG handoff + NFQ production lanes on their rebased combined source.

Recorded parent reads are from RPG fixtures. Cache delivery, cancellation timing,
and replacement acceptances are controlled simulator scenarios, not live evidence.
"""
import json

import pytest
from sqlalchemy import delete

from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.events import TradeIntentEvent
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal
from tests.unit.test_rpg1_runtime import runtime, begin, feedback, tick_clock


def nfq_enable(h):
    h.service.settings.oms_v2_webull_mirror_fresh_price_enabled = True
    FanoutSegmentIdentityStore(h.factory).record(h.state.symbol, h.state.fanout_segment_id,
        True, "CONTROLLED runtime composition", now=h.clock[0])


async def queue_price(h):
    h.service._latest_quotes_by_symbol[h.state.symbol] = {
        "ask": h.state.last_quote.ask_price, "received_at": h.clock[0],
    }
    await h.service._evaluate_nfq_holds(h.state.symbol)
    return [TradeIntentEvent.model_validate(data) for stream, data in h.service.redis.entries
            if data.get("event_type") == "trade_intent" and data["payload"]["metadata"].get("nfq_retry_token")][-1]


@pytest.mark.asyncio
async def test_recorded_cancel_read_then_nfq_hold_reauthorizes_once_and_old_token_cannot_wire(monkeypatch):
    h = await runtime(monkeypatch, "webull")
    nfq_enable(h)
    token, _ = await begin(h, "webull")
    await feedback(h)
    journal = HandoffJournal(h.factory)
    assert journal.read(token)["phase"] == "price_wait"
    assert len(h.adapter.cancels) == len(h.adapter.reads) == 1 and not h.adapter.opens
    queued = await queue_price(h)
    await h.service._handle_stream_message({"data": queued.model_dump_json()})
    assert not h.adapter.opens  # NFQ cannot bypass the RPG claim/current callback.
    tick_clock(h)
    await feedback(h)
    assert journal.read(token)["phase"] == "placed"
    assert len(h.adapter.opens) == 1
    for _ in range(3):
        await h.service._handle_stream_message({"data": queued.model_dump_json()})
        await h.service._handle_stream_message({"data": json.dumps({"event_type": "atr_reprice_tick", "token": str(token)})})
    assert len(h.adapter.opens) == 1 and not h.service._nfq_holds


@pytest.mark.asyncio
async def test_v2_reprice_of_unsubmitted_nfq_generation_never_invents_broker_cancel(monkeypatch):
    h = await runtime(monkeypatch, "webull")
    nfq_enable(h)
    with h.factory() as session:
        session.execute(delete(BrokerOrder))  # Controlled no-wire starting ledger.
        session.commit()
    await h.service._handle_stream_message({"data": h.opening.model_dump_json()})
    old_retry = await queue_price(h)
    assert not h.adapter.opens
    token, _ = await begin(h, "webull")
    job = HandoffJournal(h.factory).read(token)
    assert job["local_no_wire"] and job["reason"] == "nfq_proven_no_wire"
    await h.service._handle_stream_message({"data": old_retry.model_dump_json()})
    assert not h.adapter.opens and not h.adapter.cancels and not h.adapter.reads
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "placed"
    assert len(h.adapter.opens) == 1
    await h.service._handle_stream_message({"data": old_retry.model_dump_json()})
    assert len(h.adapter.opens) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["buy_flip", "1545"])
async def test_nfq_queued_price_wait_cannot_resurrect_after_current_gate_ends(monkeypatch, gate):
    h = await runtime(monkeypatch, "webull")
    nfq_enable(h)
    token, _ = await begin(h, "webull")
    await feedback(h)
    queued = await queue_price(h)
    tick_clock(h)
    if gate == "buy_flip":
        h.state.atr_state = "long"
    else:
        h.service.settings.strategy_schwab_1m_v2_entry_window_end_hour_et = 15
        h.service.settings.strategy_schwab_1m_v2_entry_window_end_minute_et = 45
        h.clock[0] = h.clock[0].replace(hour=19, minute=45)
    await feedback(h)
    await h.service._handle_stream_message({"data": queued.model_dump_json()})
    assert HandoffJournal(h.factory).read(token)["phase"] == "expired"
    assert not h.adapter.opens
