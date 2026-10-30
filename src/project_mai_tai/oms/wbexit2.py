"""Durable Webull close responsibility, separate from a bounded asyncio claim.

SOURCE C11056. Database functions below are PURE synchronous units for _run_db;
they never open a session, read a provider, schedule work or authorize a SELL.
SystemIncident's existing primary key supplies a unique durable address; no DDL.
The caller must retain all original managed-sell and native-proof guards.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from uuid import UUID, uuid5

from sqlalchemy import or_, select, update

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition, Strategy, SystemIncident, TradeIntent
from project_mai_tai.oms.bot_control_scope import controlled_live_scope

from project_mai_tai.oms import wbexit3

SOURCE = "wbexit2"
NAMESPACE = UUID("806dd329-8d11-4b42-95e6-146b0240798c")
REASONS = frozenset({"oms_v2_managed_exit:" + suffix for suffix in
    ("CW_FLIP", "CONFIRMATION_EXIT", "CW_TARGET", "CW_HARD_STOP", "CW_FLOOR")})
OPEN_PHASES = frozenset({"BOUND", "WAIT_TERMINAL", "TERMINAL", "SELL_RESERVED", "SELL_WORKING", "HOLD"})
BOOT_OWNER_LIMIT = 256  # Inventory safety cap, never permission to silently omit an owner.


def enabled(service):
    return (getattr(service.settings, "strategy_schwab_1m_v2_wbexit2_enabled", False) is True
            or wbexit3.enabled(service))


def selected(service, reason, account):
    if wbexit3.full_close_selected(service, reason, account):
        return True
    if not enabled(service) or reason not in REASONS:
        return False
    provider = getattr(service.settings, "provider_for_account", None)
    if (not callable(provider) or provider(account) != "webull"
            or not controlled_live_scope("schwab_1m_v2", "live", account)):
        return False
    from project_mai_tai.oms import webull_attach_riskoff as wb
    if not wb._rth(service):
        return False
    return True


async def joined(service, call, awaitable, *, completed=None):
    """Physically own this ONE unit through cancellation; no detached DB/SDK IO.

    The original cancellation remains primary. No second operation starts until
    the first is done. Joining may exceed the invocation deadline; that remains
    a failure/PAGE, never a fabricated timely completion or extended permission.
    """
    if call.wb2_pending is not None:
        if asyncio.iscoroutine(awaitable):
            awaitable.close()  # This rejected unit has NEVER been scheduled/entered.
        raise ValueError("wbexit2_overlapping_owned_IO")
    try:
        task = asyncio.create_task(awaitable)
    except BaseException:
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise
    call.wb2_pending = task
    cancelled = None
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
                service.logger.critical("[WBEXIT2-JOIN] account=%s symbol=%s owner=%s physical_IO_pending=true scan_exception=true",
                    call.account, call.symbol, (call.wb2_state or {}).get("owner_id", "UNKNOWN"))
            except Exception as exc:
                if cancelled is None:
                    raise
                service.logger.error("[WBEXIT2-JOIN] joined_secondary_failure=%s", type(exc).__name__)
                break
        if not task.cancelled() and task.exception() is None and completed is not None:
            try:
                completed(task.result())  # Loop-only, AFTER physical commit, even when waiter was cancelled.
            except BaseException as receipt_error:
                if cancelled is None:
                    raise
                service.logger.critical("[WBEXIT2-JOIN] committed_receipt_capture_UNKNOWN=%s scan_exception=true",
                                        type(receipt_error).__name__)
                raise cancelled from receipt_error
        if cancelled is not None:
            if not task.cancelled():
                failure = task.exception()
                if failure is not None:
                    service.logger.error("[WBEXIT2-JOIN] joined_secondary_failure=%s", type(failure).__name__)
            raise cancelled
        return task.result()
    finally:
        if task.done():
            call.wb2_pending = None


def check_call(service, call):
    from project_mai_tai.oms import webull_attach_riskoff as wb
    control = getattr(service, "_bot_control_runtime", None)
    if (service.__dict__.get("_wbexit2_boot_complete") is not True
            or not selected(service, call.reason, call.account)
            or not (wb._rth(service) or wbexit3.full_close_selected(service, call.reason, call.account))
            or monotonic() > call.wb2_deadline
            or getattr(service.settings, "bot_control_enabled", False) and control is None
            or control is not None and (control.stop_requested("schwab_1m_v2")
                or call.wb2_control is None or control.states.get("schwab_1m_v2") != call.wb2_control)):
        raise ValueError("wbexit2_current_invocation_STOP_session_or_deadline_changed")


async def load_for_call(service, call, *, journal_hint):
    from project_mai_tai.oms import webull_attach_riskoff as wb
    def unit(session):
        entry, row, account, strategy, owned = wb._atr(service, session, call.account, call.symbol, call.entry_coid,
                                                  allow_closed=True)
        binding = identity(entry, row, account, strategy)
        state = read(session, binding, lock=True)
        if state is None:
            journal = (entry.payload or {}).get(wb.KEY) or deepcopy(journal_hint)
            if (not isinstance(journal, dict) or journal.get("entry_id") != binding["entry_id"]
                    or journal.get("managed_id") != binding["managed_id"] or not isinstance(journal.get("attempts"), list)):
                raise ValueError("wbexit2_initial_journal_identity_unknown")
            originals = [{"base": attempt["base"], "quantity": attempt["quantity"], "children": [
                {"coid": wb.WebullBrokerAdapter._combo_leg_coid(attempt["base"], suffix),
                 "native_id": None, "price": None} for suffix in ("T", "S")]}
                for attempt in journal.get("attempts", [])]
            expected = {key: binding[key] for key in ("entry_id", "entry_coid", "managed_id", "account_id", "strategy_id")}
            expected["source"] = "new_bare_fill_writer"
            bare = not originals and (entry.payload or {}).get(wb.BARE_FILL_PROOF) == expected
            original_quantity = Decimal(originals[0]["quantity"]) if len(originals) == 1 else owned
            bought = sum((fill.quantity for fill in session.scalars(select(Fill).where(
                Fill.order_id == entry.id, Fill.side == "buy"))), Decimal(0))
            if not 0 < original_quantity <= bought or not 0 <= owned <= original_quantity:
                raise ValueError("wbexit2_original_quantity_not_backed_by_OUR_entry")
            intent = session.get(TradeIntent, entry.intent_id)
            from project_mai_tai.oms.route_owner import _utc_row_time
            anchor = _utc_row_time(entry.submitted_at) or _utc_row_time(intent.created_at if intent is not None else None)
            wb3_inputs = {}
            if wbexit3.enabled(service):
                wb3_inputs = {"wbexit3_enabled": True,
                    "prior_software_filled": wbexit3.prior_software_quantity(session, entry) if originals else Decimal(0)}
            state = begin(session, binding, quantity=original_quantity, originals=originals, reason=call.reason,
                now=_now(), bare_proven=bare, originals_proven=bare, decision=call.wb2_decision,
                attempt_started=call.wb2_started_at, entry_anchor=anchor, **wb3_inputs)
            journal = deepcopy(journal)
            journal["wbexit2_owner_id"] = state["owner_id"]
            entry.payload = {**entry.payload, wb.KEY: journal}
        if state is not None and state["sell"] is None and state.get("invocation") is None:
            state = transition(session, binding, state["generation"], operation="acquire",
                               now=wb.datetime.now(UTC), invocation=call.call_id)
        return binding, state
    def committed(result):
        call.wb2_binding, call.wb2_state = result
        call.wb2_prior_cancel_bases = frozenset((call.wb2_state or {}).get("cancel_bases", []))
        if call.wb2_state is not None:
            remember(service, call.wb2_state)
    await joined(service, call, service._run_db(unit, commit=True), completed=committed)
    wb._current_atr_call(service, call.managed_id)
    return call.wb2_state


async def checkpoint(service, call, operation, **evidence):
    from project_mai_tai.oms import webull_attach_riskoff as wb
    state = call.wb2_state
    if state is None or call.wb2_binding is None:
        raise ValueError("wbexit2_durable_owner_missing")
    def unit(session):
        return transition(session, call.wb2_binding, state["generation"],
                          operation=operation, now=wb.datetime.now(UTC), invocation=getattr(call, "call_id", None), **evidence)
    await joined(service, call, service._run_db(unit, commit=True),
                 completed=lambda state: setattr(call, "wb2_state", state))
    remember(service, call.wb2_state)
    if operation != "hold":
        wb._current_atr_call(service, call.managed_id)
    return call.wb2_state


async def begin_for_call(service, call, quantity, originals, *, bare_proven=False, terminal_reports=None):
    from project_mai_tai.oms import webull_attach_riskoff as wb
    def unit(session):
        entry, row, account, strategy, owned = wb._atr(service, session, call.account, call.symbol,
                                                      call.entry_coid, allow_closed=True)
        binding = identity(entry, row, account, strategy)
        if binding != call.wb2_binding or not 0 <= owned <= quantity or entry.status not in wb.TERMINAL:
            raise ValueError("wbexit2_pre_cancel_owner_or_quantity_changed")
        if not originals:
            expected = {key: binding[key] for key in ("entry_id", "entry_coid", "managed_id", "account_id", "strategy_id")}
            expected["source"] = "new_bare_fill_writer"
            if (entry.payload or {}).get(wb.BARE_FILL_PROOF) != expected:
                raise ValueError("wbexit2_positive_bare_fill_provenance_required")
        state = read(session, binding, lock=True)
        if state is None or state["generation"] != call.wb2_state["generation"]:
            raise ValueError("wbexit2_initial_proof_owner_CAS_changed")
        state = transition(session, binding, state["generation"], operation="bind_originals",
            now=_now(), invocation=call.call_id, originals=originals, bare_proven=bare_proven,
            native_account_id=call.wb2_native_account_id, terminal_reports=terminal_reports)
        journal = deepcopy(entry.payload[wb.KEY])
        journal["wbexit2_owner_id"] = state["owner_id"]
        entry.payload = {**entry.payload, wb.KEY: journal}
        return state
    await joined(service, call, service._run_db(unit, commit=True),
                 completed=lambda state: setattr(call, "wb2_state", state))
    remember(service, call.wb2_state)
    wb._current_atr_call(service, call.managed_id)
    return call.wb2_state


def check_native(call, reports):
    known = {child["coid"]: child["native_id"] for pair in call.wb2_state["originals"] for child in pair["children"]}
    for report in reports:
        if report is not None and (report.client_order_id not in known
                or report.broker_order_id != known[report.client_order_id]):
            raise ValueError("wbexit2_original_native_ID_changed")


def census(call, reports, base, attempts, now, *, after=None):
    """Keep the existing complete-list/freshness bounds, also before FIRST cancel."""
    import json
    acquired_min = None
    physical_account = None
    for report in reports:
        proof = json.loads(report.metadata.get("wbattach1_list_census", "null"))
        if (not isinstance(proof, dict) or proof.get("complete") is not True
                or proof.get("truncated") is not False or proof.get("broker_account_name") != call.account
                or proof.get("symbol") != call.symbol or proof.get("base") != base
                or proof.get("original_bases") != sorted(a["base"] for a in attempts)
                or type(proof.get("pages")) is not int or not 1 <= proof["pages"] <= 20):
            raise ValueError("wbexit2_original_census_unknown")
        native_account = proof.get("account_id")
        if (type(native_account) is not str or not native_account
                or physical_account is not None and native_account != physical_account
                or call.wb2_state.get("native_account_id") not in (None, native_account)):
            raise ValueError("wbexit2_original_native_account_UNKNOWN_or_changed")
        physical_account = native_account
        acquired = datetime.fromisoformat(proof["acquired_at"])
        started = datetime.fromisoformat(proof["started_at"])
        if (acquired.tzinfo is None or started.tzinfo is None
                or not 0 <= (now - acquired).total_seconds() <= 2
                or not 0 <= (acquired - started).total_seconds() <= 30
                or after is not None and started < after):
            raise ValueError("wbexit2_original_census_stale")
        acquired_min = min(acquired_min or acquired, acquired)
    if acquired_min is None:
        raise ValueError("wbexit2_original_census_empty")
    call.wb2_native_account_id = physical_account
    return acquired_min


async def finish_call(service, call):
    """Finishing a task NEVER relinquishes economic close responsibility."""
    if call.wb2_pending is not None:
        raise ValueError("wbexit2_cannot_release_invocation_with_pending_IO")
    if call.wb2_state is None:
        service.logger.critical("[WBEXIT2-DURABLE-UNKNOWN] account=%s symbol=%s row=%s owner_ref=%s remaining=UNKNOWN durable=UNKNOWN wire=0 scan_exception=true",
            call.account, call.symbol, call.managed_id, call.call_id)
        return
    if call.wb2_state["phase"] == "RESOLVED":
        return
    token = getattr(call, "call_id", None)
    if call.wb2_state.get("invocation") not in (None, token):
        local = service.__dict__.get("_wbattach_exit_owners", {}).get((call.account, call.symbol))
        if (local is not None and not local.consumed
                and local.call_id == call.wb2_state["invocation"]):
            return  # The actual same-process call still physically owns this IO.
        service.logger.critical("[WBEXIT2-PRIOR-INVOCATION-UNKNOWN] owner=%s wire=0 scan_exception=true",
                                call.wb2_state["owner_id"])
        await checkpoint(service, call, "hold", reason="prior_invocation_physical_quiescence_UNKNOWN",
                         original_attempt_failed=True)
        return  # A foreign/crashed call is not a transferable task lease.
    if (token is not None and not call.consumed and call.wb2_sell_started is None
            and call.wb2_prepared is not None):
        await abort_created_unposted(service, call)
    def reconcile(session):
        state = read(session, call.wb2_binding, lock=True)
        entry = session.get(BrokerOrder, UUID(state["identity"]["entry_id"]))
        row = session.get(OmsManagedPosition, UUID(state["identity"]["managed_id"]))
        if entry is None or row is None or identity(entry, row,
                session.get(BrokerAccount, entry.broker_account_id),
                session.get(Strategy, entry.strategy_id)) != state["identity"]:
            raise ValueError("wbexit2_resolution_entry_identity_changed")
        if token is not None and state.get("invocation") == token:
            unentered = (set(state["cancel_bases"]) - set(call.wb2_prior_cancel_bases)
                         - call.wb2_cancels_entered)
            if unentered:
                # Exact current invocation, all physical work joined, and this
                # code never entered the adapter call. NOT a native absence ACK.
                from project_mai_tai.oms import webull_attach_riskoff as wb
                journal = deepcopy((entry.payload or {}).get(wb.KEY))
                if not isinstance(journal, dict) or not isinstance(journal.get("attempts"), list):
                    raise ValueError("wbexit2_unentered_cancel_journal_unknown")
                journal["attempts"] = [{**attempt, "cancel_requested": False}
                    if attempt["base"] in unentered else attempt for attempt in journal["attempts"]]
                entry.payload = {**entry.payload, wb.KEY: journal}
                state = transition(session, call.wb2_binding, state["generation"], operation="unentered_cancel",
                    now=_now(), invocation=token, bases=sorted(unentered), physical_joined=True, adapter_never_entered=True)
        if state["sell"] is not None:
            sell = state["sell"]
            order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == sell["coid"]))
            parent = session.get(TradeIntent, order.intent_id) if order is not None else None
            if (order is not None and parent is not None and order.broker_order_id
                    and order.broker_account_id == entry.broker_account_id and order.strategy_id == entry.strategy_id
                    and order.symbol == entry.symbol and order.side == "sell" and order.quantity == Decimal(sell["quantity"])
                    and parent.reason == state["reason"] and parent.intent_type == "close"
                    and parent.strategy_id == entry.strategy_id and parent.broker_account_id == entry.broker_account_id
                    and parent.symbol == entry.symbol and parent.side == "sell" and parent.quantity == order.quantity
                    and (order.payload or {}).get("oms_managed_row_id") == str(row.id)
                    and (order.payload or {}).get("entry_order_id") == str(entry.id)
                    and session.scalar(select(BrokerOrderEvent.id).where(BrokerOrderEvent.order_id == order.id,
                        BrokerOrderEvent.event_source == "broker", BrokerOrderEvent.event_type == order.status).limit(1)) is not None):
                fills = session.scalars(select(Fill).where(Fill.order_id == order.id)).all()
                if any(f.broker_account_id != entry.broker_account_id or f.strategy_id != entry.strategy_id
                        or f.symbol != entry.symbol or f.side != "sell" or not f.broker_fill_id
                        or not f.quantity.is_finite() or not f.price.is_finite() or f.quantity <= 0 or f.price <= 0 for f in fills):
                    raise ValueError("wbexit2_OUR_recorded_execution_unknown")
                state = transition(session, call.wb2_binding, state["generation"], operation="sell_status",
                    now=_now(), coid=sell["coid"], native_proven=True, status=order.status,
                    filled=str(sum((f.quantity for f in fills), Decimal(0))))
        vp = service.store.get_virtual_position(session, strategy_id=entry.strategy_id,
            broker_account_id=entry.broker_account_id, symbol=entry.symbol)
        if (getattr(call, "record_only", False) and token is None
                and row.status == "closed" and row.current_quantity == 0
                and vp is not None and vp.quantity == 0 and Decimal(state["remaining"]) > 0):
            from project_mai_tai.oms.wbexit2_durable import reconcile_closed_originals
            state = reconcile_closed_originals(session, state, _now())
        if state["phase"] != "RESOLVED" and row.status == "closed" and row.current_quantity == 0 and vp is not None and vp.quantity == 0 and Decimal(state["remaining"]) == 0:
            state = transition(session, call.wb2_binding, state["generation"], operation="resolve",
                now=_now(), managed_closed=True, virtual_flat=True, attributed_execution_complete=True)
            from project_mai_tai.oms import webull_attach_riskoff as wb
            journal = deepcopy((entry.payload or {}).get(wb.KEY) or {})
            if journal.get("wbexit2_owner_id") == state["owner_id"]:
                journal.pop("wbexit2_owner_id")
                entry.payload = {**entry.payload, wb.KEY: journal}
        if token is not None and state.get("invocation") == token:
            state = transition(session, call.wb2_binding, state["generation"], operation="release",
                now=_now(), invocation=token, physical_joined=True)
        return state
    if token is not None:
        from project_mai_tai.oms import webull_attach_riskoff as wb
        if wb._ATR_CALL.get() is not call or asyncio.current_task() is not call.task or call.wb2_pending is not None:
            raise ValueError("wbexit2_release_requires_actual_joined_invocation")
    await joined(service, call, service._run_db(reconcile, commit=True),
                 completed=lambda state: setattr(call, "wb2_state", state))
    remember(service, call.wb2_state)
    if call.wb2_state["phase"] == "RESOLVED":
        return
    service.logger.critical("[WBEXIT2-PENDING] account=%s symbol=%s owner=%s remaining=%s phase=%s age_seconds=%s scan_exception=true original_start=%s",
        call.account, call.symbol, call.wb2_state["owner_id"], call.wb2_state["remaining"],
        call.wb2_state["phase"], pending_age(call.wb2_state, _now()), call.wb2_state["first_cancel_at"])
    await checkpoint(service, call, "hold", reason="named_close_owner_pending_next_permitted_observation",
                     original_attempt_failed=call.wb2_state["sell"] is None
                         or not getattr(call, "record_only", False) and monotonic() > call.wb2_deadline)


async def release_invocation(service, call):
    """Even record failure must not turn a JOINED call into a borrowed/stuck task.

    No economic phase, geometry, fill reservation, ACK or remaining is cleared.
    Failure of this final CAS retains UNKNOWN/PAGE; no other token is reclaimed.
    """
    from project_mai_tai.oms import webull_attach_riskoff as wb
    if call.wb2_state is None or call.wb2_state.get("invocation") != call.call_id:
        return
    if (call.wb2_pending is not None or wb._ATR_CALL.get() is not call
            or call.task is not asyncio.current_task() or call.service is not service):
        raise ValueError("wbexit2_invocation_not_physically_joined")
    def unit(session):
        state = read(session, call.wb2_binding, lock=True)
        if state["invocation"] != call.call_id:
            return state
        if state["phase"] != "RESOLVED":
            state = transition(session, call.wb2_binding, state["generation"], operation="hold",
                now=_now(), reason="invocation_record_UNPROVEN_after_physical_join", original_attempt_failed=True)
        return transition(session, call.wb2_binding, state["generation"], operation="release",
            now=_now(), invocation=call.call_id, physical_joined=True)
    await joined(service, call, service._run_db(unit, commit=True),
        completed=lambda state: setattr(call, "wb2_state", state))
    remember(service, call.wb2_state)


def remember(service, state):
    """One compact record per unresolved episode; no quote/event receipt list."""
    pending = service.__dict__.setdefault("_wbexit2_pending", {})
    binding = state["identity"]
    key = (binding["account"], binding["symbol"])
    old = pending.get(key)
    if old is not None and old.get("conflict"):
        return  # Never choose the first/last favorable owner from ambiguous rows.
    if state["phase"] == "RESOLVED":
        if old is not None and old.get("owner_id") == state["owner_id"]:
            pending.pop(key)
        return
    if old is not None and old.get("owner_id") != state["owner_id"]:
        pending[key] = {"conflict": True, "owner_id": old.get("owner_id")}
        service.logger.critical("[WBEXIT2-OWNER-CONFLICT] account=%s symbol=%s scan_exception=true", *key)
        return
    pending[key] = deepcopy(state)


@dataclass
class _RecordOnly:
    """DB-only conservation context; never placed in _ATR_CALL or wire-capable."""
    account: str
    symbol: str
    wb2_binding: dict
    wb2_state: dict
    wb2_pending: object = None
    wb2_deadline: float = 0
    record_only: bool = True


async def hydrate_at_boot(service):
    """Entire bounded durable inventory BEFORE Main starts quote exits.

    No provider operation, lease reclaim, new scheduler or fabricated child
    terminality. READY means inventory loaded; orphan nonces remain UNKNOWN.
    Fail closed on corruption/ambiguity/over-budget, never one-row boot success.
    """
    if not enabled(service):
        return {"status": "OFF", "owners": []}
    service._wbexit2_boot_complete = False
    service._wbexit2_boot_issues = []
    def unit(session):
        rows = session.scalars(select(SystemIncident).where(
            SystemIncident.service_name == "oms-risk", SystemIncident.status == "open",
            SystemIncident.payload["source"].as_string() == SOURCE,
        ).order_by(SystemIncident.id).limit(BOOT_OWNER_LIMIT + 1)).all()
        if len(rows) > BOOT_OWNER_LIMIT:
            raise ValueError("wbexit2_boot_inventory_over_bound_NO_omitted_owner_permission")
        states = []
        for row in rows:
            state = read(session, row.payload["identity"])
            if state is None or state["owner_id"] != str(row.id) or state["phase"] not in OPEN_PHASES:
                raise ValueError("wbexit2_boot_durable_row_address_or_phase_UNKNOWN")
            binding = state["identity"]
            entry = session.get(BrokerOrder, UUID(binding["entry_id"]))
            managed = session.get(OmsManagedPosition, UUID(binding["managed_id"]))
            if (entry is None or managed is None or identity(entry, managed,
                    session.get(BrokerAccount, entry.broker_account_id),
                    session.get(Strategy, entry.strategy_id)) != binding
                    or (entry.payload or {}).get("wbattach1", {}).get("wbexit2_owner_id") != state["owner_id"]):
                raise ValueError("wbexit2_boot_actual_binding_or_marker_UNKNOWN")
            states.append(state)
        return states
    context = _RecordOnly("system", "BOOT", {}, {})
    states = await joined(service, context, service._run_db(unit, commit=False))
    candidate = {}
    for state in states:
        key = (state["identity"]["account"], state["identity"]["symbol"])
        if key in candidate:
            raise ValueError("wbexit2_boot_multiple_live_owners_same_account_symbol")
        candidate[key] = deepcopy(state)
        if state.get("invocation") is not None:
            service._wbexit2_boot_issues.append({"owner_id": state["owner_id"],
                "reason": "prior_invocation_physical_quiescence_UNKNOWN"})
    service._wbexit2_pending = candidate  # Publish only the COMPLETE validated snapshot.
    service._wbexit2_boot_complete = True
    for issue in service._wbexit2_boot_issues:
        service.logger.critical("[WBEXIT2-BOOT-HOLD] owner=%s reason=%s wire=0 scan_exception=true",
                                issue["owner_id"], issue["reason"])
    return {"status": "INVENTORY_READY_HOLDS_PRESENT" if service._wbexit2_boot_issues else "INVENTORY_READY",
            "owners": [state["owner_id"] for state in states],
            "orphan_nonces": len(service._wbexit2_boot_issues), "native_terminal": "UNKNOWN_NO_BOOT_PROVIDER_READ"}


def pending_age(state, now):
    """Age of the ORIGINAL durable obligation, never a reset retry timestamp."""
    try:
        opened = datetime.fromisoformat(state["opened_at"])
        if opened.tzinfo is not None and opened <= now:
            return format((now - opened).total_seconds(), ".3f")
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return "UNKNOWN"  # Missing/future clock is not a favourably clamped zero.


def log_pending_scan(service):
    """C1 02:15:11: EACH existing RTH sync scan names EVERY unresolved cache row.

    Scan means refresh_pending's existing periodic sync hook, not each quote.
    Includes nonce/ACK/conflict HOLDs and absent quotes; no new acquisition/poll
    or task. Synchronous per-scan logging cost still needs actual qualification.
    The cached obligation is a risk record, NOT proof of a currently working
    native book or a current owned position. Resolution still requires DB proof.
    """
    from project_mai_tai.oms import webull_attach_riskoff as wb
    if not enabled(service) or not (wb._rth(service) or wbexit3.enabled(service)):
        return
    now = _now()
    for (account, symbol), state in service.__dict__.get("_wbexit2_pending", {}).items():
        if state.get("phase") == "RESOLVED":
            continue
        phase = state.get("phase") if state.get("phase") in OPEN_PHASES else "UNKNOWN"
        service.logger.critical("[WBEXIT2-PENDING-SCAN] account=%s symbol=%s age_seconds=%s phase=%s scan_exception=true native_protection=UNKNOWN reason=unresolved_close_obligation",
                                account, symbol, pending_age(state, now), phase)


async def refresh_pending(service, *, report_scan=True):
    """Existing sync hook: report every unresolved owner, reconcile ONE fairly.

    No provider call, new timer, process, quote poll or task per episode. Other
    durable rows retain responsibility while awaiting their fair cursor turn.
    Main's sync entry reports first (even if an earlier broker stage fails),
    then calls with report_scan=False to avoid a duplicate whole-cache line.
    A cold multi-owner boot is not claimed as a <10s completed recovery.
    """
    if not enabled(service) or service.__dict__.get("_wbexit2_refresh_active", False):
        return
    service._wbexit2_refresh_active = True
    try:
        if report_scan:
            log_pending_scan(service)  # Each standalone scan, even if the read then fails.
        cursor = service.__dict__.get("_wbexit2_refresh_cursor")
        cached = [UUID(value["owner_id"]) for value in service.__dict__.get("_wbexit2_pending", {}).values()
                  if value.get("owner_id")]
        def unit(session):
            query = select(SystemIncident).where(SystemIncident.service_name == "oms-risk",
                SystemIncident.payload["source"].as_string() == SOURCE,
                or_(SystemIncident.status == "open", SystemIncident.id.in_(cached)))
            row = session.scalar((query.where(SystemIncident.id > cursor) if cursor is not None else query)
                                 .order_by(SystemIncident.id).limit(1))
            if row is None and cursor is not None:
                row = session.scalar(query.order_by(SystemIncident.id).limit(1))
            if row is None:
                return None
            state = read(session, row.payload["identity"])
            return row.id, state
        context = _RecordOnly("system", "UNKNOWN", {}, {})
        result = await joined(service, context, service._run_db(unit, commit=False))
        if result is None:
            return  # Absence never clears a cached/live obligation.
        cursor, state = result
        remember(service, state)
        if state["phase"] != "RESOLVED":
            await finish_call(service, _RecordOnly(state["identity"]["account"],
                state["identity"]["symbol"], state["identity"], state))
        service._wbexit2_refresh_cursor = cursor
    finally:
        service._wbexit2_refresh_active = False


async def resume_on_quote(service, account, symbol, quote):
    """Use an ALREADY received quote and existing shared pacing, not a new poll."""
    from math import isfinite
    from project_mai_tai.oms import webull_attach_riskoff as wb
    if not enabled(service) or not (wb._rth(service) or wbexit3.enabled(service)):
        return False
    state = service.__dict__.get("_wbexit2_pending", {}).get((account, symbol))
    if state is None:
        return False
    if state.get("conflict") or state.get("sell") is not None:
        return True  # Named pending owner; existing native order reconciliation only.
    key = (account, symbol)
    if (key in service.__dict__.get("_wbattach_exit_owners", {})
            or key in service.__dict__.get("_webull_cw_exit_inflight", set())
            or service._webull_recovery_in_progress(account, symbol)
            or monotonic() < service.__dict__.get("_webull_cw_exit_next_try", {}).get(key, 0)):
        return True  # O(1), before shared path's existing DB/hand-over await.
    decision = state.get("decision")
    if not isinstance(decision, dict) or not isinstance(quote, dict):
        return True
    received = quote.get("received_at")
    now = _now()
    if (not isinstance(received, datetime) or received.tzinfo is None
            or not 0 <= (now - received).total_seconds() * 1000 <= float(service.settings.oms_v2_exit_quote_max_age_ms)):
        return True
    bid = float(quote.get("bid") or 0)
    reference = float(decision["reference_price"])
    if not isfinite(bid) or not isfinite(reference) or bid <= 0 or reference <= 0:
        return True
    context = deepcopy(decision["confirmation_context"])
    context.update(wbexit2_owner_id=state["owner_id"], wbexit2_original_risk_start=state["first_cancel_at"] or "UNKNOWN",
        wbexit2_original_decided_at=context.get("cw_decided_at", "UNKNOWN"),
        cw_decided_at=now.isoformat(), wbexit2_resume="true")
    await service._webull_cw_exit_on_shared_path(account, symbol, tag=state["reason"].removeprefix("oms_v2_managed_exit:"),
        ref=reference, bid=bid, expected_row_id=state["identity"]["managed_id"], decision_context=context)
    return True


def _now():
    from project_mai_tai.oms import webull_attach_riskoff as wb
    return wb.datetime.now(UTC)


def request_snapshot(request):
    return {"coid": request.client_order_id, "account": request.broker_account_name,
            "strategy": request.strategy_code, "symbol": request.symbol, "side": request.side,
            "intent_type": request.intent_type, "quantity": str(request.quantity), "reason": request.reason,
            "order_type": request.order_type, "time_in_force": request.time_in_force,
            "metadata": {k: deepcopy(v) for k, v in request.metadata.items() if k != "cw_submitted_at"}}


async def prepare_current(service, request, work):
    """Capture ONLY this creator's actual O14 commit; not a restored order."""
    from project_mai_tai.oms import webull_attach_riskoff as wb
    call = wb._ATR_CALL.get()
    if call is None or not (call.wb2 or getattr(call, "wb3_partial", False)):
        return await work  # Original uncovered/OFF/Schwab preparation unchanged.
    prepare_permit = None
    try:
        wb._current_atr_call(service, request.metadata["oms_managed_row_id"]
                             if "oms_managed_row_id" in request.metadata else call.managed_id)
        if wbexit3.enabled(service):
            from project_mai_tai.oms.wbexit3_vp_zero import register_prepare_permit
            prepare_permit = register_prepare_permit(service, call, request, work)
    except BaseException:
        if asyncio.iscoroutine(work):
            work.close()  # Known unscheduled prepare_managed_sell coroutine; no physical IO entered.
        raise
    def committed(intent_id):
        if intent_id is not None:
            call.wb2_prepared = (intent_id, replace(request, metadata=deepcopy(request.metadata)))
    primary = None
    try:
        result = await joined(service, call, work, completed=committed)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if prepare_permit is not None:
            from project_mai_tai.oms.wbexit3_vp_zero import revoke_prepare_permit
            try:
                revoke_prepare_permit(service, call, prepare_permit)
            except BaseException:
                if primary is None:
                    raise
                service.logger.critical("[WBEXIT3-PREPARE-UNKNOWN] exact_role_retained physical_join_unproven=true")
    wb._current_atr_call(service, call.managed_id)
    return result


async def abort_created_unposted(service, call, *, event_id=None):
    """Non-wire cleanup of this creator's joined commit, even after its deadline.

    Never for a restored pending row or after any SELL await. Uses existing O14
    audit, then validates the exact receipt. This cannot authorize a new order.
    The legacy O14 audit reason text remains unchanged; it is not a causal claim
    that the market session changed in a deadline/cancellation case.
    """
    from project_mai_tai.oms import webull_attach_riskoff as wb
    from project_mai_tai.oms.managed_sell_prewire import abandon_unposted_sell
    if (wb._ATR_CALL.get() is not call or call.task is not asyncio.current_task()
            or call.service is not service or call.wb2_pending is not None or call.wb2_sell_started is not None
            or service.__dict__.get("_wbattach_exit_owners", {}).get(call.owner_key) is not call):
        raise ValueError("wbexit2_unposted_cleanup_not_actual_creating_task")
    intent_id, request = call.wb2_prepared
    def existing_abort(session):
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        if order is None or order.status != "aborted":
            return None
        events = session.scalars(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id,
            BrokerOrderEvent.event_type == "aborted", BrokerOrderEvent.event_source == "client")).all()
        return events[0].id if len(events) == 1 else None
    if event_id is None:
        event_id = await joined(service, call, service._run_db(existing_abort, commit=False))
    if event_id is None:
        event_id = await joined(service, call, abandon_unposted_sell(service, request=request,
            intent_id=intent_id, now=_now()))
    def release(session):
        entry = session.get(BrokerOrder, UUID(call.wb2_binding["entry_id"]))
        row = session.get(OmsManagedPosition, UUID(call.managed_id))
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id).with_for_update())
        event = session.get(BrokerOrderEvent, event_id)
        parent = session.get(TradeIntent, intent_id)
        journal = deepcopy((entry.payload or {}).get(wb.KEY)) if entry is not None else None
        state = read(session, call.wb2_binding, lock=True)
        if (entry is None or row is None or row.entry_order_id != entry.id
                or row.entry_client_order_id != call.entry_coid or row.broker_account_name != call.account
                or row.strategy_code != "schwab_1m_v2" or row.symbol != call.symbol
                or order is None or parent is None or event is None or order.intent_id != intent_id
                or order.strategy_id != entry.strategy_id or order.broker_account_id != entry.broker_account_id
                or order.symbol != call.symbol or order.side != "sell" or order.quantity != request.quantity
                or order.order_type != request.order_type or order.time_in_force != request.time_in_force
                or order.status != "aborted" or order.submitted_at is not None or order.broker_order_id
                or order.payload != request.metadata or (parent.payload or {}).get("metadata") != request.metadata
                or parent.strategy_id != entry.strategy_id or parent.broker_account_id != entry.broker_account_id
                or parent.symbol != call.symbol or parent.side != "sell" or parent.intent_type != "close"
                or parent.reason != call.reason or parent.quantity != request.quantity or parent.status != "aborted"
                or event.order_id != order.id or event.event_source != "client" or event.event_type != "aborted"
                or event.payload.get("proven_not_sent") is not True or event.payload.get("client_order_id") != request.client_order_id
                or event.payload.get("metadata") != request.metadata
                or wb._aware_utc(event.event_at) < datetime.fromisoformat(request.metadata["managed_sell_prepared_at"])
                or wb._aware_utc(event.event_at) > _now()
                or session.scalar(select(Fill.id).where(Fill.order_id == order.id)) is not None
                or session.scalar(select(BrokerOrderEvent.id).where(BrokerOrderEvent.order_id == order.id,
                    BrokerOrderEvent.id != event.id)) is not None
                or not isinstance(journal, dict) or journal.get("exit_phase") != "claimed"
                or journal.get("exit_call_id") != call.call_id or state["invocation"] != call.call_id):
            raise ValueError("wbexit2_unposted_exact_O14_audit_unknown")
        if state["sell"] is not None:
            state = transition(session, call.wb2_binding, state["generation"], operation="proven_unsent",
                now=_now(), invocation=call.call_id, coid=request.client_order_id,
                abort_event_id=str(event.id), exact_O14_audit_validated=True)
        journal.pop("exit_phase")
        journal.pop("exit_call_id")
        journal["wbexit2_last_unsent"] = {"abort_event_id": str(event.id), "client_order_id": request.client_order_id,
                                          "source": "actual_creating_call_joined_O14_commit"}
        entry.payload = {**entry.payload, wb.KEY: journal}
        return state
    await joined(service, call, service._run_db(release, commit=True),
                 completed=lambda state: setattr(call, "wb2_state", state))
    call.wb2_prepared = None


async def reserve_current_sell(service, call, request):
    from project_mai_tai.oms import webull_attach_riskoff as wb
    snapshot = request_snapshot(request)
    def reserve(session):
        entry, row, account, strategy, owned = wb._atr(service, session, call.account, call.symbol,
                                                      call.entry_coid, prepared=request)
        if identity(entry, row, account, strategy) != call.wb2_binding or owned != call.owned_quantity:
            raise ValueError("wbexit2_pre_wire_owned_identity_changed")
        return transition(session, call.wb2_binding, call.wb2_state["generation"],
            operation="reserve_sell", now=_now(), invocation=call.call_id, request={"coid": request.client_order_id,
                "quantity": str(request.quantity), "request_binding": snapshot})
    await joined(service, call, service._run_db(reserve, commit=True),
                 completed=lambda state: setattr(call, "wb2_state", state))
    wb._current_atr_call(service, call.managed_id)
    if request_snapshot(request) != snapshot:
        raise ValueError("wbexit2_prepared_request_changed_after_reservation")


async def submit_current(service, request):
    """Main hook wraps ONLY the existing adapter submit, preserving CW receipt code."""
    from project_mai_tai.oms import webull_attach_riskoff as wb
    call = wb._ATR_CALL.get()
    if call is not None and getattr(call, "wb3_partial", False):
        from project_mai_tai.oms.wbexit3_partial import submit_current as submit_partial
        return await submit_partial(service, call, request)
    if call is None or not call.wb2:
        if selected(service, request.reason, request.broker_account_name):
            raise ValueError("wbexit2_covered_wire_requires_actual_durable_owner")
        return await service.broker_adapter.submit_order(request)
    wb._current_atr_call(service, call.managed_id)
    sell = (call.wb2_state or {}).get("sell")
    if (sell is None or sell["request_binding"] != request_snapshot(request)
            or call.wb2_sell_started is not None):
        raise ValueError("wbexit2_unreserved_or_duplicate_wire")
    call.wb2_sell_started = monotonic()  # UNKNOWN ACK remains reserved even on exception/cancel.
    return await joined(service, call, service.broker_adapter.submit_order(request))


def _working_geometry_statuses(statuses):
    """Accept the adapter's working vocabulary without accepting conflicting aliases."""
    from project_mai_tai.broker_adapters.webull import _ACCEPTED_STATUSES
    return len(set(statuses)) == 1 and set(statuses).issubset(_ACCEPTED_STATUSES)


def _log_original_geometry_refused(base, quantity, *, known=None, suffix=None,
                                   report=None, geometry=None, values=(), statuses=()):
    """Observe an already refused proof; diagnostics cannot change its outcome."""
    try:
        import json
        import logging
        from project_mai_tai.oms import webull_attach_riskoff as wb
        details = {}
        if report is None:
            predicate = "child_count" if len(known) != 2 else "distinct_broker_ids"
            details = {"child_count": len(known),
                       "distinct_broker_ids": len({r.broker_order_id for r in known.values()}),
                       "children": [{"client_order_id": r.client_order_id,
                                     "broker_order_id": r.broker_order_id,
                                     "origin": r.origin, "event_type": r.event_type,
                                     "quantity": r.quantity, "filled_quantity": r.filled_quantity,
                                     "geometry": r.metadata.get("wbexit2_geometry")}
                                    for r in known.values()]}
        else:
            if report.origin != "broker":
                predicate = "origin_broker"
            elif not report.broker_order_id:
                predicate = "broker_order_id_present"
            elif report.event_type != "accepted":
                predicate = "event_type_accepted"
            elif report.quantity != quantity:
                predicate = "quantity_equals_original"
            elif report.filled_quantity != 0:
                predicate = "filled_quantity_zero"
            elif not isinstance(geometry, dict):
                predicate = "geometry_present"
            elif not statuses:
                predicate = "statuses_present"
            elif not _working_geometry_statuses(statuses):
                predicate = "status"
            elif not values:
                predicate = "price_present"
            else:
                predicate = "price_unknown"
                for value in values:
                    if not value.is_finite():
                        predicate = "price_finite"
                    elif value <= 0:
                        predicate = "price_positive"
                    elif value != values[0]:
                        predicate = "price_aliases_equal"
                    elif wb.WebullBrokerAdapter._round_to_tick(value) != value:
                        predicate = "price_tick_rounded"
                    else:
                        continue
                    break
            details = {"client_order_id": report.client_order_id,
                       "broker_order_id": report.broker_order_id,
                       "expected_quantity": quantity, "price_values": list(values)}
        projected = geometry if isinstance(geometry, dict) else {}
        suppressed = json.loads(report.metadata.get("wbexit2_geometry_refusal", "null")) if report else None
        if isinstance(suppressed, dict):
            details["suppressed_projection"] = suppressed
        raw_statuses = [projected[k] for k in ("status", "order_status", "orderStatus") if k in projected]
        price_fields = {k: projected[k] for k in ("limit_price", "limitPrice", "stop_price", "stopPrice") if k in projected}
        if isinstance(suppressed, dict):
            parts = {name: suppressed.get(name + "_fields", {}) for name in ("raw", "item")}
            raw_statuses = [part[k] for part in parts.values() if isinstance(part, dict)
                            for k in ("status", "order_status", "orderStatus") if k in part]
            price_fields = {name: {k: part[k] for k in ("limit_price", "limitPrice", "stop_price", "stopPrice") if k in part}
                            for name, part in parts.items() if isinstance(part, dict)}
        logging.getLogger(__name__).warning(
            "[WBEXIT2-GEOMETRY-REFUSED] base=%s suffix=%s predicate=%s origin=%s "
            "event_type=%s quantity=%s filled_quantity=%s statuses=%s price_fields=%s "
            "geometry_present=%s details=%s",
            base, suffix or "PAIR", predicate, getattr(report, "origin", None),
            getattr(report, "event_type", None), getattr(report, "quantity", None),
            getattr(report, "filled_quantity", None), json.dumps(raw_statuses, default=str),
            json.dumps(price_fields, default=str, sort_keys=True), isinstance(geometry, dict),
            json.dumps(details, default=str, sort_keys=True))
    except Exception:  # A failed diagnostic must preserve the original proof refusal.
        pass


def original_geometry(reports, base, quantity, symbol):
    """WBEXIT2 proof validator; no dropped feature helper/flag is imported."""
    import json
    from project_mai_tai.oms import webull_attach_riskoff as wb
    known = {r.client_order_id: r for r in wb.validated_child_reads(reports, base, quantity, symbol, _now())}
    if len(known) != 2 or len({r.broker_order_id for r in known.values()}) != 2:
        _log_original_geometry_refused(base, quantity, known=known)
        raise ValueError("wbexit2_original_native_children_UNKNOWN")
    prices = []
    for suffix, fields in (("T", ("limit_price", "limitPrice")), ("S", ("stop_price", "stopPrice"))):
        report = known[wb.WebullBrokerAdapter._combo_leg_coid(base, suffix)]
        geometry = json.loads(report.metadata.get("wbexit2_geometry", "null"))
        values = [Decimal(str(geometry[k])) for k in fields if isinstance(geometry, dict) and k in geometry]
        statuses = [str(geometry[k]).upper() for k in ("status", "order_status", "orderStatus")
                    if isinstance(geometry, dict) and k in geometry]
        if (report.origin != "broker" or not report.broker_order_id or report.event_type != "accepted"
                or report.quantity != quantity or report.filled_quantity != 0
                or not statuses or not _working_geometry_statuses(statuses) or not values
                or any(not v.is_finite() or v <= 0 or v != values[0]
                       or wb.WebullBrokerAdapter._round_to_tick(v) != v for v in values)):
            _log_original_geometry_refused(base, quantity, suffix=suffix, report=report,
                                           geometry=geometry, values=values, statuses=statuses)
            raise ValueError("wbexit2_original_WORKING_geometry_UNKNOWN")
        prices.append(values[0])
    return tuple(prices)


def positive_original_terminal(reports, base, quantity, symbol, now):
    """ONE positive terminal report per original target and stop (TWO TOTAL).

    C1 02:15:11: not two per child. Absence/UNKNOWN is NEVER a terminal fact.
    This validator does not call an SDK or claim a saved census is fresh. The
    recording path separately requires the original bounded fresh census.
    """
    from project_mai_tai.oms import webull_attach_riskoff as wb
    reports = tuple(wb.validated_child_reads(reports, base, quantity, symbol, now))
    if (not wb.terminal_pair(reports, base, quantity, now, symbol=symbol)
            or len({r.broker_order_id for r in reports}) != 2
            or any(not r.broker_order_id or r.origin != "broker" or r.quantity != quantity
                   for r in reports)):
        raise ValueError("wbexit2_positive_original_terminal_UNKNOWN")
    return reports


def saved_original_terminal(state, attempt, now):
    """Revalidate the exact saved positive terminal receipt without retiming it.

    Terminal status is a durable fact; a later list omission is not a new fact.
    The positions read must STILL start after this invocation validates it.
    No saved working status or child-absence witness enters this path.
    """
    import json
    from project_mai_tai.oms import webull_attach_riskoff as wb
    raw = state.get("terminal_receipts", {}).get(attempt["base"])
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("wbexit2_saved_original_terminal_corrupt")
    reports = positive_original_terminal([wb.saved_report(p) for p in raw], attempt["base"],
                                         Decimal(attempt["quantity"]), state["identity"]["symbol"], now)
    known = {c["coid"]: c["native_id"] for p in state["originals"] for c in p["children"]}
    for report in reports:
        proof = json.loads(report.metadata.get("wbattach1_list_census", "null"))
        if (report.broker_order_id != known.get(report.client_order_id) or not isinstance(proof, dict)
                or proof.get("complete") is not True or proof.get("truncated") is not False
                or proof.get("account_id") != state["native_account_id"]
                or proof.get("broker_account_name") != state["identity"]["account"]
                or proof.get("symbol") != state["identity"]["symbol"] or proof.get("base") != attempt["base"]
                or proof.get("original_bases") != sorted(p["base"] for p in state["originals"])
                or type(proof.get("pages")) is not int or not 1 <= proof["pages"] <= 20):
            raise ValueError("wbexit2_saved_original_terminal_binding_UNKNOWN")
        acquired, started = (datetime.fromisoformat(proof[k]) for k in ("acquired_at", "started_at"))
        if (acquired.tzinfo is None or started.tzinfo is None or acquired > now
                or started < datetime.fromisoformat(state["opened_at"])
                or not 0 <= (acquired - started).total_seconds() <= 30
                or report.reported_at > acquired
                or Decimal(state["child_fill_highwater"][report.client_order_id]) != report.filled_quantity):
            raise ValueError("wbexit2_saved_original_terminal_time_or_fill_UNKNOWN")
    return reports


async def refresh_original_before_cancel(service, call, attempt, attempts):
    """Re-observe ONLY the stored original tree after a joined unentered cancel.

    Binding original IDs/geometry is not a fresh WORKING receipt. This reads
    once in the current bounded invocation, never rebinds a replacement pair,
    and keeps both the census freshness and final pre-wire freshness guards.
    Refusal means no cancel was sent here; it does not attest that an unreadable
    native book currently remains WORKING. That protection state stays UNKNOWN.
    """
    from project_mai_tai.oms import webull_attach_riskoff as wb
    after = _now()
    result = await joined(service, call, service.broker_adapter.confirm_exit_pair_from_today_list(
        broker_account_name=call.account, symbol=call.symbol, base_client_order_id=attempt["base"],
        quantity=Decimal(attempt["quantity"]), after=after, original_attempts=attempts))
    wb._current_atr_call(service, call.managed_id)
    reports = tuple(r for r in result.reports if r is not None)
    check_native(call, reports)
    observed_at = census(call, reports, attempt["base"], attempts, _now(), after=after)
    if result.outcome == "released":
        reports = positive_original_terminal(reports, attempt["base"], Decimal(attempt["quantity"]),
                                             call.symbol, _now())
        call.list_proof_at = observed_at
        return reports  # Already terminal: common path records it, no cancel.
    target, stop = original_geometry(reports if result.outcome == "reserved" else (),
                                     attempt["base"], Decimal(attempt["quantity"]), call.symbol)
    expected = {child["coid"]: child["price"] for pair in call.wb2_state["originals"]
                if pair["base"] == attempt["base"] for child in pair["children"]}
    for suffix, price in (("T", target), ("S", stop)):
        saved = expected.get(wb.WebullBrokerAdapter._combo_leg_coid(attempt["base"], suffix))
        if saved is None or Decimal(saved) != price:
            raise ValueError("wbexit2_original_WORKING_geometry_changed_before_FIRST_cancel")
    call.wb2_working_at = observed_at  # Only this exact successful fresh read authorizes cancel.
    return None


def fresh_ceiling(call, quantity):
    """Use ONE request-start-bound position receipt, never a cached fallback."""
    from math import isfinite
    if not isinstance(call.wb2_positions, tuple) or len(call.wb2_positions) != 3:
        raise ValueError("wbexit2_positions_wire_receipt_missing")
    rows, started, native_account = call.wb2_positions
    if (type(native_account) is not str or not native_account
            or native_account != call.wb2_state.get("native_account_id")):
        raise ValueError("wbexit2_positions_physical_account_not_original")
    if (type(started) not in (float, int) or not isfinite(started)
            or call.terminal_at is None or not call.terminal_at < started <= monotonic()
            or not 0 <= monotonic() - started <= 30):
        raise ValueError("wbexit2_positions_wire_receipt_stale_or_not_after_terminal")
    matches = [row for row in rows if row.symbol == call.symbol]
    if not matches or len(matches) == 1 and matches[0].broker_account_name == call.account and matches[0].quantity == 0:
        return Decimal(0)  # Broker-flat discrepancy is not an invented financial fill.
    if (len(matches) != 1 or matches[0].broker_account_name != call.account
            or not matches[0].quantity.is_finite() or matches[0].quantity < quantity
            or not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value()):
        raise ValueError("wbexit2_ledger_owned_quantity_not_backed_by_native_ceiling")
    return quantity  # Account/manual inventory NEVER chooses the owned size.


async def handoff(service, row, call):
    """New route under the surviving actual WBATTACH owner; H2 OFF body unchanged."""
    from project_mai_tai.oms import webull_attach_riskoff as wb
    account, symbol, coid = row.broker_account_name, row.symbol, row.entry_client_order_id
    if (call.account != account or call.symbol != symbol or call.entry_coid
            or row.strategy_code != "schwab_1m_v2"):
        raise ValueError("wbexit2_current_managed_episode_changed")
    key = (account, symbol)
    owners = service.__dict__.setdefault("_wbattach_exit_owners", {})
    if key in owners:
        raise ValueError("wbexit2_existing_current_invocation")
    owners[key], call.owner_key, call.entry_coid = call, key, coid
    journal, _, _, _, _ = await wb._atr_state(service, account, symbol, coid, allow_closed=True)
    await load_for_call(service, call, journal_hint=journal)
    if call.wb2_state.get("invocation") != call.call_id:
        # Preserve the exact durable row in this observation so finally can name
        # its owner. It grants no right to mutate/release a prior task's nonce.
        raise ValueError("wbexit2_prior_invocation_physical_quiescence_UNKNOWN")
    if call.wb2_state["reason"] != call.reason or call.wb2_state["sell"] is not None:
        raise ValueError("wbexit2_prior_close_or_unknown_sell_reserved")
    probe_ledger_zero = False
    def triggered(session, entry, managed, acct, strategy, owned, state):
        nonlocal probe_ledger_zero
        if state.get("exit_phase") or any(a["phase"] == "submitting" for a in state["attempts"]):
            raise ValueError("wbexit2_prior_attach_or_exit_still_inflight")
        state["exit_reason"] = call.reason
        probe_ledger_zero = (wbexit3.enabled(service) and owned == 0
                             and Decimal(managed.current_quantity) > 0)
    journal, _, _, owned, price = await wb._atr_state(service, account, symbol, coid, triggered, allow_closed=True)
    if probe_ledger_zero:
        # Diagnostic only, inside the current WB3 attempt and once per invocation.
        # The same-day recovery is separately proved AFTER exact original IDs bind.
        await wbexit3.probe_guard(service, "ledger_zero_managed_positive")
        wb._current_atr_call(service, row.id)
    if journal.get("legacy_base") or journal.get("history_proven") is not True:
        raise ValueError("wbexit2_original_history_UNPROVEN")
    attempts = journal["attempts"]
    if len(attempts) > 1:
        raise ValueError("wbexit2_initial_multi_attempt_native_geometry_UNKNOWN")
    first_terminal = None
    if attempts and call.wb2_state["originals_proven"] is not True:
        attempt = attempts[0]
        proof = await joined(service, call, service.broker_adapter.confirm_exit_pair_from_today_list(
            broker_account_name=account, symbol=symbol, base_client_order_id=attempt["base"],
            quantity=Decimal(attempt["quantity"]), after=_now(), original_attempts=attempts))
        wb._current_atr_call(service, row.id)
        reports = tuple(r for r in proof.reports if r is not None)
        original_quantity = Decimal(attempt["quantity"])
        call.wb2_working_at = census(call, reports, attempt["base"], attempts, _now())
        if proof.outcome == "released":
            first_terminal = positive_original_terminal(reports, attempt["base"], original_quantity, symbol, _now())
            prices = {r.client_order_id: None for r in first_terminal}
        else:
            target, stop = original_geometry(reports if proof.outcome == "reserved" else (),
                                             attempt["base"], original_quantity, symbol)
            if not stop < price < target:
                raise ValueError("wbexit2_original_levels_entry_binding_unknown")
            prices = {wb.WebullBrokerAdapter._combo_leg_coid(attempt["base"], suffix): str(value)
                      for suffix, value in (("T", target), ("S", stop))}
        originals = [{"base": attempt["base"], "quantity": str(original_quantity), "children": [
            {"coid": r.client_order_id, "native_id": r.broker_order_id, "price": prices[r.client_order_id]} for r in reports]}]
        await begin_for_call(service, call, original_quantity, originals,
                             terminal_reports=[wb.report_payload(r) for r in first_terminal] if first_terminal else None)
    if call.wb2_state["originals_proven"] is not True:
        raise ValueError("wbexit2_native_original_proof_UNKNOWN")
    for attempt in attempts:
        base, quantity = attempt["base"], Decimal(attempt["quantity"])
        after = None
        saved_terminal = saved_original_terminal(call.wb2_state, attempt, _now())
        terminal_reports = first_terminal or saved_terminal
        if (terminal_reports is None and base not in call.wb2_state["cancel_bases"]
                and call.wb2_working_at is None):
            terminal_reports = await refresh_original_before_cancel(service, call, attempt, attempts)
        if terminal_reports is None and base not in call.wb2_state["cancel_bases"]:
            if probe_ledger_zero:
                from project_mai_tai.oms.wbexit3_vp_zero import admit
                await admit(service, call)
                wb._current_atr_call(service, row.id)
            await checkpoint(service, call, "cancel", base=base)
            def cancel_mark(session, entry, managed, acct, strategy, current, state):
                if current != Decimal(call.wb2_state["remaining"]):
                    raise ValueError("wbexit2_owned_quantity_changed_before_cancel")
                state["attempts"] = [{**a, "cancel_requested": True} if a["base"] == base else a for a in state["attempts"]]
            await wb._atr_state(service, account, symbol, coid, cancel_mark, allow_closed=True)
            wb._current_atr_call(service, row.id)
            if call.wb2_working_at is None or not 0 <= (_now() - call.wb2_working_at).total_seconds() <= 2:
                raise ValueError("wbexit2_original_WORKING_receipt_stale_before_wire")
            call.wb2_cancels_entered.add(base)
            try:
                await joined(service, call, service.broker_adapter.cancel_exit_pair(
                    broker_account_name=account, symbol=symbol, base_client_order_id=base))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                service.logger.warning("[WBEXIT2-CANCEL-UNKNOWN] account=%s symbol=%s failure=%s", account, symbol, type(exc).__name__)
            after = _now()
        wb._current_atr_call(service, row.id)
        if terminal_reports is None:
            result = await joined(service, call, service.broker_adapter.confirm_exit_pair_from_today_list(
                broker_account_name=account, symbol=symbol, base_client_order_id=base,
                quantity=quantity, after=after, original_attempts=attempts))
            wb._current_atr_call(service, row.id)
            check_native(call, result.reports)
            reports = wb.validated_child_reads(result.reports, base, quantity, symbol, _now())
            call.list_proof_at = census(call, reports, base, attempts, _now(), after=after)
            if any(report.filled_quantity > 0 for report in reports):
                await wb._record_atr_children(service, account, symbol, coid, attempt, reports)
            if result.outcome != "released":
                raise ValueError("wbexit2_exit_waiting_for_ORIGINAL_terminal_proof")
            reports = positive_original_terminal(reports, base, quantity, symbol, _now())
        else:
            reports = terminal_reports
            check_native(call, reports)
            if first_terminal is not None:
                call.list_proof_at = call.wb2_working_at  # Actual acquisition, never retimed.
        if terminal_reports is not None and any(report.filled_quantity > 0 for report in reports):
            await wb._record_atr_children(service, account, symbol, coid, attempt, reports)
        call.wb2_terminal.update({r.client_order_id: str(r.filled_quantity) for r in reports})
        await checkpoint(service, call, "terminal", fills=call.wb2_terminal, all_originals_terminal=True,
                         terminal_receipts={base: [wb.report_payload(r) for r in reports]})
        if await wb._atr_resolved_by_child(service, journal, account, symbol, coid, base, reports):
            call.consumed = True
            return Decimal(0)
        def released(session, entry, managed, acct, strategy, current, state):
            state["attempts"] = [{**a, "phase": "released", "terminal_proof": [wb.report_payload(r) for r in reports]}
                                 if a["base"] == base else a for a in state["attempts"]]
            intent = session.get(TradeIntent, UUID(attempt["intent_id"]))
            if intent is not None:
                intent.status = "cancelled"
        await wb._atr_state(service, account, symbol, coid, released, allow_closed=True)
    if not attempts:
        await checkpoint(service, call, "terminal", fills={}, all_originals_terminal=True)
    if probe_ledger_zero:
        from project_mai_tai.oms.wbexit3_vp_zero import admit
        await admit(service, call)  # Existing proof is reused; terminal Fill resolution above runs first.
        wb._current_atr_call(service, row.id)
    _, _, _, owned, _ = await wb._atr_state(service, account, symbol, coid)
    call.terminal_at = monotonic()
    async with asyncio.timeout(2):
        call.wb2_positions = await joined(service, call, service.broker_adapter.read_wbexit2_positions(account, after=call.terminal_at))
    wb._current_atr_call(service, row.id)
    if not attempts:
        if not isinstance(call.wb2_positions, tuple) or len(call.wb2_positions) != 3:
            raise ValueError("wbexit2_bare_positions_account_UNKNOWN")
        await checkpoint(service, call, "bind_bare_account", native_account_id=call.wb2_positions[2])
    if owned == 0 and wbexit3.enabled(service):
        await wbexit3.probe_guard(service, "ledger_zero_managed_positive", receipt=call.wb2_positions)
    quantity = fresh_ceiling(call, owned)
    if quantity == 0:
        call.broker_flat = True
        await wb.resolve_atr_broker_flat(service, str(row.id))
        return Decimal(0)
    call.quantity, call.owned_quantity = quantity, owned
    def claim(session, entry, managed, acct, strategy, current, state):
        if (entry.status not in wb.TERMINAL or current != owned or state.get("exit_phase")
                or any(a["phase"] != "released" for a in state["attempts"])):
            raise ValueError("wbexit2_entry_or_owned_remainder_changed")
        state["exit_phase"], state["exit_call_id"] = "claimed", call.call_id
    await wb._atr_state(service, account, symbol, coid, claim)
    service._exit_reservation_released.add(key)
    await service._persist_webull_protect_state(account, symbol, "released")
    return quantity


async def validate_before_wire(service, call, request):
    from project_mai_tai.oms import webull_attach_riskoff as wb
    from project_mai_tai.oms.service import _managed_exit_session_matches_clock
    if (call.prepared is not None or call.quantity is None or call.terminal_at is None
            or request.reason != call.reason or request.strategy_code != "schwab_1m_v2"
            or request.broker_account_name != call.account or request.symbol != call.symbol
            or request.side != "sell" or request.intent_type != "close" or request.quantity != call.quantity
            or request.order_type not in {"market", "limit"} or request.time_in_force != "day"
            or not request.client_order_id or request.metadata.get("oms_managed_row_id") != call.managed_id
            or not request.metadata.get("managed_sell_prepared_at") or not _managed_exit_session_matches_clock(request.metadata)):
        raise ValueError("wbexit2_final_prepared_request_unknown")
    call.prepared = wb._request_binding(request)
    def check(session):
        entry, row, account, strategy, owned = wb._atr(service, session, call.account, call.symbol,
                                                     call.entry_coid, prepared=request)
        journal = entry.payload[wb.KEY]
        if getattr(call, "wb3_riskoff", False):
            from project_mai_tai.oms.wbexit3_riskoff import validate_current
            validate_current(service, session, call, request=request)
        if (identity(entry, row, account, strategy) != call.wb2_binding or owned != call.owned_quantity
                or entry.status not in wb.TERMINAL or journal.get("exit_phase") != "claimed"
                or journal.get("exit_call_id") != call.call_id or journal.get("history_proven") is not True
                or journal.get("legacy_base")):
            raise ValueError("wbexit2_final_owner_or_quantity_changed")
        for attempt in journal["attempts"]:
            if attempt["phase"] != "released" or not wb.terminal_pair(
                    [wb.saved_report(p) for p in attempt.get("terminal_proof", [])], attempt["base"],
                    Decimal(attempt["quantity"]), _now(), symbol=call.symbol):
                raise ValueError("wbexit2_final_original_tree_unknown")
    await joined(service, call, service._run_db(check, commit=False))
    wb._current_atr_call(service, call.managed_id)
    await reserve_current_sell(service, call, request)
    if (fresh_ceiling(call, call.quantity) != call.quantity or wb._request_binding(request) != call.prepared
            or call.list_proof_at is not None and not 0 <= (_now() - call.list_proof_at).total_seconds() <= 2
            or not _managed_exit_session_matches_clock(request.metadata)):
        raise ValueError("wbexit2_final_positions_list_or_request_changed")
    wb._current_atr_call(service, call.managed_id)


async def release_existing_abort(service, call, request, managed_id, abort_id):
    from project_mai_tai.oms import webull_attach_riskoff as wb
    if (call.managed_id != str(managed_id) or call.prepared != wb._request_binding(request)
            or abort_id is None or call.wb2_prepared is None):
        raise ValueError("wbexit2_abort_not_this_actual_creator")
    await abort_created_unposted(service, call, event_id=abort_id)
    call.consumed = True


def positive_quantity(value, *, zero=False):
    if not isinstance(value, str):
        raise ValueError("wbexit2_quantity_encoding_unknown")
    quantity = Decimal(value)
    if not quantity.is_finite() or quantity < 0 or not zero and quantity == 0:
        raise ValueError("wbexit2_quantity_unknown")
    if quantity != quantity.to_integral_value():
        raise ValueError("wbexit2_fractional_quantity_unknown")
    return quantity


def timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("wbexit2_aware_time_required")
    return value.isoformat()


def identity(entry, managed, account, strategy):
    """Bind actual rows, not metadata UUID agreement or account inventory."""
    if (not isinstance(entry, BrokerOrder) or not isinstance(managed, OmsManagedPosition)
            or not isinstance(account, BrokerAccount) or not isinstance(strategy, Strategy)
            or entry.id != managed.entry_order_id or entry.client_order_id != managed.entry_client_order_id
            or entry.broker_account_id != account.id or entry.strategy_id != strategy.id
            or entry.symbol != managed.symbol or entry.side != "buy" or not entry.broker_order_id
            or account.name != managed.broker_account_name or account.provider != "webull"
            or not account.is_active or strategy.code != "schwab_1m_v2"
            or not controlled_live_scope(strategy.code, strategy.execution_mode, account.name)
            or managed.strategy_code != strategy.code or not entry.client_order_id):
        raise ValueError("wbexit2_actual_entry_binding_unknown")
    return {"entry_id": str(entry.id), "entry_coid": entry.client_order_id,
            "entry_native_id": entry.broker_order_id, "managed_id": str(managed.id),
            "account_id": str(account.id), "account": account.name,
            "strategy_id": str(strategy.id), "strategy": strategy.code, "symbol": entry.symbol}


def require_obligation_episode(service, session, entry, managed, account, strategy, intent, now, call):
    """Prior-day authority ONLY for the immutable existing close obligation.

    This changes no BUY/entry/legacy session guard. Actual current owner/STOP,
    fresh original native proof and CAS still gate every wire. A cached state
    supplies immutable identity only; it is never native/nonce permission.
    """
    from project_mai_tai.oms import webull_attach_riskoff as wb
    from project_mai_tai.oms.route_owner import _utc_row_time
    binding = identity(entry, managed, account, strategy)
    marker = (entry.payload or {}).get(wb.KEY, {}).get("wbexit2_owner_id")
    if not marker:
        if wbexit3.existing_obligation_episode(service, entry, managed, account, strategy, intent, now):
            return
        wb.require_current_episode(entry, intent, now)  # FIRST obligation must still be current.
        return
    if (not enabled(service) or call.account != account.name or call.symbol != entry.symbol
            or call.managed_id != str(managed.id) or intent is None
            or intent.strategy_id != entry.strategy_id or intent.broker_account_id != entry.broker_account_id
            or intent.symbol != entry.symbol or intent.side != "buy" or intent.intent_type != "open"):
        raise ValueError("wbexit2_existing_obligation_entry_parent_UNKNOWN")
    state = call.wb2_state if call.wb2_binding == binding else read(session, binding)
    anchor = _utc_row_time(entry.submitted_at) or _utc_row_time(intent.created_at)
    if (state is None or state["identity"] != binding or state["owner_id"] != marker
            or state["phase"] not in OPEN_PHASES or state.get("schema_version") != 1
            or anchor is None or anchor > now or state.get("entry_anchor_at") != timestamp(anchor)
            or not anchor <= datetime.fromisoformat(state["opened_at"]) <= now):
        raise ValueError("wbexit2_original_entry_anchor_or_durable_owner_UNKNOWN")
    journal = entry.payload.get(wb.KEY)
    if not isinstance(journal, dict) or not isinstance(journal.get("attempts"), list):
        raise ValueError("wbexit2_original_journal_UNKNOWN")
    original = {(p["base"], positive_quantity(p["quantity"])) for p in state["originals"]}
    current = {(p["base"], positive_quantity(p["quantity"])) for p in journal["attempts"]}
    if (len(original) != len(state["originals"]) or len(current) != len(journal["attempts"])
            or original != current):
        raise ValueError("wbexit2_original_attempt_census_changed_NOT_new_WORKING_permission")


def address(binding):
    return uuid5(NAMESPACE, ":".join(binding[key] for key in
        ("entry_id", "managed_id", "account_id", "strategy_id")))


def read(session, binding, *, lock=False):
    query = select(SystemIncident).where(SystemIncident.id == address(binding)).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = session.scalar(query)
    if row is None:
        return None
    state = deepcopy(row.payload)
    if (not isinstance(state, dict) or state.get("source") != SOURCE
            or state.get("schema_version") != 1 or not isinstance(state.get("entry_anchor_at"), str)
            or state.get("identity") != binding or type(state.get("generation")) is not int
            or state["generation"] < 1 or state.get("phase") not in OPEN_PHASES | {"RESOLVED"}
            or state.get("owner_id") != str(address(binding))):
        raise ValueError("wbexit2_durable_owner_corrupt")
    return state


def begin(session, binding, *, quantity, originals, reason, now, bare_proven=False, decision=None, attempt_started=None,
          originals_proven=True, native_account_id=None, entry_anchor=None, wbexit3_enabled=False, prior_software_filled=Decimal(0)):
    """Commit this result BEFORE the first original-child cancellation.

    originals is the exact validated native WORKING proof, including its native
    IDs/geometry and the entire original-attempt census. It is never rewritten
    from a later WORKING pair. An empty tree needs positive bare-fill provenance
    from the existing _atr_state caller; absence is not that provenance.
    """
    timestamp(now)
    positive_quantity(str(quantity))
    if reason not in REASONS and not (wbexit3_enabled is True and reason in wbexit3.FULL_CLOSE_REASONS):
        raise ValueError("wbexit2_exit_reason_not_covered")
    if wbexit3_enabled is True:
        if (not isinstance(prior_software_filled, Decimal) or not prior_software_filled.is_finite()
                or not 0 <= prior_software_filled <= quantity
                or prior_software_filled != prior_software_filled.to_integral_value()):
            raise ValueError("wbexit3_prior_software_execution_quantity_UNKNOWN")
    elif prior_software_filled != Decimal(0):
        raise ValueError("wbexit3_prior_software_provenance_requires_enabled_input")
    old = read(session, binding, lock=True)
    if old is not None:
        if old["phase"] == "RESOLVED":
            raise ValueError("wbexit2_resolved_obligation_not_rearmed")
        return old
    if not isinstance(originals, list) or not originals and bare_proven is not True and originals_proven:
        raise ValueError("wbexit2_original_tree_unknown")
    if originals_proven and originals and (type(native_account_id) is not str or not native_account_id):
        raise ValueError("wbexit2_original_native_account_unknown")
    bases, children, native_ids = set(), set(), set()
    for pair in originals:
        if (not isinstance(pair, dict) or not isinstance(pair.get("base"), str) or not pair["base"]
                or pair["base"] in bases or not isinstance(pair.get("children"), list)
                or len(pair["children"]) != 2):
            raise ValueError("wbexit2_original_pair_unknown")
        bases.add(pair["base"])
        if originals_proven and positive_quantity(pair["quantity"]) != quantity:
            raise ValueError("wbexit2_original_pair_quantity_unknown")
        positive_quantity(pair["quantity"])
        for child in pair["children"]:
            key = child.get("coid")
            if not isinstance(key, str) or not key or key in children:
                raise ValueError("wbexit2_original_child_unknown")
            if originals_proven:
                if (not isinstance(child.get("native_id"), str) or not child["native_id"]
                        or child["native_id"] in native_ids or not isinstance(child.get("price"), str)):
                    raise ValueError("wbexit2_original_child_unknown")
                price = Decimal(child["price"])
                if not price.is_finite() or price <= 0:
                    raise ValueError("wbexit2_original_geometry_unknown")
            elif child.get("native_id") is not None or child.get("price") is not None:
                raise ValueError("wbexit2_unproven_original_must_be_explicit_UNKNOWN")
            children.add(key)
            native_ids.add(child["native_id"])
    if entry_anchor is None or not entry_anchor <= now:
        raise ValueError("wbexit2_original_entry_anchor_UNKNOWN")
    state = {"source": SOURCE, "schema_version": 1, "entry_anchor_at": timestamp(entry_anchor),
             "owner_id": str(address(binding)), "identity": deepcopy(binding),
             "generation": 1, "phase": "BOUND" if originals_proven else "HOLD", "reason": reason,
             "opened_at": timestamp(now), "first_cancel_at": None,
             "risk_clock_source": "PREWIRE_RESERVATION_NOT_NATIVE_RELEASE",
             "original_attempt_outcome": "PENDING", "original_quantity": str(quantity),
             "originals": deepcopy(originals), "child_fill_highwater": {key: "0" for key in children},
             "originals_proven": originals_proven is True,
             "native_account_id": native_account_id,
             "remaining": str(quantity), "cancel_bases": [], "sell": None,
             "decision": deepcopy(decision),
             "original_attempt_started_at": timestamp(attempt_started or now),
             "original_action_deadline_at": timestamp((attempt_started or now) + timedelta(seconds=6)),
             "invocation": None,
             "hold_reason": None, "scan_exception": False, "updated_at": timestamp(now)}
    if wbexit3_enabled is True:
        state["remaining"] = str(quantity - prior_software_filled)
        state["prior_software_filled"] = str(prior_software_filled)
    session.add(SystemIncident(id=address(binding), service_name="oms-risk", severity="info",
        status="open", title=f"Webull close owner: {binding['symbol']}"[:255], opened_at=now,
        payload=state))
    session.flush()  # Unique primary key refuses two concurrent creators.
    return deepcopy(state)


def _save(session, old, new):
    """Row lock plus expected generation: a stale callback cannot overwrite it."""
    new["generation"] = old["generation"] + 1
    result = session.execute(update(SystemIncident).where(
        SystemIncident.id == UUID(old["owner_id"]),
        SystemIncident.payload["source"].as_string() == SOURCE,
        SystemIncident.payload["generation"].as_integer() == old["generation"],
    ).values(payload=new, status="closed" if new["phase"] == "RESOLVED" else "open",
             severity="critical" if new["scan_exception"] else "info",
             closed_at=datetime.fromisoformat(new["resolved_at"]) if new["phase"] == "RESOLVED" else None)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise ValueError("wbexit2_CAS_lost")
    return deepcopy(new)


def transition(session, binding, expected_generation, *, operation, now, **evidence):
    """Small closed command set; no general payload mutation or wire permission.

    Every caller revalidates STOP, session, current real task, managed owner,
    quantity and exact original terminal proof after awaits and before any wire.
    A persisted responsibility may outlive any such authority.
    """
    old = read(session, binding, lock=True)
    if old is None or type(expected_generation) is not int or old["generation"] != expected_generation:
        raise ValueError("wbexit2_CAS_lost")
    if old["phase"] == "RESOLVED" and operation != "release":
        raise ValueError("wbexit2_resolved_owner_immutable")
    if operation in {"cancel", "terminal", "reserve_sell", "proven_unsent", "unentered_cancel", "bind_originals", "bind_bare_account"} and (
            old.get("invocation") is None or old["invocation"] != evidence.get("invocation")):
        raise ValueError("wbexit2_current_durable_invocation_changed")
    new = deepcopy(old)
    new["updated_at"] = timestamp(now)
    if operation == "bind_originals":
        originals = evidence.get("originals")
        native_account = evidence.get("native_account_id")
        terminal_reports = evidence.get("terminal_reports")
        if (type(native_account) is not str or not native_account
                or old.get("native_account_id") not in (None, native_account)):
            raise ValueError("wbexit2_original_native_account_changed")
        if (old["cancel_bases"] or old["sell"] is not None or not isinstance(originals, list)
                or len(originals) != len(old["originals"])):
            raise ValueError("wbexit2_cannot_rebind_after_original_cancel")
        native_ids = set()
        terminal_ids = None
        if terminal_reports is not None:
            from project_mai_tai.oms import webull_attach_riskoff as wb
            if len(originals) != 1 or not isinstance(terminal_reports, list):
                raise ValueError("wbexit2_initial_terminal_census_UNKNOWN")
            pair = originals[0]
            terminal = positive_original_terminal([wb.saved_report(p) for p in terminal_reports],
                pair["base"], Decimal(pair["quantity"]), old["identity"]["symbol"], now)
            terminal_ids = {r.client_order_id: r.broker_order_id for r in terminal}
        for actual, original in zip(originals, old["originals"], strict=True):
            if (actual.get("base") != original["base"] or Decimal(actual["quantity"]) != Decimal(original["quantity"])
                    or Decimal(actual["quantity"]) != Decimal(old["original_quantity"])
                    or {c["coid"] for c in actual["children"]} != {c["coid"] for c in original["children"]}):
                raise ValueError("wbexit2_initial_native_tree_changed")
            for child in actual["children"]:
                price = Decimal(child["price"]) if child["price"] is not None else None
                if ((terminal_ids is None and (price is None or not price.is_finite() or price <= 0))
                        or terminal_ids is not None and (price is not None or terminal_ids.get(child["coid"]) != child["native_id"])
                        or not isinstance(child["native_id"], str)
                        or not child["native_id"] or child["native_id"] in native_ids):
                    raise ValueError("wbexit2_initial_native_geometry_unknown")
                native_ids.add(child["native_id"])
        if not originals and evidence.get("bare_proven") is not True:
            raise ValueError("wbexit2_positive_bare_proof_required")
        if old["originals_proven"] and old["originals"] != originals:
            raise ValueError("wbexit2_original_native_provenance_immutable")
        new["originals"], new["originals_proven"], new["phase"] = deepcopy(originals), True, "BOUND"
        new["native_account_id"] = native_account
    elif operation == "bind_bare_account":
        native_account = evidence.get("native_account_id")
        if (old["originals"] or old["originals_proven"] is not True or old["phase"] != "TERMINAL"
                or old["sell"] is not None or type(native_account) is not str or not native_account
                or old.get("native_account_id") not in (None, native_account)):
            raise ValueError("wbexit2_positive_bare_native_account_UNKNOWN")
        new["native_account_id"] = native_account
    elif operation == "acquire":
        token = evidence.get("invocation")
        if old.get("invocation") is not None or not isinstance(token, str) or not token:
            raise ValueError("wbexit2_prior_invocation_not_physically_joined")
        new["invocation"] = token
    elif operation == "release":
        if old.get("invocation") != evidence.get("invocation") or evidence.get("physical_joined") is not True:
            raise ValueError("wbexit2_joined_invocation_release_unknown")
        new["invocation"] = None
    elif operation == "cancel":
        base = evidence.get("base")
        if (old["originals_proven"] is not True or old["sell"] is not None or base not in {pair["base"] for pair in old["originals"]}
                or base in old["cancel_bases"]):
            raise ValueError("wbexit2_cancel_not_original_or_already_requested")
        new["cancel_bases"].append(base)
        new["first_cancel_at"] = old["first_cancel_at"] or timestamp(now)
        new["phase"] = "WAIT_TERMINAL"
    elif operation == "unentered_cancel":
        bases = evidence.get("bases")
        if (old["sell"] is not None or not isinstance(bases, list) or not bases
                or not set(bases) <= set(old["cancel_bases"])
                or evidence.get("physical_joined") is not True or evidence.get("adapter_never_entered") is not True):
            raise ValueError("wbexit2_cancel_never_entered_not_proven")
        new["cancel_bases"] = [base for base in old["cancel_bases"] if base not in bases]
        new["last_unentered_cancel"] = {"invocation": evidence["invocation"], "bases": bases,
                                        "recorded_at": timestamp(now), "source": "actual_invocation_control_flow"}
        new["phase"] = "HOLD"
    elif operation == "terminal":
        fills = evidence.get("fills")
        expected = set(old["child_fill_highwater"])
        if (old["originals_proven"] is not True or not isinstance(fills, dict) or set(fills) != expected
                or evidence.get("all_originals_terminal") is not True):
            raise ValueError("wbexit2_original_terminal_census_unknown")
        total = Decimal(0)
        for key, value in fills.items():
            fill = positive_quantity(value, zero=True)
            if fill < Decimal(old["child_fill_highwater"][key]):
                raise ValueError("wbexit2_child_fill_regressed")
            total += fill
        remaining = positive_quantity(old["original_quantity"]) - total - Decimal(old.get("prior_software_filled", "0"))
        if old["sell"] is not None:
            remaining -= Decimal(old["sell"]["filled_highwater"])
        if remaining < 0:
            raise ValueError("wbexit2_attributed_fill_exceeds_original")
        new["child_fill_highwater"] = deepcopy(fills)
        new["remaining"] = str(remaining)
        supplied = evidence.get("terminal_receipts")
        if supplied is not None:
            if not isinstance(supplied, dict) or set(supplied) != {p["base"] for p in old["originals"]}:
                raise ValueError("wbexit2_positive_terminal_receipt_census_UNKNOWN")
            new["terminal_receipts"] = deepcopy(supplied)
            for pair in old["originals"]:
                saved_original_terminal(new, pair, now)  # Exact IDs, physical account, clocks and highwaters.
        new["phase"] = "TERMINAL" if old["sell"] is None else old["phase"]
        # Not RESOLVED here: fills must still reach the real managed ledger.
    elif operation == "reserve_sell":
        request = evidence.get("request")
        if (old["phase"] != "TERMINAL" or old["sell"] is not None
                or type(old.get("native_account_id")) is not str or not old["native_account_id"]
                or not isinstance(request, dict) or not request.get("coid")
                or not request.get("request_binding")
                or positive_quantity(request.get("quantity")) != positive_quantity(old["remaining"])):
            raise ValueError("wbexit2_sell_unknown_or_already_reserved")
        new["sell"] = {**deepcopy(request), "filled_highwater": "0", "ack": "UNKNOWN"}
        new["phase"] = "SELL_RESERVED"  # Crash before/after submit has SAME conservative reservation.
    elif operation == "sell_status":
        sell = old["sell"]
        if (sell is None or evidence.get("coid") != sell["coid"]
                or evidence.get("native_proven") is not True
                or evidence.get("status") not in {"accepted", "partially_filled", "filled", "cancelled", "rejected", "expired"}):
            raise ValueError("wbexit2_OUR_sell_status_unknown")
        filled = positive_quantity(evidence.get("filled"), zero=True)
        if not Decimal(sell["filled_highwater"]) <= filled <= Decimal(sell["quantity"]):
            raise ValueError("wbexit2_OUR_sell_fill_regressed_or_excess")
        new["sell"] = {**sell, "filled_highwater": str(filled), "ack": evidence["status"]}
        new["remaining"] = str(Decimal(old["original_quantity"])
            - sum((Decimal(value) for value in old["child_fill_highwater"].values()), Decimal(0))
            - Decimal(old.get("prior_software_filled", "0")) - filled)
        if Decimal(new["remaining"]) < 0:
            raise ValueError("wbexit2_recorded_executions_exceed_original")
        new["phase"] = "SELL_WORKING"
        # Even a rejection retains the exact reservation until positively
        # reconciled by existing order/managed-position code. No second SELL.
    elif operation == "proven_unsent":
        if (old["sell"] is None or evidence.get("coid") != old["sell"]["coid"]
                or not evidence.get("abort_event_id") or evidence.get("exact_O14_audit_validated") is not True):
            raise ValueError("wbexit2_exact_PROVEN_NOT_SENT_required")
        new["last_unsent_abort_event_id"] = evidence["abort_event_id"]
        new["sell"], new["phase"] = None, "TERMINAL"
    elif operation == "hold":
        reason = evidence.get("reason")
        if not isinstance(reason, str) or not reason or len(reason) > 240:
            raise ValueError("wbexit2_hold_reason_unknown")
        new["hold_reason"], new["scan_exception"] = reason, True
        if old["sell"] is None:
            new["phase"] = "HOLD"
        if evidence.get("original_attempt_failed") is True:
            new["original_attempt_outcome"] = "FAIL"  # No later retry turns it into a timely PASS.
    elif operation == "resolve":
        if (evidence.get("managed_closed") is not True or evidence.get("virtual_flat") is not True
                or Decimal(old["remaining"]) != 0 or evidence.get("attributed_execution_complete") is not True):
            raise ValueError("wbexit2_positive_resolution_required")
        new["phase"] = "RESOLVED"
        new["resolved_at"] = timestamp(now)
    else:
        raise ValueError("wbexit2_unknown_transition")
    return _save(session, old, new)
