"""ORB country-route ownership and post-fill protection, never ATR impersonation."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from datetime import UTC, datetime, time
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
import asyncio

from sqlalchemy import or_, select, text
from sqlalchemy.orm import aliased

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, OmsManagedPosition, Strategy, SystemIncident, TradeIntent, VirtualPosition
from project_mai_tai.oms.bot_control_scope import controlled_live_scope
from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload

PROTECTION = "orbroute1_protection"
_ET = ZoneInfo("America/New_York")


async def verify_country_admission(service, event, now):
    from project_mai_tai.momentum_live_sec import CountryRouter, CountryStore
    router = CountryRouter(CountryStore(service.session_factory))
    await router.reload_async()
    decision = router.route_for(event.payload.symbol, now)
    metadata = event.payload.metadata
    stamp = decision.as_of.isoformat() if decision.as_of else ""
    return (decision.broker == metadata.get("orb_country_broker")
            and decision.reason == metadata.get("orb_country_reason")
            and decision.status == metadata.get("orb_country_status")
            and stamp == metadata.get("orb_country_as_of"))


def orb_route_owner_conflict(session, account_id, account_name, symbol, strategy_id, *, admission=False):
    """Shared owner census plus ORB retained/UNKNOWN fences, never a dispatch grant."""
    from project_mai_tai.oms.route_owner import route_owner_conflict
    incoming = session.scalar(select(Strategy.code).where(Strategy.id == strategy_id))
    if incoming is None:
        raise ValueError("orb_incoming_owner_missing")
    if admission:
        from project_mai_tai.fanout_segment_store import current_session_anchor
        from project_mai_tai.oms.route_owner import _stale_row, _utc_row_time
        clock = datetime.now(UTC)
        session_start = current_session_anchor(clock)

        def stale_admission_order(order, owner):
            anchor = _utc_row_time(order.submitted_at)
            if anchor is None and order.intent_id is not None:
                parent = session.get(TradeIntent, order.intent_id)
                if (parent is not None and parent.strategy_id == order.strategy_id
                        and parent.broker_account_id == account_id and parent.symbol == symbol):
                    anchor = _utc_row_time(parent.created_at)
            return _stale_row(anchor, now=clock, session_start=session_start,
                account=account_name, symbol=symbol, owner=owner, row_id=order.id)

        shared = route_owner_conflict(session, account_id=account_id, account_name=account_name,
                                      symbol=symbol, incoming_strategy=incoming, now=clock)
        if shared is not None:
            return shared, "shared-account-reservation"
    # Admission's account-net mismatch must not block attribution of an exact
    # historical native fill after the broker has already gone flat.
    positions = session.execute(select(VirtualPosition, Strategy.code).join(Strategy).where(
        VirtualPosition.broker_account_id == account_id, VirtualPosition.symbol == symbol,
        VirtualPosition.strategy_id != strategy_id,
        or_(VirtualPosition.quantity.is_(None), VirtualPosition.quantity != 0),
    )).all()
    if positions:
        row, owner = positions[0]
        return owner, f"virtual:{row.id}"
    orders = session.execute(select(BrokerOrder, Strategy.code).join(Strategy).where(
        BrokerOrder.broker_account_id == account_id, BrokerOrder.symbol == symbol,
        BrokerOrder.strategy_id != strategy_id,
        or_(BrokerOrder.status.is_(None), BrokerOrder.status.not_in(
            {"filled", "cancelled", "rejected", "expired", "aborted"})),
    )).all()
    if orders:
        if not admission:
            row, owner = orders[0]
            return owner, row.client_order_id
        for row, owner in orders:
            if not stale_admission_order(row, owner):
                return owner, row.client_order_id
    protections = session.execute(select(BrokerOrder, Strategy.code).join(Strategy).where(
        BrokerOrder.broker_account_id == account_id, BrokerOrder.symbol == symbol,
        BrokerOrder.strategy_id != strategy_id,
        BrokerOrder.payload[PROTECTION]["phase"].as_string().in_(
            {"submitting", "accepted", "working", "unknown"}),
    ))
    if admission:
        for row, owner in protections.all():
            if not stale_admission_order(row, owner):
                return owner, row.client_order_id
    else:
        protections = protections.first()
    if not admission and protections:
        row, owner = protections
        return owner, row.client_order_id
    owner_code = session.scalar(select(Strategy.code).where(Strategy.id == strategy_id))
    managed = session.scalar(select(OmsManagedPosition).where(
        OmsManagedPosition.broker_account_name == account_name,
        OmsManagedPosition.symbol == symbol, OmsManagedPosition.status == "open",
        OmsManagedPosition.strategy_code != owner_code,
    ))
    return (managed.strategy_code, managed.entry_client_order_id or str(managed.id)) if managed else None


def entry_quantity(entry) -> Decimal:
    quantity = entry.quantity
    expected = Decimal("10") if (entry.payload or {}).get("orb_country_route") == "true" else Decimal("2")
    if not quantity.is_finite() or quantity != expected:
        raise ValueError("orb_entry_quantity_binding_invalid")
    return quantity


def _rth(now):
    local = now.astimezone(_ET)
    return local.weekday() < 5 and time(9, 30) <= local.time() < time(16)


def _snapshot(service, session, entry_id, *, stop_context=None, attach_context=None):
    entry = session.scalar(select(BrokerOrder).where(BrokerOrder.id == entry_id).with_for_update())
    if entry is None:
        raise ValueError("orb_entry_missing")
    account = session.get(BrokerAccount, entry.broker_account_id)
    strategy = session.get(Strategy, entry.strategy_id)
    metadata = dict(entry.payload or {})
    if (strategy is None or strategy.code != "orb_schwab" or account is None
            or not controlled_live_scope(strategy.code, strategy.execution_mode, account.name) or not account.is_active
            or account.provider != "webull" or account.name != service.settings.orb_webull_account_name
            or entry.side != "buy" or not entry.client_order_id or not entry.broker_order_id
            or metadata.get("orb_country_route") != "true" or metadata.get("orb_country_broker") != "webull"):
        raise ValueError("orb_entry_identity_mismatch")
    entry_quantity(entry)
    latest = session.scalar(select(BrokerOrder.id).where(
        BrokerOrder.strategy_id == strategy.id, BrokerOrder.broker_account_id == account.id,
        BrokerOrder.symbol == entry.symbol, BrokerOrder.side == "buy",
    ).order_by(BrokerOrder.submitted_at.desc(), BrokerOrder.id.desc()).limit(1))
    if latest != entry.id:
        raise ValueError("orb_entry_replaced")
    if stop_context is not None and attach_context is not None:
        raise ValueError("orb_multiple_exit_contexts")
    if attach_context is not None:
        attach_context.validate_native_owner(session, entry)
        conflict = None
    elif stop_context is not None:
        # Internal STOP owner only: shared holdings are not permission to cancel
        # them, nor a veto on accounting/releasing our exact validated tree.
        stop_context.validate_native_owner(session, entry)
        conflict = None
    else:
        conflict = orb_route_owner_conflict(session, account.id, account.name, entry.symbol, strategy.id)
    if conflict:
        service.logger.warning("[ROUTE-OWNER-CONFLICT] account=%s symbol=%s incoming_owner=orb_schwab "
                               "existing_owner=%s existing_coid=%s", account.name, entry.symbol, *conflict)
        raise ValueError("orb_foreign_owner")
    buys = session.scalars(select(Fill).where(
        Fill.order_id == entry.id, Fill.strategy_id == strategy.id,
        Fill.broker_account_id == account.id, Fill.symbol == entry.symbol, Fill.side == "buy",
        Fill.broker_fill_id.is_not(None),
    )).all()
    sold = session.scalars(select(Fill).join(BrokerOrder, BrokerOrder.id == Fill.order_id).where(
        Fill.strategy_id == strategy.id, Fill.broker_account_id == account.id,
        Fill.symbol == entry.symbol, Fill.side == "sell", or_(
            BrokerOrder.intent_id == entry.intent_id,
            BrokerOrder.payload["entry_order_id"].as_string() == str(entry.id),
        ),
    )).all()
    if any(not f.broker_fill_id or not f.quantity.is_finite() or not f.price.is_finite()
           or f.quantity <= 0 or f.price <= 0
           for f in [*buys, *sold]):
        raise ValueError("orb_fill_unproven")
    bought = sum((f.quantity for f in buys), Decimal(0))
    remaining = bought - sum((f.quantity for f in sold), Decimal(0))
    position = service.store.get_virtual_position(session, strategy_id=strategy.id,
        broker_account_id=account.id, symbol=entry.symbol)
    if (not 0 < bought <= entry.quantity or position is None
            or position.quantity != remaining or not 0 <= remaining <= entry.quantity):
        raise ValueError("orb_owned_balance_unproven")
    state = metadata.get(PROTECTION, {})
    if not isinstance(state, dict):
        raise ValueError("orb_protection_state_corrupt")
    return entry, account, strategy, remaining, state


async def _broker_owned(service, account, symbol, quantity):
    async with asyncio.timeout(2):
        rows = await service.broker_adapter.list_account_positions(account)
    matches = [r for r in rows if r.symbol == symbol]
    if (len(matches) != 1 or matches[0].broker_account_name != account
            or not matches[0].quantity.is_finite() or matches[0].quantity < quantity):
        raise ValueError("orb_broker_backing_unproven")
    if matches[0].quantity > quantity:
        service.logger.info("[ORB-PROTECTION-SHARED-BACKING] account=%s symbol=%s "
                            "broker_quantity=%s owned_protection_quantity=%s excess_untouched=1",
                            account, symbol, matches[0].quantity, quantity)


async def _phase(service, entry_id, base, phase, *, unsent=False):
    def write(session):
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.id == entry_id).with_for_update())
        state = dict((entry.payload or {}).get(PROTECTION) or {}) if entry else {}
        if state.get("base") != base:
            raise ValueError("orb_protection_owner_changed")
        entry.payload = {**entry.payload, PROTECTION: {**state, "phase": phase,
            **({"no_wire": "window_elapsed_before_post"} if unsent else {})}}
    await service._run_db(write)


def _record_zero_execution_receipts(service, session, entry_id, reports, *, stop_context=None, attach_context=None):
    entry, account, strategy, _, state = _snapshot(service, session, entry_id,
        stop_context=stop_context, attach_context=attach_context)
    base, quantity = state.get("base"), Decimal(state.get("quantity", "0"))
    if not base or not 0 < quantity <= entry.quantity:
        raise ValueError("orb_protection_binding_missing")
    child_ids = {WebullBrokerAdapter._combo_leg_coid(base, suffix) for suffix in ("T", "S")}
    intent = session.get(TradeIntent, UUID(state["intent_id"]))
    if (intent is None or intent.strategy_id != strategy.id or intent.broker_account_id != account.id
            or intent.symbol != entry.symbol or intent.side != "sell" or intent.intent_type != "close"
            or intent.quantity != quantity or intent.reason != "ORB_ATTACH_PROTECTION"
            or (intent.payload or {}).get("metadata", {}).get("entry_order_id") != str(entry.id)):
        raise ValueError("orb_protection_intent_mismatch")
    terminal = dict(state.get("terminal_children") or {})
    for report in reports:
        if report is None:
            continue
        if (report.client_order_id not in child_ids or report.symbol != entry.symbol
                or report.side != "sell" or report.intent_type not in {"close", "cancel"}):
            raise ValueError("orb_protection_child_identity_mismatch")
        if report.origin == "unknown" and report.filled_quantity == 0:
            continue
        if (report.origin != "broker" or report.reported_at.tzinfo is None
                or report.reported_at > datetime.now(UTC) or not report.filled_quantity.is_finite()
                or not 0 <= report.filled_quantity <= quantity
                or (report.event_type == "filled" and report.filled_quantity != quantity)):
            raise ValueError("orb_protection_child_execution_unknown")
        from project_mai_tai.oms.webull_attach_riskoff import confirmed_child_absence
        absent = confirmed_child_absence(report)
        if report.event_type in {"filled", "cancelled", "rejected"} and (
            report.metadata.get("orbroute1_fresh_read") == "true"
            or report.event_type == "filled"
            or report.metadata.get("eod_exit_pair_confirmation") == "true"
        ):
            if (not report.broker_order_id and not absent) or report.reported_at.tzinfo is None:
                raise ValueError("orb_protection_terminal_identity_unknown")
            terminal[report.client_order_id] = {
                "event_type": report.event_type, "broker_order_id": report.broker_order_id,
                "quantity": str(report.quantity), "origin": report.origin,
                "filled_quantity": str(report.filled_quantity),
                "fill_price": str(report.fill_price) if report.fill_price is not None else None,
                "broker_fill_id": report.broker_fill_id, "reported_at": report.reported_at.isoformat(),
                "metadata": dict(report.metadata),
            }
    if terminal != state.get("terminal_children", {}):
        entry.payload = {**entry.payload, PROTECTION: {**state, "terminal_children": terminal}}


async def record_protection_reports(service, entry_id, reports, *, stop_context=None, attach_context=None):
    """Record a proven sibling even when the other child's read is UNKNOWN."""
    if type(reports) in (list, tuple) and all(
        report is None or isinstance(report, ExecutionReport) and type(report.filled_quantity) is Decimal
        and report.filled_quantity.is_finite() and report.filled_quantity == 0
        for report in reports
    ):
        receipts = tuple(reports)
        await service._run_db(lambda session: _record_zero_execution_receipts(
            service, session, entry_id, receipts, stop_context=stop_context, attach_context=attach_context))
        return
    with service.session_factory() as session:
        entry, account, strategy, _, state = _snapshot(service, session, entry_id,
            stop_context=stop_context, attach_context=attach_context)
        base, quantity = state.get("base"), Decimal(state.get("quantity", "0"))
        if not base or not 0 < quantity <= entry.quantity:
            raise ValueError("orb_protection_binding_missing")
        child_ids = {WebullBrokerAdapter._combo_leg_coid(base, suffix) for suffix in ("T", "S")}
        intent = session.get(TradeIntent, UUID(state["intent_id"]))
        if (intent is None or intent.strategy_id != strategy.id or intent.broker_account_id != account.id
                or intent.symbol != entry.symbol or intent.side != "sell" or intent.intent_type != "close"
                or intent.quantity != quantity or intent.reason != "ORB_ATTACH_PROTECTION"
                or (intent.payload or {}).get("metadata", {}).get("entry_order_id") != str(entry.id)):
            raise ValueError("orb_protection_intent_mismatch")
        published = []
        terminal = dict(state.get("terminal_children") or {})
        for report in reports:
            if report is None:
                continue
            if (report.client_order_id not in child_ids or report.symbol != entry.symbol
                    or report.side != "sell" or report.intent_type not in {"close", "cancel"}):
                raise ValueError("orb_protection_child_identity_mismatch")
            if report.origin == "unknown" and report.filled_quantity == 0:
                # No execution or terminal authority from this sibling. Do not
                # roll back the other child's independently proven execution.
                continue
            if (report.origin != "broker" or report.reported_at.tzinfo is None
                    or report.reported_at > datetime.now(UTC) or not report.filled_quantity.is_finite()
                    or not 0 <= report.filled_quantity <= quantity
                    or (report.event_type == "filled" and report.filled_quantity != quantity)):
                raise ValueError("orb_protection_child_execution_unknown")
            from project_mai_tai.oms.webull_attach_riskoff import confirmed_child_absence
            absent = confirmed_child_absence(report)
            if report.event_type in {"filled", "cancelled", "rejected"} and (
                report.metadata.get("orbroute1_fresh_read") == "true"
                or report.event_type == "filled"
                or report.metadata.get("eod_exit_pair_confirmation") == "true"
            ):
                if (not report.broker_order_id and not absent) or report.reported_at.tzinfo is None:
                    raise ValueError("orb_protection_terminal_identity_unknown")
                terminal[report.client_order_id] = {
                    "event_type": report.event_type, "broker_order_id": report.broker_order_id,
                    "quantity": str(report.quantity), "origin": report.origin,
                    "filled_quantity": str(report.filled_quantity),
                    "fill_price": str(report.fill_price) if report.fill_price is not None else None,
                    "broker_fill_id": report.broker_fill_id, "reported_at": report.reported_at.isoformat(),
                    "metadata": dict(report.metadata),
                }
            if report.filled_quantity == 0:
                continue
            if (not report.broker_order_id or not report.broker_fill_id or report.fill_price is None
                    or not report.fill_price.is_finite() or report.fill_price <= 0
                    or not report.filled_quantity.is_finite() or not 0 < report.filled_quantity <= quantity
                    or report.reported_at.tzinfo is None or report.reported_at > datetime.now(UTC)):
                raise ValueError("orb_protection_execution_unproven")
            child = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == report.client_order_id))
            if child is not None and (child.strategy_id != strategy.id or child.broker_account_id != account.id
                    or child.symbol != entry.symbol or child.side != "sell"
                    or child.intent_id != intent.id or (child.broker_order_id is not None
                        and child.broker_order_id != report.broker_order_id)):
                raise ValueError("orb_protection_persisted_child_identity_mismatch")
            fills = session.scalars(select(Fill).where(Fill.order_id == child.id)).all() if child else []
            previous = sum((fill.quantity for fill in fills), Decimal(0))
            previous_cash = sum((fill.quantity * fill.price for fill in fills), Decimal(0))
            if report.filled_quantity < previous:
                raise ValueError("orb_protection_cumulative_execution_decreased")
            cumulative_cash = report.filled_quantity * report.fill_price
            delta_quantity = report.filled_quantity - previous
            delta_cash = cumulative_cash - previous_cash
            # Fill.price is Numeric(18, 8); a derived ratio can repeat. A
            # quantity-identical replay may differ only by its storage rounding.
            cash_roundoff = previous * Decimal("0.000000005")
            if not cumulative_cash.is_finite() or not previous_cash.is_finite() or (
                delta_quantity == 0 and abs(delta_cash) > cash_roundoff
            ) or (delta_quantity > 0 and (not delta_cash.is_finite() or delta_cash <= 0)):
                raise ValueError("orb_protection_cumulative_cash_inconsistent")
            _, _, _, remaining, _ = _snapshot(service, session, entry_id,
                stop_context=stop_context, attach_context=attach_context)
            if delta_quantity > remaining:
                raise ValueError("orb_protection_execution_exceeds_owned")
            # The generic writer derives delta quantity, but expects its price,
            # not the broker's cumulative VWAP. Keep raw evidence and time intact.
            # Preserve terminal status while admitting its proven execution to
            # the existing generic ledger's Webull terminal-partial contract.
            writer_report = replace(report,
                fill_price=delta_cash / delta_quantity if delta_quantity else report.fill_price,
                metadata={**report.metadata,
                    **({"webull_terminal_with_partial_fill": report.event_type}
                       if report.event_type in {"cancelled", "rejected"} else {}),
                    "orbroute1_broker_cumulative_quantity": str(report.filled_quantity),
                    "orbroute1_broker_cumulative_average": str(report.fill_price),
                    "orbroute1_broker_cumulative_cash": str(cumulative_cash),
                    "orbroute1_incremental_cash": str(delta_cash),
                    "orbroute1_fill_price_source": "derived_cumulative_cash_delta"})
            request = OrderRequest(report.client_order_id, account.name, "orb_schwab", entry.symbol,
                "sell", "close", quantity, "ORB_NATIVE_PROTECTION_EXECUTION",
                {"entry_order_id": str(entry.id), "orbroute1_child": "true"})
            event = TradeIntentEvent(source_service="oms-risk", payload=TradeIntentPayload(
                strategy_code="orb_schwab", broker_account_name=account.name,
                symbol=entry.symbol, side="sell", intent_type="close", quantity=quantity,
                reason=request.reason, metadata=dict(request.metadata)))
            published.extend(await service._record_order_reports(session=session, intent=intent,
                strategy_id=strategy.id, broker_account_id=account.id, intent_event=event,
                request=request, reports=[writer_report]))
        if terminal != state.get("terminal_children", {}):
            entry.payload = {**entry.payload, PROTECTION: {**state, "terminal_children": terminal}}
        session.commit()
    for event in published:
        await service._publish_order_event(event)


async def _probe(service, entry_id, account, symbol, state, *, fresh=False, stop_context=None, attach_context=None):
    reports = []
    for suffix in ("T", "S"):
        coid = WebullBrokerAdapter._combo_leg_coid(state["base"], suffix)
        cached = (state.get("terminal_children") or {}).get(coid)
        if cached is not None:
            reports.append(ExecutionReport(cached["event_type"], coid,
                broker_order_id=cached["broker_order_id"], broker_fill_id=cached["broker_fill_id"],
                symbol=symbol, side="sell", intent_type="close", quantity=Decimal(state["quantity"]),
                filled_quantity=Decimal(cached["filled_quantity"]),
                fill_price=Decimal(cached["fill_price"]) if cached["fill_price"] is not None else None,
                metadata=dict(cached["metadata"]), reported_at=datetime.fromisoformat(cached["reported_at"]),
                origin="broker"))
            continue
        try:
            report = await service.broker_adapter.fetch_order_update(OrderRequest(
                coid, account, "orb_schwab", symbol, "sell", "close", Decimal(state["quantity"]),
                "ORB_VERIFY_PROTECTION_CHILD", {"orbroute1_child": "true",
                **({"orbroute1_fresh_read": "true"} if fresh else {})}))
        except Exception:
            report = None
        reports.append(report)
    await record_protection_reports(service, entry_id, reports, stop_context=stop_context,
                                    attach_context=attach_context)
    return reports


async def release_protection(service, entry_id, *, stop_context=None, attach_context=None):
    def read(session):
        entry, account, _, _, state = _snapshot(service, session, entry_id,
            stop_context=stop_context, attach_context=attach_context)
        return account.name, entry.symbol, dict(state)
    account, symbol, state = await service._run_db(read, commit=False)
    if not state:
        return True
    if attach_context is not None:
        from project_mai_tai.oms.webull_attach_riskoff import orb_terminal_state_proven
        if orb_terminal_state_proven(state, account, symbol, datetime.now(UTC)):
            return True
    if state.get("phase") == "released":
        if attach_context is not None:
            from project_mai_tai.oms.webull_attach_riskoff import orb_terminal_state_proven
            return orb_terminal_state_proven(state, account, symbol, datetime.now(UTC))
        if state.get("no_wire") == "window_elapsed_before_post":
            return True
        expected = {WebullBrokerAdapter._combo_leg_coid(state["base"], s) for s in ("T", "S")}
        receipts = state.get("terminal_children") or {}
        if set(receipts) != expected:
            return False
        return all(r.get("broker_order_id") and r.get("event_type") in {"filled", "cancelled", "rejected"}
                   and (r["event_type"] == "filled" or (r.get("metadata") or {}).get("orbroute1_fresh_read") == "true")
                   for r in receipts.values())
    def claim_cancel(session):
        entry, _, _, _, current = _snapshot(service, session, entry_id,
            stop_context=stop_context, attach_context=attach_context)
        if current.get("base") != state["base"]:
            raise ValueError("orb_protection_release_owner_changed")
        if current.get("cancel_requested"):
            return False
        entry.payload = {**entry.payload, PROTECTION: {**current, "cancel_requested": True}}
        return True
    if await service._run_db(claim_cancel):
        # Persist before cancel; responses are activity, never terminal authority.
        await service.broker_adapter.cancel_exit_pair(
            broker_account_name=account, symbol=symbol, base_client_order_id=state["base"])
    if attach_context is not None:
        result = await service.broker_adapter.confirm_exit_pair_terminal(
            broker_account_name=account, symbol=symbol, base_client_order_id=state["base"])
        reports = result.reports
        await record_protection_reports(service, entry_id, reports, attach_context=attach_context)
    else:
        reports = await _probe(service, entry_id, account, symbol, state, fresh=True, stop_context=stop_context)
    expected = {WebullBrokerAdapter._combo_leg_coid(state["base"], s) for s in ("T", "S")}
    if (len(reports) != 2 or any(r is None for r in reports)
            or {r.client_order_id for r in reports} != expected
            or any(r.event_type not in {"filled", "cancelled", "rejected"} or r.origin != "broker"
                   for r in reports)):
        return False
    if attach_context is not None:
        from project_mai_tai.oms.webull_attach_riskoff import terminal_pair
        if not terminal_pair(reports, state["base"], Decimal(state["quantity"]), datetime.now(UTC), symbol=symbol):
            return False
    await _phase(service, entry_id, state["base"], "released")
    return True


async def read_terminal_protection_for_stop(service, entry_id, *, stop_context):
    """Original exact children, READ ONLY at venue: never cancel/re-attach/submit."""
    def read(session):
        entry, account, _, _, state = _snapshot(service, session, entry_id, stop_context=stop_context)
        if not state.get("base") or not state.get("cancel_requested"):
            raise ValueError("orb_stop_native_original_cancel_unknown")
        return account.name, entry.symbol, dict(state)
    account, symbol, state = await service._run_db(read, commit=False)
    reports = await _probe(service, entry_id, account, symbol, state, fresh=True, stop_context=stop_context)
    expected = {WebullBrokerAdapter._combo_leg_coid(state["base"], suffix) for suffix in ("T", "S")}
    if (len(reports) != 2 or any(report is None for report in reports)
            or {report.client_order_id for report in reports} != expected
            or any(report.origin != "broker" or not report.broker_order_id
                   or report.event_type not in {"filled", "cancelled", "rejected"} for report in reports)):
        return False
    await _phase(service, entry_id, state["base"], "released")
    return True


async def reconcile_one(service, entry_id):
    from project_mai_tai.oms.webull_attach_riskoff import maybe_orb_riskoff
    if await maybe_orb_riskoff(service, entry_id):
        return
    def read(session):
        entry, account, _, quantity, state = _snapshot(service, session, entry_id)
        return account.name, entry.symbol, quantity, dict(state), dict(entry.payload)
    account, symbol, quantity, state, metadata = await service._run_db(read, commit=False)
    if metadata.get("orb_schwab_eod_close"):
        # A claimed close owns release. Never attach a fresh pair between its
        # terminal child receipt and its one already-fenced sell.
        if state and state.get("phase") not in {"released", "rejected", "settled"}:
            await _probe(service, entry_id, account, symbol, state)
        return
    if state and state.get("phase") not in {"released", "rejected"}:
        reports = await _probe(service, entry_id, account, symbol, state)
        _, _, quantity, _, _ = await service._run_db(read, commit=False)
        if quantity != Decimal(state["quantity"]):
            if await release_protection(service, entry_id):
                if quantity > 0:
                    await reconcile_one(service, entry_id)
            return
        if all(r is not None and r.event_type == "accepted" and r.origin == "broker"
               and r.broker_order_id and r.quantity == Decimal(state["quantity"])
               and r.filled_quantity == 0 for r in reports):
            await _phase(service, entry_id, state["base"], "working")
            if state.get("phase") != "working":
                service.logger.info("[WEBULL-PROTECT-ATTACHED] strategy=orb_schwab account=%s "
                    "symbol=%s entry=%s base=%s qty=%s geometry=trigger_5_8", account, symbol,
                    entry_id, state["base"], state["quantity"])
        return
    if quantity <= 0 or not _rth(datetime.now(UTC)):
        return
    if state.get("phase") == "rejected":
        from project_mai_tai.oms.webull_attach_riskoff import orb_retry_terminal_proven
        if not orb_retry_terminal_proven(state, account, symbol, datetime.now(UTC)) or int(state.get("attempt", 1)) >= 5:
            return
        retry_at = datetime.fromisoformat(state["retry_at"])
        if datetime.now(UTC) < retry_at:
            return
    target = Decimal(metadata["orb_target_price"])
    protect = Decimal(metadata["orb_protect_price"])
    trigger = Decimal(metadata["stop_price"])
    if not all(p.is_finite() and p > 0 for p in (target, protect, trigger)) or not protect < trigger < target:
        raise ValueError("orb_protection_plan_invalid")
    from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata
    original = build_orb_schwab_bracket_metadata(Decimal(metadata["orb_intended_break_level"]))
    if (trigger != Decimal(original["stop_price"]) or target != Decimal(original["bracket_target_price"])
            or protect != Decimal(original["bracket_stop_price"])):
        raise ValueError("orb_protection_geometry_changed")
    await _broker_owned(service, account, symbol, quantity)
    if await service._webull_protect_unplaceable_reason(
            broker_account_name=account, symbol=symbol, target=float(target), protect=float(protect)):
        raise ValueError("orb_protection_prices_not_placeable")
    base = f"orbprot-{uuid4().hex[:24]}"
    def prepare(session):
        entry, acct, strategy, owned, current = _snapshot(service, session, entry_id)
        if owned != quantity or current != state:
            raise ValueError("orb_protection_preimage_changed")
        event = TradeIntentEvent(source_service="oms-risk", payload=TradeIntentPayload(
            strategy_code="orb_schwab", broker_account_name=acct.name, symbol=symbol,
            side="sell", intent_type="close", quantity=quantity, reason="ORB_ATTACH_PROTECTION",
            metadata={"entry_order_id": str(entry_id)}))
        allowed, reason = service._evaluate_risk(event)
        if not allowed:
            raise ValueError(f"orb_protection_risk:{reason}")
        intent = service.store.create_trade_intent(session, strategy=strategy, broker_account=acct, event=event)
        entry.payload = {**entry.payload, PROTECTION: {"phase": "submitting", "base": base,
            "attempt": int(state.get("attempt", 0)) + 1,
            "refused_history": [*state.get("refused_history", []), *([{
                key: state[key] for key in ("base", "intent_id", "quantity", "attempt", "phase")
            }] if state.get("original_terminal_proof") else [])],
            "quantity": str(quantity), "intent_id": str(intent.id),
            "target": str(target), "protect": str(protect), "trigger": str(trigger)}}
    await service._run_db(prepare)  # Durable prewire fence: restart is reconciliation, not a retry.
    if not _rth(datetime.now(UTC)):
        await _phase(service, entry_id, base, "released", unsent=True)
        return
    await _broker_owned(service, account, symbol, quantity)
    def prewire(session):
        _, _, _, current_quantity, current = _snapshot(service, session, entry_id)
        if current_quantity != quantity or current.get("base") != base or current.get("phase") != "submitting":
            raise ValueError("orb_protection_owner_changed_before_wire")
    await service._run_db(prewire, commit=False)
    if not _rth(datetime.now(UTC)):
        await _phase(service, entry_id, base, "released", unsent=True)
        return
    request = OrderRequest(base, account, "orb_schwab", symbol, "sell", "close", quantity,
        "ORB_ATTACH_PROTECTION", {"entry_order_id": str(entry_id), "webull_exit_only_pair": "true",
        "bracket_target_price": str(target), "bracket_stop_price": str(protect), "market_session": "RTH"})
    reports = await service.broker_adapter.submit_order(request)
    if (len(reports) == 1 and reports[0].event_type == "accepted" and reports[0].broker_order_id
            and reports[0].client_order_id == base and reports[0].symbol == symbol
            and reports[0].side == "sell"):
        await _phase(service, entry_id, base, "accepted")
        _, _, _, state, _ = await service._run_db(read, commit=False)
        await _probe(service, entry_id, account, symbol, state)
    elif reports and all(r.event_type == "rejected" and r.origin == "broker" for r in reports):
        await _phase(service, entry_id, base, "rejected")
        from project_mai_tai.oms.webull_attach_riskoff import reconcile_orb_rejection
        await reconcile_orb_rejection(service, entry_id, base)
        raise ValueError("orb_protection_broker_rejected")
    else:
        raise ValueError("orb_protection_submission_unknown")


async def reconcile_protection(service):
    lock = service.__dict__.setdefault("_orbroute1_protection_lock", asyncio.Lock())
    if lock.locked() or not service.settings.orb_webull_account_name:
        return
    async with lock:
        def work(session):
            newer = aliased(BrokerOrder)
            superseded = select(newer.id).where(
                newer.strategy_id == BrokerOrder.strategy_id,
                newer.broker_account_id == BrokerOrder.broker_account_id,
                newer.symbol == BrokerOrder.symbol, newer.side == "buy",
                or_(newer.submitted_at > BrokerOrder.submitted_at,
                    (newer.submitted_at == BrokerOrder.submitted_at) & (newer.id > BrokerOrder.id)),
            ).exists()
            has_buy_fill = select(Fill.id).where(
                Fill.order_id == BrokerOrder.id, Fill.side == "buy", Fill.quantity > 0,
            ).exists()
            return session.scalars(select(BrokerOrder.id).join(Strategy).join(
                BrokerAccount, BrokerAccount.id == BrokerOrder.broker_account_id,
            ).outerjoin(VirtualPosition, (
                (VirtualPosition.strategy_id == BrokerOrder.strategy_id)
                & (VirtualPosition.broker_account_id == BrokerOrder.broker_account_id)
                & (VirtualPosition.symbol == BrokerOrder.symbol)
            )).where(Strategy.code == "orb_schwab",
                BrokerAccount.name == service.settings.orb_webull_account_name,
                BrokerOrder.side == "buy", or_(
                    (~superseded) & or_(VirtualPosition.quantity > 0,
                        VirtualPosition.quantity.is_(None) & has_buy_fill),
                    BrokerOrder.payload[PROTECTION]["phase"].as_string().in_(
                        {"submitting", "accepted", "working", "unknown"})),
                BrokerOrder.payload["orb_country_route"].as_string() == "true",
            )).all()
        for entry_id in await service._run_db(work, commit=False):
            try:
                await reconcile_one(service, entry_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = str(exc)
                def incident(session):
                    existing = session.scalar(select(SystemIncident.id).where(
                        SystemIncident.payload["source"].as_string() == "orbroute1_protection_unknown",
                        SystemIncident.payload["entry_order_id"].as_string() == str(entry_id),
                        SystemIncident.status == "open",
                    ))
                    if existing:
                        return False
                    session.add(SystemIncident(service_name="oms-risk", severity="critical", status="open",
                        title="ORB Webull protection requires reconciliation", payload={
                            "source": "orbroute1_protection_unknown", "entry_order_id": str(entry_id),
                            "reason": reason}))
                    return True
                if await service._run_db(incident):
                    service.logger.error("[ORB-PROTECTION-UNKNOWN] entry=%s reason=%s no_extra_pair=1",
                                         entry_id, reason)


def spawn_reconciliation(service):
    if not service.settings.orb_webull_account_name:
        return
    running = service.__dict__.get("_orbroute1_reconciliation_task")
    if running is not None and not running.done():
        return
    task = asyncio.create_task(reconcile_protection(service))
    service._orbroute1_reconciliation_task = task
    tasks = service.__dict__.setdefault("_webull_protect_tasks", set())
    tasks.add(task)
    def finished(completed):
        tasks.discard(completed)
        if not completed.cancelled() and completed.exception() is not None:
            service.logger.error("[ORB-PROTECTION-UNKNOWN] reconciliation_task_failed=%s",
                                 type(completed.exception()).__name__)
    task.add_done_callback(finished)


def retain_owned_routed_position(session, position):
    """A broker snapshot cannot erase an exact routed ORB execution ledger."""
    entry = session.scalar(select(BrokerOrder).where(
        BrokerOrder.strategy_id == position.strategy_id,
        BrokerOrder.broker_account_id == position.broker_account_id,
        BrokerOrder.symbol == position.symbol, BrokerOrder.side == "buy",
    ).order_by(BrokerOrder.submitted_at.desc(), BrokerOrder.id.desc()).limit(1))
    if entry is None or (entry.payload or {}).get("orb_country_route") != "true":
        return False
    # Preserve even an uncertain balance for explicit reconciliation, never reset
    # ownership from absence while its native execution receipts may arrive late.
    return True


async def reprice_webull(service, session, intent, event, target):
    """Cancel/read zero BEFORE preparing one replacement; uncertain attempts stay fenced."""
    from project_mai_tai.orb_country_route import country_metadata, selection_from_metadata
    from project_mai_tai.orb_schwab_order_route import orb_schwab_intent_refusal

    account = session.get(BrokerAccount, target.broker_account_id)
    strategy = session.get(Strategy, target.strategy_id)
    metadata = dict(target.payload or {})
    selected = selection_from_metadata(target.symbol, account.name, metadata) if account else None
    if (selected is None or strategy is None or strategy.code != "orb_schwab"
            or not controlled_live_scope(strategy.code, strategy.execution_mode, account.name) or not account.is_active
            or account.provider != "webull" or account.name != event.payload.broker_account_name
            or target.symbol != event.payload.symbol or target.side != "buy"
            or not target.broker_order_id or not target.client_order_id
            or entry_quantity(target) != Decimal("10")
            or any(event.payload.metadata.get(k) != v for k, v in country_metadata(selected).items())
            or Decimal(event.payload.metadata["stop_price"]) <= Decimal(metadata["stop_price"])):
        raise ValueError("orb_webull_reprice_binding_invalid")
    refusal = orb_schwab_intent_refusal(event, service.settings, datetime.now(UTC))
    if refusal:
        raise ValueError(refusal)
    old_id, old_coid = target.broker_order_id, target.client_order_id
    if metadata.get("orb_replace_hold"):
        raise ValueError("orb_webull_reprice_already_owned")
    target.payload = {**metadata, "orb_replace_hold": {
        "phase": "cancelling", "event_id": str(event.event_id), "old_id": old_id,
        "desired": dict(event.payload.metadata),
    }}
    session.commit()
    cancel = OrderRequest(old_coid, account.name, "orb_schwab", target.symbol, "buy", "cancel",
        target.quantity, "ORB_REPRICE_CANCEL_OLD", {**metadata, "broker_order_id": old_id,
        "resting_entry_cancel": "true", "orb_reprice_cancel": "true"})
    await service.broker_adapter.submit_order(cancel)
    readback = await service.broker_adapter.read_atr_resting_buy_after_cancel(cancel)
    if not readback.can_replace or readback.broker_order_id != old_id:
        raise ValueError("orb_webull_reprice_cancel_not_terminal_zero")
    session.expire_all()
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                        {"key": f"orb-v2:{event.payload.broker_account_name}:{event.payload.symbol.upper()}"})
    target = session.scalar(select(BrokerOrder).where(BrokerOrder.id == target.id).with_for_update())
    hold = (target.payload or {}).get("orb_replace_hold") or {}
    if (hold.get("event_id") != str(event.event_id) or hold.get("old_id") != old_id
            or target.broker_order_id != old_id or target.client_order_id != old_coid
            or target.status not in {*service.store.OPEN_ORDER_STATUSES, "cancelled"}):
        raise ValueError("orb_webull_reprice_owner_changed_after_cancel")
    old_fills = session.scalar(select(Fill.id).where(Fill.order_id == target.id).limit(1))
    if old_fills is not None:
        raise ValueError("orb_webull_reprice_fill_arrived")
    conflict = orb_route_owner_conflict(session, account.id, account.name, target.symbol, strategy.id,
                                        admission=True)
    if conflict:
        raise ValueError("orb_webull_reprice_foreign_owner")
    target.status = "cancelled"
    target.payload = {**target.payload, "orb_replace_hold": {
        **target.payload["orb_replace_hold"], "phase": "cancelled_zero"}}
    metadata = {k: v for k, v in event.payload.metadata.items() if k != "orb_schwab_reprice"}
    metadata["orb_replacement_of"] = str(target.id)
    replacement = TradeIntentEvent(source_service="orb-schwab", payload=TradeIntentPayload(
        strategy_code="orb_schwab", broker_account_name=account.name, symbol=target.symbol,
        side="buy", intent_type="open", quantity=target.quantity,
        reason="ORB_REPLACE_PROVEN_UNFILLED_REST", metadata=metadata))
    allowed, reason = service._evaluate_risk(replacement)
    if not allowed:
        raise ValueError(reason)
    coid = f"orb-r-{event.event_id.hex[:24]}"
    request = OrderRequest(coid, account.name, "orb_schwab", target.symbol, "buy", "open",
        target.quantity, replacement.payload.reason, metadata, "STOP_LIMIT", "day")
    new_intent = service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=replacement)
    service.store.get_or_create_order(session, intent=new_intent, strategy_id=strategy.id,
        broker_account_id=account.id, client_order_id=coid, symbol=target.symbol, side="buy",
        quantity=target.quantity, metadata=metadata, status="pending", order_type="STOP_LIMIT")
    session.commit()  # A crash after this fence is reconciled, never blindly resubmitted.
    if orb_schwab_intent_refusal(event, service.settings, datetime.now(UTC)):
        raise ValueError("orb_webull_reprice_window_elapsed")
    reports = await service.broker_adapter.submit_order(request)
    if not reports or any(
        report.client_order_id != coid or report.symbol != request.symbol or report.side != "buy"
        or report.intent_type != "open" or not report.filled_quantity.is_finite()
        or not 0 <= report.filled_quantity <= request.quantity for report in reports
    ):
        raise ValueError("orb_webull_reprice_dispatch_receipt_unknown")
    result = await service._record_order_reports(session=session, intent=new_intent,
        strategy_id=strategy.id, broker_account_id=account.id, intent_event=replacement,
        request=request, reports=reports)
    service.store.mark_intent_status(intent, "submitted")
    return result
