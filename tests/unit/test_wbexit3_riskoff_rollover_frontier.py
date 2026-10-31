"""SOURCE UNRUN: C12204 proven-origin riskoff EH causal controls.

This demonstrates a REAL original O14 riskoff intent/abort at the RTH boundary.
The narrow source successor is NOT Main/C1 qualified or a native PASS. Original
RTH attachment and arbitrary EH REASON without a real origin remain refused.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition, TradeIntent
from project_mai_tai.oms import service as oms, webull_attach_riskoff as wb, wbexit2, wbexit3, wbexit3_riskoff
from tests.unit.test_wbattach1_riskoff import Broker, make_case as make_case, attach, closes
from tests.unit.test_wbexit2_route import ControlledOwnerBroker, owner_case
from tests.unit.test_wbexit3_obligation import obligations, restarted


class RolloverPort(ControlledOwnerBroker):
    async def list_account_positions(self, account):
        # A controlled ACTUAL call at the external complete-read boundary, not
        # a permission/freshness helper patched to True.
        self.wire_started = monotonic()
        return await super().list_account_positions(account)

    async def cancel_exit_pair(self, **binding):
        actual_call = wb._ATR_CALL.get()
        if actual_call is not None and actual_call.wb2:
            return await super().cancel_exit_pair(**binding)
        return await Broker.cancel_exit_pair(self, **binding)


@pytest.mark.asyncio
@pytest.mark.parametrize("on,native_filled,origin_fault,next_day", [
    (False, 0, None, False), (True, 0, None, False), (True, 3, None, False),
    (True, 0, "abort_event_changed", False), (True, 0, "uncertain_original", False),
    (True, 3, None, True),
], ids=["OLD", "NEW_exact10", "NEW_native3_remaining7", "NEW_bad_abort", "NEW_uncertain_original", "NEW_next_day_durable7"])
async def test_real_RTH_prepared_origin_rolls_to_exact_EH_remaining_without_rewriting_original(
        make_case, monkeypatch, on, native_filled, origin_fault, next_day, record_property):
    case = make_case()
    service, factory, old, entry_id, clock = case
    port = RolloverPort(service, factory, old.account)
    port.bid = 10.
    service.broker_adapter = port
    case = service, factory, port, entry_id, clock
    service.__dict__.pop("_v2_eod_handover_ready", None)
    service.__dict__.pop("_fetch_quote_for_order", None)
    object.__setattr__(service.settings, "oms_webull_exit3_enabled", on)
    if on:
        await wbexit2.hydrate_at_boot(service)
        await wbexit3.hydrate_at_boot(service)
    clock[0] = datetime(2026, 10, 15, 19, 59, 50, tzinfo=UTC)
    assert await attach(case)
    assert not closes(port)
    clock[0] = datetime(2026, 10, 15, 19, 59, 59, 800000, tzinfo=UTC)
    port.bid = 9.4
    original = service._run_db
    prepared = []
    async def boundary_after_real_commit(fn, *, commit=True):
        result = await original(fn, commit=commit)
        if commit and isinstance(result, UUID) and not prepared:
            with factory() as db:
                intent = db.get(TradeIntent, result)
                riskoff = intent is not None and intent.reason == wb.REASON
            if riskoff:
                prepared.append(result)
                # Controlled clock at the actual committed O14 DB port, not a
                # patched owner/native/final-proof success result.
                clock[0] += timedelta(milliseconds=200)
        return result
    monkeypatch.setattr(service, "_run_db", boundary_after_real_commit)
    try:
        await attach(case)
    finally:
        # End and physically join this fixture's original automatic recovery
        # tasks before direct SQLite inspection or fixture DB disposal.
        tasks = tuple(service._confirmation_exit_recovery_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    assert len(prepared) == 1 and not closes(port)
    with factory() as db:
        intent = db.get(TradeIntent, prepared[0])
        order = db.scalar(select(BrokerOrder).where(BrokerOrder.intent_id == intent.id))
        event = db.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
        managed = db.scalar(select(OmsManagedPosition))
        assert intent.reason == wb.REASON and intent.status == "aborted" and intent.quantity == 10
        assert order.status == "aborted" and order.submitted_at is None and not order.broker_order_id
        assert event.event_source == "client" and event.payload["proven_not_sent"] is True
        assert not list(db.scalars(select(Fill).where(Fill.order_id == order.id)))
        assert managed.status == "open" and managed.current_quantity == 10
        journal = db.get(BrokerOrder, entry_id).payload[wb.KEY]
        assert journal["unsent_handoffs"][-1]["abort_event_id"] == str(event.id)
        row_id = str(managed.id)
        old_order_id, old_event_id = order.id, event.id
    assert port.total == Decimal(1010)
    if not on:
        assert await wb.retry_atr_proven_unsent_exit(service, port.account, "ABC", row_id) is None
        assert not closes(port) and port.total == Decimal(1010)
        return
    if origin_fault:
        # Controlled adverse mutation AFTER obtaining a real canonical abort;
        # no missing proof may be repaired by the candidate or a test helper.
        with factory.begin() as db:
            if origin_fault == "abort_event_changed":
                receipt_event = db.get(BrokerOrderEvent, old_event_id)
                receipt_event.payload = {**receipt_event.payload, "proven_not_sent": False}
            else:
                original_order = db.get(BrokerOrder, old_order_id)
                original_order.status = "accepted"
                original_order.broker_order_id = "CONTROLLED-uncertain-original"
        with pytest.raises(ValueError, match="origin_UNKNOWN"):
            await wb.retry_atr_proven_unsent_exit(service, port.account, "ABC", row_id)
        assert not closes(port) and port.total == Decimal(1010)
        return
    # Only external native-fill observations change after the real old abort.
    # O14 origin, DB/owner/canonical writers and final validators remain real.
    strict = port
    strict.child_filled, strict.bid = Decimal(native_filled), 9.4
    strict.child_applied = False
    service.broker_adapter = strict
    service._latest_quotes_by_symbol["ABC"] = {"received_at": clock[0], "bid": strict.bid}
    monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: "PM")
    strict.positions_unknown = next_day
    await wb.retry_atr_proven_unsent_exit(service, strict.account, "ABC", row_id)
    if next_day:
        assert not closes(strict)
        prior = obligations((service, factory, strict, entry_id, clock))[0]
        assert prior["active_token"] is None and prior["phase"] == "RETRY"
        restored = restarted((service, factory, strict, entry_id, clock))
        service = restored[0]
        clock[0] = datetime(2026, 10, 16, 8, 5, tzinfo=UTC)
        monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: "AM")
        strict.positions_unknown = False
        await wbexit2.hydrate_at_boot(service)
        await wbexit3.hydrate_at_boot(service)
        await wbexit3.resume_on_quote(service, strict.account, "ABC", {"received_at": clock[0], "bid": strict.bid})
        current = obligations(restored)[0]
        assert current["owner_id"] == prior["owner_id"] and current["opened_at"] == prior["opened_at"]
    sells = closes(strict)
    assert len(sells) == 1 and sells[0].quantity == Decimal(10 - native_filled)
    assert sells[0].reason == wb.REASON and sells[0].order_type == "limit"
    assert sells[0].metadata[wbexit3_riskoff.PREFIX + "intent_id"] == str(prepared[0])
    assert sells[0].metadata[wbexit3_riskoff.PREFIX + "order_id"] == str(old_order_id)
    assert sells[0].metadata[wbexit3_riskoff.PREFIX + "abort_event_id"] == str(old_event_id)
    assert strict.total == Decimal(1000)
    with factory() as db:
        old_intent = db.get(TradeIntent, prepared[0])
        assert old_intent.quantity == 10 and old_intent.status == "aborted"
        assert db.get(BrokerOrder, old_order_id).status == "aborted"
        new_order = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == sells[0].client_order_id))
        new_intent = db.get(TradeIntent, new_order.intent_id)
        assert new_intent.id != old_intent.id
        assert new_intent.quantity == new_order.quantity == Decimal(10 - native_filled)
        assert db.scalar(select(OmsManagedPosition.status)) == "closed"
    assert obligations((service, factory, strict, entry_id, clock))[0]["phase"] == "RESOLVED"
    record_property("riskoff_rollover", "CONTROLLED_NOT_NATIVE_PASS: real RTH O14 origin; new linked exact EH parent; original10 unchanged; manual1000 untouched")


@pytest.mark.asyncio
async def test_arbitrary_EH_riskoff_reason_without_real_origin_cannot_remove_RTH_guard(make_case, monkeypatch):
    case = await owner_case(make_case)
    service, factory, port, _, clock = case
    object.__setattr__(service.settings, "oms_webull_exit3_enabled", True)
    await wbexit3.hydrate_at_boot(service)
    clock[0] = clock[0].replace(hour=21)
    monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: "PM")
    with factory() as db:
        row = db.scalar(select(OmsManagedPosition))
        position, row_id = service._hydrate_v2_position(row), str(row.id)
    requests, reads = len(port.requests), port.positions_reads
    result = await service._emit_v2_exit_on_loop_claimed(port.account, port.symbol, position, 10.,
        kind="HARD", reference_price=9.4, reason=wb.REASON, bid=9.4,
        close_on_fill=True, expected_managed_row_id=row_id, confirmation_context={})
    assert result == "refused" and len(port.requests) == requests and port.positions_reads == reads
    assert not port.cancelled and not closes(port)
    assert not obligations(case)
