"""ATR STOP is a reason of the existing claimed managed-exit owner, never a second seller."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time
from decimal import Decimal, ROUND_FLOOR
from hashlib import sha256
import json
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.db.models import (
    AccountPosition, BotControlAttempt, BrokerAccount, BrokerOrder, Fill,
    OmsManagedPosition, Strategy, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.bot_control import ControlConflict
from project_mai_tai.oms.bot_control_evidence import positions_after_protection
from project_mai_tai.oms.bot_control_executor import StopProgress
from project_mai_tai.oms.bot_control_scope import controlled_live_scope
from project_mai_tai.momentum_live_sec import US_MARKET_HOLIDAYS

STOP_REASON = "KILLSW1_STOP"
HELD_BOOT_MESSAGE = "STOP: held across restart \u2014 sell manually or wait for normal exit"
STRATEGY = "schwab_1m_v2"
TERMINAL = {"filled", "cancelled", "canceled", "rejected", "expired", "aborted"}
READABLE = (TERMINAL - {"aborted"}) | {"accepted", "working", "open", "partially_filled"}
BOUND = 512
_ET = ZoneInfo("America/New_York")
_CONTEXT = ContextVar("killsw1_atr_stop", default=None)


@dataclass
class StopContext:
    service: object
    lease: object
    journal: object
    managed_id: str
    phase: str = "BLOCKED_UNKNOWN"
    owner_task: object = None


def current_stop(service, managed_id=""):
    context = _CONTEXT.get()
    if (context is None or context.service is not service
            or (managed_id and context.managed_id != str(managed_id))):
        return None
    return context


def _number(value):
    if type(value) is int:
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ControlConflict("atr_stop_quantity_UNKNOWN")
    return value


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _native(entry, order):
    from project_mai_tai.oms.service import oco_exit_client_order_id
    return (order.intent_id == entry.intent_id and order.order_type == "oco_exit"
            and bool(order.broker_order_id) and order.client_order_id == oco_exit_client_order_id(
                entry.client_order_id, order.broker_order_id))


def _managed(service, session, lease, identity, *, unsent_order=None, protection_terminal=False,
             allow_closed_original=False):

    row = session.get(OmsManagedPosition, UUID(str(identity)))
    strategy = session.get(Strategy, lease.strategy_id)
    account = session.scalar(select(BrokerAccount).where(
        BrokerAccount.name == row.broker_account_name)) if row is not None else None
    if (row is None or row.strategy_code != STRATEGY or (row.status != "open" and not (
            allow_closed_original and row.status == "closed" and row.current_quantity == 0))
            or strategy is None or strategy.code != STRATEGY
            or account is None or account.id not in lease.account_ids
            or not controlled_live_scope(strategy.code, strategy.execution_mode, account.name)
            or account.provider not in {"schwab", "webull"}
            or not row.entry_order_id or not row.entry_client_order_id):
        raise ControlConflict("atr_stop_managed_binding_UNKNOWN")
    entry = session.get(BrokerOrder, row.entry_order_id)
    if (entry is None or entry.strategy_id != strategy.id or entry.broker_account_id != account.id
            or entry.symbol != row.symbol or entry.side != "buy" or not entry.broker_order_id
            or entry.client_order_id != row.entry_client_order_id
            or entry.status not in TERMINAL or not entry.submitted_at):
        raise ControlConflict("atr_stop_entry_terminal_UNKNOWN")
    from project_mai_tai.oms.webull_c6_proof import inventory, original_child
    originals = inventory(session, row, entry, account)
    if row.status == "closed" and not originals:
        raise ControlConflict("atr_stop_closed_original_inventory_UNKNOWN")

    def quantity(order):
        total = Decimal(0)
        for fill in session.scalars(select(Fill).where(Fill.order_id == order.id)).all():
            amount = _number(fill.quantity)
            if (fill.strategy_id != strategy.id or fill.broker_account_id != account.id
                    or fill.symbol != row.symbol or fill.side != order.side):
                raise ControlConflict("atr_stop_execution_binding_UNKNOWN")
            if amount and (not fill.broker_fill_id or not isinstance(fill.price, Decimal)
                           or not fill.price.is_finite() or fill.price <= 0):
                raise ControlConflict("atr_stop_execution_identity_UNKNOWN")
            total += amount
        if total > _number(order.quantity) or (order.status == "filled" and total != order.quantity):
            raise ControlConflict("atr_stop_order_execution_bounds_UNKNOWN")
        return total

    entered, exited = quantity(entry), Decimal(0)
    if not 0 < entered <= _number(entry.quantity) or (
            entry.status == "filled" and entered != entry.quantity):
        raise ControlConflict("atr_stop_entry_execution_UNKNOWN")
    sells = session.scalars(select(BrokerOrder).where(
        BrokerOrder.strategy_id == strategy.id, BrokerOrder.broker_account_id == account.id,
        BrokerOrder.symbol == row.symbol, BrokerOrder.side == "sell",
        or_(BrokerOrder.intent_id == entry.intent_id,
            BrokerOrder.payload["entry_order_id"].as_string() == str(entry.id),
            BrokerOrder.payload["oms_managed_row_id"].as_string() == str(row.id),
            BrokerOrder.submitted_at >= entry.submitted_at),
    ).limit(BOUND + 1)).all()
    if len(sells) > BOUND:
        raise ControlConflict("atr_stop_history_bound_UNKNOWN")
    for sell in sells:
        metadata = sell.payload or {}
        canonical = _native(entry, sell)
        original = original_child(sell, originals, entry, account)
        native = canonical or original
        if sell.id == unsent_order:
            if (sell.status != "pending" or sell.broker_order_id or sell.submitted_at
                    or metadata.get("killsw1_command") != str(lease.command_id)):
                raise ControlConflict("atr_stop_prepared_exit_changed")
            if session.scalar(select(Fill.id).where(Fill.order_id == sell.id).limit(1)) is not None:
                raise ControlConflict("atr_stop_unsent_exit_execution_UNKNOWN")
            continue
        if sell.status not in TERMINAL and (not native or protection_terminal):
            raise ControlConflict("atr_stop_existing_exit_working_UNKNOWN")
        amount = quantity(sell)
        if not amount:
            continue
        if not native and metadata.get("entry_order_id") != str(entry.id):
            raise ControlConflict("atr_stop_normal_exit_episode_UNKNOWN")
        if canonical:
            for fill in session.scalars(select(Fill).where(Fill.order_id == sell.id)).all():
                if _number(fill.quantity) > 0 and not str(fill.broker_fill_id or "").startswith(
                        f"{sell.broker_order_id}:"):
                    raise ControlConflict("atr_stop_child_execution_identity_UNKNOWN")
        exited += amount
    if exited > entered:
        raise ControlConflict("atr_stop_execution_overfill_UNKNOWN")
    vp = session.scalars(select(VirtualPosition).where(
        VirtualPosition.strategy_id == strategy.id, VirtualPosition.broker_account_id == account.id,
        VirtualPosition.symbol == row.symbol)).all()
    if len(vp) != 1:
        raise ControlConflict("atr_stop_owned_vp_UNKNOWN")
    if row.status == "closed" and (vp[0].quantity != 0 or entered != exited):
        raise ControlConflict("atr_stop_closed_original_execution_UNKNOWN")
    return row, entry, account, min(entered - exited, _number(vp[0].quantity),
                                    _number(row.current_quantity))


def _session_plan(provider, now, quote):
    local = now.astimezone(_ET)
    if (local.weekday() >= 5 or local.date() in US_MARKET_HOLIDAYS
            or not time(4) <= local.time() < time(20)
            or (provider == "schwab" and local.time() < time(7))):
        raise ControlConflict("atr_stop_session_BLOCKED")
    if time(9, 30) <= local.time() < time(16):
        return "market", "NORMAL", None
    if quote is None:
        raise ControlConflict("atr_stop_bid_UNKNOWN")
    bid, observed = quote
    if (not isinstance(bid, Decimal) or not bid.is_finite() or bid <= 0
            or not isinstance(observed, datetime) or observed.tzinfo is None
            or not 0 <= (now - observed).total_seconds() <= 2):
        raise ControlConflict("atr_stop_bid_not_fresh")
    limit = bid.quantize(Decimal(".01") if bid >= 1 else Decimal(".0001"), rounding=ROUND_FLOOR)
    if limit <= 0:
        raise ControlConflict("atr_stop_bid_below_tick")
    return "limit", "AM" if local.time() < time(9, 30) else "PM", limit


def _quote(service, symbol):
    value = service.__dict__.get("_latest_quotes_by_symbol", {}).get(symbol)
    if value is None:
        return None
    # Keep the actual producer/event timestamp; never stamp a cached bid as fresh.
    if not isinstance(value, dict) or not isinstance(value.get("received_at"), datetime):
        return None
    return Decimal(str(value.get("bid", 0))), value["received_at"]


async def _block(service, context, reason):
    context.phase = "BLOCKED_UNKNOWN"
    def write(session):
        existing = session.scalar(select(SystemIncident.id).where(
            SystemIncident.payload["source"].as_string() == "killsw1_atr_blocked",
            SystemIncident.payload["command_id"].as_string() == str(context.lease.command_id),
            SystemIncident.payload["managed_row_id"].as_string() == context.managed_id,
            SystemIncident.status == "open"))
        if existing is None:
            session.add(SystemIncident(service_name="oms-risk", severity="critical", status="open",
                title=HELD_BOOT_MESSAGE if reason == HELD_BOOT_MESSAGE else "ATR STOP blocked; owned exposure retained", payload={
                    "source": "killsw1_atr_blocked", "command_id": str(context.lease.command_id),
                    "managed_row_id": context.managed_id, "reason": reason}))
            return True
        if reason == HELD_BOOT_MESSAGE:
            row = session.get(SystemIncident, existing)
            if (row.payload or {}).get("reason") != HELD_BOOT_MESSAGE:
                row.title = HELD_BOOT_MESSAGE
                row.payload = {**(row.payload or {}), "reason": HELD_BOOT_MESSAGE}
                return True
        return False
    created = await service._run_db(write)
    if reason == HELD_BOOT_MESSAGE and not created:
        return
    log = service.logger.critical if reason == HELD_BOOT_MESSAGE else service.logger.error
    log("[KILLSW1-ATR-BLOCKED] command=%s row=%s reason=%s no_retry=1",
                         context.lease.command_id, context.managed_id, reason)


async def emit_owned_stop(service, session, row):
    """Called ONLY from _emit_v2_managed_sell under the existing managed claim."""
    context = current_stop(service, row.id)
    if (context is None or not getattr(service.settings, "bot_control_enabled", False)
            or not getattr(service.settings, "strategy_schwab_1m_v2_country_route_enabled", False)):
        raise ControlConflict("atr_stop_owner_context_UNKNOWN")
    lease, journal = context.lease, context.journal
    session.commit()
    try:
        bound = await service._run_db(lambda unit: _managed(service, unit, lease, row.id,
            allow_closed_original=True), commit=False)
        current, entry, account, remaining = bound
        if str(current.id) in (service._bot_control_runtime.boot_managed_ids or ()):
            raise ControlConflict(HELD_BOOT_MESSAGE)
        if service._bot_control_runtime.boot_managed_ids is None:
            raise ControlConflict("atr_stop_boot_census_UNKNOWN")
        _session_plan(account.provider, datetime.now(UTC), _quote(service, row.symbol))
        from project_mai_tai.oms import webull_c6_proof as original_proof
        originals = ()
        if account.provider == "webull" and "wbattach1" in (entry.payload or {}):
            originals = await service._run_db(lambda unit: original_proof.inventory(
                unit, unit.get(OmsManagedPosition, row.id), unit.get(BrokerOrder, entry.id), account), commit=False)
        if originals:
            proof = await original_proof.prepare(service, context, current, entry, account, originals)
            # Native child executions committed in another session. The later
            # canonical software-fill writer must subtract from that fresh row.
            session.refresh(row)
            if row.status == "closed" and row.current_quantity == 0:
                context.phase = "RECONCILING_NATIVE"
                return []
        else:
            proof = None
        binding = _hash({"managed": str(row.id), "entry": str(entry.id),
                         "coid": entry.client_order_id, "broker_root": entry.broker_order_id,
                         "base": service._oco_exit_base_for_entry(entry,
                             broker_account_name=account.name, symbol=row.symbol)})
        prepared = (await asyncio.to_thread(journal.prepare_native, lease, entry_id=entry.id,
            managed_id=row.id, binding_key=binding, now=datetime.now(UTC))) if proof is None else None
        if proof is None and prepared.status == "PREPARED":
            await asyncio.to_thread(journal.dispatch, lease, prepared.attempt_id, now=datetime.now(UTC))
            if account.provider == "schwab":
                outcome = await service._release_native_oco_for_cw_flip(
                    account.name, row.symbol, expected_row_id=str(row.id))
            else:
                with service.session_factory() as unit:
                    release = await service._release_exit_reservation_before_close(
                        session=unit, broker_account_name=account.name, symbol=row.symbol,
                        terminal_after_attempt=True)
                    unit.commit()
                outcome = release.outcome
            if outcome in {"released", "resolved_by_fill"}:
                try:
                    _, remaining_native = await _read_native_children(
                        service, lease, row.id, require_inventory=outcome == "resolved_by_fill")
                    outcome = "resolved_by_fill" if remaining_native == 0 else "released"
                except Exception:
                    await asyncio.to_thread(journal.record_native_outcome, lease,
                        prepared.attempt_id, outcome="unanswerable", now=datetime.now(UTC))
                    raise
            prepared = await asyncio.to_thread(journal.record_native_outcome, lease,
                prepared.attempt_id, outcome=outcome, now=datetime.now(UTC))
        if proof is None and prepared.status != "TERMINAL":
            # Recovery is read-only: only a complete known child inventory can
            # resolve an uncertain prior release, never another cancellation.
            _, remaining_native = await _read_native_children(service, lease, row.id, require_inventory=True)
            prepared = await asyncio.to_thread(journal.record_native_outcome, lease,
                prepared.attempt_id, outcome="resolved_by_fill" if remaining_native == 0 else "released",
                now=datetime.now(UTC))
        def native_evidence(unit):
            return dict(unit.get(BotControlAttempt, prepared.attempt_id).terminal_evidence or {})
        evidence = await service._run_db(native_evidence, commit=False) if proof is None else {"outcome": "released"}
        if evidence.get("outcome") == "resolved_by_fill":
            context.phase = "RECONCILING_NATIVE"
            return []
        if proof is None:
            await _read_native_children(service, lease, row.id)

        unsent_order = None
        def ceiling(unit):
            current, parent, acct, own = _managed(service, unit, lease, row.id,
                unsent_order=unsent_order, protection_terminal=True)
            attempt = unit.get(BotControlAttempt, prepared.attempt_id)
            evidence = attempt.terminal_evidence or {} if attempt is not None else {}
            if (attempt is None or attempt.status != "TERMINAL"
                    or evidence.get("outcome") != "released"):
                raise ControlConflict("atr_stop_native_fill_or_terminal_UNKNOWN")
            terminal_at = datetime.fromisoformat(evidence["observed_at"])
            children = unit.scalars(select(BrokerOrder).where(
                BrokerOrder.strategy_id == lease.strategy_id,
                BrokerOrder.broker_account_id == acct.id,
                BrokerOrder.symbol == current.symbol, BrokerOrder.side == "sell")).all()
            for child in children:
                if _native(parent, child) and (child.payload or {}).get("killsw1_terminal_read_at"):
                    terminal_at = max(terminal_at, datetime.fromisoformat(child.payload["killsw1_terminal_read_at"]))
            read_at = positions_after_protection(unit, account_name=acct.name,
                                               terminal_at=terminal_at, now=datetime.now(UTC))
            positions = unit.scalars(select(AccountPosition).where(
                AccountPosition.broker_account_id == acct.id, AccountPosition.symbol == row.symbol)).all()
            if len(positions) != 1:
                raise ControlConflict("atr_stop_broker_total_UNKNOWN")
            amount = min(own, _number(positions[0].quantity))
            if amount <= 0 or amount != amount.to_integral_value():
                raise ControlConflict("atr_stop_owned_sell_quantity_UNKNOWN")
            return current, parent, acct, amount, read_at

        async def current_ceiling():
            if proof is not None:
                return await original_proof.ceiling(service, context, row.id, unsent_order=unsent_order)
            return await service._run_db(ceiling, commit=False)

        current, entry, account, quantity, _ = await current_ceiling()
        if proof is not None and quantity == 0:
            await original_proof.flat(service, context, row.id)
            return []
        now = datetime.now(UTC)
        order_type, market_session, limit = _session_plan(account.provider, now, _quote(service, row.symbol))
        coid = f"ks1-atr-{lease.command_id.hex[:16]}-{row.id.hex[:16]}"
        metadata = {"oms_v2_managed_exit": "true", "entry_order_id": str(entry.id),
                    "managed_row_id": str(row.id), "killsw1_command": str(lease.command_id),
                    "order_type": order_type, "session": market_session, "time_in_force": "day",
                    "reference_price": str(current.entry_price)}
        if limit is not None:
            metadata.update(limit_price=str(limit), price_source="bid", extended_hours="true")
        event = TradeIntentEvent(source_service="oms-risk", payload=TradeIntentPayload(
            strategy_code=STRATEGY, broker_account_name=account.name, symbol=row.symbol,
            side="sell", intent_type="close", quantity=quantity, reason=STOP_REASON, metadata=metadata))
        request = OrderRequest(coid, account.name, STRATEGY, row.symbol, "sell", "close", quantity,
                               STOP_REASON, metadata, order_type, "day")
        strategy = session.get(Strategy, lease.strategy_id)
        account_row = session.get(BrokerAccount, account.id)
        if session.scalar(select(BrokerOrder.id).where(BrokerOrder.client_order_id == coid)) is not None:
            raise ControlConflict("atr_stop_exit_identity_already_exists_READ_FIRST")
        intent = service.store.create_trade_intent(session, strategy=strategy, broker_account=account_row, event=event)
        order = service.store.get_or_create_order(session, intent=intent, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id=coid, symbol=row.symbol, side="sell",
            quantity=quantity, metadata=metadata, status="pending", order_type=order_type)
        if order.broker_order_id or (order.payload or {}).get("killsw1_command") != str(lease.command_id):
            raise ControlConflict("atr_stop_prepared_exit_identity_changed")
        order.submitted_at = None  # Only this new, STOP-specific pending row.
        unsent_order = order.id
        session.commit()
        attempt = await asyncio.to_thread(journal.prepare, lease, order_id=order.id,
            operation="SUBMIT_EXIT", sequence_no=0, owner_hash=_hash(metadata),
            owner_revision=lease.generation, now=datetime.now(UTC))
        if attempt.status != "PREPARED":
            raise ControlConflict("atr_stop_exit_already_uncertain")
        # Recheck own fills/VP/receipt/bid after every asynchronous preparation.
        latest = await current_ceiling()
        if latest[3] != quantity:
            raise ControlConflict("atr_stop_quantity_changed_before_wire")
        await asyncio.to_thread(journal.dispatch, lease, attempt.attempt_id, now=datetime.now(UTC))
        latest = await current_ceiling()
        now = datetime.now(UTC)
        if not 0 <= (now - latest[4]).total_seconds() <= 30:
            raise ControlConflict("atr_stop_receipt_aged_before_wire")
        if latest[3] != quantity or _session_plan(
                account.provider, now, _quote(service, row.symbol)) != (order_type, market_session, limit):
            raise ControlConflict("atr_stop_evidence_changed_before_wire")
        reports = await service.broker_adapter.submit_order(request)
        events = []
        for report in reports:
            events.extend(await _record_sell(service, session, lease, journal, attempt,
                                            order, intent, event, request, report))
        if not reports or any(report.event_type in {"rejected", "cancelled", "canceled", "expired"}
                              for report in reports):
            await _block(service, context, "atr_stop_broker_refused_or_UNKNOWN")
        else:
            context.phase = "EXIT_WORKING"
        return events
    except asyncio.CancelledError:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        from project_mai_tai.oms.webull_c6_proof import report_wait
        await report_wait(service, context)
        await _block(service, context, str(exc))
        return []
    finally:
        from project_mai_tai.oms.webull_c6_proof import release_owner
        release_owner(service, context)


def _validated_sell(report, request, order):
    if (report is None or report.origin != "broker" or not report.broker_order_id
            or report.client_order_id != request.client_order_id or report.symbol != request.symbol
            or report.side != "sell" or report.intent_type != "close"
            or report.event_type not in READABLE
            or (order.broker_order_id and report.broker_order_id != order.broker_order_id)
            or not 0 <= _number(report.filled_quantity) <= request.quantity
            or (report.event_type == "filled" and report.filled_quantity != request.quantity)):
        raise ControlConflict("atr_stop_wire_receipt_UNKNOWN")
    if report.filled_quantity and (not report.broker_fill_id
            or not isinstance(report.fill_price, Decimal)
            or not report.fill_price.is_finite() or report.fill_price <= 0):
        raise ControlConflict("atr_stop_execution_UNKNOWN")


def _incremental_report(session, order, report):
    fills = session.scalars(select(Fill).where(Fill.order_id == order.id)).all()
    before, cash = Decimal(0), Decimal(0)
    for fill in fills:
        if (fill.strategy_id != order.strategy_id or fill.broker_account_id != order.broker_account_id
                or fill.symbol != order.symbol or fill.side != "sell"
                or not fill.broker_fill_id or not isinstance(fill.price, Decimal)
                or not fill.price.is_finite() or fill.price <= 0):
            raise ControlConflict("atr_stop_recorded_execution_UNKNOWN")
        before += _number(fill.quantity)
        cash += fill.quantity * fill.price
    delta = report.filled_quantity - before
    if delta < 0:
        raise ControlConflict("atr_stop_execution_regressed")
    if report.filled_quantity:
        new_cash = report.filled_quantity * report.fill_price - cash
        if (delta > 0 and new_cash <= 0) or (delta == 0 and abs(new_cash) > before * Decimal(".000000005")):
            raise ControlConflict("atr_stop_execution_cash_conflict")
        if delta:
            return replace(report, fill_price=new_cash / delta,
                metadata={**report.metadata, "killsw1_cumulative_average": str(report.fill_price),
                          "killsw1_cumulative_quantity": str(report.filled_quantity),
                          "killsw1_cumulative_cash": str(report.filled_quantity * report.fill_price),
                          "killsw1_fill_price_source": "derived_cumulative_cash_delta"})
    return report


async def _canonical_sell_reports(service, session, order, intent, event, request, report):
    normalized = _incremental_report(session, order, report)
    reports = [normalized]
    if report.event_type in {"cancelled", "canceled", "rejected", "expired"} and report.filled_quantity:
        # The trusted response proves an execution AND a terminal remainder. Feed
        # that execution through the existing fill writer, then retain terminality.
        reports = [replace(normalized, event_type="partially_filled"), report]
    events = []
    for observed in reports:
        events.extend(await service._record_order_reports(session=session, intent=intent,
            strategy_id=order.strategy_id, broker_account_id=order.broker_account_id,
            intent_event=event, request=request, reports=[observed]))
    session.flush()
    fills = session.scalars(select(Fill).where(Fill.order_id == order.id)).all()
    if sum((_number(fill.quantity) for fill in fills), Decimal(0)) != report.filled_quantity:
        raise ControlConflict("atr_stop_execution_writer_incomplete")
    return events


async def _read_native_children(service, lease, managed_id, *, require_inventory=False):
    def read(unit):
        row, entry, account, _ = _managed(service, unit, lease, managed_id)
        children = unit.scalars(select(BrokerOrder).where(
            BrokerOrder.strategy_id == lease.strategy_id, BrokerOrder.broker_account_id == account.id,
            BrokerOrder.symbol == row.symbol, BrokerOrder.side == "sell")).all()
        return row, entry, account, [child for child in children if _native(entry, child)]
    # Once this exact child is terminal, its first proof timestamp is retained.
    # Re-reading/re-stamping it on each cycle would chase the positions receipt
    # forever (wire T1, child T2; wire T3, child T4 ...).
    row, entry, account, children = await service._run_db(read, commit=False)
    if require_inventory and len(children) < 2:
        raise ControlConflict("atr_stop_complete_native_inventory_UNKNOWN")
    observations = []
    for child in children:
        if child.status in TERMINAL and (child.payload or {}).get("killsw1_terminal_read_at"):
            continue
        request = OrderRequest(child.client_order_id, account.name, STRATEGY, row.symbol, "sell", "close",
            child.quantity, STOP_REASON, {**(child.payload or {}), "broker_order_id": child.broker_order_id,
                "entry_order_id": str(entry.id), "oms_v2_managed_exit": "true"}, child.order_type, child.time_in_force)
        report = await service.broker_adapter.fetch_order_update(request)
        _validated_sell(report, request, child)
        if report.event_type not in TERMINAL or (report.filled_quantity
                and not str(report.broker_fill_id).startswith(f"{child.broker_order_id}:")):
            raise ControlConflict("atr_stop_native_child_terminal_UNKNOWN")
        observations.append((child, request, report))
    # No fill/managed close is published until EVERY bound child read is terminal.
    for child, request, report in observations:
        event = TradeIntentEvent(source_service="oms-risk", payload=TradeIntentPayload(
            strategy_code=STRATEGY, broker_account_name=account.name, symbol=row.symbol,
            side="sell", intent_type="close", quantity=child.quantity, reason=STOP_REASON,
            metadata=dict(request.metadata)))
        with service.session_factory() as unit:
            current = unit.get(BrokerOrder, child.id)
            parent = unit.get(BrokerOrder, entry.id)
            if (current is None or parent is None or not _native(parent, current)
                    or current.strategy_id != lease.strategy_id or current.broker_account_id != account.id):
                raise ControlConflict("atr_stop_native_child_owner_changed")
            if report.filled_quantity:
                intent = unit.get(TradeIntent, child.intent_id) if child.intent_id else None
                if intent is None:
                    raise ControlConflict("atr_stop_native_child_intent_UNKNOWN")
                events = await _canonical_sell_reports(service, unit, current, intent, event, request, report)
            else:
                if unit.scalar(select(Fill.id).where(Fill.order_id == child.id).limit(1)) is not None:
                    raise ControlConflict("atr_stop_native_child_execution_regressed")
                current.status = report.event_type
                events = []
            current.payload = {**(current.payload or {}), "killsw1_terminal_read_at": datetime.now(UTC).isoformat()}
            unit.commit()
        for event in events:
            await service._publish_order_event(event)
    remaining = await service._run_db(lambda unit: _number(
        unit.get(OmsManagedPosition, managed_id).current_quantity), commit=False)
    return len(children), remaining


async def _record_sell(service, session, lease, journal, attempt, order, intent, event, request, report):
    audit = session.get(BotControlAttempt, attempt.attempt_id) if attempt is not None else None
    if (order is None or attempt is None or intent is None
            or order.strategy_id != lease.strategy_id or order.broker_account_id not in lease.account_ids
            or order.client_order_id != request.client_order_id or order.symbol != request.symbol
            or order.side != "sell" or order.quantity != request.quantity
            or (order.payload or {}).get("killsw1_command") != str(lease.command_id)
            or audit is None or audit.command_id != lease.command_id or audit.target_order_id != order.id):
        raise ControlConflict("atr_stop_sell_owner_changed")
    _validated_sell(report, request, order)
    events = await _canonical_sell_reports(service, session, order, intent, event, request, report)
    session.commit()
    # The canonical fill/VP/managed writer commits before the audit can resolve.
    await asyncio.to_thread(journal.reconcile, lease, attempt.attempt_id,
        broker_order_id=report.broker_order_id, client_order_id=report.client_order_id,
        status=report.event_type, filled_quantity=report.filled_quantity,
        observed_at=datetime.now(UTC), now=datetime.now(UTC))
    return events


async def _read_exit(service, lease, journal, order_id):
    def read(unit):
        order = unit.get(BrokerOrder, order_id)
        if (order is None or order.side != "sell" or order.strategy_id != lease.strategy_id
                or order.broker_account_id not in lease.account_ids
                or (order.payload or {}).get("killsw1_command") != str(lease.command_id)
                or not order.broker_order_id):
            raise ControlConflict("atr_stop_existing_sell_READ_FIRST_UNKNOWN")
        attempts = unit.scalars(select(BotControlAttempt).where(
            BotControlAttempt.command_id == lease.command_id,
            BotControlAttempt.target_order_id == order.id,
            BotControlAttempt.operation == "SUBMIT_EXIT")).all()
        account = unit.get(BrokerAccount, order.broker_account_id)
        intent = unit.get(TradeIntent, order.intent_id)
        if (len(attempts) != 1 or attempts[0].status not in {"MAY_HAVE_SENT", "WORKING", "UNKNOWN"}
                or account is None or intent is None):
            raise ControlConflict("atr_stop_existing_sell_attempt_UNKNOWN")
        return order, attempts[0].attempt_id, account.name, intent
    order, attempt_id, account, intent = await service._run_db(read, commit=False)
    request = OrderRequest(order.client_order_id, account, STRATEGY, order.symbol, "sell", "close",
        order.quantity, STOP_REASON, {**(order.payload or {}), "broker_order_id": order.broker_order_id},
        order.order_type, order.time_in_force)
    report = await service.broker_adapter.fetch_order_update(request)
    event = TradeIntentEvent(source_service="oms-risk", payload=TradeIntentPayload(
        strategy_code=STRATEGY, broker_account_name=account, symbol=order.symbol,
        side="sell", intent_type="close", quantity=order.quantity, reason=STOP_REASON,
        metadata=dict(request.metadata)))
    with service.session_factory() as unit:
        current = unit.get(BrokerOrder, order.id)
        attempt = unit.get(BotControlAttempt, attempt_id)
        events = await _record_sell(service, unit, lease, journal, attempt,
            current, unit.get(TradeIntent, intent.id), event, request, report)
    for event in events:
        await service._publish_order_event(event)


async def _cancel_entry(service, lease, journal, order_id):
    # Keep C6's exact identity, uncertain-wire and partial-native protection fence.
    from project_mai_tai.oms.bot_control_legacy import cancel_owned_entry
    return await cancel_owned_entry(service, lease, journal, order_id, STRATEGY)


def _settled_census(service, unit, lease):
    pending = unit.scalar(select(TradeIntent.id).where(
        TradeIntent.strategy_id == lease.strategy_id,
        TradeIntent.broker_account_id.in_(lease.account_ids), TradeIntent.side == "buy",
        TradeIntent.status.not_in(TERMINAL | {"completed", "superseded"}),
        ~select(BrokerOrder.id).where(BrokerOrder.intent_id == TradeIntent.id).exists(),
    ).limit(1))
    if pending is not None:
        raise ControlConflict("atr_stop_unbound_entry_UNKNOWN")
    positions = unit.scalars(select(VirtualPosition).where(
        VirtualPosition.strategy_id == lease.strategy_id,
        VirtualPosition.broker_account_id.in_(lease.account_ids))).all()
    if any(_number(position.quantity) != 0 for position in positions):
        raise ControlConflict("atr_stop_owned_position_remaining")
    orders = unit.scalars(select(BrokerOrder).where(
        BrokerOrder.strategy_id == lease.strategy_id,
        BrokerOrder.broker_account_id.in_(lease.account_ids)).limit(BOUND + 1)).all()
    if len(orders) > BOUND or any(order.status not in TERMINAL for order in orders):
        raise ControlConflict("atr_stop_order_census_UNKNOWN")
    if unit.scalar(select(BotControlAttempt.attempt_id).where(
            BotControlAttempt.command_id == lease.command_id,
            BotControlAttempt.status.not_in(("TERMINAL", "PROVEN_NOT_SENT"))).limit(1)) is not None:
        raise ControlConflict("atr_stop_unresolved_attempt_UNKNOWN")
    totals = {}
    for order in orders:
        if order.side not in {"buy", "sell"}:
            raise ControlConflict("atr_stop_flat_order_side_UNKNOWN")
        key = (str(order.broker_account_id), order.symbol)
        totals.setdefault(key, Decimal(0))
        executed = Decimal(0)
        for fill in unit.scalars(select(Fill).where(Fill.order_id == order.id)).all():
            if (fill.strategy_id != lease.strategy_id or fill.broker_account_id != order.broker_account_id
                    or fill.symbol != order.symbol or fill.side != order.side or not fill.broker_fill_id
                    or not isinstance(fill.price, Decimal) or not fill.price.is_finite() or fill.price <= 0):
                raise ControlConflict("atr_stop_flat_execution_UNKNOWN")
            executed += _number(fill.quantity)
        if executed > _number(order.quantity) or (order.status == "filled" and executed != order.quantity):
            raise ControlConflict("atr_stop_flat_order_execution_UNKNOWN")
        totals[key] += executed * (1 if order.side == "buy" else -1)
    if any(total != 0 for total in totals.values()):
        raise ControlConflict("atr_stop_flat_fill_conservation_UNKNOWN")
    observed = datetime.now(UTC).isoformat()
    return {str(account): {"complete": True, "owned_quantity": 0, "working_orders": 0,
        "in_flight": 0, "observed_at": observed, "evidence_hash": _hash({
            "command": str(lease.command_id), "generation": lease.generation,
            "account": str(account), "orders": sorted(str(order.id) for order in orders),
            "source": "exact_owned_fill_vp_order_conservation"})} for account in lease.account_ids}


class AtrStopDriver:
    def __init__(self, service):
        self.service = service

    async def __call__(self, lease, journal):
        service = self.service
        if not getattr(service.settings, "strategy_schwab_1m_v2_country_route_enabled", False):
            from project_mai_tai.oms.bot_control_legacy import LegacyStopDriver
            return await LegacyStopDriver(service, STRATEGY)(lease, journal)
        def inventory(unit):
            strategy = unit.get(Strategy, lease.strategy_id)
            accounts = unit.scalars(select(BrokerAccount).where(BrokerAccount.id.in_(lease.account_ids))).all()
            if (not lease.scope_complete or not lease.account_ids or strategy is None
                    or strategy.code != STRATEGY
                    or len(accounts) != len(lease.account_ids)
                    or any(not controlled_live_scope(strategy.code, strategy.execution_mode, acct.name)
                           or acct.provider not in {"schwab", "webull"}
                           for acct in accounts)):
                raise ControlConflict("atr_stop_scope_UNKNOWN")
            names = [acct.name for acct in accounts]
            recovery = select(BotControlAttempt.managed_row_id).where(
                BotControlAttempt.command_id == lease.command_id,
                BotControlAttempt.binding_kind == "managed_native_tree",
                BotControlAttempt.status != "TERMINAL")
            original_recovery = select(BrokerOrder.id).where(
                BrokerOrder.payload["wbattach1"]["c6_list_after"].as_string().is_not(None))
            rows = unit.scalars(select(OmsManagedPosition).where(
                OmsManagedPosition.strategy_code == STRATEGY,
                OmsManagedPosition.broker_account_name.in_(names),
                or_(OmsManagedPosition.status == "open", and_(
                    OmsManagedPosition.status == "closed", OmsManagedPosition.current_quantity == 0,
                    OmsManagedPosition.broker_account_name.in_([acct.name for acct in accounts if acct.provider == "webull"]),
                    OmsManagedPosition.entry_order_id.in_(original_recovery),
                    OmsManagedPosition.id.in_(recovery)))).limit(BOUND + 1)).all()
            working = unit.scalars(select(BrokerOrder).where(
                BrokerOrder.strategy_id == lease.strategy_id,
                BrokerOrder.broker_account_id.in_(lease.account_ids),
                or_(BrokerOrder.status.is_(None), BrokerOrder.status.not_in(TERMINAL)),
            ).limit(BOUND + 1)).all()
            if len(rows) > BOUND or len(working) > BOUND:
                raise ControlConflict("atr_stop_inventory_bound_UNKNOWN")
            known_native, original_errors = set(), []
            for managed in rows:
                entry = unit.get(BrokerOrder, managed.entry_order_id) if managed.entry_order_id else None
                if (entry is None or entry.strategy_id != lease.strategy_id
                        or entry.client_order_id != managed.entry_client_order_id):
                    continue
                from project_mai_tai.oms.webull_c6_proof import inventory as original_inventory, original_child
                account = next(acct for acct in accounts if acct.name == managed.broker_account_name)
                try:
                    originals = original_inventory(unit, managed, entry, account)
                except Exception as exc:
                    original_errors.append((str(managed.id), str(exc)))
                    continue
                for order in working:
                    try:
                        if (order.side == "sell" and order.broker_account_id == entry.broker_account_id
                                and order.symbol == entry.symbol and (_native(entry, order)
                                    or original_child(order, originals, entry, account))):
                            known_native.add(order.id)
                    except Exception as exc:
                        original_errors.append((str(managed.id), str(exc)))
            refused = unit.scalar(select(BrokerOrder.id).where(
                BrokerOrder.strategy_id == lease.strategy_id,
                BrokerOrder.payload["killsw1_command"].as_string() == str(lease.command_id),
                BrokerOrder.status.in_(("rejected", "expired", "cancelled", "canceled"))).limit(1))
            if refused is not None:
                raise ControlConflict("atr_stop_refusal_persisted_no_retry")
            return tuple(str(row.id) for row in rows), tuple(
                (order.id, order.side) for order in working if order.id not in known_native), tuple(original_errors)
        try:
            identities, working, original_errors = await service._run_db(inventory, commit=False)
            if original_errors:
                from project_mai_tai.oms.webull_c6_proof import report_wait
                for identity, reason in original_errors:
                    context = StopContext(service, lease, journal, identity)
                    await report_wait(service, context)
                    await _block(service, context, reason)
                return StopProgress("BLOCKED_UNKNOWN")
            if working:
                for order_id, side in working:
                    if side == "buy":
                        await _cancel_entry(service, lease, journal, order_id)
                    elif side == "sell":
                        await _read_exit(service, lease, journal, order_id)
                    else:
                        raise ControlConflict("atr_stop_order_side_UNKNOWN")
                return StopProgress("CANCELLING_ENTRIES" if any(side == "buy" for _, side in working)
                                    else "EXIT_WORKING")
            if not identities:
                census = await service._run_db(lambda unit: _settled_census(service, unit, lease), commit=False)
                return StopProgress("SETTLED", census)
            blocked = False
            for identity in identities:
                context = StopContext(service, lease, journal, identity, owner_task=asyncio.current_task())
                token = _CONTEXT.set(context)
                try:
                    boot_ids = service._bot_control_runtime.boot_managed_ids
                    if boot_ids is None or identity in boot_ids:
                        await _block(service, context, HELD_BOOT_MESSAGE if boot_ids is not None
                                     else "atr_stop_boot_census_UNKNOWN")
                        blocked = True
                        continue
                    def read(unit):
                        row, _, _, _ = _managed(service, unit, lease, identity, allow_closed_original=True)
                        return row.broker_account_name, row.symbol, service._hydrate_v2_position(row), float(row.entry_price)
                    account, symbol, position, entry_price = await service._run_db(read, commit=False)
                    await service._emit_v2_exit_on_loop_claimed(account, symbol, position, entry_price,
                        kind="HARD", reference_price=entry_price, reason=STOP_REASON, bid=0,
                        close_on_fill=True, expected_managed_row_id=identity)
                    if context.phase.startswith("BLOCKED"):
                        blocked = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    from project_mai_tai.oms.webull_c6_proof import report_wait
                    await report_wait(service, context)
                    await _block(service, context, str(exc))
                    blocked = True
                finally:
                    _CONTEXT.reset(token)
            return StopProgress("BLOCKED_UNKNOWN" if blocked else "EXIT_WORKING")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            service.logger.error("[KILLSW1-ATR-BLOCKED] command=%s reason=%s", lease.command_id, exc)
            return StopProgress("BLOCKED_UNKNOWN")
