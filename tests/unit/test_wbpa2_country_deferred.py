"""UNRUN SOURCE. Main runs exact OLD/NEW profiles; original rows stay pinned.

Raw CHOW rejection/metadata are retained; later in-band quotes, relational setup
and every adverse DB mutation/reply are CONTROLLED, not observed native fills.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, DashboardSnapshot
from project_mai_tai.fanout_segment_store import SNAPSHOT_TYPE
from tests.unit.wbpa2_controlled_transport import ControlledSubmitReply
from tests.unit.wbpa2_episode_port import (ACCOUNT, BOUNDARY, ORIGINAL_ORDER, SLOT, SYMBOL,
    append_recorded_rows, close_case, drain_ticks, event_from_intent, make_case,
    observe_original, queued, quote, real_v2_cancel, serial, state)

pytestmark = pytest.mark.skipif(not os.environ.get("WBPA2_CHOW_EPISODE_SOURCE"),
    reason="Requires the exact reviewed private CHOW export; qualified profile supplies it")

REJECT = ControlledSubmitReply("rejected", "CONTROLLED repeated PRICE_AGGRESSIVE417",
                              error_code="ORDER_RISK_RULE_PRICE_AGGRESSIVE")


@pytest.mark.asyncio
async def test_original_CHOW_prefix_OLD_no_retry_NEW_one_earned_serial_retry(tmp_path, monkeypatch):
    mode = os.environ["WBPA2_EXPECTED_MODE"]
    assert mode in {"OLD", "NEW"}
    case = await make_case(tmp_path, monkeypatch, replies=[REJECT])
    try:
        observe_original(case)
        await quote(case, "5.39", age=2.164151)
        await drain_ticks(case)
        assert queued(case) == [] and case.provider.requests == []
        if mode == "OLD":
            assert state(case) is None
            await quote(case, BOUNDARY)
            await drain_ticks(case)
            assert state(case) is None and queued(case) == [] and case.provider.entries == []
        else:
            assert state(case) is not None
            with case.factory() as session:
                # Execute the ACTUAL reader, including original canonical audit,
                # fills, every same-account order and real segment snapshot.
                assert case.service._mirrorhold_gate(session, case.event, session_bound=True) is None
            await quote(case, BOUNDARY - Decimal(".0001"))
            await drain_ticks(case)
            assert queued(case) == []
            await quote(case, BOUNDARY)
            await drain_ticks(case)
            assert len(queued(case)) == 1 and case.provider.entries == []
            retry = queued(case)[0]
            for key in ("fanout_slot_id", "fanout_segment_id", "atr_country_schema",
                        "atr_country_broker", "atr_country_account", "stop_price"):
                assert retry.payload.metadata[key] == case.event.payload.metadata[key]
            # Existing ROUNDUP1/PA1 helper normalizes decimal text on retry.
            # Preserve exact economic geometry, not original trailing zeros.
            assert Decimal(retry.payload.metadata["limit_price"]) == Decimal(case.event.payload.metadata["limit_price"])
            assert retry.event_id != case.event.event_id
            await serial(case, retry)
            assert len(case.provider.requests) == 1
            request = case.provider.requests[0]
            assert request.broker_account_name == ACCOUNT and request.symbol == SYMBOL
            assert request.quantity == Decimal(48)
            assert request.metadata["stop_price"] == "6.2600"
            assert Decimal(request.metadata["limit_price"]) == Decimal("6.29")
            assert [entry["operation"] for entry in case.provider.entries] == ["submit", "positions"]
            assert case.provider.entries[1]["result"] == "REFUSED_UNKNOWN_NEVER_FLAT"
            assert all(not entry["market_task"] for entry in case.provider.entries)
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_NEW_three_rejected_resubmissions_cap_and_repeated_ticks_never_duplicate(tmp_path, monkeypatch):
    case = await make_case(tmp_path, monkeypatch, replies=[REJECT] * 3)
    try:
        observe_original(case)
        assert state(case) is not None
        for attempt in range(1, 4):
            case.clock[0] += timedelta(seconds=6)
            for _ in range(3):
                await quote(case, BOUNDARY)
            await drain_ticks(case)
            assert len(queued(case)) == attempt
            assert len(case.provider.requests) == attempt - 1
            await serial(case, queued(case)[-1])
            assert len(case.provider.requests) == attempt
        case.clock[0] += timedelta(seconds=6)
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        assert state(case) is None and len(queued(case)) == 3
        assert len(case.provider.requests) == 3 and not case.provider.live_clients
        assert [entry["operation"] for entry in case.provider.entries] == ["submit", "positions"] * 3
        assert all(entry["result"] == "REFUSED_UNKNOWN_NEVER_FLAT"
                   for entry in case.provider.entries if entry["operation"] == "positions")
        observe_original(case)  # late retained reject must not resurrect a capped generation
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        assert state(case) is None and len(queued(case)) == 3
    finally:
        await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [11, -1])
async def test_NEW_stale_or_future_market_cache_cannot_queue(tmp_path, monkeypatch, age):
    case = await make_case(tmp_path, monkeypatch)
    try:
        observe_original(case)
        assert state(case) is not None
        await quote(case, BOUNDARY, age=age)
        await drain_ticks(case)
        assert state(case) is not None and queued(case) == [] and case.provider.entries == []
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_serial_outside_band_precheck_keeps_attempt_and_real_five_second_rearm(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from project_mai_tai.oms import webull_country_deferred as country
    case = await make_case(tmp_path, monkeypatch)
    controlled_monotonic = [100.0]
    # Replace only this module's clock operand, never global time/asyncio or a
    # permission function. This deterministic timing control is not the rate.
    monkeypatch.setattr(country, "time", SimpleNamespace(monotonic=lambda: controlled_monotonic[0]))
    try:
        observe_original(case)
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        first, = queued(case)
        await quote(case, "5.39")  # cache moved before actual serial precheck
        await drain_ticks(case)
        await serial(case, first)
        assert case.provider.requests == case.provider.entries == []
        assert state(case) is not None and state(case).attempts == 1
        assert state(case).retry_not_before_monotonic == 105.0
        controlled_monotonic[0] = 104.999
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        assert len(queued(case)) == 1
        controlled_monotonic[0] = 105.0
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        assert len(queued(case)) == 2 and state(case).attempts == 2
        assert case.provider.entries == []
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_existing_PA1_flag_OFF_country_actor_remains_dormant(tmp_path, monkeypatch):
    case = await make_case(tmp_path, monkeypatch, enabled=False)
    try:
        observe_original(case)
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        assert state(case) is None and queued(case) == [] and case.provider.entries == []
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_whole_recorded_reprices_are_new_original_attempts_not_deferred_resubmissions(tmp_path, monkeypatch):
    case = await make_case(tmp_path, monkeypatch)
    try:
        observe_original(case)
        assert state(case) is not None
        later = [row for row in case.raw["intents"] if row["id"] != "1c696692-1c90-4408-b584-e7fca8919f26"]
        assert [row["intent_type"] for row in later] == ["cancel", "open", "cancel", "open", "cancel", "open"]
        from project_mai_tai.broker_adapters.protocols import ExecutionReport
        for row in later:
            append_recorded_rows(case, datetime.fromisoformat(row["updated_at"]) + timedelta(milliseconds=20))
            event = event_from_intent(row)
            case.service._observe_webull_mirror_deferred_intent(event)
            if row["intent_type"] == "cancel":
                assert row["payload"]["metadata"]["reason"] == "reprice"
                assert row["payload"]["refusal_code"] == "cancel_target_not_found"
                assert state(case) is None
            else:
                order = next(order for order in case.raw["orders"] if order["intent_id"] == row["id"])
                audit = next(audit for audit in case.raw["order_events"] if audit["order_id"] == order["id"])
                assert "webull_deferred_resubmit" not in row["payload"]["metadata"]
                report = ExecutionReport("rejected", order["client_order_id"], symbol=SYMBOL,
                    side="buy", intent_type="open", quantity=Decimal(str(order["quantity"])),
                    reported_at=datetime.fromisoformat(audit["event_at"]), origin="broker",
                    reason=audit["payload"]["reason"], metadata=deepcopy(audit["payload"]["metadata"]))
                with case.factory.begin() as session:
                    case.service._observe_webull_mirror_deferred_reports(event=event,
                        reports=[report], session=session)
                assert state(case) is not None
        assert [len(case.seeded_ids[key]) for key in ("intents", "orders", "order_events")] == [7, 4, 4]
        assert queued(case) == [] and case.provider.entries == []
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_final_560_slot_CONTROLLED_retry_clock_vs_C1_reported_cross(tmp_path, monkeypatch, record_property):
    case = await make_case(tmp_path, monkeypatch, replies=[REJECT], final_slot=True)
    reported_cross = datetime.fromisoformat("2026-10-30T16:00:56+00:00")
    controlled_eligible = datetime.fromisoformat("2026-10-30T15:59:30+00:00")
    try:
        assert case.clock[0] > datetime.fromisoformat("2026-10-30T15:45:05.878586+00:00")
        assert case.event.payload.metadata["stop_price"] == "5.6000"
        threshold = Decimal("5.60") * Decimal(".92")
        assert threshold == Decimal("5.152")
        observe_original(case)
        assert state(case) is not None
        # Retained last ask4.45 at15:45:02.083538 precedes this captured reject.
        await quote(case, "4.45", age=3.916462)
        await drain_ticks(case)
        assert queued(case) == [] and case.provider.entries == []
        case.clock[0] = controlled_eligible
        await quote(case, threshold - Decimal(".0001"))
        await drain_ticks(case)
        assert queued(case) == []
        await quote(case, threshold)
        await drain_ticks(case)
        retry, = queued(case)
        await serial(case, retry)
        assert len(case.provider.requests) == 1 and case.provider.requests[0].quantity == Decimal(53)
        assert len(case.provider.entries) == 2
        assert retry.produced_at == controlled_eligible < reported_cross
        assert [entry["operation"] for entry in case.provider.entries] == ["submit", "positions"]
        assert case.provider.entries[1]["result"] == "REFUSED_UNKNOWN_NEVER_FLAT"
        record_property("comparison_scope", "CONTROLLED_ELIGIBILITY_AND_RETRY_VS_C1_REPORTED_CROSS_NOT_NATIVE_FIRST_TICK")
        record_property("retained_trigger", "5.60")
        record_property("exact_eight_percent_threshold", str(threshold))
        record_property("CONTROLLED_eligible_at", controlled_eligible.isoformat())
        record_property("CONTROLLED_retry_event_at", retry.produced_at.isoformat())
        record_property("C1_REPORTED_cross_at", reported_cross.isoformat())
        record_property("CONTROLLED_lead_seconds", (reported_cross - retry.produced_at).total_seconds())
        record_property("ACTUAL_first_eligible_or_retry_at", "UNKNOWN_MISSING_TIMESTAMPED_QUOTES")
    finally:
        await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["working", "pending", "missing_audit", "foreign_audit", "segment_unknown", "segment_stale"])
async def test_NEW_no_working_slot_proof_is_earned_and_unknown_never_posts(tmp_path, monkeypatch, fault):
    case = await make_case(tmp_path, monkeypatch, replies=[REJECT])
    try:
        observe_original(case)
        assert state(case) is not None
        with case.factory.begin() as session:
            order = session.get(BrokerOrder, UUID(ORIGINAL_ORDER))
            audit = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
            segment = session.scalar(select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE))
            if fault in {"working", "pending"}:
                order.status = fault  # CONTROLLED ambiguous/live order, raw fixture unchanged.
            elif fault == "missing_audit":
                session.delete(audit)
            elif fault == "foreign_audit":
                payload = deepcopy(audit.payload)
                payload["metadata"]["fanout_slot_id"] = "CONTROLLED-foreign-slot"
                audit.payload = payload
            elif fault == "segment_unknown":
                session.delete(segment)
            elif fault == "segment_stale":
                segment.payload = {**segment.payload, "session_anchor": "2026-10-29T08:00:00+00:00"}
        with case.factory() as session:
            assert case.service._mirrorhold_gate(session, case.event, session_bound=True) is not None
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        retry, = queued(case)
        await serial(case, retry)
        assert case.provider.requests == []
        assert not case.provider.live_clients
        assert case.provider.entries == [] and state(case) is None
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_foreign_generation_cancel_cannot_poison_current_rejection_retry(tmp_path, monkeypatch):
    case = await make_case(tmp_path, monkeypatch, replies=[REJECT])
    try:
        observe_original(case)
        current = state(case)
        assert current is not None
        cancel = real_v2_cancel(case)
        cancel.payload.metadata["rpg_resting_generation"] = "CONTROLLED-foreign-generation"
        case.service._observe_webull_mirror_deferred_intent(cancel)
        assert state(case) is current
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        assert len(queued(case)) == 1
        await serial(case, queued(case)[0])
        assert len(case.provider.requests) == 1
        assert [entry["operation"] for entry in case.provider.entries] == ["submit", "positions"]
        assert case.provider.entries[1]["result"] == "REFUSED_UNKNOWN_NEVER_FLAT"
        assert state(case) is not None and state(case).attempts == 1
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_order_appearing_after_real_claim_is_rechecked_immediately_before_wire(tmp_path, monkeypatch):
    from project_mai_tai.oms import webull_country_deferred as country
    case = await make_case(tmp_path, monkeypatch, replies=[REJECT])
    actual_claim = country.claim
    observed = []

    def after_actual_claim(service, event):
        allowed = actual_claim(service, event)  # All real proof must succeed first.
        if allowed is True:
            observed.append(allowed)
            with case.factory.begin() as session:
                original = session.get(BrokerOrder, UUID(ORIGINAL_ORDER))
                # CONTROLLED external account/slot book changes after claim.
                # No application gate, claim result, or permission is replaced.
                session.add(BrokerOrder(id=uuid4(), intent_id=original.intent_id,
                    strategy_id=original.strategy_id, broker_account_id=original.broker_account_id,
                    client_order_id="CONTROLLED-WORKING-BUY-AFTER-CLAIM", broker_order_id="CONTROLLED-NATIVE-RACE",
                    symbol=SYMBOL, side="buy", order_type="STOP_LIMIT", time_in_force="day",
                    quantity=original.quantity, status="working", payload=deepcopy(original.payload),
                    submitted_at=case.clock[0], updated_at=case.clock[0]))
        return allowed

    monkeypatch.setattr(country, "claim", after_actual_claim)
    try:
        observe_original(case)
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        retry, = queued(case)
        await serial(case, retry)
        assert observed == [True]
        assert case.provider.requests == [] and case.provider.entries == []
        with case.factory() as session:
            own = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id ==
                case.service._build_client_order_id(retry)))
            assert own is not None and own.status == "aborted"
            raced = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id ==
                "CONTROLLED-WORKING-BUY-AFTER-CLAIM"))
            assert raced.status == "working"
    finally:
        await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["v2_sell_flip_cancel", "filled", "partially_filled"])
async def test_NEW_current_slot_cancel_or_execution_forgets_and_queued_copy_cannot_post(tmp_path, monkeypatch, kind):
    case = await make_case(tmp_path, monkeypatch, replies=[REJECT])
    try:
        observe_original(case)
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        assert len(queued(case)) == 1
        if kind == "v2_sell_flip_cancel":
            case.service._observe_webull_mirror_deferred_intent(real_v2_cancel(case))
        else:
            from dataclasses import replace
            report = replace(case.report, event_type=kind, filled_quantity=Decimal(1),
                             fill_price=Decimal("6.26"), reason="CONTROLLED execution response")
            with case.factory.begin() as session:
                case.service._observe_webull_mirror_deferred_reports(event=case.event,
                    reports=[report], session=session)
        assert state(case) is None
        observe_original(case)  # stale PRICE_AGGRESSIVE after terminal/cancel is not new authority
        assert state(case) is None
        await serial(case, queued(case)[0])
        assert case.provider.requests == [] and case.provider.entries == []
    finally:
        await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("at", ["2026-10-30T11:29:04+00:00", "2026-10-30T20:29:04+00:00"])
async def test_PM_AH_software_rest_is_outside_country_broker_stop_actor(tmp_path, monkeypatch, at):
    case = await make_case(tmp_path, monkeypatch)
    try:
        case.clock[0] = datetime.fromisoformat(at)
        event = case.event.model_copy(deep=True)
        event.produced_at = case.clock[0]
        event.payload.metadata.update(order_type="LIMIT", atr_country_sealed_at=at)
        # This is a CONTROLLED software-order shape, not an altered original row.
        with case.factory.begin() as session:
            case.service._observe_webull_mirror_deferred_reports(event=event,
                reports=[case.report], session=session)
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        assert state(case) is None and queued(case) == [] and case.provider.entries == []
    finally:
        await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["ordinary_buy", "selected_schwab"])
async def test_non_country_resting_shapes_do_not_enter_new_actor(tmp_path, monkeypatch, shape):
    case = await make_case(tmp_path, monkeypatch)
    try:
        event = case.event.model_copy(deep=True)
        if shape == "ordinary_buy":
            event.payload.metadata.pop("resting_entry")
            event.payload.metadata["order_type"] = "MARKET"
        else:
            event.payload.broker_account_name = "live:schwab_1m_v2"
            event.payload.metadata.update(atr_country_broker="schwab",
                atr_country_account="live:schwab_1m_v2", atr_country_notional_usd="600",
                atr_country_reason="sec_us", atr_country_status="READY")
            event.payload.metadata.pop("fanout_leg")  # selected primary has no Webull mirror leg
            from project_mai_tai.atr_country_route import selection_from_metadata
            selected = selection_from_metadata(event.payload.metadata, case.service.settings)
            assert selected.broker == "schwab" and selected.account == event.payload.broker_account_name
        with case.factory.begin() as session:
            case.service._observe_webull_mirror_deferred_reports(event=event,
                reports=[case.report], session=session)
        assert state(case) is None and case.provider.entries == []
    finally:
        await close_case(case)


@pytest.mark.asyncio
async def test_accepted_country_retry_and_old_queue_redelivery_never_create_second_live_buy(tmp_path, monkeypatch):
    case = await make_case(tmp_path, monkeypatch, replies=[ControlledSubmitReply(
        "accepted", "CONTROLLED one live resting BUY", native_id="CONTROLLED-ONE-LIVE-BUY")])
    try:
        observe_original(case)
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        retry, = queued(case)
        await serial(case, retry)
        assert state(case) is None
        assert case.provider.live_clients == {case.service._build_client_order_id(retry)}
        observe_original(case)  # late original reject cannot reopen an accepted generation
        for _ in range(3):
            await quote(case, BOUNDARY)
        await drain_ticks(case)
        await serial(case, retry)  # actual stream redelivery of the old queued envelope
        assert state(case) is None and len(queued(case)) == len(case.provider.requests) == 1
        assert [entry["operation"] for entry in case.provider.entries] == ["submit", "positions"]
        assert case.provider.entries[1]["result"] == "REFUSED_UNKNOWN_NEVER_FLAT"
    finally:
        await close_case(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_original_order", "execution_audit_without_fill_row"])
async def test_country_original_terminal_proof_cannot_be_replaced_by_empty_or_conflicting_ledger(
    tmp_path, monkeypatch, fault,
):
    from project_mai_tai.oms import webull_country_deferred as country
    case = await make_case(tmp_path, monkeypatch)
    try:
        observe_original(case)
        with case.factory.begin() as session:
            order = session.get(BrokerOrder, UUID(ORIGINAL_ORDER))
            audit = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id))
            if fault == "missing_original_order":
                session.delete(audit)
                session.flush()
                session.delete(order)
            else:
                # A conflicting actual-style execution audit must block even
                # when the materialized Fill row has not been written.
                session.add(BrokerOrderEvent(id=uuid4(), order_id=order.id,
                    event_type="partially_filled", event_source="broker", event_at=case.clock[0],
                    payload={"client_order_id":order.client_order_id,
                             "filled_quantity":"1", "metadata":deepcopy(order.payload)}))
        with case.factory() as session:
            assert country.no_working_reason(case.service, session, case.event) is not None
        await quote(case, BOUNDARY)
        await drain_ticks(case)
        retry, = queued(case)
        await serial(case, retry)
        assert state(case) is None and case.provider.entries == []
    finally:
        await close_case(case)
