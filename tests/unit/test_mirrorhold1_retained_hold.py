"""Recorded Step0 prices/logs; SDK replies, delivery, and crashes are CONTROLLED.

The real OMS/adapter submit path uses a fake SDK endpoint. No acceptance or fill
in this file is an observed Webull execution, including IPDN's earlier segments.
"""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.broker_adapters import webull
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, TradeIntent
from project_mai_tai.events import TradeIntentEvent
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms import service as oms
from project_mai_tai.oms.mirror_retained_hold import price_generation, row_id
from project_mai_tai.oms.mirror_fresh_price import SNAPSHOT_TYPE as NFQ_SNAPSHOT
from project_mai_tai.settings import Settings
from tests.unit.test_oms_webull_mirror_deferred_resubmit import _integrated_service, _mirror_event
from tests.unit.test_rpg1_runtime import _session_factory
from tests.unit.test_webull_adapter import fake_sdk as _fake_sdk, _adapter, _FakeClient, _ServerException


RECORDED = json.loads((Path(__file__).parents[1] / "fixtures/mirrorhold1_recorded_step0.json").read_text())


@pytest.fixture
def lane(monkeypatch):
    _fake_sdk.__wrapped__(monkeypatch)
    factory = _session_factory()
    service, _ = _integrated_service(factory, enabled=True, nfq_enabled=True)
    service.settings.oms_v2_webull_mirror_retained_hold_enabled = True
    clock = [datetime(2026, 10, 6, 14, 27, 4, tzinfo=UTC)]
    monkeypatch.setattr(oms, "utcnow", lambda: clock[0])
    monkeypatch.setattr(oms.OmsRiskService, "_market_is_fillable", lambda self, now=None: True)

    class ControlledClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(webull, "datetime", ControlledClock)
    client = _FakeClient({"place": {"client_order_id": "CONTROLLED ACK"}, "positions": [], "today": []})
    adapter = _adapter(client, settings=service.settings)
    adapter._instrument_cache.update(IPDN="CONTROLLED-IPDN", AIXI="CONTROLLED-AIXI", XHG="CONTROLLED-XHG", MEDS="CONTROLLED-MEDS")
    service.broker_adapter = adapter
    return service, client, factory, clock


def event_for(lane, *, symbol="IPDN", segment="1791296823073", stop="5.3502", quantity="1"):
    service, _, factory, clock = lane
    event = _mirror_event(segment_id=segment, slot_id="controlled-slot-" + segment)
    event.produced_at = clock[0]
    event.payload.symbol = symbol
    event.payload.quantity = Decimal(quantity)
    event.payload.metadata.update(stop_price=stop, limit_price=str(Decimal(stop) * Decimal("1.005")),
                                  reference_price=stop, cw_entry_slot="first", rpg_resting_generation=str(uuid4()),
                                  webull_mirror_generation_id=str(uuid4()))
    FanoutSegmentIdentityStore(factory).record(symbol, int(segment), True, "CONTROLLED recorded segment binding", now=clock[0])
    return event


def quote(lane, event, price, *, age=0):
    service, _, _, clock = lane
    service._latest_quotes_by_symbol[event.payload.symbol] = {
        "ask": Decimal(price), "received_at": clock[0] - timedelta(seconds=age),
    }


def state(lane, event):
    with lane[2]() as session:
        row = session.get(DashboardSnapshot, row_id(event))
        return dict(row.payload) if row is not None else None


def queued(lane):
    return [TradeIntentEvent.model_validate(data) for stream, data in lane[0].redis.entries
            if stream.endswith("strategy-intents")]


async def hold_and_queue(lane):
    service, client, _, clock = lane
    event = event_for(lane)
    quote(lane, event, "4.7")
    await service.process_trade_intent(event)
    assert client.calls.get("place", 0) == 0
    clock[0] += timedelta(seconds=1)
    quote(lane, event, "5.0")
    await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    return event, queued(lane)[-1]


def restart(lane):
    service, client, factory, clock = lane
    fresh, _ = _integrated_service(factory, enabled=True, nfq_enabled=True)
    fresh.settings = service.settings
    fresh.broker_adapter = service.broker_adapter
    fresh._restore_mirrorhold()
    return fresh, client, factory, clock


def test_default_off_and_historical_cancel_boundary():
    assert Settings().oms_v2_webull_mirror_retained_hold_enabled is False
    assert RECORDED["boundaries"]["AIXI_0957_terminal_cancel"].startswith("UNMEASURED")
    v2 = next(r for r in RECORDED["bounded_timeline"] if r["path"].endswith("schwab-1m-v2.log"))
    aixi = [r["raw"] for r in v2["rows"] if "AIXI" in r["raw"]]
    assert any("13:56" in r and "CANCEL" in r and "reason=reprice" in r for r in aixi)
    assert any("13:57" in r and "PLACE" in r for r in aixi)
    assert not any("13:57" in r and "CANCEL" in r for r in aixi)


@pytest.mark.asyncio
async def test_recorded_ipdn_1027_proxy_steps_survive_until_1048(lane):
    service, client, _, clock = lane
    rows = [r for r in RECORDED["capture_proxies"]["rows"] if r["segment"] == "1791296823073"]
    assert len(rows) == 9
    first = None
    generations = []
    for index, row in enumerate(rows):
        clock[0] = datetime.fromisoformat(row["created_at"])
        event = event_for(lane, stop=row["stop"])
        first = first or event
        service._latest_quotes_by_symbol[event.payload.symbol] = {
            "ask": Decimal(row["ask_price"]), "received_at": datetime.fromisoformat(row["quote_at"]),
        }
        # Projection is a controlled cache replay; captured received_at is retained
        # in the fixture but is not claimed to be the historical OMS cache clock.
        result = await service.process_trade_intent(event)
        generations.append(state(lane, event)["price_generation"])
        if index < 8:
            assert client.calls.get("place", 0) == 0
            assert state(lane, event)["phase"] == "held"
            assert state(lane, event)["wire_submissions"] == 0
            for reason in ("rpg_reauthorizes_price_wait", "rpg_proven_no_wire", "new_mirror_attempt"):
                service._forget_webull_mirror_deferred(event.payload.metadata["fanout_slot_id"], reason=reason)
                assert state(lane, first)["phase"] == "held"
        else:
            assert row["stop"] == "4.73" and Decimal(row["ask_price"]) == Decimal("4.38")
            assert result[-1].payload.status == "accepted"
    assert client.calls["place"] == 1  # CONTROLLED SDK ACK, NOT observed Webull execution.
    assert state(lane, first)["phase"] == "accepted"
    assert state(lane, first)["wire_submissions"] == 1
    assert len(set(generations)) == 9


@pytest.mark.asyncio
async def test_recorded_ipdn_1155_outside_8375_holds_until_next_sample(lane):
    service, client, _, clock = lane
    clock[0] = datetime(2026, 10, 6, 15, 55, 12, tzinfo=UTC)
    original = event_for(lane, segment="1791301442514", stop="4.5839")
    quote(lane, original, "4.2")
    for attempt in range(8):
        event = original.model_copy(deep=True)
        event.event_id = uuid4()
        event.produced_at = clock[0]
        event.payload.metadata["webull_deferred_resubmit_attempt"] = str(attempt)
        await service.process_trade_intent(event)
        assert state(lane, original)["wire_submissions"] == 0
        assert state(lane, original)["phase"] == "held"
        clock[0] += timedelta(milliseconds=100)
    assert client.calls.get("place", 0) == 0
    row = next(r for r in RECORDED["capture_proxies"]["rows"] if r["segment"] == "1791301442514" and r["stop"] == "4.54")
    clock[0] = datetime.fromisoformat(row["created_at"])
    next_price = event_for(lane, segment=original.payload.metadata["fanout_segment_id"], stop=row["stop"])
    quote(lane, next_price, row["ask_price"])
    await service.process_trade_intent(next_price)
    assert client.calls["place"] == 1
    assert state(lane, original)["phase"] == "accepted"


@pytest.mark.asyncio
async def test_queue_and_duplicate_copies_spend_only_one_actual_wire(lane):
    service, client, _, _ = lane
    event, retry = await hold_and_queue(lane)
    for _ in range(6):
        await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    assert len(queued(lane)) == 1 and state(lane, event)["wire_submissions"] == 0
    for _ in range(4):
        await service._handle_stream_message({"data": retry.model_dump_json()})
    assert client.calls["place"] == 1
    assert state(lane, event)["wire_submissions"] == 1


@pytest.mark.asyncio
async def test_three_actual_resubmissions_cap_survives_reprice_and_restart(lane):
    service, client, _, clock = lane
    event = event_for(lane)
    client.raises["place"] = _ServerException("ORDER_RISK_RULE_PRICE_AGGRESSIVE", "CONTROLLED broker refusal", 417)
    for number in range(4):
        event = event_for(lane, stop=str(Decimal("5.3502") - Decimal(number) / 100))
        event.payload.metadata["webull_deferred_resubmit_attempt"] = "700"  # Not the wire budget.
        quote(lane, event, "5.1")
        await service.process_trade_intent(event)
        assert state(lane, event)["wire_submissions"] == number + 1
        clock[0] += timedelta(seconds=1)
        lane = restart(lane)
        service = lane[0]
    assert state(lane, event)["phase"] == "capped"
    for _ in range(5):
        again = event_for(lane, stop="5.2")
        quote(lane, again, "5.1")
        await service.process_trade_intent(again)
        await service._evaluate_webull_mirror_deferred_resubmits(again.payload.symbol)
    assert client.calls["place"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["buy_flip", "window_closed", "segment_end"])
async def test_controlled_actual_terminal_cancel_fences_queued_generation(lane, reason):
    service, client, _, clock = lane
    event, retry = await hold_and_queue(lane)
    cancel = event.model_copy(deep=True)
    cancel.event_id = uuid4()
    cancel.produced_at = clock[0]
    cancel.payload.intent_type = "cancel"
    cancel.payload.metadata.update(resting_entry_cancel="true", reason=reason)
    service._observe_webull_mirror_deferred_intent(cancel)
    assert state(lane, event)["phase"] == "retired"
    lane = restart(lane)
    quote(lane, event, "5.2")
    await lane[0]._handle_stream_message({"data": retry.model_dump_json()})
    await lane[0]._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    await lane[0].process_trade_intent(event)
    assert client.calls.get("place", 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["account", "generation", "slot", "segment", "reprice"])
async def test_foreign_stale_or_reprice_cancel_does_not_retire_current_hold(lane, change):
    service, _, _, clock = lane
    event, _ = await hold_and_queue(lane)
    cancel = event.model_copy(deep=True)
    cancel.event_id = uuid4()
    cancel.produced_at = clock[0]
    cancel.payload.intent_type = "cancel"
    cancel.payload.metadata["reason"] = "buy_flip"
    if change == "account":
        cancel.payload.broker_account_name = "live:schwab_1m_v2"
    elif change == "generation":
        cancel.payload.metadata["rpg_resting_generation"] = "stale"
    elif change == "slot":
        cancel.payload.metadata["fanout_slot_id"] = "stale"
    elif change == "segment":
        cancel.payload.metadata["fanout_segment_id"] = "1791290000000"
    else:
        cancel.payload.metadata.update(reason="reprice", atr_reprice="true")
    service._observe_webull_mirror_deferred_intent(cancel)
    assert state(lane, event)["phase"] == "queued"


@pytest.mark.asyncio
async def test_restart_invalidates_queue_but_retains_free_hold(lane):
    service, client, _, _ = lane
    event, old = await hold_and_queue(lane)
    lane = restart(lane)
    service = lane[0]
    assert state(lane, event)["phase"] == "held"
    quote(lane, event, "5.1")
    await service._handle_stream_message({"data": old.model_dump_json()})
    assert not client.calls.get("place")
    await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    new = queued(lane)[-1]
    await service._handle_stream_message({"data": new.model_dump_json()})
    await service._handle_stream_message({"data": old.model_dump_json()})
    assert client.calls["place"] == 1


@pytest.mark.asyncio
async def test_enqueue_ack_loss_invalidates_old_copy_without_losing_hold(lane, monkeypatch):
    service, client, _, clock = lane
    event = event_for(lane)
    quote(lane, event, "4.7")
    await service.process_trade_intent(event)
    clock[0] += timedelta(seconds=1)
    quote(lane, event, "5.1")
    original = service.redis.xadd

    async def lost_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise ConnectionError("CONTROLLED append succeeded, ACK lost")

    monkeypatch.setattr(service.redis, "xadd", lost_ack)
    await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    old = queued(lane)[-1]
    assert state(lane, event)["phase"] == "held" and state(lane, event)["wire_submissions"] == 0
    monkeypatch.setattr(service.redis, "xadd", original)
    await service._handle_stream_message({"data": old.model_dump_json()})
    assert client.calls.get("place", 0) == 0
    await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    new = queued(lane)[-1]
    for retry in (old, new, old, new):
        await service._handle_stream_message({"data": retry.model_dump_json()})
    assert client.calls["place"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport_after_post", "crash_after_report"])
async def test_unknown_wire_stays_addressable_without_second_post_after_restart(lane, monkeypatch, failure):
    service, client, factory, _ = lane
    event = event_for(lane)
    quote(lane, event, "5.1")
    if failure == "transport_after_post":
        client.raises["place"] = TimeoutError("CONTROLLED server may have accepted")
        await service.process_trade_intent(event)
    else:
        original = service._record_order_reports

        async def crash(**kwargs):
            await original(**kwargs)
            raise RuntimeError("CONTROLLED crash before report transaction commits")

        monkeypatch.setattr(service, "_record_order_reports", crash)
        with pytest.raises(RuntimeError):
            await service.process_trade_intent(event)
    lane = restart(lane)
    assert state(lane, event)["phase"] == "uncertain"
    with factory() as session:
        order = session.scalar(select(BrokerOrder))
        assert order.client_order_id == state(lane, event)["dispatch_client"]
    client.raises.clear()
    for _ in range(5):
        again = event_for(lane, stop="5.3")
        quote(lane, again, "5.1")
        await lane[0].process_trade_intent(again)
        await lane[0]._evaluate_webull_mirror_deferred_resubmits(again.payload.symbol)
    assert client.calls["place"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["token", "quantity", "price"])
async def test_changed_serial_claim_cannot_wire(lane, field):
    service, client, _, _ = lane
    _, retry = await hold_and_queue(lane)
    if field == "quantity":
        retry.payload.quantity += 1
    elif field == "token":
        retry.payload.metadata["mirrorhold_token"] = "foreign"
    else:
        retry.payload.metadata["stop_price"] = "5.0"
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert client.calls.get("place", 0) == 0


def test_stale_revision_cannot_replace_generation(lane):
    service, _, factory, _ = lane
    event = event_for(lane)
    service._observe_webull_mirror_deferred_intent(event)
    with factory() as stale, factory() as current:
        old = service._mirrorhold_read(stale, event)
        new = service._mirrorhold_read(current, event)
        service._mirrorhold_write(current, new, {**new.payload, "reason": "CONTROLLED concurrent revision"})
        current.commit()
        with pytest.raises(RuntimeError, match="stale revision"):
            service._mirrorhold_write(stale, old, {**old.payload, "phase": "dispatching"})


def test_wire_price_hash_is_not_raw_precision_or_reauth_nonce(lane):
    event = event_for(lane, stop="5.3502")
    event.payload.metadata["limit_price"] = "5.38"
    same_wire = event.model_copy(deep=True)
    same_wire.payload.metadata.update(stop_price="5.3549", rpg_handoff_token="different nonce")
    assert price_generation(event) == price_generation(same_wire)
    changed_wire = event.model_copy(deep=True)
    changed_wire.payload.metadata["stop_price"] = "5.36"
    assert price_generation(event) != price_generation(changed_wire)


@pytest.mark.asyncio
@pytest.mark.parametrize("off,terminal_cancel", [(False, False), (True, False), (True, True)])
async def test_unknown_dispatch_blocks_new_segment_even_after_off_or_terminal_cancel(lane, off, terminal_cancel):
    service, client, factory, clock = lane
    event = event_for(lane)
    quote(lane, event, "5.1")
    client.raises["place"] = TimeoutError("CONTROLLED unknown wire")
    await service.process_trade_intent(event)
    if terminal_cancel:
        cancel = event.model_copy(deep=True)
        cancel.event_id = uuid4()
        cancel.produced_at = clock[0]
        cancel.payload.intent_type = "cancel"
        cancel.payload.metadata["reason"] = "buy_flip"
        service._observe_webull_mirror_deferred_intent(cancel)
    if off:
        service.settings.oms_v2_webull_mirror_retained_hold_enabled = False
    lane = restart(lane)
    with factory() as session:
        order = session.scalar(select(BrokerOrder))
        assert order.status == "pending"
        assert session.get(TradeIntent, order.intent_id).status == "submitted"
    clock[0] += timedelta(minutes=1)
    client.raises.clear()
    next_segment = event_for(lane, segment="1791298000000", stop="5.3")
    quote(lane, next_segment, "5.1")
    await lane[0].process_trade_intent(next_segment)
    assert client.calls["place"] == 1
    assert state(lane, event)["dispatch_unresolved"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "accepted", "partially_filled", "filled"])
async def test_xhg_primary_outcome_cannot_retire_webull_leg(lane, outcome):
    service, client, _, _ = lane
    event = event_for(lane, symbol="XHG", segment="1791297123018", stop="3.0618")
    quote(lane, event, "2.5")
    await service.process_trade_intent(event)
    primary = event.model_copy(deep=True)
    primary.payload.broker_account_name = "live:schwab_1m_v2"
    primary.payload.metadata.pop("fanout_leg")
    report = ExecutionReport(outcome, "CONTROLLED primary client", symbol="XHG", quantity=Decimal("1"),
                             origin="broker", reason="CONTROLLED sibling outcome")
    with lane[2]() as session:
        service._observe_webull_mirror_deferred_reports(event=primary, reports=[report], session=session)
        session.commit()
    assert state(lane, event)["phase"] == "held"
    quote(lane, event, "2.9")
    await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    await service._handle_stream_message({"data": queued(lane)[-1].model_dump_json()})
    assert client.calls["place"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["partially_filled", "filled"])
async def test_mirror_fill_is_one_leg_terminal_and_cannot_rebuy(lane, outcome):
    service, client, factory, _ = lane
    event, retry = await hold_and_queue(lane)
    await service._handle_stream_message({"data": retry.model_dump_json()})
    report = ExecutionReport(outcome, state(lane, event)["dispatch_client"], symbol=event.payload.symbol,
        quantity=retry.payload.quantity, origin="broker", filled_quantity=Decimal("0.5"),
        fill_price=Decimal("5.1"), broker_fill_id="CONTROLLED fill identity")
    with factory() as session:
        reported = TradeIntentEvent.model_validate(state(lane, event)["event"])
        service._mirrorhold_reports(session, reported, [report])
        session.commit()
    assert state(lane, event)["phase"] == "filled"
    lane = restart(lane)
    for _ in range(3):
        quote(lane, event, "5.1")
        await lane[0]._handle_stream_message({"data": retry.model_dump_json()})
        await lane[0].process_trade_intent(event_for(lane, stop="5.3"))
    assert client.calls["place"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["account", "quantity", "client", "generation", "origin"])
async def test_foreign_report_cannot_settle_dispatch_reservation(lane, monkeypatch, change):
    service, client, factory, _ = lane
    event = event_for(lane)
    quote(lane, event, "5.1")
    client.raises["place"] = TimeoutError("CONTROLLED post uncertainty")
    await service.process_trade_intent(event)
    reported = event.model_copy(deep=True)
    report = ExecutionReport("accepted", state(lane, event)["dispatch_client"], symbol=event.payload.symbol,
                             quantity=event.payload.quantity, origin="broker")
    if change == "account":
        reported.payload.broker_account_name = "foreign:account"
    elif change == "quantity":
        reported.payload.quantity += 1
        report = replace(report, quantity=reported.payload.quantity)
    elif change == "client":
        report = replace(report, client_order_id="foreign-client")
    elif change == "generation":
        reported.payload.metadata["mirrorhold_dispatch_generation"] = "foreign"
    else:
        report = replace(report, origin="client")
    with factory() as session:
        service._mirrorhold_reports(session, reported, [report])
        session.commit()
    assert state(lane, event)["phase"] == "uncertain"


@pytest.mark.asyncio
async def test_late_exact_broker_acceptance_settles_uncertainty_without_new_wire(lane):
    service, client, factory, _ = lane
    event = event_for(lane)
    quote(lane, event, "5.1")
    client.raises["place"] = TimeoutError("CONTROLLED ACK lost")
    await service.process_trade_intent(event)
    report = ExecutionReport("accepted", state(lane, event)["dispatch_client"], symbol=event.payload.symbol,
                             quantity=event.payload.quantity, origin="broker")
    with factory() as session:
        service._mirrorhold_reports(session, event, [report])
        session.commit()
    assert state(lane, event)["phase"] == "accepted"
    assert state(lane, event)["wire_submissions"] == 1
    assert client.calls["place"] == 1


@pytest.mark.asyncio
async def test_late_report_cannot_resurrect_terminally_cancelled_dispatch(lane):
    service, client, factory, clock = lane
    event = event_for(lane)
    quote(lane, event, "5.1")
    client.raises["place"] = TimeoutError("CONTROLLED wire unknown")
    await service.process_trade_intent(event)
    cancel = event.model_copy(deep=True)
    cancel.event_id = uuid4()
    cancel.produced_at = clock[0]
    cancel.payload.intent_type = "cancel"
    cancel.payload.metadata.update(resting_entry_cancel="true", reason="buy_flip")
    service._observe_webull_mirror_deferred_intent(cancel)
    report = ExecutionReport("rejected", state(lane, event)["dispatch_client"], symbol=event.payload.symbol,
        quantity=event.payload.quantity, origin="broker",
        metadata={"webull_error_code": "ORDER_RISK_RULE_PRICE_AGGRESSIVE"})
    with factory() as session:
        service._mirrorhold_reports(session, event, [report])
        session.commit()
    assert state(lane, event)["phase"] == "retired"
    assert state(lane, event)["dispatch_client"] == report.client_order_id


@pytest.mark.asyncio
async def test_existing_wire_budget_is_adopted_not_reset(lane):
    service, client, factory, clock = lane
    event = event_for(lane)
    client.raises["place"] = _ServerException("ORDER_RISK_RULE_PRICE_AGGRESSIVE", "CONTROLLED", 417)
    quote(lane, event, "5.1")
    await service.process_trade_intent(event)
    with factory() as session:
        session.delete(session.get(DashboardSnapshot, row_id(event)))
        session.commit()
    clock[0] += timedelta(seconds=1)
    next_price = event_for(lane, stop="5.3")
    quote(lane, next_price, "5.1")
    await service.process_trade_intent(next_price)
    assert state(lane, next_price)["wire_submissions"] == 2


@pytest.mark.asyncio
async def test_real_adapter_no_wire_freshness_is_free_but_false_marker_is_not(lane, monkeypatch):
    service, client, factory, clock = lane
    event = event_for(lane)
    quote(lane, event, "5.1")
    original = service.broker_adapter._submit_blocking

    def delayed(account, request):
        clock[0] += timedelta(seconds=11)
        return original(account, request)

    monkeypatch.setattr(service.broker_adapter, "_submit_blocking", delayed)
    result = await service.process_trade_intent(event)
    assert not client.calls.get("place")
    assert result[-1].payload.metadata["webull_local_no_wire"] == "true"
    assert state(lane, event)["phase"] == "held" and state(lane, event)["wire_submissions"] == 0
    with factory() as session:
        row = service._mirrorhold_read(session, event)
        service._mirrorhold_write(session, row, {**row.payload, "phase": "dispatching"})
        report = ExecutionReport("rejected", row.payload["dispatch_client"], symbol=event.payload.symbol,
            quantity=event.payload.quantity, origin="client", metadata={"webull_local_no_wire": "false",
            "webull_resting_mirror_shape": "abandoned_no_fresh_quote"})
        service._mirrorhold_reports(session, event, [report])
        session.commit()
    assert state(lane, event)["phase"] == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("all_on", [False, True])
async def test_rpg_nonces_after_many_free_waits_do_not_spend_actual_wire_budget(monkeypatch, all_on):
    from tests.unit.test_rpg1_runtime import runtime, begin, feedback, tick_clock
    from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal
    overrides = None
    if all_on:
        from tests.unit.test_all_on_pm import ALL_ON, completed_seeded_line
        # Explicit controlled completed-line prerequisite, not recorded ATR proof.
        completed_seeded_line.__wrapped__(monkeypatch)
        overrides = ALL_ON
    h = await runtime(monkeypatch, "webull", strategy_overrides=overrides)
    if all_on:
        h.service.settings = h.service.settings.model_copy(update=overrides)
        assert len(overrides) == 12
        assert all(getattr(h.strategy.settings, key) is True and getattr(h.service.settings, key) is True
                   for key in overrides)
    h.service.settings.oms_v2_webull_mirror_retained_hold_enabled = True
    FanoutSegmentIdentityStore(h.factory).record(h.state.symbol, h.state.fanout_segment_id, True,
                                                 "CONTROLLED current RPG segment", now=h.clock[0])
    h.state.last_quote = replace(h.state.last_quote, ask_price=2.5, bid_price=2.49, last_price=2.5)
    h.service._latest_quotes_by_symbol[h.state.symbol] = {"ask": Decimal("2.5"), "received_at": h.clock[0]}
    token, _ = await begin(h, "webull")
    await feedback(h)
    journal = HandoffJournal(h.factory)
    assert journal.read(token)["phase"] == "price_wait"
    for _ in range(7):
        tick_clock(h)
        h.service._latest_quotes_by_symbol[h.state.symbol]["received_at"] = h.clock[0]
        await feedback(h)
        assert journal.read(token)["phase"] == "price_wait"
        assert not h.adapter.opens
    job = journal.read(token)
    current = TradeIntentEvent.model_validate(job["authorization"]["event"])
    local_lane = h.service, None, h.factory, h.clock
    assert job["attempt"] >= 7 and state(local_lane, current)["wire_submissions"] == 1
    original = h.adapter.submit_order
    h.adapter.refusal = "ORDER_RISK_RULE_PRICE_AGGRESSIVE"
    h.adapter.refusal_metadata = {"webull_error_code": "ORDER_RISK_RULE_PRICE_AGGRESSIVE"}

    async def controlled_wire(request):
        reports = await original(request)
        if request.intent_type == "open":
            reports = [replace(r, metadata={**r.metadata, "webull_wire_submitted_at_utc": h.clock[0].isoformat()}) for r in reports]
        return reports

    monkeypatch.setattr(h.adapter, "submit_order", controlled_wire)
    tick_clock(h)
    h.state.last_quote = replace(h.state.last_quote, ask_price=3.02, bid_price=3.01, last_price=3.02)
    h.service._latest_quotes_by_symbol[h.state.symbol] = {"ask": Decimal("3.02"), "received_at": h.clock[0]}
    await feedback(h)
    assert len(h.adapter.opens) == 1
    assert journal.read(token)["phase"] == "price_wait"
    assert state(local_lane, current)["wire_submissions"] == 2
    h.adapter.refusal = None
    tick_clock(h)
    h.service._latest_quotes_by_symbol[h.state.symbol]["received_at"] = h.clock[0]
    await feedback(h)
    assert journal.read(token)["phase"] == "placed"
    assert len(h.adapter.opens) == 2
    assert state(local_lane, current)["wire_submissions"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal,proven", [
    ("rpg_old_buy_still_owned", True),
    ("CONTROLLED authorization changed after reservation", False),
])
async def test_pre_wire_callback_refusal_keeps_free_hold_and_can_retry(lane, monkeypatch, refusal, proven):
    service, client, factory, clock = lane
    event = event_for(lane)
    quote(lane, event, "5.1")
    original = service._rpg_open_refusal

    def after_pending(event, *, session=None):
        if session is not None:
            order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == service._build_client_order_id(event)))
            if order is not None and order.status == "pending":
                # T43's ordinary-entry abort proof accepts this real refusal,
                # not an invented code without a hand-off ticket.
                return refusal
        return original(event, session=session)

    monkeypatch.setattr(service, "_rpg_open_refusal", after_pending)
    await service.process_trade_intent(event)
    assert client.calls.get("place", 0) == 0
    assert state(lane, event)["phase"] == "held"
    assert state(lane, event)["wire_submissions"] == 0
    assert not state(lane, event)["dispatch_unresolved"]
    with factory() as session:
        order = session.scalar(select(BrokerOrder))
        assert service._mirrorhold_clear_order(session, order) is proven
    monkeypatch.setattr(service, "_rpg_open_refusal", original)
    clock[0] += timedelta(seconds=1)
    next_event = event_for(lane, stop="5.3")
    quote(lane, next_event, "5.1")
    await service.process_trade_intent(next_event)
    assert client.calls.get("place", 0) == int(proven)


@pytest.mark.asyncio
async def test_nfq_held_transfer_invalidates_old_queue_without_two_owners(lane):
    service, client, factory, clock = lane
    service.settings.oms_v2_webull_mirror_retained_hold_enabled = False
    event = event_for(lane)
    await service.process_trade_intent(event)
    quote(lane, event, "5.1")
    await service._evaluate_nfq_holds(event.payload.symbol)
    old = queued(lane)[-1]
    service.settings.oms_v2_webull_mirror_retained_hold_enabled = True
    service._restore_mirrorhold()
    assert not service._nfq_holds
    assert state(lane, event)["phase"] == "held"
    with factory() as session:
        row = session.scalar(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == NFQ_SNAPSHOT))
        assert row.payload["phase"] == "retired" and not row.payload["token"]
    await service._handle_stream_message({"data": old.model_dump_json()})
    assert client.calls.get("place", 0) == 0
    await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    await service._handle_stream_message({"data": queued(lane)[-1].model_dump_json()})
    assert client.calls["place"] == 1


@pytest.mark.asyncio
async def test_nfq_uncertain_transfer_retains_exact_pending_client_and_never_resubmits(lane, monkeypatch):
    service, client, factory, _ = lane
    service.settings.oms_v2_webull_mirror_retained_hold_enabled = False
    event = event_for(lane)
    await service.process_trade_intent(event)
    quote(lane, event, "5.1")
    await service._evaluate_nfq_holds(event.payload.symbol)
    retry = queued(lane)[-1]
    original = service._record_order_reports

    async def crash(**kwargs):
        await original(**kwargs)
        raise RuntimeError("CONTROLLED legacy NFQ crash after ACK")

    monkeypatch.setattr(service, "_record_order_reports", crash)
    with pytest.raises(RuntimeError):
        await service._handle_stream_message({"data": retry.model_dump_json()})
    service.settings.oms_v2_webull_mirror_retained_hold_enabled = True
    service._restore_mirrorhold()
    assert state(lane, event)["phase"] == "uncertain"
    assert state(lane, event)["dispatch_client"] == service._build_client_order_id(retry)
    with factory() as session:
        order = session.scalar(select(BrokerOrder))
        assert order.payload["mirrorhold_id"] == "99a48593-ea0b-5db0-8ed6-7189d28c246b"
        reported = TradeIntentEvent.model_validate(state(lane, event)["event"])
        report = ExecutionReport("accepted", order.client_order_id, symbol=order.symbol, quantity=order.quantity,
                                 origin="broker", reason="CONTROLLED positive late read")
        service._mirrorhold_reports(session, reported, [report])
        session.commit()
    assert state(lane, event)["phase"] == "accepted"
    assert state(lane, event)["wire_submissions"] == 1
    assert client.calls["place"] == 1


@pytest.mark.asyncio
async def test_accepted_fourth_wire_then_cancel_does_not_spend_refusal_cap(lane):
    service, client, factory, clock = lane
    client.raises["place"] = _ServerException("ORDER_RISK_RULE_PRICE_AGGRESSIVE", "CONTROLLED", 417)
    for number in range(4):
        event = event_for(lane, stop=str(Decimal("5.35") - Decimal(number) / 100))
        quote(lane, event, "5.1")
        if number == 3:
            client.raises.clear()
        await service.process_trade_intent(event)
        clock[0] += timedelta(seconds=1)
    assert state(lane, event)["phase"] == "accepted"
    with factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == state(lane, event)["dispatch_client"]))
        order.status = "cancelled"
        session.add(BrokerOrderEvent(order_id=order.id, event_type="cancelled", event_source="broker",
            payload={"client_order_id": order.client_order_id, "metadata": dict(order.payload)}))
        session.commit()
    next_price = event_for(lane, stop="5.3")
    quote(lane, next_price, "5.1")
    result = await service.process_trade_intent(next_price)
    assert client.calls["place"] == 5
    assert result[-1].payload.status == "accepted"
    assert state(lane, next_price)["wire_submissions"] == 5
    assert state(lane, next_price)["price_aggressive_refusals"] == 3


@pytest.mark.asyncio
async def test_direct_dispatch_guard_refuses_a_previously_used_client(lane):
    service, client, factory, _ = lane
    event = event_for(lane)
    quote(lane, event, "5.1")
    client.raises["place"] = _ServerException("ORDER_RISK_RULE_PRICE_AGGRESSIVE", "CONTROLLED", 417)
    await service.process_trade_intent(event)
    assert state(lane, event)["phase"] == "held"
    with factory() as session:
        assert service._mirrorhold_dispatch(session, event) == "mirrorhold_duplicate_client"
    assert client.calls["place"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["risk", "collision"])
async def test_local_refusal_is_free_and_hold_survives(lane, monkeypatch, failure):
    service, client, _, clock = lane
    event, retry = await hold_and_queue(lane)
    name = "_evaluate_risk" if failure == "risk" else "_fanout_webull_collision_reason"
    original = getattr(service, name)
    refusal = (lambda event: (False, "CONTROLLED local risk")) if failure == "risk" else (
        lambda **kwargs: "CONTROLLED collision")
    monkeypatch.setattr(service, name, refusal)
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert state(lane, event)["phase"] == "held"
    assert state(lane, event)["wire_submissions"] == 0 and not client.calls.get("place")
    monkeypatch.setattr(service, name, original)
    clock[0] += timedelta(seconds=6)
    quote(lane, event, "5.1")
    await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    await service._handle_stream_message({"data": queued(lane)[-1].model_dump_json()})
    assert client.calls["place"] == 1


@pytest.mark.asyncio
async def test_final_sizing_uses_actual_wire_limit_before_reservation(lane):
    service, client, _, _ = lane
    service.settings.strategy_schwab_1m_v2_webull_entry_notional_usd = 300
    event = event_for(lane, stop="4.73")
    quote(lane, event, "4.38")
    await service.process_trade_intent(event)
    request = client.last["place"]
    assert request.values["qty"] == "63"
    assert Decimal(str(request.values["limit_price"])) == Decimal("4.75")
    assert Decimal(state(lane, event)["event"]["payload"]["quantity"]) == Decimal("63")
    assert state(lane, event)["wire_submissions"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["price", "stale_quote", "outside_band", "quantity"])
async def test_final_dispatch_rechecks_post_claim_changes_without_spending(lane, change):
    service, client, factory, clock = lane
    event, retry = await hold_and_queue(lane)
    assert service._mirrorhold_claim(retry)
    if change == "price":
        retry.payload.metadata["stop_price"] = "5.0"
    elif change == "quantity":
        retry.payload.quantity += 1
    elif change == "stale_quote":
        clock[0] += timedelta(seconds=11)
    else:
        quote(lane, event, "4.7")
    with factory() as session:
        assert service._mirrorhold_dispatch(session, retry) is not None
    assert state(lane, event)["wire_submissions"] == 0 and not client.calls.get("place")


@pytest.mark.asyncio
async def test_rth_hold_does_not_widen_into_premarket_or_next_session(lane):
    service, client, _, clock = lane
    event, retry = await hold_and_queue(lane)
    clock[0] = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    lane = restart(lane)
    assert state(lane, event)["phase"] == "retired"
    quote(lane, event, "5.1")
    await lane[0]._handle_stream_message({"data": retry.model_dump_json()})
    await lane[0]._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    assert not client.calls.get("place")
    pm = event_for(lane, segment="1791371000000")
    pm.payload.metadata.update(fanout_source="eh_resting", eh_resting="true", order_type="limit")
    assert not lane[0]._mirrorhold_scope(pm)
    quote(lane, pm, "5.1", age=3)
    assert lane[0]._band_capped_marketable_limit(symbol=pm.payload.symbol, level=5.35,
        band_pct=0.5, max_age_ms=2000)[1] == "NO_FRESH_QUOTE"


@pytest.mark.asyncio
async def test_missing_segment_proof_cannot_wire_and_known_end_retires(lane):
    service, client, factory, clock = lane
    event, retry = await hold_and_queue(lane)
    FanoutSegmentIdentityStore(factory).record(event.payload.symbol, int(event.payload.metadata["fanout_segment_id"]),
                                              False, "CONTROLLED terminal segment", now=clock[0] + timedelta(seconds=1))
    await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    assert state(lane, event)["phase"] == "retired"
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert not client.calls.get("place")


@pytest.mark.asyncio
async def test_unproven_aborted_order_is_still_inflight(lane):
    service, client, factory, _ = lane
    event = event_for(lane)
    quote(lane, event, "5.1")
    client.raises["place"] = _ServerException("ORDER_RISK_RULE_PRICE_AGGRESSIVE", "CONTROLLED", 417)
    await service.process_trade_intent(event)
    with factory() as session:
        order = session.scalar(select(BrokerOrder))
        order.status = "aborted"  # No affirmative T43 no-wire proof.
        session.commit()
        assert service._mirrorhold_clear_order(session, order) is False
        assert service._mirrorhold_gate(session, event) == "dispatch_uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "client_audit", "generation", "quantity", "event", "fill", "missing_wire"])
async def test_persisted_exact_terminal_audit_reconciles_only_this_reservation(lane, monkeypatch, change):
    service, client, factory, clock = lane
    event = event_for(lane)
    quote(lane, event, "5.1")
    original = service._record_order_reports

    async def crash(**kwargs):
        await original(**kwargs)
        raise RuntimeError("CONTROLLED crash before wire evidence commits")

    monkeypatch.setattr(service, "_record_order_reports", crash)
    with pytest.raises(RuntimeError):
        await service.process_trade_intent(event)
    assert state(lane, event)["dispatch_unresolved"]
    with factory() as session:
        order = session.scalar(select(BrokerOrder))
        order.status = "cancelled"
        md = dict(order.payload)
        if change != "missing_wire":
            md["webull_wire_submitted_at_utc"] = clock[0].isoformat()
        if change == "generation":
            order.payload = {**order.payload, "mirrorhold_dispatch_generation": "foreign"}
        elif change == "quantity":
            order.quantity += 1
        elif change == "event":
            intent = session.get(TradeIntent, order.intent_id)
            intent.payload = {**intent.payload, "event_id": str(uuid4())}
        elif change == "fill":
            session.add(Fill(order_id=order.id, strategy_id=order.strategy_id,
                broker_account_id=order.broker_account_id, symbol=order.symbol, side="buy",
                quantity=Decimal("0.5"), price=Decimal("5.1"), payload={}))
        session.add(BrokerOrderEvent(order_id=order.id, event_type="cancelled",
            event_source="client" if change == "client_audit" else "broker",
            payload={"client_order_id": order.client_order_id, "metadata": md}))
        session.commit()
    lane = restart(lane)
    next_event = event_for(lane, stop="5.3")
    allowed = lane[0]._mirrorhold_admit(next_event)
    assert allowed is (change is None)
    assert state(lane, event)["dispatch_unresolved"] is (change is not None)
    if change is None:
        assert state(lane, event)["phase"] == "blocked"
        assert state(lane, event)["wire_submissions"] == 1
    assert client.calls["place"] == 1  # Recovery itself never sends a POST.


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "cancelled", "expired"])
async def test_terminal_report_without_submission_evidence_does_not_release_unknown_budget(lane, monkeypatch, outcome):
    service, client, factory, _ = lane
    event = event_for(lane)
    quote(lane, event, "5.1")
    original = service._record_order_reports

    async def crash(**kwargs):
        await original(**kwargs)
        raise RuntimeError("CONTROLLED crash before wire evidence commits")

    monkeypatch.setattr(service, "_record_order_reports", crash)
    with pytest.raises(RuntimeError):
        await service.process_trade_intent(event)
    report = ExecutionReport(outcome, state(lane, event)["dispatch_client"], symbol=event.payload.symbol,
        quantity=event.payload.quantity, origin="broker",
        metadata={"webull_error_code": "ORDER_RISK_RULE_PRICE_AGGRESSIVE"})
    with factory() as session:
        service._mirrorhold_reports(session, event, [report])
        session.commit()
    assert state(lane, event)["dispatch_unresolved"]
    assert state(lane, event)["phase"] == "uncertain"
    assert state(lane, event)["wire_submissions"] == 0
    assert client.calls["place"] == 1


@pytest.mark.asyncio
async def test_off_keeps_existing_owner_without_recreating_a_second_nfq_actor(lane, monkeypatch):
    service, client, factory, clock = lane
    event, retry = await hold_and_queue(lane)
    service.settings.oms_v2_webull_mirror_retained_hold_enabled = False
    original = service.broker_adapter._submit_blocking

    def delayed(account, request):
        clock[0] += timedelta(seconds=11)
        return original(account, request)

    monkeypatch.setattr(service.broker_adapter, "_submit_blocking", delayed)
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert state(lane, event)["phase"] == "held"
    assert not service._nfq_holds and not client.calls.get("place")
    with factory() as session:
        assert not session.scalar(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == NFQ_SNAPSHOT))
    monkeypatch.setattr(service.broker_adapter, "_submit_blocking", original)
    quote(lane, event, "5.1")
    await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    await service._handle_stream_message({"data": queued(lane)[-1].model_dump_json()})
    assert client.calls["place"] == 1 and state(lane, event)["wire_submissions"] == 1
