"""Additional piece-3 scenarios; all transport races/acceptances are simulators.

Recorded parent bodies are unchanged for terminal evidence. Unrecorded working,
error and successor-order variants are explicitly controlled projections.
"""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullAccountConfig, WebullBrokerAdapter
from project_mai_tai.db.models import BrokerOrder, Fill, OmsManagedPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal
from tests.unit.test_rpg1_runtime import runtime, begin, feedback, tick_clock


async def emit_reprice(h, broker):
    h.strategy._cw_v2_resting_track(h.state, None)
    primary, mirror = h.strategy.drain_pending_intents(), h.strategy.drain_webull_direct_intents()
    draft, = primary if broker == "schwab" else mirror
    event = TradeIntentEvent(source_service="schwab-1m-v2", produced_at=h.clock[0],
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2",
            broker_account_name=h.old.broker_account_name, symbol=draft.symbol,
            side="buy", intent_type="cancel", quantity=draft.quantity,
            reason=draft.reason, metadata=draft.metadata))
    await h.service._handle_stream_message({"data": event.model_dump_json()})
    return event


def simulated_book(h, monkeypatch):
    """Assert no overlap at each simulated wire, not only in the ending ledger."""
    live = {h.old.client_order_id}
    history = []
    submit, read = h.adapter.submit_order, h.adapter.read_atr_resting_buy_after_cancel

    async def wire(request):
        history.append((request.intent_type, request.client_order_id, h.clock[0]))
        if request.intent_type == "open" and request.side == "buy":
            assert not live, "SIMULATOR detected two simultaneous BUY parents"
            live.add(request.client_order_id)
        reports = await submit(request)
        if request.intent_type == "open" and request.side == "buy":
            reports = [replace(r, broker_order_id=f"SIMULATED-{request.client_order_id}") for r in reports]
        return reports

    async def detail(request):
        result = await read(request)
        history.append(("read", request.client_order_id, h.clock[0]))
        if result.can_replace:
            live.discard(request.client_order_id)
        return result

    monkeypatch.setattr(h.adapter, "submit_order", wire)
    monkeypatch.setattr(h.adapter, "read_atr_resting_buy_after_cancel", detail)
    return live, history


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_working_pending_cancel_waits_then_recorded_zero_replaces_without_overlap(monkeypatch, broker):
    h = await runtime(monkeypatch, broker)
    lines = []
    monkeypatch.setattr(h.service.logger, "info", lambda fmt, *args: lines.append(fmt % args))
    live, history = simulated_book(h, monkeypatch)
    recorded = deepcopy(h.adapter.body)
    parent = h.adapter.body if broker == "schwab" else h.adapter.body["items"][0]
    parent["status" if broker == "schwab" else "order_status"] = "PENDING_CANCEL"  # CONTROLLED variant.
    token, event = await begin(h, broker)
    for _ in range(3):
        await h.service._handle_stream_message({"data": event.model_dump_json()})
    assert len(h.adapter.reads) == 1
    for _ in range(3):
        tick_clock(h)
        await h.service._handle_stream_message({"data": event.model_dump_json()})
        await feedback(h)
        assert live == {h.old.client_order_id} and not h.adapter.opens
    h.adapter.body = recorded
    tick_clock(h)
    await h.service._handle_stream_message({"data": event.model_dump_json()})
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "placed"
    assert len(h.adapter.cancels) == len(h.adapter.opens) == len(live) == 1
    assert [kind for kind, _, _ in history] == ["cancel", *(["read"] * 5), "open"]
    placed = next(line for line in lines if "phase=placed" in line)
    assert f"account={h.old.broker_account_name}" in placed
    assert "cancel_to_read_ms=4000.0" in placed and "cancel_to_report_ms=4000.0" in placed
    assert "timing=local_observation" in placed  # Simulated time, not a venue-latency claim.


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("fault", ["http", "exception", "shape", "missing_fill"])
async def test_unknown_cap_notifies_v2_once_even_after_replay_and_restart(monkeypatch, broker, fault):
    h = await runtime(monkeypatch, broker)
    body = deepcopy(h.adapter.body)
    parent = body if broker == "schwab" else body["items"][0]
    if fault == "shape":
        body["orderId" if broker == "schwab" else "client_order_id"] = "CONTROLLED wrong parent"
    if fault == "missing_fill":
        parent.pop("filledQuantity" if broker == "schwab" else "filled_qty")
    cls = SchwabBrokerAdapter if broker == "schwab" else WebullBrokerAdapter
    reader = object.__new__(cls)
    config = SchwabAccountConfig("SIMULATED account") if broker == "schwab" else WebullAccountConfig("SIMULATED account")
    reader.accounts_by_name = {h.old.broker_account_name: config}
    calls = []

    def transport(*args, **kwargs):
        calls.append(h.clock[0])
        if fault == "exception":
            raise TimeoutError("CONTROLLED transport failure, no network")
        return (503 if fault == "http" else 200), body

    async def schwab_transport(*args, **kwargs):
        status, answer = transport(*args, **kwargs)
        return status, {}, answer

    monkeypatch.setattr(reader, "_authorized_request_json" if broker == "schwab" else "_atr_buy_order_detail",
                        schwab_transport if broker == "schwab" else transport)
    monkeypatch.setattr(h.adapter, "read_atr_resting_buy_after_cancel", reader.read_atr_resting_buy_after_cancel)
    lines = []
    monkeypatch.setattr(h.service.logger, "info", lambda fmt, *args: lines.append(fmt % args))
    token, event = await begin(h, broker)
    for _ in range(30):
        tick_clock(h)
        await h.service._handle_stream_message({"data": event.model_dump_json()})
    h.service.__dict__.pop("_atr_reprice_controller")
    for _ in range(3):
        await h.service._handle_stream_message({"data": event.model_dump_json()})
    h.strategy._rpg_handoffs.clear()  # Fresh v2 process must learn ownership from the journal.
    await feedback(h)
    assert len(calls) == 30 and len(h.adapter.cancels) == 1 and not h.adapter.opens
    assert all((b - a).total_seconds() >= 1 for a, b in zip(calls, calls[1:]))
    assert HandoffJournal(h.factory).read(token)["phase"] == "held_unknown"
    assert h.strategy._rpg_handoffs[str(token)]["phase"] == "held_unknown"
    assert h.strategy._rpg_entry_owned(h.state)
    terminal = [line for line in lines if f"token={token}" in line and "phase=held_unknown" in line]
    assert len(terminal) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("broker,total,partial", [("schwab", 197, 31), ("webull", 98, 17)])
@pytest.mark.parametrize("full", [False, True])
async def test_controlled_raw_fill_race_books_original_position_without_remainder(monkeypatch, broker, total, partial, full):
    h = await runtime(monkeypatch, broker, quantity=total, amod=broker == "schwab")
    filled = total if full else partial
    # No recording of these dollar-size races exists. Explicit raw-body variants
    # retain the real exact parent identity but project quantity/fill/status.
    if broker == "schwab":
        h.adapter.body.update(quantity=total, filledQuantity=filled,
            remainingQuantity=0, status="FILLED" if full else "CANCELED")
        h.adapter.body["orderLegCollection"][0]["quantity"] = total
        h.adapter.body["orderActivityCollection"] = [{"executionType": "FILL",
            "executionLegs": [{"quantity": filled, "price": 3.05}]}]
        h.adapter.body.pop("childOrderStrategies")  # No invented native child scaling evidence.
    else:
        h.adapter.body["items"][0].update(qty=str(total), filled_qty=str(filled),
            filled_price="3.05", order_status="FILLED" if full else "CANCELLED")
    token, event = await begin(h, broker)
    await feedback(h)
    await h.service._handle_stream_message({"data": event.model_dump_json()})
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "filled" and job["no_rebuy"] and h.state.cw_resting_taken
    assert not h.adapter.opens
    with h.factory() as session:
        fills = list(session.scalars(select(Fill)))
        assert len(fills) == 1 and fills[0].quantity == Decimal(filled)
        assert session.scalar(select(OmsManagedPosition)).current_quantity == Decimal(filled)


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("second_before_clear", [False, True])
async def test_two_reprices_same_minute_keep_one_latest_order_and_new_attempt(monkeypatch, broker, second_before_clear):
    h = await runtime(monkeypatch, broker)
    live, history = simulated_book(h, monkeypatch)
    token, first_cancel = await begin(h, broker)
    if not second_before_clear:
        await feedback(h)
        await feedback(h)  # Accepted replacement restores its actual generation.
        first_open, = h.adapter.opens
        # CONTROLLED successor identity projection: there is no recorded future
        # replacement. Identity/quantity follow the simulated accepted request;
        # terminal-zero/status and other shape fields derive from the fixture.
        if broker == "schwab":
            h.adapter.body["orderId"] = f"SIMULATED-{first_open.client_order_id}"
            h.adapter.body["quantity"] = float(first_open.quantity)
            h.adapter.body["orderLegCollection"][0]["quantity"] = float(first_open.quantity)
        else:
            h.adapter.body["client_order_id"] = first_open.client_order_id
            h.adapter.body["order_id"] = f"SIMULATED-{first_open.client_order_id}"
            h.adapter.body["items"][0]["qty"] = str(first_open.quantity)
    tick_clock(h, 2)
    h.state.atr_trail = 2.99
    if not second_before_clear:
        await emit_reprice(h, broker)
    else:
        h.strategy._cw_v2_resting_track(h.state, None)
        assert not h.strategy.drain_pending_intents() and not h.strategy.drain_webull_direct_intents()
    await feedback(h)
    await feedback(h)
    await h.service._handle_stream_message({"data": first_cancel.model_dump_json()})
    expected = 1 if second_before_clear else 2
    assert len(h.adapter.opens) == len(h.adapter.cancels) == expected
    latest = h.adapter.opens[-1]
    assert latest.metadata["stop_price"] == "3.0050"
    assert live == {latest.client_order_id}
    assert len({r.metadata["fanout_attempt_id"] for r in h.adapter.opens}) == expected
    assert all(r.metadata["fanout_slot_id"] == h.opening.payload.metadata["fanout_slot_id"] for r in h.adapter.opens)
    assert (history[-1][2] - history[0][2]).total_seconds() < 60
    with h.factory() as session:
        active = list(session.scalars(select(BrokerOrder).where(BrokerOrder.status == "accepted")))
        assert len(active) == 1 and active[0].client_order_id == latest.client_order_id


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_stale_replacement_resumes_on_actual_quote_callback_without_bar(monkeypatch, broker):
    h = await runtime(monkeypatch, broker)
    token, _ = await begin(h, broker)
    h.state.last_quote = replace(h.state.last_quote, quote_time_ms=h.strategy._now_ms() - 17000)
    await feedback(h)
    assert not h.adapter.opens
    bars = len(h.state.bars)
    h.bot._last_tick_at, h.bot._last_quote_at_ms, h.bot._last_quote_by_symbol = {}, {}, {}
    h.bot._gap_hold_enabled = False
    h.bot._observe_halt_from_quote = lambda *_: None
    h.bot._maybe_emit = AsyncMock()
    h.bot._emit_webull_fanout_legs = AsyncMock()
    h.bot.intent_emitter = h.bot.webull_intent_emitter = None
    tick_clock(h)
    await h.bot._handle_quote(h.state.symbol, h.state.last_quote)
    ticks = [data for _, data in h.service.redis.entries if data.get("event_type") == "atr_reprice_tick"]
    assert ticks
    for tick in ticks * 2:
        await h.service._handle_stream_message({"data": json.dumps(tick)})
    assert len(h.adapter.opens) == 1 and len(h.state.bars) == bars
    assert HandoffJournal(h.factory).read(token)["phase"] == "placed"
