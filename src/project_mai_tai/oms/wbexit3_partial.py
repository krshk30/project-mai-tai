"""Exact-quantity Webull EH scale under the existing managed/ATR invocation.

Uses the separate WBEXIT3 operation, never the full-close WBEXIT2 owner key.
Native terminal validators, ledger/O14 caps and final proof stay load-bearing.
Remaining shares retain the software ladder; no EH native stop is invented.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from time import monotonic
from uuid import UUID

from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.db.models import BrokerOrder, Fill, SystemIncident, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import wbexit2, wbexit3


def selected(service, reason, account, kind):
    return bool(kind == "SCALE" and wbexit3.wire_session(service, reason, account)
                and wbexit3.session(wbexit3._now()) in {"PM", "AH"})


def _context(service, call, *, allow_consumed=False):
    current = wbexit3._ATTEMPT.get()
    if (current is None or current["service"] is not service or current["task"] is not asyncio.current_task()
            or current["state"] is None or current["state"]["kind"] != "SCALE"
            or not getattr(call, "wb3_partial", False) or call.service is not service
            or call.task is not asyncio.current_task() or call.consumed and not allow_consumed
            or current["state"]["binding"]["managed_id"] != call.managed_id):
        raise ValueError("wbexit3_partial_actual_operation_UNKNOWN")
    return current


def check_call(service, call):
    if (not selected(service, call.reason, call.account, "SCALE") or monotonic() > call.wb2_deadline):
        raise ValueError("wbexit3_partial_session_or_deadline_changed")
    control = getattr(service, "_bot_control_runtime", None)
    if (getattr(service.settings, "bot_control_enabled", False) and control is None
            or control is not None and (control.stop_requested("schwab_1m_v2")
                or call.wb2_control is None or control.states.get("schwab_1m_v2") != call.wb2_control)):
        raise ValueError("wbexit3_partial_control_owner_changed")


async def _write(service, call, operation, **evidence):
    current = _context(service, call, allow_consumed=operation == "unsent")
    def unit(db):
        incident = db.scalar(select(SystemIncident).where(
            SystemIncident.id == UUID(current["state"]["owner_id"])).with_for_update())
        state = deepcopy(wbexit3._validate(incident.payload, incident.id))
        if state.get("active_token") != current["token"]:
            raise ValueError("wbexit3_partial_durable_invocation_changed")
        if operation == "originals":
            originals = deepcopy(evidence["originals"])
            if state.get("partial_originals") not in (None, originals):
                raise ValueError("wbexit3_partial_original_ID_or_geometry_changed")
            state["partial_originals"] = originals
            native = evidence["native_account_id"]
            if state.get("partial_native_account_id") not in (None, native):
                raise ValueError("wbexit3_partial_native_account_changed")
            state["partial_native_account_id"] = native
            state.setdefault("partial_cancel_bases", [])
            if evidence.get("zero_baseline_at") is not None:
                state.setdefault("partial_zero_baseline_at", evidence["zero_baseline_at"])
        elif operation == "cancel":
            base = evidence["base"]
            if base in state.get("partial_cancel_bases", []):
                raise ValueError("wbexit3_partial_original_cancel_already_requested")
            if base not in {p["base"] for p in state.get("partial_originals", [])}:
                raise ValueError("wbexit3_partial_cancel_not_original")
            state["partial_cancel_bases"] = [*state.get("partial_cancel_bases", []), base]
        elif operation == "unentered_cancel":
            if call.wb2_pending is not None or set(evidence["bases"]) & call.wb2_cancels_entered:
                raise ValueError("wbexit3_partial_unentered_cancel_not_proven")
            state["partial_cancel_bases"] = [b for b in state.get("partial_cancel_bases", []) if b not in evidence["bases"]]
        elif operation == "terminal":
            state["partial_terminal_reports"] = deepcopy(evidence["reports"])
            progress = Decimal(evidence["native_progress"])
            if progress < Decimal(state.get("partial_native_progress", "0")):
                raise ValueError("wbexit3_partial_native_execution_regressed")
            state["partial_native_progress"] = str(progress)
        elif operation == "sell":
            previous = state.get("partial_sell")
            request = evidence["request"]
            if previous is not None and previous != request:
                raise ValueError("wbexit3_partial_prior_SELL_requires_terminal_read")
            state["partial_sell"] = deepcopy(request)
        elif operation in {"retired_sell", "unsent"}:
            previous = state.get("partial_sell")
            if previous is not None and previous["coid"] != evidence["coid"]:
                raise ValueError("wbexit3_partial_retirement_identity_changed")
            state["partial_sell"] = None
        else:
            raise ValueError("wbexit3_partial_unknown_transition")
        state["revision"] += 1
        incident.payload = state
        return state
    state = await wbexit3._joined(service._run_db(unit, commit=True))
    current["state"] = state
    wbexit3._remember(service, state)
    return state


async def _previous_sell(service, call):
    """READ one prior exact scale order through the original canonical writer."""
    current = _context(service, call)
    state = current["state"]
    previous = state.get("partial_sell")
    if previous is None:
        return
    def snapshot(db):
        row, entry, actual = wbexit3._binding(service, db, call.account, call.symbol, call.managed_id)
        if actual != state["binding"]:
            raise ValueError("wbexit3_partial_previous_episode_changed")
        order = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == previous["coid"]))
        intent = db.get(TradeIntent, order.intent_id) if order is not None else None
        if (order is None or intent is None or order.strategy_id != entry.strategy_id
                or order.broker_account_id != entry.broker_account_id or order.symbol != call.symbol
                or order.side != "sell" or intent.intent_type != "scale"
                or (intent.payload or {}).get("metadata", {}).get("wbexit3_owner_id") != state["owner_id"]
                or order.quantity != Decimal(previous["quantity"])):
            raise ValueError("wbexit3_partial_previous_own_order_UNKNOWN")
        request = OrderRequest(order.client_order_id, call.account, "schwab_1m_v2", call.symbol, "sell", "scale",
            order.quantity, intent.reason, {**(order.payload or {}), "broker_order_id": order.broker_order_id or ""},
            order.order_type, order.time_in_force)
        return order.id, intent.id, request, order.broker_order_id
    order_id, intent_id, request, native_id = await wbexit3._joined(service._run_db(snapshot, commit=False))
    report = await wbexit2.joined(service, call, service.broker_adapter.fetch_order_update(request))
    check_call(service, call)
    if (report is None or report.origin != "broker" or not report.broker_order_id
            or native_id not in (None, report.broker_order_id) or report.client_order_id != request.client_order_id
            or report.symbol != call.symbol or report.side != "sell" or report.intent_type != "scale"
            or report.quantity != request.quantity
            or not report.filled_quantity.is_finite() or not 0 <= report.filled_quantity <= request.quantity
            or report.reported_at.tzinfo is None or report.reported_at > wbexit3._now()
            or report.event_type not in {"filled", "cancelled", "rejected", "expired"}
            or report.event_type == "filled" and report.filled_quantity != request.quantity
            or report.filled_quantity and (not report.broker_fill_id or report.fill_price is None
                or not report.fill_price.is_finite() or report.fill_price <= 0)):
        raise ValueError("wbexit3_partial_previous_SELL_terminal_read_UNKNOWN")
    from project_mai_tai.oms.bot_control_atr import _canonical_sell_reports
    event = TradeIntentEvent(source_service="oms-risk", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=call.account, symbol=call.symbol,
        side="sell", intent_type="scale", quantity=request.quantity, reason=request.reason, metadata=request.metadata))
    with service.session_factory() as db:
        order = db.get(BrokerOrder, order_id)
        if order is None or order.broker_order_id not in (None, report.broker_order_id):
            raise ValueError("wbexit3_partial_previous_native_ID_changed")
        events = await _canonical_sell_reports(service, db, order, db.get(TradeIntent, intent_id), event, request, report)
        entry = db.get(BrokerOrder, UUID(state["binding"]["entry_id"]))
        journal = deepcopy((entry.payload or {}).get("wbattach1") or {})
        if (journal.get("wbexit3_partial_owner_id") != state["owner_id"]
                or journal.get("wbexit3_partial_sell_coid") != request.client_order_id):
            raise ValueError("wbexit3_partial_previous_claim_not_this_operation")
        journal.pop("exit_phase", None)
        journal.pop("exit_call_id", None)
        journal.pop("wbexit3_partial_sell_coid", None)
        entry.payload = {**entry.payload, "wbattach1": journal}
        db.commit()
    for event_out in events:
        await service._publish_order_event(event_out)
    await _write(service, call, "retired_sell", coid=request.client_order_id)


async def handoff(service, row, call):
    from project_mai_tai.oms import webull_attach_riskoff as wb
    current = _context(service, call)
    key = (call.account, call.symbol)
    owners = service.__dict__.setdefault("_wbattach_exit_owners", {})
    if key in owners:
        raise ValueError("wbexit3_partial_current_owner_busy")
    owners[key], call.owner_key, call.entry_coid = call, key, row.entry_client_order_id
    await _previous_sell(service, call)
    journal, _, _, owned, _ = await wb._atr_state(service, call.account, call.symbol, call.entry_coid, allow_closed=True)
    if journal.get("wbexit2_owner_id") or journal.get("exit_phase") or journal.get("legacy_base") or journal.get("history_proven") is not True:
        raise ValueError("wbexit3_partial_original_history_or_other_close_UNKNOWN")
    attempts = journal["attempts"]
    if len(attempts) > 1:
        raise ValueError("wbexit3_partial_original_multi_attempt_census_UNKNOWN")
    state = current["state"]
    native_progress = Decimal(state.get("partial_native_progress", "0"))
    for attempt in attempts:
        base, quantity = attempt["base"], Decimal(attempt["quantity"])
        result = await wbexit2.joined(service, call, service.broker_adapter.confirm_exit_pair_from_today_list(
            broker_account_name=call.account, symbol=call.symbol, base_client_order_id=base,
            quantity=quantity, after=wbexit3._now(), original_attempts=attempts))
        wb._current_atr_call(service, row.id)
        reports = tuple(wb.validated_child_reads(result.reports, base, quantity, call.symbol, wbexit3._now()))
        call.wb2_state = {"originals": state.get("partial_originals", []),
                          "native_account_id": state.get("partial_native_account_id")}
        acquired = wbexit2.census(call, reports, base, attempts, wbexit3._now())
        if state.get("partial_originals") is None:
            if result.outcome == "released":
                wbexit2.positive_original_terminal(reports, base, quantity, call.symbol, wbexit3._now())
                prices = {r.client_order_id: None for r in reports}
            else:
                target, stop = wbexit2.original_geometry(reports if result.outcome == "reserved" else (), base, quantity, call.symbol)
                prices = {wb.WebullBrokerAdapter._combo_leg_coid(base, suffix): str(value)
                    for suffix, value in (("T", target), ("S", stop))}
            originals = [{"base": base, "quantity": str(quantity), "children": [
                {"coid": r.client_order_id, "native_id": r.broker_order_id, "price": prices[r.client_order_id]} for r in reports]}]
            state = await _write(service, call, "originals", originals=originals,
                native_account_id=call.wb2_native_account_id,
                zero_baseline_at=acquired.isoformat() if all(r.filled_quantity == 0 for r in reports) else None)
            call.wb2_state = {"originals": originals, "native_account_id": state["partial_native_account_id"]}
        else:
            wbexit2.check_native(call, reports)
        if result.outcome != "released":
            if state.get("partial_terminal_reports"):
                raise ValueError("wbexit3_partial_original_terminal_status_regressed")
            if base not in state.get("partial_cancel_bases", []):
                wbexit2.original_geometry(reports if result.outcome == "reserved" else (), base, quantity, call.symbol)
                call.wb3_partial_reserved_cancels = getattr(call, "wb3_partial_reserved_cancels", set()) | {base}
                await _write(service, call, "cancel", base=base)
                wb._current_atr_call(service, row.id)
                if not 0 <= (wbexit3._now() - acquired).total_seconds() <= 2:
                    raise ValueError("wbexit3_partial_original_working_proof_stale")
                call.wb2_cancels_entered.add(base)
                try:
                    await wbexit2.joined(service, call, service.broker_adapter.cancel_exit_pair(
                        broker_account_name=call.account, symbol=call.symbol, base_client_order_id=base))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # ACK/error is NEVER terminal evidence; the one bounded read below decides.
            result = await wbexit2.joined(service, call, service.broker_adapter.confirm_exit_pair_from_today_list(
                broker_account_name=call.account, symbol=call.symbol, base_client_order_id=base,
                quantity=quantity, after=wbexit3._now(), original_attempts=attempts))
            wb._current_atr_call(service, row.id)
            reports = tuple(wb.validated_child_reads(result.reports, base, quantity, call.symbol, wbexit3._now()))
            wbexit2.check_native(call, reports)
            acquired = wbexit2.census(call, reports, base, attempts, wbexit3._now())
        if result.outcome != "released":
            raise ValueError("wbexit3_partial_original_terminal_read_UNKNOWN")
        reports = wbexit2.positive_original_terminal(reports, base, quantity, call.symbol, wbexit3._now())
        def highwater(db):
            for observed in reports:
                child = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == observed.client_order_id))
                fills = list(db.scalars(select(Fill).where(Fill.order_id == child.id).limit(wbexit3.LIMIT + 1))) if child is not None else []
                if (child is not None and child.broker_order_id not in (None, observed.broker_order_id)
                        or len(fills) > wbexit3.LIMIT
                        or sum((f.quantity for f in fills), Decimal(0)) > observed.filled_quantity):
                    raise ValueError("wbexit3_partial_native_read_regressed_vs_actual_fills")
        await wbexit2.joined(service, call, service._run_db(highwater, commit=False))
        await wb._record_atr_children(service, call.account, call.symbol, call.entry_coid, attempt, reports)
        if await wb._atr_resolved_by_child(service, journal, call.account, call.symbol, call.entry_coid, base, reports):
            # The entire owned entry really executed. No timing assumption is
            # needed to conclude this partial was displaced by the native exit.
            await _write(service, call, "terminal", native_progress=str(sum((r.filled_quantity for r in reports), Decimal(0))),
                reports=[wb.report_payload(r) for r in reports])
            call.consumed = True
            return Decimal(0)
        filled = sum((r.filled_quantity for r in reports), Decimal(0))
        baseline = state.get("partial_zero_baseline_at")
        if filled and (baseline is None or any(r.filled_quantity and r.reported_at < datetime.fromisoformat(baseline) for r in reports)):
            raise ValueError("wbexit3_partial_native_fill_timing_ambiguous")
        native_progress = filled
        state = await _write(service, call, "terminal", native_progress=str(native_progress),
            reports=[wb.report_payload(r) for r in reports])
        def released(db, entry, managed, account, strategy, quantity_now, actual):
            actual["attempts"] = [{**a, "phase": "released", "terminal_proof": [wb.report_payload(r) for r in reports]}
                if a["base"] == base else a for a in actual["attempts"]]
        await wb._atr_state(service, call.account, call.symbol, call.entry_coid, released, allow_closed=True)
        call.list_proof_at = acquired
    _, _, _, owned, _ = await wb._atr_state(service, call.account, call.symbol, call.entry_coid)
    call.terminal_at = monotonic()
    call.wb2_positions = await wbexit2.joined(service, call,
        service.broker_adapter.read_wbexit2_positions(call.account, after=call.terminal_at))
    if not attempts:
        state = await _write(service, call, "originals", originals=[], native_account_id=call.wb2_positions[2])
        call.wb2_state = {"originals": [], "native_account_id": state["partial_native_account_id"]}
    def remaining(db):
        managed, entry, binding = wbexit3._binding(service, db, call.account, call.symbol, call.managed_id)
        snapshot = deepcopy(current["state"])
        wbexit3._execution_complete(service, db, snapshot, managed, entry)
        return Decimal(snapshot["requested"]) - Decimal(snapshot.get("filled_highwater", "0")) - native_progress
    requested = await wbexit2.joined(service, call, service._run_db(remaining, commit=False))
    if requested <= 0:
        return Decimal(0)
    quantity = min(requested, owned)
    if quantity != requested:
        raise ValueError("wbexit3_partial_exact_request_not_independently_owned")
    if wbexit2.fresh_ceiling(call, quantity) != quantity:
        raise ValueError("wbexit3_partial_fresh_native_ceiling_UNKNOWN")
    call.quantity, call.owned_quantity = quantity, owned
    def claim(db, entry, managed, account, strategy, quantity_now, actual):
        if (entry.status not in wb.TERMINAL or quantity_now != owned or actual.get("exit_phase")
                or any(a["phase"] != "released" for a in actual["attempts"])):
            raise ValueError("wbexit3_partial_owner_or_remainder_changed")
        actual.update(exit_phase="claimed", exit_call_id=call.call_id,
                      wbexit3_partial_owner_id=current["state"]["owner_id"])
    await wb._atr_state(service, call.account, call.symbol, call.entry_coid, claim)
    service._exit_reservation_released.add(key)
    await service._persist_webull_protect_state(call.account, call.symbol, "released")
    return quantity


async def validate_before_wire(service, call, request):
    from project_mai_tai.oms import webull_attach_riskoff as wb
    from project_mai_tai.oms.service import _managed_exit_session_matches_clock
    current = _context(service, call)
    wb._current_atr_call(service, call.managed_id)
    if (call.prepared is not None or request.intent_type != "scale" or request.side != "sell"
            or request.strategy_code != "schwab_1m_v2" or request.reason != call.reason
            or request.broker_account_name != call.account or request.symbol != call.symbol
            or request.quantity != call.quantity or request.time_in_force != "day"
            or request.order_type != "limit" or request.metadata.get("extended_hours") != "true"
            or request.metadata.get("wbexit3_owner_id") != current["state"]["owner_id"]
            or not _managed_exit_session_matches_clock(request.metadata)):
        raise ValueError("wbexit3_partial_exact_prepared_request_changed")
    call.prepared = wb._request_binding(request)
    def check(db):
        entry, row, account, strategy, owned = wb._atr(service, db, call.account, call.symbol, call.entry_coid, prepared=request)
        journal = entry.payload[wb.KEY]
        if (entry.status not in wb.TERMINAL or owned != call.owned_quantity or journal.get("exit_phase") != "claimed"
                or journal.get("exit_call_id") != call.call_id
                or journal.get("wbexit3_partial_owner_id") != current["state"]["owner_id"]):
            raise ValueError("wbexit3_partial_current_claim_changed")
        for attempt in journal["attempts"]:
            reports = [wb.saved_report(p) for p in attempt.get("terminal_proof", [])]
            if attempt["phase"] != "released":
                raise ValueError("wbexit3_partial_original_tree_not_released")
            wbexit2.positive_original_terminal(reports, attempt["base"], Decimal(attempt["quantity"]), call.symbol, wbexit3._now())
            wbexit2.check_native(call, reports)
    await wbexit2.joined(service, call, service._run_db(check, commit=False))
    await _write(service, call, "sell", request=wbexit2.request_snapshot(request))
    # The mark uses the existing strict prepared reader rather than its
    # unprepared pending-order refusal. No extra native read intervenes.
    def mark_prepared(db):
        entry, _, _, _, _ = wb._atr(service, db, call.account, call.symbol, call.entry_coid, prepared=request)
        journal = deepcopy(entry.payload[wb.KEY])
        journal["wbexit3_partial_sell_coid"] = request.client_order_id
        entry.payload = {**entry.payload, wb.KEY: journal}
    await wbexit2.joined(service, call, service._run_db(mark_prepared, commit=True))
    if (wbexit2.fresh_ceiling(call, call.quantity) != call.quantity
            or call.list_proof_at is not None and not 0 <= (wbexit3._now() - call.list_proof_at).total_seconds() <= 2
            or not _managed_exit_session_matches_clock(request.metadata)):
        raise ValueError("wbexit3_partial_final_positions_or_session_changed")
    wb._current_atr_call(service, call.managed_id)


async def submit_current(service, call, request):
    from project_mai_tai.oms import webull_attach_riskoff as wb
    current = _context(service, call)
    wb._current_atr_call(service, call.managed_id)
    if (call.wb2_sell_started is not None or current["state"].get("partial_sell") != wbexit2.request_snapshot(request)):
        raise ValueError("wbexit3_partial_unreserved_or_duplicate_wire")
    call.wb2_sell_started = monotonic()
    return await wbexit2.joined(service, call, service.broker_adapter.submit_order(request))


async def clear_proven_unsent(service, call, request):
    # Called ONLY after the existing release_atr_proven_unsent validates the
    # actual creator's committed O14 abort receipt, including no fills/events.
    if call.wb2_pending is not None or call.wb2_sell_started is not None:
        raise ValueError("wbexit3_partial_unposted_cleanup_after_wire_or_pending_IO")
    await _write(service, call, "unsent", coid=request.client_order_id)


def unsent_creator(service, call, request, managed_id):
    """Identify the physically joined actual O14 creator for NON-WIRE cleanup.

    Its deadline/STOP may have expired; that never permits another wire. The
    caller is exclusively the existing positive-abort receipt validator, whose
    full DB audit remains required before it can clear this exact claim.
    """
    from project_mai_tai.oms import webull_attach_riskoff as wb
    current = _context(service, call)
    if (wb._ATR_CALL.get() is not call or call.managed_id != str(managed_id)
            or call.wb2_pending is not None or call.wb2_sell_started is not None or call.wb2_prepared is None
            or service.__dict__.get("_wbattach_exit_owners", {}).get(call.owner_key) is not call
            or wbexit2.request_snapshot(call.wb2_prepared[1]) != wbexit2.request_snapshot(request)
            or request.metadata.get("wbexit3_owner_id") != current["state"]["owner_id"]):
        raise ValueError("wbexit3_partial_unsent_not_actual_joined_O14_creator")
    return call


async def finish_created_unsent(service, call):
    """Actual current creator only; physical IO is joined before known-unsent cleanup."""
    _context(service, call, allow_consumed=True)
    unentered = getattr(call, "wb3_partial_reserved_cancels", set()) - call.wb2_cancels_entered
    if unentered:
        await _write(service, call, "unentered_cancel", bases=sorted(unentered))
    if call.consumed or call.wb2_sell_started is not None:
        return
    from project_mai_tai.oms import webull_attach_riskoff as wb
    if call.wb2_prepared is None:
        if call.entry_coid and call.prepared is None:
            await wb._release_unprepared_software_handoff(service, call)
            # Capture identity on-loop; the DB unit cannot inspect asyncio tasks.
            identity = _context(service, call)["state"]["binding"]["entry_id"]
            def read_released(db):
                entry = db.get(BrokerOrder, UUID(identity))
                journal = (entry.payload or {}).get(wb.KEY) or {}
                return journal.get("exit_call_id") != call.call_id or not journal.get("exit_phase")
            if await wbexit2.joined(service, call, service._run_db(read_released, commit=False)):
                call.consumed = True
        return
    from project_mai_tai.oms.managed_sell_prewire import abandon_unposted_sell
    intent_id, request = call.wb2_prepared
    receipt = await wbexit2.joined(service, call, abandon_unposted_sell(
        service, request=request, intent_id=intent_id, now=wbexit3._now()))
    if call.prepared is None:
        call.prepared = wb._request_binding(request)
    await wb.release_atr_proven_unsent(service, request, call.managed_id, abort_event_id=receipt)
