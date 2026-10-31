"""C12204: EH exit retry OF a real RTH riskoff O14 proven-unsent intent.

Read-only origin/canonical-quantity proof. Never creates a Fill/VP, adopts an old
call, changes original intent quantity, grants native protection or sends an
order. The actual new managed/ATR/WB2/O14 owner still earns every wire gate.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID

from sqlalchemy import or_, select

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition, SystemIncident, TradeIntent
from project_mai_tai.oms import wbexit3

REASON = "WEBULL_ATTACH_RISK_OFF"
PREFIX = "wbexit3_riskoff_"
FIELDS = ("intent_id", "order_id", "coid", "abort_event_id", "original_call_id", "metadata_sha256")


def selected(service, account):
    return wbexit3.account_session(service, account) and wbexit3.session(wbexit3._now()) in {"PM", "AH"}


def _digest(metadata):
    return sha256(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_origin(service, db, account, symbol, managed_id, *, supplied=None, allow_call_id=None, allow_closed=False, responsibility_only=False):
    """Exact existing positive audit; absence or a reason string proves nothing."""
    from project_mai_tai.oms import webull_attach_riskoff as wb
    row, entry, binding = wbexit3._binding(service, db, account, symbol, str(managed_id))
    journal = (entry.payload or {}).get(wb.KEY) or {}
    receipts = journal.get("unsent_handoffs", [])
    busy_for_recording = False
    if responsibility_only and journal.get("exit_phase") == "claimed" and journal.get("wbexit2_owner_id"):
        owner = db.get(SystemIncident, UUID(journal["wbexit2_owner_id"]))
        prior = owner.payload if owner is not None else {}
        identity = prior.get("identity", {})
        busy_for_recording = (prior.get("source") == "wbexit2" and identity.get("entry_id") == str(entry.id)
            and identity.get("managed_id") == str(row.id) and identity.get("account") == account
            and identity.get("entry_coid") == entry.client_order_id)
    if (not selected(service, account) or row.status != "open" and not (allow_closed and row.status == "closed" and row.current_quantity == 0)
            or journal.get("entry_id") != str(entry.id) or journal.get("managed_id") != str(row.id)
            or journal.get("history_proven") is not True or journal.get("legacy_base")
            or not isinstance(receipts, list) or not 0 < len(receipts) <= wbexit3.LIMIT
            or not isinstance(journal.get("attempts"), list) or not 0 < len(journal["attempts"]) <= 1
            or any(a.get("phase") != "released" for a in journal["attempts"])
            or journal.get("exit_phase") and not (
                allow_call_id and journal.get("exit_phase") == "claimed" and journal.get("exit_call_id") == allow_call_id
                or busy_for_recording  # RECORD duty only; validate_current never enables this branch.
                or allow_closed and journal.get("exit_phase") == "resolved_by_child_fill")):
        raise ValueError("wbexit3_riskoff_actual_origin_episode_or_reservation_UNKNOWN")
    if supplied is None:
        receipt = receipts[-1]
    else:
        matches = [r for r in receipts if r.get("abort_event_id") == supplied.get(PREFIX + "abort_event_id")]
        if len(matches) != 1:
            raise ValueError("wbexit3_riskoff_exact_origin_receipt_missing_or_ambiguous")
        receipt = matches[0]
    if receipt.get("outcome") != "PROVEN_NOT_SENT" or not receipt.get("call_id") or receipt.get("reason") != REASON:
        raise ValueError("wbexit3_riskoff_origin_not_positive_unposted_riskoff")
    order = db.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == receipt.get("client_order_id")))
    event = db.get(BrokerOrderEvent, UUID(receipt["abort_event_id"]))
    intent = db.get(TradeIntent, order.intent_id) if order is not None else None
    metadata = deepcopy(order.payload or {}) if order is not None else {}
    try:
        prepared_at = datetime.fromisoformat(metadata["managed_sell_prepared_at"])
        original_call = str(UUID(receipt["call_id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("wbexit3_riskoff_origin_clock_or_call_UNKNOWN") from exc
    if (order is None or intent is None or event is None
            or order.strategy_id != entry.strategy_id or order.broker_account_id != entry.broker_account_id
            or order.symbol != symbol or order.side != "sell" or order.status != "aborted"
            or order.submitted_at is not None or order.broker_order_id or order.order_type != "market" or order.time_in_force != "day"
            or not order.quantity.is_finite() or not 0 < order.quantity <= entry.quantity
            or order.quantity != order.quantity.to_integral_value()
            or intent.strategy_id != entry.strategy_id or intent.broker_account_id != entry.broker_account_id
            or intent.symbol != symbol or intent.side != "sell" or intent.intent_type != "close"
            or intent.reason != REASON or intent.status != "aborted" or intent.quantity != order.quantity
            or not receipt.get("exit_reason") or metadata.get("entry_order_id") != str(entry.id)
            or metadata.get("oms_managed_row_id") != str(row.id) or metadata.get("session") != "NORMAL"
            or prepared_at.tzinfo is None or wbexit3.session(prepared_at) != "RTH"
            or not datetime.fromisoformat(binding["entry_anchor_at"]) <= prepared_at <= wb._aware_utc(event.event_at) <= wbexit3._now()
            or (intent.payload or {}).get("metadata") != metadata
            or event.order_id != order.id or event.event_source != "client" or event.event_type != "aborted"
            or (event.payload or {}).get("proven_not_sent") is not True
            or (event.payload or {}).get("client_order_id") != order.client_order_id
            or (event.payload or {}).get("metadata") != metadata
            or db.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1)) is not None
            or db.scalar(select(BrokerOrderEvent.id).where(BrokerOrderEvent.order_id == order.id,
                BrokerOrderEvent.id != event.id).limit(1)) is not None):
        raise ValueError("wbexit3_riskoff_real_RTH_O14_proven_unsent_origin_UNKNOWN")
    origin = {PREFIX + "intent_id": str(intent.id), PREFIX + "order_id": str(order.id),
        PREFIX + "coid": order.client_order_id, PREFIX + "abort_event_id": str(event.id),
        PREFIX + "original_call_id": original_call, PREFIX + "metadata_sha256": _digest(metadata)}
    if supplied is not None and any(supplied.get(key) != value for key, value in origin.items()):
        raise ValueError("wbexit3_riskoff_origin_changed")
    buys = db.scalars(select(Fill).where(Fill.order_id == entry.id).limit(wbexit3.LIMIT + 1)).all()
    if len(buys) > wbexit3.LIMIT or not buys or any(f.strategy_id != entry.strategy_id
            or f.broker_account_id != entry.broker_account_id or f.symbol != symbol or f.side != "buy"
            or not f.broker_fill_id or not f.quantity.is_finite() or f.quantity <= 0
            or not f.price.is_finite() or f.price <= 0 for f in buys):
        raise ValueError("wbexit3_riskoff_canonical_entry_UNKNOWN")
    executions = db.execute(select(Fill, BrokerOrder).join(BrokerOrder, BrokerOrder.id == Fill.order_id)
        .join(TradeIntent, TradeIntent.id == BrokerOrder.intent_id).where(BrokerOrder.strategy_id == entry.strategy_id,
            BrokerOrder.broker_account_id == entry.broker_account_id, BrokerOrder.symbol == symbol, BrokerOrder.side == "sell",
            or_(BrokerOrder.payload["entry_order_id"].as_string() == str(entry.id),
                TradeIntent.payload["metadata"]["entry_order_id"].as_string() == str(entry.id),
                (BrokerOrder.intent_id == entry.intent_id) & (BrokerOrder.order_type == "oco_exit")))
        .limit(wbexit3.LIMIT + 1)).all()
    if len(executions) > wbexit3.LIMIT or any(f.strategy_id != entry.strategy_id or f.broker_account_id != entry.broker_account_id
            or f.symbol != symbol or f.side != "sell" or not f.broker_fill_id or not child.broker_order_id
            or not f.quantity.is_finite() or f.quantity <= 0 or not f.price.is_finite() or f.price <= 0 for f, child in executions):
        raise ValueError("wbexit3_riskoff_canonical_exit_UNKNOWN")
    bought = sum((f.quantity for f in buys), Decimal(0))
    remaining = bought - sum((f.quantity for f, _ in executions), Decimal(0))
    vp = service.store.get_virtual_position(db, strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id, symbol=symbol)
    if (not 0 <= remaining <= bought <= entry.quantity or vp is None or not vp.quantity.is_finite()
            or not 0 <= vp.quantity <= remaining or not 0 <= Decimal(row.current_quantity) <= remaining
            or Decimal(row.current_quantity) > intent.quantity):
        raise ValueError("wbexit3_riskoff_current_owned_vs_canonical_remaining_UNKNOWN")
    return row, origin, min(remaining, vp.quantity, Decimal(row.current_quantity))


def recover_candidate(service, db, account, symbol, managed_id):
    candidate = db.get(OmsManagedPosition, UUID(managed_id))
    entry = db.get(BrokerOrder, candidate.entry_order_id) if candidate is not None and candidate.entry_order_id else None
    receipts = ((entry.payload or {}).get("wbattach1") or {}).get("unsent_handoffs", []) if entry is not None else []
    if not receipts or not isinstance(receipts[-1], dict) or receipts[-1].get("reason") != REASON:
        return None  # Preserve the original unrelated software-exit retry path.
    row, origin, _ = read_origin(service, db, account, symbol, managed_id)
    return service._hydrate_v2_position(row), float(row.entry_price), REASON, origin


def activate(service, call, state):
    """Only the actual NEW duty enables this scoped EH current-call branch."""
    current = wbexit3._ATTEMPT.get()
    if (current is None or current["service"] is not service or current["task"] is not asyncio.current_task()
            or current["state"] is not state or not state.get("riskoff_origin")
            or call is None or call.service is not service or call.task is not asyncio.current_task()
            or not call.wb2 or call.reason != REASON or call.managed_id != state["binding"]["managed_id"]):
        raise ValueError("wbexit3_riskoff_not_actual_new_validated_owner")
    call.wb3_riskoff = True


def validate_current(service, db, call, *, request=None, allow_closed=False):
    supplied = (call.wb2_decision or {}).get("confirmation_context") or {}
    row, _, cap = read_origin(service, db, call.account, call.symbol, call.managed_id,
        supplied=supplied, allow_call_id=call.call_id, allow_closed=allow_closed)
    if request is not None and (request.reason != REASON or request.intent_type != "close"
            or request.order_type != "limit" or request.metadata.get("extended_hours") != "true"
            or any(request.metadata.get(PREFIX + field) != supplied.get(PREFIX + field) for field in FIELDS)
            or request.quantity > cap or request.quantity != Decimal(row.current_quantity)):
        raise ValueError("wbexit3_riskoff_exact_linked_remaining_or_EH_request_changed")
    return cap
