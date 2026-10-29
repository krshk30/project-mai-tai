"""RTH attach failures hand off to existing owners; this module never submits a SELL."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from math import isfinite
from threading import Lock
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import or_, select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition, Strategy, SystemIncident, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.bot_control_scope import controlled_live_scope
from project_mai_tai.oms.price_binding import stored_price

REASON = "WEBULL_ATTACH_RISK_OFF"
KEY = "wbattach1"
BARE_FILL_PROOF = "wbattach1_bare_fill_observed"
TERMINAL = {"filled", "cancelled", "rejected", "expired"}
SOFTWARE_EXIT_REASONS = frozenset({
    "oms_v2_managed_exit:CW_FLIP", "oms_v2_managed_exit:CW_HARD_STOP",
    "oms_v2_managed_exit:CONFIRMATION_EXIT", "oms_v2_managed_exit:CW_FLOOR",
})


def marked_atr_episode(payload):
    # Presence, not truthiness: corrupt/empty strict state cannot opt into legacy.
    return KEY in payload or BARE_FILL_PROOF in payload


async def atr_attach_is_strict(service, account_name, symbol, coid):
    def read(session):
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == coid))
        if entry is None:
            raise ValueError("wbattach1_attach_entry_unknown")
        if marked_atr_episode(entry.payload or {}):
            return True
        account = session.get(BrokerAccount, entry.broker_account_id)
        strategy = session.get(Strategy, entry.strategy_id)
        if (account is None or strategy is None or account.name != account_name
                or account.provider != "webull" or strategy.code != "schwab_1m_v2"
                or entry.symbol != symbol or entry.side != "buy" or not entry.broker_order_id):
            raise ValueError("wbattach1_legacy_attach_entry_binding_unknown")
        return False
    return await service._run_db(read, commit=False)


@dataclass
class _AtrCall:
    service: object
    task: object
    account: str
    symbol: str
    managed_id: str
    call_id: str
    entry_coid: str = ""
    quantity: Decimal | None = None
    owned_quantity: Decimal | None = None
    terminal_at: float | None = None
    prepared: tuple | None = None
    consumed: bool = False
    reason: str = REASON
    owner_key: tuple | None = None
    broker_flat: bool = False
    list_proof_at: datetime | None = None
    wb2: bool = False
    wb2_state: dict | None = None
    wb2_binding: dict | None = None
    wb2_pending: object = None
    wb2_deadline: float = 0
    wb2_control: object = None
    wb2_terminal: dict = field(default_factory=dict)
    wb2_decision: dict | None = None
    wb2_prior_cancel_bases: frozenset = frozenset()
    wb2_cancels_entered: set = field(default_factory=set)
    wb2_started_at: datetime | None = None
    wb2_prepared: tuple | None = None
    wb2_working_at: datetime | None = None
    wb2_positions: tuple | None = None
    wb2_sell_started: float | None = None
    wb2_native_account_id: str | None = None


_ATR_CALL = ContextVar("wbattach1_atr_call", default=None)


def atr_handoff_call(method):
    """Scope proof to the actual claimed-owner invocation, including flag-OFF use."""
    @wraps(method)
    async def bound(service, *args, **kwargs):
        reason = kwargs.get("reason")
        account = args[0] if args else kwargs.get("acct")
        from project_mai_tai.oms import wbexit2
        wb2 = wbexit2.selected(service, reason, account)
        if reason != REASON and reason not in SOFTWARE_EXIT_REASONS and not wb2:
            return await method(service, *args, **kwargs)
        symbol = args[1] if len(args) > 1 else kwargs.get("symbol")
        call = _AtrCall(service, asyncio.current_task(), account, symbol,
                        kwargs.get("expected_managed_row_id", ""), str(uuid4()))
        call.reason = reason
        call.wb2 = wb2
        if wb2:
            call.wb2_started_at = datetime.now(UTC)
            call.wb2_deadline = monotonic() + 6
            call.wb2_control = deepcopy(getattr(getattr(service, "_bot_control_runtime", None), "states", {}).get("schwab_1m_v2"))
            call.wb2_decision = {"reference_price": str(kwargs.get("reference_price")),
                                "confirmation_context": deepcopy(kwargs.get("confirmation_context") or {})}
        token = _ATR_CALL.set(call)
        primary = None
        try:
            if wb2:
                async with asyncio.timeout(6):
                    return await method(service, *args, **kwargs)
            return await method(service, *args, **kwargs)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                if call.entry_coid and call.prepared is None and not call.consumed:
                    await _release_unprepared_software_handoff(service, call)
            finally:
                try:
                    if wb2:
                        finish_primary = None
                        try:
                            await wbexit2.finish_call(service, call)
                        except BaseException as exc:
                            finish_primary = exc
                            service.logger.critical("[WBEXIT2-RECORD-UNKNOWN] account=%s symbol=%s failure=%s scan_exception=true",
                                                    account, symbol, type(exc).__name__)
                            if primary is None:
                                raise
                        finally:
                            try:
                                await wbexit2.release_invocation(service, call)
                            except BaseException as exc:
                                service.logger.critical("[WBEXIT2-INVOCATION-UNKNOWN] account=%s symbol=%s failure=%s scan_exception=true",
                                                        account, symbol, type(exc).__name__)
                                if primary is None and finish_primary is None:
                                    raise
                finally:
                    call.consumed = True
                    owners = service.__dict__.get("_wbattach_exit_owners", {})
                    if call.owner_key is not None and owners.get(call.owner_key) is call:
                        owners.pop(call.owner_key)
                    _ATR_CALL.reset(token)
    return bound


async def _release_unprepared_software_handoff(service, call):
    try:
        def unprepared(session):
            entry, row, _, _, owned = _atr(service, session, call.account, call.symbol, call.entry_coid)
            state = dict(entry.payload[KEY])
            if not state.get("exit_phase"):
                return  # The shielded claim transaction refused before committing a claim.
            if (str(row.id) != call.managed_id or owned != call.owned_quantity
                    or state.get("exit_phase") != "claimed" or state.get("exit_call_id") != call.call_id):
                raise ValueError("wbattach1_unprepared_handoff_changed")
            # No prepared request reached final validation, and _atr rejects
            # any remaining nonterminal SELL reservation.
            state.pop("exit_phase")
            state.pop("exit_call_id")
            entry.payload = {**entry.payload, KEY: state}
        await service._run_db(unprepared, commit=True)
    except Exception as exc:
        await critical(service, call.account, call.symbol,
            f"unprepared_software_handoff_UNKNOWN:{type(exc).__name__}", entry_id=call.entry_coid)


def _current_atr_call(service, managed_id, *, require_rth=True):
    from project_mai_tai.oms import service as module
    call = _ATR_CALL.get()
    if (call is None or call.consumed or call.service is not service
            or call.task is not asyncio.current_task() or call.managed_id != str(managed_id)
            or not call.managed_id or require_rth and call.reason == REASON and not _rth(service)):
        raise ValueError("wbattach1_live_owner_call_unknown")
    if (call.owner_key is not None
            and service.__dict__.get("_wbattach_exit_owners", {}).get(call.owner_key) is not call):
        raise ValueError("wbattach1_exit_owner_claim_lost")
    if call.wb2:
        from project_mai_tai.oms.wbexit2 import check_call
        check_call(service, call)
    if getattr(service.settings, "bot_control_enabled", False):
        owner = module._CONTROL_MANAGED_EMIT.get()
        key = (call.account, call.symbol.upper(), call.managed_id)
        if (owner is None or service.__dict__.get("_control_managed_exit_claims", {}).get(key) is not owner
                or service.__dict__.get("_control_managed_exit_symbol_owners", {}).get(key[:2]) is not owner
                or owner.get("claim") is None or owner["claim"][1] != key):
            raise ValueError("wbattach1_real_owner_claim_lost")
    return call


def stamp_new_bare_fill(session, row, *, entry_id, coid, quantity):
    """Only the new managed-row branch calls this, inside the real fill transaction."""
    entry = session.get(BrokerOrder, entry_id) if entry_id is not None else None
    account = session.get(BrokerAccount, entry.broker_account_id) if entry is not None else None
    strategy = session.get(Strategy, entry.strategy_id) if entry is not None else None
    if (entry is None or row is None or account is None or strategy is None
            or entry.client_order_id != coid or not entry.broker_order_id or entry.side != "buy"
            or row.entry_order_id != entry.id or row.entry_client_order_id != coid
            or row.strategy_code != "schwab_1m_v2" or strategy.code != row.strategy_code
            or row.broker_account_name != account.name or row.symbol != entry.symbol
            or row.status != "open" or account.provider != "webull" or not account.is_active
            or not controlled_live_scope(strategy.code, strategy.execution_mode, account.name)
            or str((entry.payload or {}).get("fanout_leg", "")).lower() != "webull"
            or str((entry.payload or {}).get("native_oco_bracket", "")).lower() == "true"
            or marked_atr_episode(entry.payload or {})
            or (entry.payload or {}).get("webull_protect_base_client_order_id")):
        return False
    intent = session.get(TradeIntent, entry.intent_id)
    if intent is None or intent.side != "buy" or intent.intent_type != "open":
        return False
    try:
        require_current_episode(entry, intent, datetime.now(UTC))
    except ValueError:
        return False
    fills = session.scalars(select(Fill).where(Fill.order_id == entry.id)).all()
    if not fills or any(f.strategy_id != strategy.id or f.broker_account_id != account.id
            or f.symbol != entry.symbol or f.side != "buy" or not f.broker_fill_id
            or not f.quantity.is_finite() or not f.price.is_finite() or f.quantity <= 0 or f.price <= 0
            or f.filled_at is None or _aware_utc(f.filled_at) > datetime.now(UTC) for f in fills):
        return False
    bought = sum((f.quantity for f in fills), Decimal(0))
    if (not entry.quantity.is_finite() or not 0 < bought <= entry.quantity
            or bought != quantity or bought != Decimal(row.original_quantity)
            or bought != Decimal(row.current_quantity)):
        return False
    entry.payload = {**(entry.payload or {}), BARE_FILL_PROOF: {
        "entry_id": str(entry.id), "entry_coid": coid, "managed_id": str(row.id),
        "account_id": str(account.id), "strategy_id": str(strategy.id),
        "source": "new_bare_fill_writer"}}
    return True


def _aware_utc(value):
    # SQLite drops timezone information from canonical UTC model timestamps.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _rth(service):
    from project_mai_tai.oms import service as module
    return module._is_regular_market_session()


def require_current_episode(entry, intent, now):
    from project_mai_tai.fanout_segment_store import current_session_anchor
    from project_mai_tai.oms.route_owner import _utc_row_time
    if (intent is None or intent.strategy_id != entry.strategy_id
            or intent.broker_account_id != entry.broker_account_id or intent.symbol != entry.symbol):
        raise ValueError("wbattach1_episode_time_binding_unknown")
    anchor = _utc_row_time(entry.submitted_at) or _utc_row_time(intent.created_at)
    if anchor is None or anchor > now or anchor < current_session_anchor(now):
        raise ValueError("wbattach1_prior_or_unknown_session_requires_audited_recovery")


async def critical(service, account, symbol, reason, *, entry_id=""):
    service.logger.critical("[WBATTACH1-BLOCKED] account=%s symbol=%s entry=%s reason=%s sell=0",
                            account, symbol, entry_id, reason)
    def write(session):
        reference = str(entry_id)
        matches = [BrokerOrder.client_order_id == reference]
        try:
            matches.append(BrokerOrder.id == UUID(reference))
        except (ValueError, TypeError, AttributeError):
            pass
        entries = session.scalars(select(BrokerOrder).join(
            BrokerAccount, BrokerAccount.id == BrokerOrder.broker_account_id).where(
            or_(*matches), BrokerAccount.name == account, BrokerAccount.provider == "webull",
            BrokerOrder.symbol == symbol, BrokerOrder.side == "buy").limit(2)).all()
        entry = entries[0] if len(entries) == 1 else None
        canonical = str(entry.id) if entry is not None else "UNRESOLVED:" + reference
        aliases = {canonical, entry.client_order_id} if entry is not None else {canonical}
        old = session.scalar(select(SystemIncident).where(
            SystemIncident.payload["source"].as_string() == KEY,
            SystemIncident.payload["account"].as_string() == account,
            SystemIncident.payload["symbol"].as_string() == symbol,
            SystemIncident.payload["entry_id"].as_string().in_(aliases),
            SystemIncident.status == "open",
        ).order_by(SystemIncident.opened_at, SystemIncident.id).limit(1))
        identity = {"entry_id": canonical,
                    "entry_client_order_id": entry.client_order_id if entry is not None else "",
                    "entry_identity": "canonical" if entry is not None else "unknown"}
        if old is not None:
            # Adopt only exact account/symbol-bound legacy COID aliases. The
            # original incident id/opened_at and unrelated payload survive.
            old.payload = {**(old.payload or {}), **identity}
        waiting = "exit waiting for broker proof:" in reason.lower()
        if old is not None and waiting:
            old.title = f"Exit waiting for broker proof: {symbol}"[:255]
            old.payload = {**(old.payload or {}), "reason": reason, "sell": "BLOCKED"}
        if old is None:
            session.add(SystemIncident(service_name="oms-risk", severity="critical", status="open",
                title=(f"Exit waiting for broker proof: {symbol}" if waiting else
                       f"Webull attach/exit UNKNOWN: {symbol} on {account}")[:255],
                opened_at=datetime.now(UTC), payload={"source": KEY, "account": account,
                "symbol": symbol, **identity, "entry_reference": reference,
                "reason": reason, "sell": "BLOCKED"}))
    await service._run_db(write)


def _atr(service, session, account_name, symbol, coid, *, allow_closed=False, prepared=None):
    row = service.store.get_open_managed_position(session, broker_account_name=account_name, symbol=symbol)
    account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == account_name))
    strategy = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
    entry = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == coid).with_for_update())
    if row is None and allow_closed and entry is not None:
        row = session.scalar(select(OmsManagedPosition).where(
            OmsManagedPosition.entry_order_id == entry.id,
            OmsManagedPosition.entry_client_order_id == coid,
            OmsManagedPosition.broker_account_name == account_name, OmsManagedPosition.symbol == symbol,
            OmsManagedPosition.strategy_code == "schwab_1m_v2", OmsManagedPosition.status == "closed"))
    if (row is None or account is None or strategy is None or entry is None
            or row.strategy_code != strategy.code or row.entry_order_id != entry.id
            or row.entry_client_order_id != coid or entry.strategy_id != strategy.id
            or entry.broker_account_id != account.id or entry.symbol != symbol or entry.side != "buy"
            or not entry.broker_order_id or account.provider != "webull" or not account.is_active
            or not controlled_live_scope(strategy.code, strategy.execution_mode, account.name)
            or str((entry.payload or {}).get("native_oco_bracket", "")).lower() == "true"
            or str((entry.payload or {}).get("fanout_leg", "")).lower() != "webull"):
        raise ValueError("wbattach1_exact_bare_episode_unknown")
    call = _ATR_CALL.get()
    if call is not None and call.wb2 and call.service is service:
        from project_mai_tai.oms.wbexit2 import require_obligation_episode
        require_obligation_episode(service, session, entry, row, account, strategy,
                                   session.get(TradeIntent, entry.intent_id), datetime.now(UTC), call)
    else:
        require_current_episode(entry, session.get(TradeIntent, entry.intent_id), datetime.now(UTC))
    fills = session.scalars(select(Fill).where(Fill.order_id == entry.id)).all()
    if not fills or any(f.strategy_id != strategy.id or f.broker_account_id != account.id
            or f.symbol != symbol or f.side != "buy" or not f.broker_fill_id
            or not f.quantity.is_finite() or not f.price.is_finite() or f.quantity <= 0 or f.price <= 0
            for f in fills):
        raise ValueError("wbattach1_buy_fill_unknown")
    bought = sum((f.quantity for f in fills), Decimal(0))
    vp = service.store.get_virtual_position(session, strategy_id=strategy.id,
        broker_account_id=account.id, symbol=symbol)
    if (not entry.quantity.is_finite() or not 0 < bought <= entry.quantity or vp is None
            or not vp.quantity.is_finite() or not 0 <= vp.quantity <= bought
            or not 0 <= Decimal(row.current_quantity) <= bought
            or row.current_quantity == 0 and (not allow_closed or row.status != "closed" or vp.quantity != 0)):
        raise ValueError("wbattach1_owned_quantity_unknown")
    pending = session.scalars(select(BrokerOrder).where(
        BrokerOrder.strategy_id == strategy.id, BrokerOrder.broker_account_id == account.id,
        BrokerOrder.symbol == symbol, BrokerOrder.side == "sell",
        or_(BrokerOrder.status.is_(None), BrokerOrder.status.not_in(TERMINAL | {"aborted"})),
    )).all()
    prepared_seen = False
    for order in pending:
        parent = session.get(TradeIntent, order.intent_id)
        metadata = order.payload or {}
        if prepared is not None and order.client_order_id == prepared.client_order_id:
            if (order.status != "pending" or order.submitted_at is not None or order.broker_order_id
                    or order.quantity != prepared.quantity or order.order_type != prepared.order_type
                    or order.time_in_force != "day" or parent is None
                    or parent.strategy_id != strategy.id or parent.broker_account_id != account.id
                    or parent.symbol != symbol or parent.side != "sell" or parent.intent_type != "close"
                    or parent.reason != prepared.reason or parent.quantity != prepared.quantity
                    or metadata.get("oms_managed_row_id") != str(row.id)
                    or not metadata.get("managed_sell_prepared_at")
                    or metadata.get("managed_sell_prepared_at") != prepared.metadata.get("managed_sell_prepared_at")
                    or metadata != prepared.metadata
                    or (parent.payload or {}).get("metadata") != prepared.metadata
                    or (parent.payload or {}).get("metadata", {}).get("oms_managed_row_id") != str(row.id)
                    or session.scalar(select(Fill.id).where(Fill.order_id == order.id)) is not None
                    or session.scalar(select(BrokerOrderEvent.id).where(BrokerOrderEvent.order_id == order.id)) is not None):
                raise ValueError("wbattach1_prepared_order_not_this_unposted_close")
            prepared_seen = True
            continue
        if (metadata.get("wbattach1_child") == "true" and metadata.get("entry_order_id") == str(entry.id)
                and parent is not None and parent.reason == "WBATTACH1_PROTECTION"
                and parent.strategy_id == strategy.id and parent.broker_account_id == account.id
                and parent.symbol == symbol and parent.side == "sell" and parent.intent_type == "close"
                and (parent.payload or {}).get("metadata", {}).get("entry_order_id") == str(entry.id)):
            continue
        raise ValueError("wbattach1_existing_own_sell")
    if prepared is not None and not prepared_seen:
        raise ValueError("wbattach1_exact_prepared_order_missing")
    return entry, row, account, strategy, min(bought, vp.quantity, Decimal(row.current_quantity))


def _control_free(service, account, symbol):
    if (account, symbol) in service.__dict__.get("_wbattach_exit_owners", {}):
        return False
    if (account, symbol) in service.__dict__.get("_control_managed_exit_symbol_owners", {}):
        return False
    control = getattr(service, "_bot_control_runtime", None)
    return not (control is not None and control.stop_requested("schwab_1m_v2"))


def refused_before_creation(reports, request):
    """No reviewed HTTP response proves that neither original child exists.

    In particular 417 TRADE_PLACE_ORDER_REPEAT can identify an existing tree.
    Retain the compatibility reader for old journals, but never trust its flag.
    """
    return False


def confirmed_child_absence(report):
    """Existing confirm_exit_pair_terminal's exact ORDER_NOT_FOUND read contract."""
    return bool(report.origin == "broker" and report.event_type == "cancelled"
        and not report.broker_order_id and not report.broker_fill_id
        and report.quantity == 0 and report.filled_quantity == 0
        and report.metadata.get("eod_exit_pair_confirmation") == "true"
        and report.metadata.get("cancel_outcome") == "already_absent")


def terminal_pair(reports, base, quantity, now, *, symbol):
    expected = {WebullBrokerAdapter._combo_leg_coid(base, s) for s in ("T", "S")}
    ids = [r.broker_order_id for r in reports if r is not None and r.broker_order_id]
    return bool(len(reports) == 2 and all(r is not None for r in reports)
        and {r.client_order_id for r in reports} == expected
        and len(ids) == len(set(ids))
        and all(r.origin == "broker" and (r.broker_order_id or confirmed_child_absence(r))
            and r.symbol == symbol and r.side == "sell"
            and r.intent_type in {"close", "cancel"}
            and r.event_type in TERMINAL and r.reported_at.tzinfo is not None
            and r.reported_at <= now and r.filled_quantity.is_finite()
            and 0 <= r.filled_quantity <= quantity
            and (r.event_type != "filled" or r.filled_quantity == quantity)
            and (r.filled_quantity == 0 or r.broker_fill_id and r.fill_price is not None
                 and r.fill_price.is_finite() and r.fill_price > 0) for r in reports))


def report_payload(report):
    return {"event_type": report.event_type, "coid": report.client_order_id,
        "broker_order_id": report.broker_order_id, "broker_fill_id": report.broker_fill_id,
        "symbol": report.symbol, "side": report.side, "intent_type": report.intent_type,
        "quantity": str(report.quantity), "filled_quantity": str(report.filled_quantity),
        "fill_price": str(report.fill_price) if report.fill_price is not None else None,
        "reported_at": report.reported_at.isoformat(), "origin": report.origin,
        "metadata": dict(report.metadata)}


def saved_report(proof):
    return ExecutionReport(proof["event_type"], proof["coid"], broker_order_id=proof["broker_order_id"],
        broker_fill_id=proof["broker_fill_id"], symbol=proof["symbol"], side=proof["side"],
        intent_type=proof["intent_type"], quantity=Decimal(proof["quantity"]),
        filled_quantity=Decimal(proof["filled_quantity"]),
        fill_price=Decimal(proof["fill_price"]) if proof["fill_price"] is not None else None,
        reported_at=datetime.fromisoformat(proof["reported_at"]), origin=proof["origin"],
        metadata=dict(proof["metadata"]))


def orb_terminal_state_proven(state, account, symbol, now):
    if not state:
        return True
    quantity, base = Decimal(state["quantity"]), state["base"]
    if state.get("original_terminal_proof"):
        return terminal_pair([saved_report(p) for p in state["original_terminal_proof"]],
                             base, quantity, now, symbol=symbol)
    if state.get("phase") != "released":
        return False
    reports = [ExecutionReport(p["event_type"], coid, broker_order_id=p["broker_order_id"],
        broker_fill_id=p["broker_fill_id"], symbol=symbol, side="sell", intent_type="close",
        quantity=Decimal(p.get("quantity", str(quantity))),
        filled_quantity=Decimal(p["filled_quantity"]),
        fill_price=Decimal(p["fill_price"]) if p["fill_price"] is not None else None,
        reported_at=datetime.fromisoformat(p["reported_at"]), origin=p.get("origin", "broker"),
        metadata=dict(p["metadata"]))
        for coid, p in (state.get("terminal_children") or {}).items()]
    return terminal_pair(reports, base, quantity, now, symbol=symbol)


def orb_retry_terminal_proven(state, account, symbol, now):
    proofs = state.get("original_terminal_proof") or []
    return bool(proofs and orb_terminal_state_proven(state, account, symbol, now)
                and all(saved_report(proof).filled_quantity == 0 for proof in proofs))


async def reconcile_orb_rejection(service, entry_id, base):
    """Read ONLY the original children before considering another attach base."""
    from project_mai_tai.oms.orb_country import PROTECTION, record_protection_reports
    context = OrbAttachExit(service, entry_id)
    def read(session):
        entry = session.get(BrokerOrder, entry_id)
        context.validate_native_owner(session, entry)
        account = session.get(BrokerAccount, entry.broker_account_id)
        state = dict((entry.payload or {}).get(PROTECTION) or {})
        if state.get("base") != base:
            raise ValueError("wbattach1_orb_rejection_base_changed")
        return account.name, entry.symbol, state
    account, symbol, state = await service._run_db(read, commit=False)
    result = await service.broker_adapter.confirm_exit_pair_terminal(
        broker_account_name=account, symbol=symbol, base_client_order_id=base)
    reports = result.reports
    known = validated_child_reads(reports, base, Decimal(state["quantity"]), symbol, datetime.now(UTC))
    await record_protection_reports(service, entry_id, known, attach_context=context)
    if not terminal_pair(reports, base, Decimal(state["quantity"]), datetime.now(UTC), symbol=symbol):
        return
    def write(session):
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.id == entry_id).with_for_update())
        context.validate_native_owner(session, entry)
        current = dict((entry.payload or {}).get(PROTECTION) or {})
        if current.get("base") != base:
            raise ValueError("wbattach1_orb_rejection_owner_changed")
        from datetime import timedelta
        current["original_terminal_proof"] = [report_payload(r) for r in reports]
        current["retry_at"] = (datetime.now(UTC) + timedelta(
            seconds=min(2 ** int(current.get("attempt", 1)), 15))).isoformat()
        current.pop("rejected_no_wire", None)
        entry.payload = {**entry.payload, PROTECTION: current}
        intent = session.get(TradeIntent, UUID(current["intent_id"]))
        if intent is not None:
            intent.status = "cancelled"
    await service._run_db(write)


async def broker_ceiling(service, account, symbol, quantity, *, after=None, allow_flat=False):
    async with asyncio.timeout(2):
        rows = await service.broker_adapter.list_account_positions(account)
    sampled = monotonic()
    age_fn = getattr(service.broker_adapter, "positions_wire_read_age_seconds", None)
    age = age_fn(account) if callable(age_fn) else None
    if (isinstance(age, bool) or not isinstance(age, (float, int)) or not isfinite(age)
            or not 0 <= age <= 30 or after is not None and sampled - age <= after):
        raise ValueError("wbattach1_positions_wire_receipt_unknown_or_stale")
    matches = [p for p in rows if p.symbol == symbol]
    if allow_flat and (not matches or len(matches) == 1 and matches[0].broker_account_name == account
                       and matches[0].quantity.is_finite() and matches[0].quantity == 0):
        return Decimal(0)  # Only after the complete positions reader's fresh wire receipt check.
    if (len(matches) != 1 or matches[0].broker_account_name != account
            or not matches[0].quantity.is_finite() or matches[0].quantity <= 0):
        raise ValueError("wbattach1_broker_quantity_unknown")
    result = min(quantity, matches[0].quantity)
    if result <= 0 or result != result.to_integral_value():
        raise ValueError("wbattach1_owned_quantity_not_whole")
    return result


async def _atr_state(service, account, symbol, coid, mutate=None, *, allow_closed=False):
    call = _ATR_CALL.get()
    def unit(session):
        entry, row, acct, strategy, quantity = _atr(service, session, account, symbol, coid, allow_closed=allow_closed)
        payload = entry.payload or {}
        expected_proof = {
            "entry_id": str(entry.id), "entry_coid": entry.client_order_id,
            "managed_id": str(row.id), "account_id": str(acct.id), "strategy_id": str(strategy.id),
            "source": "new_bare_fill_writer",
        }
        if BARE_FILL_PROOF in payload and payload[BARE_FILL_PROOF] != expected_proof:
            raise ValueError("wbattach1_bare_fill_proof_corrupt")
        if KEY in payload and (not isinstance(payload[KEY], dict) or not payload[KEY]
                or not isinstance(payload[KEY].get("attempts"), list)):
            raise ValueError("wbattach1_journal_corrupt")
        state = dict(payload.get(KEY) or {})
        if not state:
            history_proven = payload.get(BARE_FILL_PROOF) == expected_proof
            state = {"entry_id": str(entry.id), "managed_id": str(row.id), "attempts": [],
                     "history_proven": history_proven,
                     "legacy_base": (entry.payload or {}).get("webull_protect_base_client_order_id", "")}
        if state.get("entry_id") != str(entry.id) or state.get("managed_id") != str(row.id):
            raise ValueError("wbattach1_journal_episode_changed")
        if mutate is not None:
            mutate(session, entry, row, acct, strategy, quantity, state)
            entry.payload = {**entry.payload, KEY: state}
        return state, str(row.id), str(entry.id), quantity, row.entry_price
    work = service._run_db(unit, commit=mutate is not None)
    if call is not None and call.wb2:
        from project_mai_tai.oms.wbexit2 import joined
        result = await joined(service, call, work)
        if not call.consumed:
            _current_atr_call(service, call.managed_id)
        return result
    return await work


async def _atr_rejection_terminal_read(service, account, symbol, coid, base, quantity):
    """A new retry identity requires original-child proof, never HTTP provenance."""
    result = await service.broker_adapter.confirm_exit_pair_terminal(
        broker_account_name=account, symbol=symbol, base_client_order_id=base)
    reports = result.reports
    known = validated_child_reads(reports, base, quantity, symbol, datetime.now(UTC))
    state, _, _, _, _ = await _atr_state(service, account, symbol, coid, allow_closed=True)
    attempt = next(a for a in state["attempts"] if a["base"] == base)
    if any(r.filled_quantity > 0 for r in known):
        await _record_atr_children(service, account, symbol, coid, attempt, known)
    if not terminal_pair(reports, base, quantity, datetime.now(UTC), symbol=symbol):
        return False
    if await _atr_resolved_by_child(service, state, account, symbol, coid, base, reports):
        return False  # No further attach; original owner reconciles the closed episode.

    def released(session, entry, managed, acct, strategy, owned, journal):
        if journal.get("exit_phase") or journal["attempts"][-1]["base"] != base:
            raise ValueError("wbattach1_refusal_read_owner_changed")
        journal["attempts"] = [{**a, "phase": "released",
            "terminal_proof": [report_payload(r) for r in reports]} if a["base"] == base else a
            for a in journal["attempts"]]
        parent = session.get(TradeIntent, UUID(attempt["intent_id"]))
        if parent is not None:
            parent.status = "cancelled"
    await _atr_state(service, account, symbol, coid, released)
    return not any(r.filled_quantity > 0 for r in reports)


async def attach_atr(service, *, broker_account_name, symbol, quantity, entry_price,
                     strategy_code, entry_client_order_id):
    if strategy_code != "schwab_1m_v2" or not _rth(service):
        return False
    account, coid = broker_account_name, entry_client_order_id
    try:
        target = entry_price * (1 + float(service.settings.oms_v2_cw_target_pct) / 100)
        protect = entry_price * (1 - float(service.settings.oms_v2_cw_hard_stop_pct) / 100)
        if not all(isfinite(p) and p > 0 for p in (entry_price, target, protect)) or not protect < entry_price < target:
            raise ValueError("wbattach1_protection_geometry_unknown")
        attempts = max(1, int(getattr(service.settings, "oms_webull_protect_attempts", 5)))
        interval = max(0.0, float(getattr(service.settings, "oms_webull_protect_interval_seconds", 2.0)))
        backoff = max(1.0, float(getattr(service.settings, "oms_webull_protect_backoff_multiplier", 2.0)))
        max_interval = max(0.0, float(getattr(service.settings, "oms_webull_protect_max_interval_seconds", 15.0)))
        for number in range(1, attempts + 1):
            if not _rth(service) or not _control_free(service, account, symbol):
                return False
            state, _, _, owned, proven_price = await _atr_state(service, account, symbol, coid, allow_closed=True)
            if stored_price(entry_price) != stored_price(proven_price):
                raise ValueError("wbattach1_entry_price_binding_changed")
            if state.get("exit_phase") or state.get("wbexit2_owner_id"):
                return False  # An existing owner, not another attach, owns the episode now.
            if state.get("exit_reason") in SOFTWARE_EXIT_REASONS:
                return False  # A proof-waiting real exit must not re-arm a second native writer.
            if state.get("unsent_handoffs"):
                return await request_atr_exit(service, account, symbol, coid,
                    "original_exit_proven_unsent_requires_fresh_owner")
            # ACK uncertainty is not an exit signal. Keep the original address
            # reserved for reconciliation, never cancel/re-place it from attach.
            if any(a["phase"] in {"submitting", "unknown"} for a in state["attempts"]):
                await critical(service, account, symbol, "attach_ack_unknown_requires_child_proof",
                               entry_id=coid)
                return False
            if len(state["attempts"]) >= attempts:
                if any(a["phase"] == "accepted" for a in state["attempts"]):
                    return True
                return await request_atr_exit(service, account, symbol, coid,
                    "attach_refused_attempts_exhausted")
            unplaceable = await service._webull_protect_unplaceable_reason(
                broker_account_name=account, symbol=symbol, target=target, protect=protect)
            pending = [a for a in state["attempts"] if a["phase"] != "released"]
            if unplaceable or pending or state.get("legacy_base"):
                if pending and pending[-1]["phase"] == "accepted" and not unplaceable:
                    return True
                return await request_atr_exit(service, account, symbol, coid,
                    unplaceable or "original_attach_requires_reconciliation")
            owned = await broker_ceiling(service, account, symbol, min(Decimal(quantity), owned))
            base = f"{strategy_code}-{symbol}-protect-{uuid4().hex[:12]}"
            def prepare(session, entry, row, acct, strategy, current, journal):
                if (current < owned or journal.get("exit_phase") or journal.get("wbexit2_owner_id")
                        or journal["attempts"] != state["attempts"]):
                    raise ValueError("wbattach1_attach_preimage_changed")
                event = TradeIntentEvent(source_service="oms-risk", payload=TradeIntentPayload(
                    strategy_code=strategy.code, broker_account_name=acct.name, symbol=symbol,
                    side="sell", intent_type="close", quantity=owned, reason="WBATTACH1_PROTECTION",
                    metadata={"entry_order_id": str(entry.id), "managed_row_id": str(row.id),
                              "webull_protect_base_client_order_id": base,
                              "webull_protect_target_client_order_id": WebullBrokerAdapter._combo_leg_coid(base, "T"),
                              "webull_protect_stop_client_order_id": WebullBrokerAdapter._combo_leg_coid(base, "S")}))
                intent = service.store.create_trade_intent(session, strategy=strategy, broker_account=acct, event=event)
                journal["attempts"] = [*journal["attempts"], {"base": base, "quantity": str(owned),
                    "intent_id": str(intent.id), "phase": "submitting", "cancel_requested": False}]
                entry.payload = {**entry.payload, "webull_protect_base_client_order_id": base,
                                 service._WEBULL_PROTECT_STATE_KEY: "submitting"}
            await _atr_state(service, account, symbol, coid, prepare)
            service._webull_protect_base[(account, symbol)] = base
            await service._bind_webull_protect_memory(account, symbol, base, coid)
            if not _rth(service) or not _control_free(service, account, symbol):
                raise ValueError("wbattach1_owner_changed_before_attach")
            checked, _, _, current, _ = await _atr_state(service, account, symbol, coid)
            if (current < owned or checked["attempts"][-1]["base"] != base
                    or checked.get("exit_phase") or not _rth(service)
                    or not _control_free(service, account, symbol)):
                raise ValueError("wbattach1_owner_or_quantity_changed_before_attach")
            request = OrderRequest(base, account, strategy_code, symbol, "sell", "close", owned,
                "webull attach protection after bare resting fill", {"webull_exit_only_pair": "true",
                "bracket_target_price": f"{target:.4f}", "bracket_stop_price": f"{protect:.4f}",
                "source": "oms_v2_webull_protect", "market_session": "RTH"}, "limit", "day")
            try:
                reports = await service.broker_adapter.submit_order(request)
            except Exception:
                reports = []
            accepted = bool(len(reports) == 1 and reports[0].origin == "broker"
                and reports[0].event_type == "accepted" and reports[0].broker_order_id
                and reports[0].client_order_id == base and reports[0].symbol == symbol
                and reports[0].side == "sell" and reports[0].filled_quantity == 0)
            # A rejected response records cause, not proof of absent children.
            rejected = bool(len(reports) == 1 and reports[0].origin == "broker"
                and reports[0].event_type == "rejected"
                and reports[0].client_order_id == base and reports[0].symbol == symbol
                and reports[0].side == "sell" and reports[0].filled_quantity == 0)
            def result(session, entry, row, acct, strategy, current, journal):
                last = dict(journal["attempts"][-1])
                if last["base"] != base:
                    raise ValueError("wbattach1_attach_base_changed")
                last["phase"] = "accepted" if accepted else "unknown"
                if rejected:
                    last["rejection_response"] = report_payload(reports[0])
                intent = session.get(TradeIntent, UUID(last["intent_id"]))
                if intent is not None:
                    intent.status = "accepted" if accepted else "pending"
                    if accepted:
                        last["combo_id"] = reports[0].broker_order_id
                        intent.payload = {**intent.payload, "metadata": {
                            **intent.payload.get("metadata", {}),
                            "webull_protect_combo_id": reports[0].broker_order_id}}
                journal["attempts"] = [*journal["attempts"][:-1], last]
            await _atr_state(service, account, symbol, coid, result)
            if accepted:
                await service._persist_webull_protect_base(account, symbol, base, entry_client_order_id=coid)
                service.logger.info("[WEBULL-PROTECT-ATTACHED] %s %s qty=%s entry=%s target=%s stop=%s "
                    "session=RTH attempt=%s", symbol, account, owned, entry_price, target, protect, number)
                return True
            if not rejected:
                await critical(service, account, symbol, "attach_ack_unknown_requires_child_proof",
                               entry_id=coid)
                return False
            if not await _atr_rejection_terminal_read(service, account, symbol, coid, base, owned):
                return await request_atr_exit(service, account, symbol, coid, "rejected_original_tree_requires_owner")
            if number < attempts and interval:
                await asyncio.sleep(min(interval * backoff ** (number - 1), max_interval))
        return await request_atr_exit(service, account, symbol, coid, "attach_refused_attempts_exhausted")
    except Exception as exc:
        await critical(service, account, symbol, str(exc), entry_id=coid)
        return False


async def retry_atr_proven_unsent_exit(service, account, symbol, managed_id):
    """A new owner may retry an exact aborted close, never an uncertain wire."""
    def read(session):
        row = session.get(OmsManagedPosition, UUID(managed_id))
        if (row is None or row.status != "open" or row.broker_account_name != account
                or row.symbol != symbol or row.strategy_code != "schwab_1m_v2"):
            return None
        entry, row, _, _, _ = _atr(service, session, account, symbol, row.entry_client_order_id)
        state = (entry.payload or {}).get(KEY) or {}
        receipts = state.get("unsent_handoffs", [])
        if (state.get("exit_phase") or not receipts or not state.get("attempts")
                or any(a.get("phase") != "released" for a in state["attempts"])):
            return None
        receipt = receipts[-1]
        if receipt.get("outcome") != "PROVEN_NOT_SENT" or not receipt.get("call_id"):
            return None
        order = session.scalar(select(BrokerOrder).where(
            BrokerOrder.client_order_id == receipt.get("client_order_id")))
        event = session.get(BrokerOrderEvent, UUID(receipt["abort_event_id"]))
        intent = session.get(TradeIntent, order.intent_id) if order is not None else None
        metadata = order.payload or {} if order is not None else {}
        try:
            prepared_at = datetime.fromisoformat(metadata["managed_sell_prepared_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if (order is None or event is None or intent is None
                or order.strategy_id != entry.strategy_id or order.broker_account_id != entry.broker_account_id
                or order.symbol != symbol or order.side != "sell" or order.status != "aborted"
                or order.submitted_at is not None or order.broker_order_id
                or order.order_type not in {"market", "limit"} or order.time_in_force != "day"
                or intent.strategy_id != entry.strategy_id or intent.broker_account_id != entry.broker_account_id
                or intent.symbol != symbol or intent.side != "sell" or intent.intent_type != "close"
                or intent.status != "aborted" or intent.quantity != order.quantity
                or intent.reason not in SOFTWARE_EXIT_REASONS | {REASON}
                or receipt.get("reason") != intent.reason
                or not receipt.get("exit_reason") or receipt["exit_reason"] != state.get("exit_reason")
                or intent.reason in SOFTWARE_EXIT_REASONS and state.get("exit_reason") != intent.reason
                or metadata.get("oms_managed_row_id") != managed_id
                or metadata.get("entry_order_id") != str(entry.id)
                or prepared_at.tzinfo is None
                or not prepared_at <= _aware_utc(event.event_at) <= datetime.now(UTC)
                or (intent.payload or {}).get("metadata") != metadata
                or event.order_id != order.id or event.event_source != "client" or event.event_type != "aborted"
                or (event.payload or {}).get("proven_not_sent") is not True
                or (event.payload or {}).get("client_order_id") != order.client_order_id
                or (event.payload or {}).get("metadata") != metadata
                or session.scalar(select(Fill.id).where(Fill.order_id == order.id)) is not None
                or session.scalar(select(BrokerOrderEvent.id).where(BrokerOrderEvent.order_id == order.id,
                    BrokerOrderEvent.id != event.id)) is not None):
            return None
        return service._hydrate_v2_position(row), float(row.entry_price), intent.reason

    bound = await service._run_db(read, commit=False)
    if bound is None:
        return None
    position, entry_price, reason = bound
    if reason == REASON and not _rth(service):
        return None  # Canonical attach risk-off has never authorized an EH market close.
    bid = 0.0
    context = None
    if not _rth(service):
        quote = service.__dict__.get("_latest_quotes_by_symbol", {}).get(symbol) or {}
        at = quote.get("received_at")
        bid = float(quote.get("bid") or 0)
        if (not service._market_is_fillable() or not isinstance(at, datetime) or at.tzinfo is None
                or not 0 <= (datetime.now(UTC) - at).total_seconds() <= 2.5
                or not isfinite(bid) or bid <= 0):
            return "refused"
        context = {"wbattach1_unsent_retry_quote_at": at.isoformat()}
    # The regular emitter reclaims ownership, re-reads every original child and
    # broker quantity, and prepares a NEW identity. It also enforces EH pricing.
    return await service._emit_v2_exit_on_loop(account, symbol, position, entry_price,
        kind="HARD", reference_price=bid or entry_price, reason=reason,
        bid=bid, close_on_fill=True, expected_managed_row_id=managed_id,
        confirmation_context=context)


async def request_atr_exit(service, account, symbol, coid, reason):
    def mark(session, entry, row, acct, strategy, quantity, state):
        if not state.get("exit_phase"):
            state["exit_reason"] = reason
    state, row_id, _, _, _ = await _atr_state(service, account, symbol, coid, mark, allow_closed=True)
    if state.get("exit_phase") or not _rth(service):
        return False
    snapshot = await service._run_db(lambda s: service._read_v2_managed_snapshot(s, account, symbol, True), commit=False)
    if snapshot is None:
        def closed(session):
            _, row, _, _, owned = _atr(service, session, account, symbol, coid, allow_closed=True)
            if row.status != "closed" or owned != 0:
                raise ValueError("wbattach1_closed_episode_unknown")
            return service._hydrate_v2_position(row), float(row.entry_price)
        position, entry_price = await service._run_db(closed, commit=False)
    elif snapshot.managed_row_id != row_id:
        raise ValueError("wbattach1_managed_episode_changed")
    else:
        position, entry_price = service._hydrate_v2_position(snapshot), snapshot.entry_price
    await service._emit_v2_exit_on_loop(account, symbol, position, entry_price,
        kind="WBATTACH1", reference_price=entry_price, reason=REASON,
        bid=0, close_on_fill=True, expected_managed_row_id=row_id)
    return False  # A close handoff is never reported as a successful attach.


async def _record_atr_children(service, account, symbol, coid, attempt, reports):
    if type(reports) in (list, tuple) and all(
        isinstance(report, ExecutionReport) and type(report.filled_quantity) is Decimal
        and report.filled_quantity.is_finite() and report.filled_quantity == 0
        for report in reports
    ):
        def validate(session):
            entry, _, acct, strategy, _ = _atr(service, session, account, symbol, coid, allow_closed=True)
            intent = session.get(TradeIntent, UUID(attempt["intent_id"]))
            if (intent is None or intent.strategy_id != strategy.id or intent.broker_account_id != acct.id
                    or intent.symbol != symbol or intent.side != "sell" or intent.intent_type != "close"
                    or (intent.payload or {}).get("metadata", {}).get("entry_order_id") != str(entry.id)):
                raise ValueError("wbattach1_protection_intent_changed")
        # No execution reaches the mixed async ledger/native-guard writer.
        await service._run_db(validate)
        return
    with service.session_factory() as session:
        entry, row, acct, strategy, owned = _atr(service, session, account, symbol, coid, allow_closed=True)
        intent = session.get(TradeIntent, UUID(attempt["intent_id"]))
        if (intent is None or intent.strategy_id != strategy.id or intent.broker_account_id != acct.id
                or intent.symbol != symbol or intent.side != "sell" or intent.intent_type != "close"
                or (intent.payload or {}).get("metadata", {}).get("entry_order_id") != str(entry.id)):
            raise ValueError("wbattach1_protection_intent_changed")
        events = []
        for report in reports:
            if report.filled_quantity == 0:
                continue
            child = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == report.client_order_id))
            if child is not None and (child.intent_id != intent.id or child.strategy_id != strategy.id
                    or child.broker_account_id != acct.id or child.symbol != symbol or child.side != "sell"
                    or child.broker_order_id not in {None, report.broker_order_id}):
                raise ValueError("wbattach1_child_binding_changed")
            fills = session.scalars(select(Fill).where(Fill.order_id == child.id)).all() if child else []
            prior = sum((f.quantity for f in fills), Decimal(0))
            cash = sum((f.quantity * f.price for f in fills), Decimal(0))
            delta = report.filled_quantity - prior
            delta_cash = report.filled_quantity * report.fill_price - cash
            if (delta < 0 or delta > owned or delta > 0 and delta_cash <= 0
                    or delta == 0 and abs(delta_cash) > prior * Decimal("0.000000005")):
                raise ValueError("wbattach1_child_cumulative_execution_inconsistent")
            # Reuse the store's terminal-execution contract without inventing a
            # different broker status or discarding the cancelled remainder.
            writer = replace(report, quantity=Decimal(attempt["quantity"]),
                fill_price=delta_cash / delta if delta else report.fill_price,
                metadata={**report.metadata, "wbattach1_cumulative_average": str(report.fill_price),
                          "wbattach1_cumulative_quantity": str(report.filled_quantity),
                          "wbattach1_broker_report_quantity": str(report.quantity),
                          **({"webull_terminal_with_partial_fill": report.event_type}
                             if report.event_type in {"cancelled", "rejected"} else {})})
            request = OrderRequest(report.client_order_id, account, strategy.code, symbol, "sell", "close",
                Decimal(attempt["quantity"]), "WBATTACH1_CHILD_EXECUTION",
                {"entry_order_id": str(entry.id), "managed_row_id": str(row.id), "wbattach1_child": "true"})
            event = TradeIntentEvent(source_service="oms-risk", payload=TradeIntentPayload(
                strategy_code=strategy.code, broker_account_name=account, symbol=symbol, side="sell",
                intent_type="close", quantity=request.quantity, reason=request.reason, metadata=request.metadata))
            events.extend(await service._record_order_reports(session=session, intent=intent,
                strategy_id=strategy.id, broker_account_id=acct.id, intent_event=event,
                request=request, reports=[writer]))
            owned -= delta
        session.commit()
    for event in events:
        await service._publish_order_event(event)


def validated_child_reads(reports, base, quantity, symbol, now):
    expected = {WebullBrokerAdapter._combo_leg_coid(base, suffix) for suffix in ("T", "S")}
    known = [r for r in reports if r is not None]
    if len({r.client_order_id for r in known}) != len(known):
        raise ValueError("wbattach1_duplicate_child_read")
    for r in known:
        if (r.client_order_id not in expected or r.symbol != symbol or r.side != "sell"
                or r.intent_type not in {"close", "cancel"}
                or r.reported_at.tzinfo is None or r.reported_at > now
                or not r.filled_quantity.is_finite() or not 0 <= r.filled_quantity <= quantity
                or r.filled_quantity > 0 and (r.origin != "broker" or not r.broker_order_id
                    or not r.broker_fill_id or r.fill_price is None or not r.fill_price.is_finite()
                    or r.fill_price <= 0)):
            raise ValueError("wbattach1_child_read_identity_or_execution_unknown")
    return known


async def _atr_resolved_by_child(service, state, account, symbol, coid, base, reports):
    def write(session):
        row = session.get(OmsManagedPosition, UUID(state["managed_id"]))
        entry = session.get(BrokerOrder, UUID(state["entry_id"]))
        if (row is None or entry is None or row.entry_order_id != entry.id
                or row.entry_client_order_id != coid or row.broker_account_name != account
                or row.symbol != symbol or row.strategy_code != "schwab_1m_v2"):
            raise ValueError("wbattach1_closed_child_episode_changed")
        if row.status != "closed" or row.current_quantity != 0:
            return False
        vp = service.store.get_virtual_position(session, strategy_id=entry.strategy_id,
            broker_account_id=entry.broker_account_id, symbol=symbol)
        if vp is None or vp.quantity != 0:
            raise ValueError("wbattach1_closed_child_balance_unknown")
        journal = dict(entry.payload[KEY])
        journal["attempts"] = [{**a, "phase": "released", "terminal_proof": [report_payload(r) for r in reports]}
                               if a["base"] == base else a
                               for a in journal["attempts"]]
        journal["exit_phase"] = "resolved_by_child_fill"
        entry.payload = {**entry.payload, KEY: journal}
        attempt = next(a for a in journal["attempts"] if a["base"] == base)
        intent = session.get(TradeIntent, UUID(attempt["intent_id"]))
        if intent is not None:
            intent.status = "filled"
        return True
    return await service._run_db(write)


def settle_cached_protection_intent(service, session, intent):
    """Change only lifecycle bookkeeping after exact native and ledger proof."""
    if (intent.reason != "WBATTACH1_PROTECTION" or intent.status not in {"accepted", "pending"}
            or intent.side != "sell" or intent.intent_type != "close"):
        return False
    metadata = (intent.payload or {}).get("metadata", {})
    entry = session.get(BrokerOrder, UUID(metadata["entry_order_id"]))
    row = session.get(OmsManagedPosition, UUID(metadata["managed_row_id"]))
    account = session.get(BrokerAccount, intent.broker_account_id)
    strategy = session.get(Strategy, intent.strategy_id)
    if (entry is None or row is None or account is None or account.provider != "webull"
            or strategy is None or strategy.code != "schwab_1m_v2"
            or entry.strategy_id != intent.strategy_id or entry.broker_account_id != intent.broker_account_id
            or entry.symbol != intent.symbol or entry.side != "buy" or not entry.broker_order_id
            or row.entry_order_id != entry.id or row.entry_client_order_id != entry.client_order_id
            or row.broker_account_name != account.name or row.strategy_code != strategy.code
            or row.symbol != intent.symbol):
        return False
    journal = (entry.payload or {}).get(KEY, {})
    if journal.get("entry_id") != str(entry.id) or journal.get("managed_id") != str(row.id):
        return False
    attempts = [a for a in journal.get("attempts", []) if a.get("intent_id") == str(intent.id)]
    if len(attempts) != 1:
        return False
    attempt = attempts[0]
    base = metadata.get("webull_protect_base_client_order_id")
    coids = [WebullBrokerAdapter._combo_leg_coid(base, suffix) for suffix in ("T", "S")] if base else []
    quantity = Decimal(attempt["quantity"])
    if (not base or attempt.get("base") != base or not quantity.is_finite() or quantity <= 0
            or intent.quantity != quantity or [metadata.get("webull_protect_target_client_order_id"),
                metadata.get("webull_protect_stop_client_order_id")] != coids
            or not attempt.get("combo_id") or attempt["combo_id"] != metadata.get("webull_protect_combo_id")
            or len([a for a in journal["attempts"] if a.get("base") == base]) != 1):
        return False
    reader = getattr(service.broker_adapter, "cached_attach_pair_terminal", None)
    proof = reader(account.name, intent.symbol, base, quantity) if reader else None
    if (not isinstance(proof, dict) or proof.get("source") != "complete_cached_today_list"
            or proof.get("terminal_observation_contract") != "WBATTACH-L9-own-row-after-winner-v1"
            or not proof.get("scan_generation") or not proof.get("account_id")
            or (proof.get("account"), proof.get("symbol"), proof.get("base")) != (account.name, intent.symbol, base)):
        return False
    children = proof.get("children", [])
    if len(children) != 2 or {c.get("coid") for c in children} != set(coids):
        return False
    if len({c.get("broker_order_id") for c in children}) != 2:
        return False
    for child in children:
        evidence = child.get("evidence", {})
        acquired = datetime.fromisoformat(evidence["acquired_at"])
        filled = Decimal(child["filled_quantity"])
        if (child.get("status") not in {"filled", "cancelled", "rejected"} or not child.get("broker_order_id")
                or not filled.is_finite() or not 0 <= filled <= quantity
                or child["status"] == "filled" and filled != quantity
                or evidence.get("client_order_id") != child["coid"]
                or evidence.get("account_id") != proof["account_id"]
                or evidence.get("scan_generation") != proof["scan_generation"]
                or evidence.get("failed") is not False
                or evidence.get("complete") is not True or evidence.get("truncated") is not False
                or child.get("terminal_observed_at") != evidence["acquired_at"]
                or acquired.tzinfo is None or acquired > datetime.now(UTC)):
            return False
        if filled == 0 and session.scalar(select(Fill.id).join(BrokerOrder, BrokerOrder.id == Fill.order_id).where(
                BrokerOrder.broker_order_id == child["broker_order_id"],
                BrokerOrder.broker_account_id == entry.broker_account_id).limit(1)) is not None:
            return False  # A zero native receipt must not contradict an existing execution.
    total = sum((Decimal(c["filled_quantity"]) for c in children), Decimal(0))
    if total == 0:
        status = "cancelled"
    elif total == quantity:
        winners = [c for c in children if Decimal(c["filled_quantity"]) > 0]
        if (len(winners) != 1 or winners[0]["status"] != "filled"
                or row.status != "closed" or row.current_quantity != 0):
            return False
        winner = winners[0]
        price = Decimal(winner["price"])
        filled_at = datetime.fromisoformat(winner["filled_at"])
        if (not price.is_finite() or price <= 0 or filled_at.tzinfo is None
                or filled_at > datetime.now(UTC)
                or any(datetime.fromisoformat(c["evidence"]["acquired_at"]) <= filled_at for c in children)):
            return False
        # Inspect the existing economic writer's rows; never record/reparent a fill here.
        orders = session.scalars(select(BrokerOrder).where(
            BrokerOrder.broker_order_id == winner["broker_order_id"],
            BrokerOrder.intent_id == entry.intent_id, BrokerOrder.strategy_id == entry.strategy_id,
            BrokerOrder.broker_account_id == entry.broker_account_id,
            BrokerOrder.symbol == intent.symbol, BrokerOrder.side == "sell").limit(2)).all()
        if len(orders) != 1 or (orders[0].payload or {}).get("source") != "native_oco_child_leg":
            return False
        fills = session.scalars(select(Fill).where(Fill.order_id == orders[0].id).limit(2)).all()
        if (len(fills) != 1 or fills[0].broker_account_id != entry.broker_account_id
                or fills[0].strategy_id != entry.strategy_id or fills[0].symbol != intent.symbol
                or fills[0].side != "sell" or fills[0].quantity != quantity
                or fills[0].price != price.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
                or (fills[0].filled_at.astimezone(UTC) if fills[0].filled_at.tzinfo is not None
                    else fills[0].filled_at.replace(tzinfo=UTC)) != filled_at.astimezone(UTC)
                or not str(fills[0].broker_fill_id or "").startswith(winner["broker_order_id"] + ":")):
            return False
        status = "filled"
    else:
        return False  # Partial execution or overfill cannot clear the lifecycle alarm.
    intent.status = status
    intent.payload = {**intent.payload, "metadata": {**metadata,
        "wbattach1_terminal_bookkeeping": json.dumps({"status": status, "combo_id": attempt["combo_id"],
            "proof": proof, "recorded_at": datetime.now(UTC).isoformat()}, sort_keys=True)}}
    return True


_PROTECTION_BATCH_SIZE = 8
_PROTECTION_BATCH_SECONDS = 0.025


@dataclass
class _ProtectionSweep:
    lock: object
    cursors: dict
    refreshes: dict = field(default_factory=dict)


def _refresh_protection_scan_page(service, session, intents, state, scope, deadline):
    """One attempt per physical account across the entire frozen-ceiling sweep.

    Claims survive commit failure/cancel and are spent before I/O, not per row.
    Copy bounded request identities then release the read transaction before HTTP.
    """
    get_scope = getattr(service.broker_adapter, "attach_terminal_scan_scope", None)
    refresh = getattr(service.broker_adapter, "refresh_attach_terminal_scan", None)
    if not callable(get_scope) or not callable(refresh):
        return
    requests = []
    for intent in intents:
        try:
            metadata = (intent.payload or {}).get("metadata", {})
            base = metadata.get("webull_protect_base_client_order_id")
            if not isinstance(base, str) or not base or not intent.quantity.is_finite() or intent.quantity <= 0:
                continue
            account = session.get(BrokerAccount, intent.broker_account_id)
            if account is not None and account.provider == "webull":
                requests.append((account.name, intent.symbol, base, intent.quantity))
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            continue
    claimed = state.refreshes[scope][1]
    attempted = False
    for account, symbol, base, quantity in requests:
        if attempted and monotonic() >= deadline:
            break
        try:
            physical = get_scope(account)
            if physical is None or physical in claimed:
                continue
            if service.broker_adapter.cached_attach_pair_terminal(account, symbol, base, quantity) is not None:
                continue
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            continue  # Malformed scope/evidence is UNKNOWN, not refresh authority.
        claimed.add(physical)
        if not attempted:
            session.rollback()  # No mutations yet; do not hold a DB transaction across HTTP.
        attempted = True
        try:
            refresh(account)
        except Exception:
            pass  # Budget denial,429,error/incomplete is UNKNOWN; never retry this sweep.


async def reconcile_cached_protection_intents(service, account_names):
    if not callable(getattr(service.broker_adapter, "cached_attach_pair_terminal", None)):
        return
    scope = None if account_names is None else tuple(sorted(set(account_names)))
    if scope == ():
        return
    state = service.__dict__.setdefault("_wbattach_bookkeeping_sweep", _ProtectionSweep(Lock(), {}))

    def unit():
        # The worker owns the lock through commit, even if its awaiting task is cancelled.
        if not state.lock.acquire(blocking=False):
            return
        try:
            with service.session_factory() as session:
                query = select(TradeIntent).join(BrokerAccount, BrokerAccount.id == TradeIntent.broker_account_id).where(
                    TradeIntent.reason == "WBATTACH1_PROTECTION", TradeIntent.status.in_(["accepted", "pending"]),
                    BrokerAccount.provider == "webull")
                if scope is not None:
                    query = query.where(BrokerAccount.name.in_(scope))
                after, ceiling = state.cursors.get(scope, (None, None))
                if ceiling is None and scope in state.refreshes:
                    ceiling = state.refreshes[scope][0]  # Failed commit retains its sweep/claims.
                if ceiling is None:
                    ceiling = session.scalar(query.with_only_columns(TradeIntent.id).order_by(
                        TradeIntent.id.desc()).limit(1))
                if ceiling is None:
                    state.refreshes.pop(scope, None)
                    return
                state.refreshes.setdefault(scope, (ceiling, set()))
                page = query.where(TradeIntent.id <= ceiling)
                if after is not None:
                    page = page.where(TradeIntent.id > after)
                intents = session.scalars(page.order_by(TradeIntent.id).limit(_PROTECTION_BATCH_SIZE)).all()
                deadline = monotonic() + _PROTECTION_BATCH_SECONDS
                _refresh_protection_scan_page(service, session, intents, state, scope, deadline)
                visited = 0
                for intent in intents:
                    if visited and monotonic() >= deadline:
                        break
                    try:
                        settle_cached_protection_intent(service, session, intent)
                    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
                        pass  # UNKNOWN/legacy stays eligible on the next sweep.
                    after = intent.id
                    visited += 1
                session.commit()
                # Never publish progress from a rolled-back transaction. A frozen upper
                # bound lets each sweep finish rather than chasing newly appended IDs.
                complete = visited == len(intents) and (len(intents) < _PROTECTION_BATCH_SIZE or after == ceiling)
                state.cursors[scope] = (None, None) if complete else (after, ceiling)
                if complete:
                    state.refreshes.pop(scope, None)
        finally:
            state.lock.release()

    await asyncio.to_thread(unit)


def _cancel_read_summary(reports):
    """Bounded diagnostics only; malformed receipts remain explicitly unknown."""
    if not isinstance(reports, (list, tuple)):
        return "UNKNOWN"
    values = []
    for report in reports[:2]:
        if not isinstance(report, ExecutionReport):
            values.append("UNKNOWN")
            continue
        metadata = report.metadata if isinstance(report.metadata, dict) else {}
        values.append(f"{report.client_order_id}/{report.broker_order_id or 'UNKNOWN'}/"
                      f"{report.event_type}/{report.origin}/{metadata.get('cancel_outcome', 'UNKNOWN')}")
    return f"count={len(reports)}:" + (",".join(values) or "UNKNOWN")


async def strict_atr_handoff(service, row):
    """Called only INSIDE the real managed owner; it never wires the close itself."""
    account, symbol, coid = row.broker_account_name, row.symbol, row.entry_client_order_id
    candidate = _ATR_CALL.get()
    if (candidate is not None and candidate.service is service
            and candidate.task is asyncio.current_task() and (candidate.reason in SOFTWARE_EXIT_REASONS or candidate.wb2)
            and candidate.account == account and candidate.symbol == symbol and not candidate.managed_id):
        candidate.managed_id = str(row.id)  # Exact row read by this existing owner invocation.
    call = _current_atr_call(service, row.id)
    if call.wb2:
        from project_mai_tai.oms.wbexit2 import handoff
        return await handoff(service, row, call)
    if call.account != account or call.symbol != symbol or call.entry_coid:
        raise ValueError("wbattach1_handoff_call_binding_changed")
    if call.reason == REASON and not _rth(service):
        raise ValueError("wbattach1_outside_rth")
    key = (account, symbol)
    owners = service.__dict__.setdefault("_wbattach_exit_owners", {})
    if key in owners:
        raise ValueError("wbattach1_existing_exit_owner")
    owners[key], call.owner_key = call, key
    if call.reason in SOFTWARE_EXIT_REASONS:
        def triggered(session, entry, managed, acct, strategy, owned, journal):
            if journal.get("exit_phase") or any(a["phase"] == "submitting" for a in journal["attempts"]):
                raise ValueError("wbattach1_protection_dispatch_or_exit_inflight")
            journal["exit_reason"] = call.reason
        await _atr_state(service, account, symbol, coid, triggered, allow_closed=True)
    state, _, entry_id, _, _ = await _atr_state(service, account, symbol, coid, allow_closed=True)
    if (not state.get("exit_reason") or state.get("exit_phase") or state.get("legacy_base")
            or state.get("history_proven") is not True):
        raise ValueError("wbattach1_original_tree_or_close_unknown")
    if state.get("wbexit2_owner_id"):
        raise ValueError("wbexit2_retained_owner_requires_its_own_enabled_route")
    terminal_at = monotonic()
    proof_base = None
    if call.reason in SOFTWARE_EXIT_REASONS and state["attempts"]:
        unreleased = [a for a in state["attempts"] if a["phase"] != "released"]
        if len(unreleased) > 1:
            raise ValueError("exit waiting for broker proof: " + symbol)
        proof_base = (unreleased or state["attempts"])[-1]["base"]
    for attempt_number, attempt in enumerate(state["attempts"], 1):
        base, quantity = attempt["base"], Decimal(attempt["quantity"])
        list_proof = call.reason in SOFTWARE_EXIT_REASONS
        after_cancel = None
        cancel_context = (account, symbol, coid, entry_id, base, state["exit_reason"],
                          attempt_number, len(state["attempts"]), call.call_id)
        cancel_format = ("[WBATTACH1-CANCEL] account=%s symbol=%s entry=%s entry_id=%s base=%s "
                         "reason=%s attempt=%s/%s call=%s actor=ATR_OWNER ")
        # Historical rejected_no_wire flags are untrusted; reconcile their exact
        # original children through the same cancel/read path as UNKNOWN.
        if attempt["phase"] == "released":
            receipts = [saved_report(p) for p in attempt.get("terminal_proof", [])]
            if not terminal_pair(receipts, base, quantity, datetime.now(UTC), symbol=symbol):
                raise ValueError("wbattach1_saved_release_not_proven")
            if not list_proof or base != proof_base:
                continue
        if attempt["phase"] != "released" and not attempt.get("cancel_requested"):
            def cancel(session, entry, managed, acct, strategy, owned, journal):
                journal["attempts"] = [{**a, "cancel_requested": True} if a["base"] == base else a
                                       for a in journal["attempts"]]
            await _atr_state(service, account, symbol, coid, cancel, allow_closed=True)
            service.logger.info(cancel_format + "action=request terminal=UNKNOWN", *cancel_context)
            try:
                cancel_reports = await service.broker_adapter.cancel_exit_pair(broker_account_name=account,
                    symbol=symbol, base_client_order_id=base)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                service.logger.warning(cancel_format + "action=call_unknown terminal=UNKNOWN failure=%s",
                                       *cancel_context, type(exc).__name__)
                if not list_proof:
                    raise
            else:
                service.logger.info(cancel_format + "action=returned terminal=UNPROVEN receipts=%s",
                                    *cancel_context, _cancel_read_summary(cancel_reports))
            after_cancel = datetime.now(UTC)
        else:
            service.logger.info(cancel_format + "action=not_resent prior_cancel_requested=true terminal=UNKNOWN",
                                *cancel_context)
        try:
            if list_proof:
                result = await service.broker_adapter.confirm_exit_pair_from_today_list(
                    broker_account_name=account, symbol=symbol, base_client_order_id=base,
                    quantity=quantity, after=after_cancel, original_attempts=state["attempts"])
                for original in state["attempts"]:
                    expected = {WebullBrokerAdapter._combo_leg_coid(original["base"], suffix) for suffix in ("T", "S")}
                    reads = tuple(r for r in result.reports if r is not None and r.client_order_id in expected)
                    known = validated_child_reads(reads, original["base"], Decimal(original["quantity"]),
                                                  symbol, datetime.now(UTC))
                    if any(r.filled_quantity > 0 for r in known):
                        await _record_atr_children(service, account, symbol, coid, original, known)
                expected = {WebullBrokerAdapter._combo_leg_coid(base, suffix) for suffix in ("T", "S")}
                result = replace(result, reports=tuple(r for r in result.reports
                    if r is not None and r.client_order_id in expected))
            else:
                result = await service.broker_adapter.confirm_exit_pair_terminal(
                    broker_account_name=account, symbol=symbol, base_client_order_id=base)
        except (Exception, asyncio.CancelledError) as exc:
            service.logger.warning(cancel_format + "action=confirm_unknown terminal=UNKNOWN failure=%s",
                                   *cancel_context, type(exc).__name__)
            raise
        reports = result.reports
        service.logger.info(cancel_format + "action=confirmation_read outcome=%s receipts=%s validation=PENDING",
                            *cancel_context, result.outcome, _cancel_read_summary(reports))
        known = validated_child_reads(reports, base, quantity, symbol, datetime.now(UTC))
        if not list_proof and any(r.filled_quantity > 0 for r in known):
            await _record_atr_children(service, account, symbol, coid, attempt, known)
        if ((list_proof and result.outcome != "released")
                or not terminal_pair(reports, base, quantity, datetime.now(UTC), symbol=symbol)):
            service.logger.warning(cancel_format + "action=blocked terminal=UNKNOWN sell=0", *cancel_context)
            raise ValueError("exit waiting for broker proof: " + symbol if list_proof
                             else "wbattach1_original_children_not_terminal")
        if list_proof:
            for report in reports:
                proof = json.loads(report.metadata.get("wbattach1_list_census", "null"))
                if (not isinstance(proof, dict) or proof.get("complete") is not True
                        or proof.get("truncated") is not False or proof.get("broker_account_name") != account
                        or proof.get("symbol") != symbol or proof.get("base") != base
                        or proof.get("original_bases") != sorted(a["base"] for a in state["attempts"])
                        or type(proof.get("pages")) is not int or not 1 <= proof["pages"] <= 20):
                    raise ValueError("exit waiting for broker proof: " + symbol)
                acquired = datetime.fromisoformat(proof["acquired_at"])
                started = datetime.fromisoformat(proof["started_at"])
                if (acquired.tzinfo is None or started.tzinfo is None
                        or not 0 <= (datetime.now(UTC) - acquired).total_seconds() <= 2
                        or not 0 <= (acquired - started).total_seconds() <= 30
                        or after_cancel is not None and started < after_cancel):
                    raise ValueError("exit waiting for broker proof: " + symbol)
                call.list_proof_at = min(call.list_proof_at or acquired, acquired)
        service.logger.info(cancel_format + "action=confirmed terminal=PROVEN", *cancel_context)
        if await _atr_resolved_by_child(service, state, account, symbol, coid, base, reports):
            return Decimal(0)
        def released(session, entry, managed, acct, strategy, owned, journal):
            journal["attempts"] = [{**a, "phase": "released", "terminal_proof": [report_payload(r) for r in reports]}
                                   if a["base"] == base else a
                                   for a in journal["attempts"]]
            intent = session.get(TradeIntent, UUID(attempt["intent_id"]))
            if intent is not None:
                intent.status = "cancelled"
        await _atr_state(service, account, symbol, coid, released, allow_closed=True)
        terminal_at = monotonic()
    def claim(session, entry, managed, acct, strategy, owned, journal):
        if (entry.status not in TERMINAL or call.reason == REASON and not _rth(service)
                or owned < quantity or journal.get("exit_phase")
                or any(a["phase"] != "released" for a in journal["attempts"])):
            raise ValueError("wbattach1_buy_remainder_or_session_unknown")
        journal["exit_phase"] = "claimed"
        journal["exit_call_id"] = call.call_id
    _, _, _, owned, _ = await _atr_state(service, account, symbol, coid)
    quantity = await broker_ceiling(service, account, symbol, owned, after=terminal_at,
                                   allow_flat=call.reason in SOFTWARE_EXIT_REASONS)
    if quantity == 0:
        call.entry_coid, call.broker_flat = coid, True
        await resolve_atr_broker_flat(service, str(row.id))
        return Decimal(0)
    # Register before the durable claim await: cancellation after its commit
    # must still find this exact, never-dispatched owner in the finally cleanup.
    call.entry_coid, call.quantity, call.owned_quantity, call.terminal_at = coid, quantity, owned, terminal_at
    claim_task = asyncio.create_task(_atr_state(service, account, symbol, coid, claim))
    try:
        await asyncio.shield(claim_task)
    except asyncio.CancelledError:
        await claim_task  # Do not race cleanup against a still-committing DB worker.
        raise
    service._exit_reservation_released.add((account, symbol))
    await service._persist_webull_protect_state(account, symbol, "released")
    service.logger.info("[WBATTACH1-HANDOFF] account=%s symbol=%s entry=%s quantity=%s owner=ATR",
                        account, symbol, entry_id, quantity)
    return quantity


def _request_binding(request):
    return (request.client_order_id, request.broker_account_name, request.strategy_code,
            request.symbol, request.side, request.intent_type, request.quantity, request.reason,
            request.order_type, request.time_in_force, request.metadata.get("oms_managed_row_id"),
            deepcopy(request.metadata), request.metadata.get("managed_sell_prepared_at"))


def atr_software_exit_required(service, session, row, reason):
    try:
        from project_mai_tai.oms import wbexit2
        if (reason not in SOFTWARE_EXIT_REASONS and not wbexit2.selected(service, reason, getattr(row, "broker_account_name", None))
                or getattr(row, "strategy_code", None) != "schwab_1m_v2"):
            return False
        provider = getattr(service.settings, "provider_for_account", None)
        if not callable(provider) or provider(getattr(row, "broker_account_name", None)) != "webull":
            return False  # Registered routing configuration is in-memory; no unrelated broker reads.
        entry_id = getattr(row, "entry_order_id", None)
        entry = session.get(BrokerOrder, entry_id) if entry_id is not None else None
        account = session.get(BrokerAccount, entry.broker_account_id) if entry is not None else None
        return account is not None and account.provider == "webull" and marked_atr_episode(entry.payload or {})
    except Exception:
        service.logger.exception("[WBATTACH1-SOFTWARE-CHECK-UNKNOWN] reason=%s legacy_exit_guards=retained", reason)
        return False


def atr_handoff_ready(service, managed_row_id):
    call = _ATR_CALL.get()
    return bool(call is not None and not call.consumed and call.service is service
                and call.task is asyncio.current_task() and call.managed_id == str(managed_row_id)
                and call.entry_coid and call.terminal_at is not None)


async def resolve_atr_broker_flat(service, managed_row_id):
    call = _ATR_CALL.get()
    if (call is None or not call.broker_flat or call.service is not service
            or call.task is not asyncio.current_task() or call.managed_id != str(managed_row_id)):
        return False
    def flat(session, entry, row, account, strategy, owned, journal):
        if (str(row.id) != call.managed_id or journal.get("exit_phase")
                or any(a["phase"] != "released" for a in journal["attempts"])):
            raise ValueError("wbattach1_flat_owner_or_original_children_changed")
        service.store.close_managed_position(session, row)
        journal["exit_phase"] = "resolved_by_broker_flat"
        journal["broker_flat_confirmed_at"] = datetime.now(UTC).isoformat()
    await _atr_state(service, call.account, call.symbol, call.entry_coid, flat, allow_closed=True)
    call.consumed = True
    service._managed_v2_symbols.discard((call.account, call.symbol))
    # No fill/ledger execution is fabricated from inventory. Native fills were
    # already attributed; an external discrepancy remains visible for reconciliation.
    await critical(service, call.account, call.symbol,
        "broker flat before software exit; execution ledger reconciliation required", entry_id=call.entry_coid)
    return True


async def validate_atr_before_wire(service, request, managed_row_id):
    """Main calls this AFTER O14 preparation, immediately before the FIRST broker await.

    The existing owner prevents another managed seller. Original native children
    are terminal; neither a new cancel nor a retry/reprice is authorized here.
    """
    call = _current_atr_call(service, managed_row_id, require_rth=False)
    if call.wb2:
        from project_mai_tai.oms.wbexit2 import validate_before_wire
        return await validate_before_wire(service, call, request)
    if (not call.entry_coid or call.quantity is None or call.terminal_at is None
            or request.reason != call.reason or request.strategy_code != "schwab_1m_v2"
            or request.broker_account_name != call.account or request.symbol != call.symbol
            or request.side != "sell" or request.intent_type != "close"
            or not request.quantity.is_finite() or request.quantity != call.quantity
            or request.order_type not in ({"market"} if call.reason == REASON else {"market", "limit"})
            or request.time_in_force != "day"
            or not request.client_order_id or request.metadata.get("oms_managed_row_id") != call.managed_id
            or not request.metadata.get("managed_sell_prepared_at")):
        raise ValueError("wbattach1_final_request_binding_unknown")
    binding = _request_binding(request)
    if call.prepared is not None:
        raise ValueError("wbattach1_final_request_already_checked_no_retry")
    # Retain even a failed validation's identity so ONLY its exact O14 abort
    # event can release this never-sent handoff while this invocation is alive.
    call.prepared = binding
    _current_atr_call(service, managed_row_id)
    if call.reason in SOFTWARE_EXIT_REASONS:
        from project_mai_tai.oms.service import _managed_exit_session_matches_clock
        if not _managed_exit_session_matches_clock(request.metadata):
            raise ValueError("wbattach1_software_exit_session_changed")
        if call.list_proof_at is not None and not 0 <= (
                datetime.now(UTC) - call.list_proof_at).total_seconds() <= 2:
            raise ValueError("wbattach1_final_list_proof_stale")
        if request.metadata.get("session") != "NORMAL":
            limit = Decimal(request.metadata.get("limit_price", "0"))
            if (request.order_type != "limit" or not limit.is_finite() or limit <= 0
                    or request.metadata.get("extended_hours") != "true"):
                raise ValueError("wbattach1_software_exit_extended_hours_price_unknown")

    def check(session):
        entry, row, account, strategy, owned = _atr(service, session, call.account,
            call.symbol, call.entry_coid, prepared=request)
        state = (entry.payload or {}).get(KEY) or {}
        if (str(row.id) != call.managed_id or state.get("exit_phase") != "claimed"
                or state.get("exit_call_id") != call.call_id or state.get("history_proven") is not True
                or state.get("legacy_base") or entry.status not in TERMINAL or owned != call.owned_quantity):
            raise ValueError("wbattach1_final_owner_or_quantity_changed")
        for attempt in state.get("attempts", []):
            quantity, base = Decimal(attempt["quantity"]), attempt["base"]
            if attempt["phase"] == "released" and terminal_pair(
                    [saved_report(p) for p in attempt.get("terminal_proof", [])],
                    base, quantity, datetime.now(UTC), symbol=call.symbol):
                continue
            raise ValueError("wbattach1_final_original_tree_unknown")
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        if order is None or order.status != "pending":
            raise ValueError("wbattach1_final_prepared_order_missing")
        return owned

    await service._run_db(check)
    # This is deliberately the last await: do not refresh quotes, reprice or
    # prepare anything between this proof and the caller's broker submit.
    ceiling = await broker_ceiling(service, call.account, call.symbol, call.quantity, after=call.terminal_at,
                                  allow_flat=call.reason in SOFTWARE_EXIT_REASONS)
    if ceiling == 0:
        call.broker_flat = True
        raise ValueError("wbattach1_broker_flat_before_wire")
    _current_atr_call(service, managed_row_id)
    if (ceiling != call.quantity or _request_binding(request) != binding
            or call.list_proof_at is not None and not 0 <= (
                datetime.now(UTC) - call.list_proof_at).total_seconds() <= 2):
        raise ValueError("wbattach1_final_broker_ceiling_or_request_changed")
    if call.reason in SOFTWARE_EXIT_REASONS:
        from project_mai_tai.oms.service import _managed_exit_session_matches_clock
        if not _managed_exit_session_matches_clock(request.metadata):
            raise ValueError("wbattach1_software_exit_session_changed")

    quote_at = request.metadata.get("wbattach1_unsent_retry_quote_at")
    if quote_at is not None:
        at = datetime.fromisoformat(quote_at)
        if at.tzinfo is None or not 0 <= (datetime.now(UTC) - at).total_seconds() <= 2.5:
            raise ValueError("wbattach1_unsent_retry_quote_stale_before_wire")


async def release_atr_proven_unsent(service, request, managed_row_id, *, abort_event_id):
    """Consume ONLY O14's committed PROVEN_NOT_SENT audit event for this call.

    Integration contract: abandon_unposted_sell must return its freshly committed
    BrokerOrderEvent.id. None, HTTP ACKs, phase strings and restored rows do not
    qualify. The caller must never invoke this after entering any broker await.
    """
    candidate = _ATR_CALL.get()
    if candidate is not None and candidate.wb2:
        from project_mai_tai.oms.wbexit2 import release_existing_abort
        return await release_existing_abort(service, candidate, request, managed_row_id, abort_event_id)
    call = _current_atr_call(service, managed_row_id, require_rth=False)
    if call.prepared is None or call.prepared != _request_binding(request) or abort_event_id is None:
        raise ValueError("wbattach1_unsent_receipt_call_unknown")

    def release(session):
        entry = session.scalar(select(BrokerOrder).where(
            BrokerOrder.client_order_id == call.entry_coid).with_for_update())
        row = session.get(OmsManagedPosition, UUID(call.managed_id))
        order = session.scalar(select(BrokerOrder).where(
            BrokerOrder.client_order_id == request.client_order_id).with_for_update())
        event = session.get(BrokerOrderEvent, abort_event_id)
        intent = session.get(TradeIntent, order.intent_id) if order is not None else None
        state = dict((entry.payload or {}).get(KEY) or {}) if entry is not None else {}
        if (entry is None or row is None or order is None or event is None or intent is None
                or row.entry_order_id != entry.id or row.entry_client_order_id != call.entry_coid
                or row.strategy_code != request.strategy_code or row.broker_account_name != call.account
                or row.symbol != call.symbol or row.status != "open"
                or order.strategy_id != entry.strategy_id or order.broker_account_id != entry.broker_account_id
                or order.symbol != call.symbol or order.side != "sell" or order.quantity != request.quantity
                or order.status != "aborted" or order.submitted_at is not None or order.broker_order_id
                or intent.strategy_id != entry.strategy_id or intent.broker_account_id != entry.broker_account_id
                or intent.symbol != call.symbol or intent.side != "sell" or intent.intent_type != "close"
                or intent.reason != call.reason or intent.status != "aborted" or intent.quantity != request.quantity
                or order.order_type != request.order_type or order.time_in_force != "day"
                or order.payload != request.metadata or (intent.payload or {}).get("metadata") != request.metadata
                or event.order_id != order.id or event.event_source != "client" or event.event_type != "aborted"
                or (event.payload or {}).get("proven_not_sent") is not True
                or (event.payload or {}).get("client_order_id") != request.client_order_id
                or (event.payload or {}).get("metadata") != order.payload
                or (order.payload or {}).get("managed_sell_prepared_at") != call.prepared[-1]
                or (order.payload or {}).get("oms_managed_row_id") != call.managed_id
                or _aware_utc(event.event_at) < datetime.fromisoformat(call.prepared[-1])
                or _aware_utc(event.event_at) > datetime.now(UTC)
                or state.get("exit_phase") != "claimed" or state.get("exit_call_id") != call.call_id
                or session.scalar(select(Fill.id).where(Fill.order_id == order.id)) is not None
                or session.scalar(select(BrokerOrderEvent.id).where(BrokerOrderEvent.order_id == order.id,
                    BrokerOrderEvent.id != event.id)) is not None):
            raise ValueError("wbattach1_unsent_receipt_not_proven")
        state["unsent_handoffs"] = [*state.get("unsent_handoffs", []), {
            "call_id": call.call_id, "client_order_id": request.client_order_id,
            "abort_event_id": str(event.id), "outcome": "PROVEN_NOT_SENT",
            "reason": call.reason, "exit_reason": state.get("exit_reason")}]
        state.pop("exit_phase", None)
        state.pop("exit_call_id", None)
        entry.payload = {**entry.payload, KEY: state}
    await service._run_db(release, commit=True)
    call.consumed = True


def atr_other_exit_blocked(session, row):
    entry = session.get(BrokerOrder, row.entry_order_id) if row.entry_order_id is not None else None
    state = (entry.payload or {}).get(KEY) or {} if entry is not None else {}
    return bool(state.get("exit_phase") or any(a.get("phase") in {"submitting", "unknown"}
                                               for a in state.get("attempts", [])))


class OrbAttachExit:
    """Explicit attach-risk-off proof, NOT a STOP context or new execution owner."""

    def __init__(self, service, entry_id):
        self.service = service
        self.entry_id = entry_id
        self.terminal_at = None
        self.prepared_quantity = None

    def validate_native_owner(self, session, entry):
        from project_mai_tai.oms.orb_country import PROTECTION
        strategy = session.get(Strategy, entry.strategy_id)
        account = session.get(BrokerAccount, entry.broker_account_id)
        intent = session.get(TradeIntent, entry.intent_id)
        if (entry.id != self.entry_id or strategy is None or account is None or intent is None
                or strategy.code != "orb_schwab" or not controlled_live_scope(
                    strategy.code, strategy.execution_mode, account.name)
                or account.provider != "webull" or not account.is_active
                or account.name != self.service.settings.orb_webull_account_name
                or entry.side != "buy" or not entry.client_order_id or not entry.broker_order_id
                or intent.strategy_id != strategy.id or intent.broker_account_id != account.id
                or intent.symbol != entry.symbol or intent.side != "buy" or intent.intent_type != "open"
                or (entry.payload or {}).get("orb_country_route") != "true"
                or (entry.payload or {}).get("orb_country_broker") != "webull"):
            raise ValueError("wbattach1_orb_exact_owner_unknown")
        require_current_episode(entry, intent, datetime.now(UTC))
        state = (entry.payload or {}).get(PROTECTION)
        if state is not None and not isinstance(state, dict):
            raise ValueError("wbattach1_orb_tree_corrupt")
        from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata
        metadata = entry.payload or {}
        original = build_orb_schwab_bracket_metadata(Decimal(metadata["orb_intended_break_level"]))
        for field, canonical in (("stop_price", "stop_price"), ("orb_target_price", "bracket_target_price"),
                                 ("orb_protect_price", "bracket_stop_price")):
            price = Decimal(metadata[field])
            if not price.is_finite() or price != Decimal(original[canonical]):
                raise ValueError("wbattach1_orb_original_geometry_changed")
        if state:
            root = session.get(TradeIntent, UUID(state["intent_id"]))
            quantity = Decimal(state["quantity"])
            if (not state.get("base") or not quantity.is_finite() or not 0 < quantity <= entry.quantity
                    or root is None or root.strategy_id != strategy.id or root.broker_account_id != account.id
                    or root.symbol != entry.symbol or root.side != "sell" or root.intent_type != "close"
                    or root.quantity != quantity or root.reason != "ORB_ATTACH_PROTECTION"
                    or (root.payload or {}).get("metadata", {}).get("entry_order_id") != str(entry.id)):
                raise ValueError("wbattach1_orb_original_tree_binding_changed")

    def execution_metadata(self, target, now):
        from project_mai_tai.oms.orb_country import _rth
        if now.tzinfo is None or not _rth(now):
            raise ValueError("wbattach1_outside_rth")
        return {"order_type": "market", "session": "NORMAL", "extended_hours": "false"}

    def ownership_conflict(self, session, target):
        from project_mai_tai.oms.orb_country import PROTECTION
        entry = session.get(BrokerOrder, target.entry_id)
        self.validate_native_owner(session, entry)
        state = (entry.payload or {}).get(PROTECTION) or {}
        query = select(BrokerOrder.id).where(
            BrokerOrder.strategy_id == target.strategy_id,
            BrokerOrder.broker_account_id == target.account_id, BrokerOrder.symbol == target.symbol,
            BrokerOrder.side == "sell",
            or_(BrokerOrder.status.is_(None), BrokerOrder.status.not_in(TERMINAL | {"aborted"})))
        if state:
            query = query.where(or_(BrokerOrder.intent_id.is_(None), BrokerOrder.intent_id != UUID(state["intent_id"])))
        exit_state = (entry.payload or {}).get("orb_schwab_eod_close") or {}
        if exit_state.get("reason") == REASON and exit_state.get("exit_order_id"):
            prepared = session.get(BrokerOrder, UUID(exit_state["exit_order_id"]))
            if (prepared is None or prepared.client_order_id != exit_state.get("exit_coid")
                    or prepared.strategy_id != target.strategy_id or prepared.broker_account_id != target.account_id
                    or prepared.symbol != target.symbol or prepared.side != "sell"
                    or prepared.quantity != self.prepared_quantity
                    or str(prepared.intent_id) != exit_state.get("exit_intent_id")
                    or (prepared.payload or {}).get("entry_order_id") != str(target.entry_id)):
                raise ValueError("wbattach1_orb_prepared_identity_changed")
            query = query.where(BrokerOrder.id != prepared.id)
        return session.scalar(query.limit(1)) is not None

    async def poll_children(self, target):
        from project_mai_tai.oms.orb_country import _probe, _snapshot
        def read(session):
            return _snapshot(self.service, session, target.entry_id, attach_context=self)[4]
        state = await self.service._run_db(read, commit=False)
        if state and not orb_terminal_state_proven(state, target.account, target.symbol, datetime.now(UTC)):
            await _probe(self.service, target.entry_id, target.account, target.symbol, state,
                         fresh=True, attach_context=self)

    async def quantity_after_release(self, target, terminal_at):
        from project_mai_tai.oms.orb_country import _snapshot
        self.terminal_at = monotonic()
        owned = await self.service._run_db(
            lambda s: _snapshot(self.service, s, target.entry_id, attach_context=self)[3], commit=False)
        quantity = await broker_ceiling(self.service, target.account, target.symbol, owned, after=self.terminal_at)
        self.prepared_quantity = quantity
        return quantity

    def validate_prepared(self, session, target, quantity):
        from project_mai_tai.oms.orb_country import _snapshot
        entry, _, _, owned, _ = _snapshot(self.service, session, target.entry_id, attach_context=self)
        if (quantity != self.prepared_quantity or not 0 < quantity <= owned
                or entry.status not in TERMINAL or self.ownership_conflict(session, target)):
            raise ValueError("wbattach1_orb_owned_preimage_changed")

    async def validate_wire(self, target, request):
        if self.terminal_at is None or request.quantity != self.prepared_quantity:
            raise ValueError("wbattach1_orb_release_proof_unknown")
        control = getattr(self.service, "_bot_control_runtime", None)
        if control is not None and control.stop_requested("orb_schwab"):
            raise ValueError("wbattach1_orb_stop_owner_has_priority")
        quantity = await broker_ceiling(self.service, target.account, target.symbol,
                                       request.quantity, after=self.terminal_at)
        if quantity != request.quantity:
            raise ValueError("wbattach1_orb_broker_ceiling_changed")
        await self.service._run_db(lambda s: self.validate_prepared(s, target, request.quantity), commit=False)
        age = self.service.broker_adapter.positions_wire_read_age_seconds(target.account)
        if (isinstance(age, bool) or not isinstance(age, (int, float)) or not isfinite(age)
                or not 0 <= age <= 30 or monotonic() - age <= self.terminal_at):
            raise ValueError("wbattach1_orb_wire_receipt_aged_before_submit")

    async def recover_existing_exit(self, target):
        """Run under _attempt's original episode claim, including after restart."""
        from project_mai_tai.oms import orb_schwab_eod as owner
        from project_mai_tai.oms.orb_country import PROTECTION
        def read(session):
            entry = session.get(BrokerOrder, target.entry_id)
            self.validate_native_owner(session, entry)
            state = dict((entry.payload or {}).get(owner._STATE) or {})
            if not state.get("exit_order_id"):
                return None
            order = session.get(BrokerOrder, UUID(state["exit_order_id"]))
            intent = session.get(TradeIntent, UUID(state["exit_intent_id"]))
            if (state.get("reason") != REASON or order is None or intent is None
                    or order.client_order_id != state.get("exit_coid") or order.intent_id != intent.id
                    or order.strategy_id != target.strategy_id or order.broker_account_id != target.account_id
                    or order.symbol != target.symbol or order.side != "sell" or not order.quantity.is_finite()
                    or not 0 < order.quantity <= target.entry_quantity or intent.reason != REASON
                    or intent.strategy_id != target.strategy_id or intent.broker_account_id != target.account_id
                    or intent.symbol != target.symbol or intent.side != "sell" or intent.intent_type != "close"
                    or intent.quantity != order.quantity
                    or (order.payload or {}).get("entry_order_id") != str(entry.id)):
                raise ValueError("wbattach1_orb_original_exit_binding_changed")
            require_current_episode(order, intent, datetime.now(UTC))
            metadata = dict(order.payload or {})
            if not all(isinstance(v, str) for v in metadata.values()):
                raise ValueError("wbattach1_orb_original_exit_metadata_invalid")
            if order.broker_order_id:
                metadata["broker_order_id"] = order.broker_order_id
            return (order.id, intent.id, OrderRequest(order.client_order_id, target.account, "orb_schwab",
                target.symbol, "sell", "close", order.quantity, REASON, metadata,
                order.order_type, order.time_in_force), order.broker_order_id)
        saved = await self.service._run_db(read, commit=False)
        if saved is None:
            return False
        order_id, intent_id, request, broker_id = saved
        report = await self.service.broker_adapter.fetch_order_update(request)
        if (report is None or report.origin != "broker" or not report.broker_order_id
                or broker_id is not None and report.broker_order_id != broker_id
                or report.client_order_id != request.client_order_id or report.symbol != request.symbol
                or report.side != "sell" or report.intent_type != "close" or report.quantity != request.quantity
                or report.event_type not in TERMINAL | {"accepted", "partially_filled"}
                or report.event_type == "filled" and report.filled_quantity != request.quantity
                or report.reported_at.tzinfo is None or report.reported_at > datetime.now(UTC)
                or not report.filled_quantity.is_finite() or not 0 <= report.filled_quantity <= request.quantity
                or report.filled_quantity > 0 and (not report.broker_fill_id or report.fill_price is None
                    or not report.fill_price.is_finite() or report.fill_price <= 0)):
            raise ValueError("wbattach1_orb_original_exit_read_unknown")
        with self.service.session_factory() as session:
            order = session.scalar(select(BrokerOrder).where(BrokerOrder.id == order_id).with_for_update())
            intent = session.get(TradeIntent, intent_id)
            fills = session.scalars(select(Fill).where(Fill.order_id == order_id)).all()
            previous = sum((f.quantity for f in fills), Decimal(0))
            previous_cash = sum((f.quantity * f.price for f in fills), Decimal(0))
            delta = report.filled_quantity - previous
            if delta < 0:
                raise ValueError("wbattach1_orb_original_execution_decreased")
            writer = report
            if report.filled_quantity:
                cash = report.filled_quantity * report.fill_price - previous_cash
                if (delta > 0 and cash <= 0 or delta == 0 and abs(cash) > previous * Decimal("0.000000005")):
                    raise ValueError("wbattach1_orb_original_cash_inconsistent")
                writer = replace(report, fill_price=cash / delta if delta else report.fill_price,
                    metadata={**report.metadata, "wbattach1_broker_cumulative_average": str(report.fill_price)})
            vp = self.service.store.get_virtual_position(session, strategy_id=target.strategy_id,
                broker_account_id=target.account_id, symbol=target.symbol)
            if order is None or intent is None or vp is None or delta > vp.quantity:
                raise ValueError("wbattach1_orb_original_execution_exceeds_owned")
            event = TradeIntentEvent(source_service="oms-risk", payload=TradeIntentPayload(
                strategy_code="orb_schwab", broker_account_name=target.account, symbol=target.symbol,
                side="sell", intent_type="close", quantity=request.quantity, reason=REASON, metadata=request.metadata))
            events = await self.service._record_order_reports(session=session, intent=intent,
                strategy_id=target.strategy_id, broker_account_id=target.account_id,
                intent_event=event, request=request, reports=[writer])
            entry = session.get(BrokerOrder, target.entry_id)
            self.validate_native_owner(session, entry)
            native = (entry.payload or {}).get(PROTECTION) or {}
            flat = vp.quantity == 0
            native_terminal = orb_terminal_state_proven(native, target.account, target.symbol, datetime.now(UTC))
            session.commit()
        for event in events:
            await self.service._publish_order_event(event)
        if report.event_type == "filled" and report.filled_quantity == request.quantity and flat and native_terminal:
            await owner._finish(self.service, target, "settled", datetime.now(UTC))
        elif report.event_type in {"cancelled", "rejected", "expired"}:
            await critical(self.service, target.account, target.symbol,
                "original_riskoff_exit_terminal_no_blind_retry", entry_id=target.entry_id)
        return True


async def maybe_orb_riskoff(service, entry_id):
    """Inspect failure/price state; only the original EOD/signal owner may close."""
    from project_mai_tai.oms.orb_country import PROTECTION, _rth, _snapshot
    from project_mai_tai.oms import orb_schwab_eod as owner
    now = datetime.now(UTC)
    if not _rth(now):
        return False
    context = OrbAttachExit(service, entry_id)
    def inspect(session):
        entry = session.get(BrokerOrder, entry_id)
        if entry is None or (entry.payload or {}).get("orb_country_broker") != "webull":
            return None
        context.validate_native_owner(session, entry)
        state = dict((entry.payload or {}).get(PROTECTION) or {})
        exit_state = (entry.payload or {}).get(owner._STATE) or {}
        if exit_state and exit_state.get("reason") != REASON:
            return None
        if exit_state.get("phase") in {"settled", "resolved_by_child_fill"}:
            return None
        if not exit_state and state.get("phase") in {"working", "accepted", "released"}:
            return None
        entry, account, _, quantity, _ = _snapshot(service, session, entry_id, attach_context=context)
        if quantity <= 0 and not exit_state and orb_terminal_state_proven(state, account.name, entry.symbol, now):
            return None
        return account.name, entry.symbol, state, dict(entry.payload)
    try:
        initial = await service._run_db(inspect, commit=False)
        if initial is None:
            return False
        account, symbol, state, payload = initial
        prior_exit = payload.get(owner._STATE) or {}
        unplaceable = "" if prior_exit else await service._webull_protect_unplaceable_reason(
            broker_account_name=account, symbol=symbol,
            target=float(payload["orb_target_price"]), protect=float(payload["orb_protect_price"]))
        failed = bool(prior_exit) or (state.get("phase") in {"submitting", "rejected"}
                  and (not orb_retry_terminal_proven(state, account, symbol, now)
                       or int(state.get("attempt", 1)) >= 5))
        if not unplaceable and not failed:
            return False
        control = getattr(service, "_bot_control_runtime", None)
        if control is not None and control.stop_requested("orb_schwab"):
            return True  # STOP keeps its real lease and owner; no fabricated STOP context.
        event_id = UUID(prior_exit["event_id"]) if prior_exit else uuid4()
        def claim(session):
            entry, acct, strategy, owned, _ = _snapshot(service, session, entry_id, attach_context=context)
            existing = (entry.payload or {}).get(owner._STATE) or {}
            if existing and existing.get("reason") != REASON:
                return None
            if entry.status not in TERMINAL or not _rth(datetime.now(UTC)):
                raise ValueError("wbattach1_orb_buy_remainder_or_session_unknown")
            from project_mai_tai.oms.orb_country import entry_quantity
            target = owner._Target(entry.id, strategy.id, acct.id, acct.name, entry.symbol,
                entry.broker_order_id, entry.client_order_id, owned or entry_quantity(entry), entry_quantity(entry), True)
            if not existing:
                entry.payload = {**entry.payload, owner._STATE: {"phase": "checking", "reason": REASON,
                    "event_id": str(event_id), "updated_at": now.isoformat()}}
            return target
        target = await service._run_db(claim)
        if target is not None:
            try:
                completed = await owner._attempt(service, target, event_id, lambda: datetime.now(UTC),
                    reason=REASON, attach_context=context)
                if not completed:
                    raise ValueError("wbattach1_orb_existing_owner_busy")
            except Exception as exc:
                await owner._finish(service, target, "needs_attention", datetime.now(UTC))
                await critical(service, account, symbol, str(exc), entry_id=entry_id)
        return True
    except Exception as exc:
        await critical(service, initial[0] if "initial" in locals() and initial else "UNKNOWN",
                       initial[1] if "initial" in locals() and initial else "UNKNOWN", str(exc), entry_id=entry_id)
        if isinstance(exc, ValueError) and str(exc) == "wbattach1_orb_original_geometry_changed":
            raise
        return True
