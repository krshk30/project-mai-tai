"""Read-only recovery of a lost admission token from its original local event.

This is local identity evidence, never broker acceptance or native flatness.
Only the existing off-loop OMS census calls it, with the independent switch ON.
Every query has a population cap; overflow and conflicting bindings refuse.
"""
import ast
from datetime import UTC
from decimal import Decimal
import json

from sqlalchemy import or_, select

from project_mai_tai.brokerdecides1 import MAX_SYMBOLS
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, TradeIntent

NATIVE_RECORD_BYTES = 64 * 1024


def _metadata(row):
    payload = row.payload
    value = payload.get("metadata", payload) if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def _at(value):
    if value is None:
        raise ValueError("event_binding_clock_UNKNOWN")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_native_record_key")
        result[key] = value
    return result


def _native_record(value):
    """Decode the exact retained native dict, never an error/status substring."""
    if not isinstance(value, str) or not 0 < len(value.encode("utf-8")) <= NATIVE_RECORD_BYTES:
        raise ValueError("bounded_native_record_required")
    try:
        record = json.loads(value, object_pairs_hook=_pairs, parse_float=Decimal,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_native_record")))
    except json.JSONDecodeError:
        tree = ast.parse(value, mode="eval")
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = [ast.literal_eval(key) for key in node.keys]
                if any(type(key) is not str for key in keys) or len(keys) != len(set(keys)):
                    raise ValueError("ambiguous_native_record_keys")
        record = ast.literal_eval(tree)
    if not isinstance(record, dict):
        raise ValueError("native_record_object_required")
    return record


def schwab_terminal_zero(order, event):
    """C1 15:50: explicit native zero; missing fields never become zero."""
    try:
        if event.event_source != "broker":
            return False
        native = _native_record(event.payload.get("reason"))
        filled, remaining = native["filledQuantity"], native["remainingQuantity"]
        if type(filled) is bool or type(remaining) is bool or filled is None or remaining is None:
            return False
        filled, remaining = Decimal(str(filled)), Decimal(str(remaining))
        return bool(native.get("status") == "CANCELED"
            and native.get("orderId") is not None
            and str(native["orderId"]) == order.broker_order_id
            and filled.is_finite() and filled == 0
            and remaining.is_finite() and remaining >= 0)
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError, SyntaxError,
            RecursionError, UnicodeError):
        return False


def accepted_events_query(tokens, session_anchor):
    """Use the existing type/time indexes before testing the JSON token."""
    return (select(BrokerOrderEvent, BrokerOrder, TradeIntent)
        .join(BrokerOrder, BrokerOrder.id == BrokerOrderEvent.order_id)
        .outerjoin(TradeIntent, TradeIntent.id == BrokerOrder.intent_id)
        .where(BrokerOrderEvent.event_type == "accepted",
               BrokerOrderEvent.event_at >= session_anchor,
               BrokerOrderEvent.payload["metadata"]["clearwait_dispatch_token"].as_string().in_(tuple(tokens)))
        .limit(MAX_SYMBOLS + 1))


def accepted_event_bindings(session, attempts, account_ids, strategy_id, observed_ms, session_anchor):
    """Return exact single-order links; no row/payload/retirement writes.

    The accepted event is the original local report and may have unknown origin.
    Broker terminality is deliberately proved separately by CANCELACK. A reused
    token, alternate accepted identity, missing parent or any execution stays held.
    """
    if not attempts:
        return {}
    if len(attempts) > MAX_SYMBOLS:
        raise ValueError("event_binding_attempt_overflow")
    rows = session.execute(accepted_events_query(attempts, session_anchor)).all()
    if len(rows) > MAX_SYMBOLS:
        raise ValueError("event_binding_event_overflow")
    grouped = {}
    for event, order, parent in rows:
        grouped.setdefault(_metadata(event).get("clearwait_dispatch_token"), []).append((event, order, parent))
    order_ids = {order.id for _, order, _ in rows}
    history = session.scalars(select(BrokerOrderEvent).where(
        BrokerOrderEvent.order_id.in_(order_ids)).limit(MAX_SYMBOLS + 1)).all() if order_ids else []
    filled = session.scalars(select(Fill.order_id).where(
        Fill.order_id.in_(order_ids)).limit(MAX_SYMBOLS + 1)).all() if order_ids else []
    if len(history) > MAX_SYMBOLS or len(filled) > MAX_SYMBOLS:
        raise ValueError("event_binding_history_overflow")
    filled = set(filled)
    result = {}
    for token, members in grouped.items():
        # One original token-bearing event/order, not a count or symbol match.
        if token not in attempts or len(members) != 1:
            continue
        event, order, parent = members[0]
        attempt = attempts[token]
        data, md, current = attempt.payload, _metadata(event), _metadata(order)
        own_history = [item for item in history if item.order_id == order.id]
        try:
            account = account_ids[data["account_name"]]
            segment = str(data["opportunity_id"])
            parent_md = _metadata(parent) if parent is not None else {}
            if (event.event_type != "accepted" or parent is None or order.id in filled
                    or order.strategy_id != strategy_id or parent.strategy_id != strategy_id
                    or order.broker_account_id != account or parent.broker_account_id != account
                    or order.symbol != data["symbol"] or parent.symbol != order.symbol
                    or order.side != "buy" or parent.side != "buy" or parent.intent_type != "open"
                    or event.payload.get("client_order_id") != order.client_order_id
                    or event.payload.get("broker_order_id") not in (None, order.broker_order_id)
                    or md.get("source") != "schwab_1m_v2"
                    or str(md.get("fanout_segment_id")) != segment
                    or parent_md.get("clearwait_dispatch_token") != token
                    or str(parent_md.get("fanout_segment_id")) != segment
                    or current.get("clearwait_dispatch_token") not in (None, token)
                    or current.get("fanout_segment_id") is not None
                        and str(current["fanout_segment_id"]) != segment
                    or not _at(attempt.created_at) <= _at(event.event_at) <= _at(order.updated_at)
                    # Native/local report time may precede the local parent's
                    # insertion by milliseconds. Do not order different clocks.
                    or not _at(parent.created_at) <= _at(order.updated_at)
                    or int(_at(order.updated_at).timestamp() * 1000) > observed_ms
                    or any(item.payload.get("broker_fill_id")
                        or item.event_type in {"filled", "partially_filled"}
                        or _metadata(item).get("clearwait_dispatch_token") not in (None, token)
                        for item in own_history)):
                continue
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            continue
        result[token] = order
    return result


def zero_execution_closure(session, order, observed_ms):
    """Require the entire exact replacement closure to have no executions.

    Existing CANCELACK validates every native terminal and replacement identity.
    Its managed-and-closed fill path is insufficient for this narrower recovery.
    """
    from project_mai_tai.cancelack1 import _closed_with_replacements

    jobs = session.scalars(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == "atr_reprice_handoff",
        or_(DashboardSnapshot.payload["old"]["client_order_id"].as_string() == order.client_order_id,
            DashboardSnapshot.payload["replacement"]["client_order_id"].as_string() == order.client_order_id))
        .limit(MAX_SYMBOLS + 1)).all()
    if len(jobs) > MAX_SYMBOLS:
        raise ValueError("event_binding_replacement_overflow")
    if not _closed_with_replacements(session, order, observed_ms, jobs=jobs):
        return False
    clients = {order.client_order_id}
    for job in jobs:
        for request in (job.payload.get("old"), job.payload.get("replacement")):
            if request is not None:
                if not isinstance(request, dict) or not request.get("client_order_id"):
                    return False
                clients.add(request["client_order_id"])
    if len(clients) > MAX_SYMBOLS:
        raise ValueError("event_binding_replacement_order_overflow")
    orders = session.scalars(select(BrokerOrder).where(
        BrokerOrder.client_order_id.in_(clients)).limit(MAX_SYMBOLS + 1)).all()
    if len(orders) != len(clients) or {row.client_order_id for row in orders} != clients:
        return False
    ids = {row.id for row in orders}
    if session.scalar(select(Fill.id).where(Fill.order_id.in_(ids)).limit(1)) is not None:
        return False
    events = session.scalars(select(BrokerOrderEvent).where(
        BrokerOrderEvent.order_id.in_(ids)).limit(MAX_SYMBOLS + 1)).all()
    if len(events) > MAX_SYMBOLS:
        raise ValueError("event_binding_terminal_history_overflow")
    for event in events:
        if event.event_type in {"filled", "partially_filled"} or event.payload.get("broker_fill_id"):
            return False
        for value in (event.payload.get("filled_quantity"),
                      _metadata(event).get("rpg_terminal_filled_quantity")):
            if value is not None:
                try:
                    quantity = Decimal(str(value))
                    if not quantity.is_finite() or quantity != 0:
                        return False
                except (ValueError, TypeError, ArithmeticError):
                    return False
    for member in orders:
        account = session.get(BrokerAccount, member.broker_account_id)
        history = [event for event in events if event.order_id == member.id]
        if account is None or not history:
            return False
        latest = max(history, key=lambda event: (_at(event.event_at), str(event.id)))
        if account.provider != "schwab" or not schwab_terminal_zero(member, latest):
            # Precise Webull positive proof is the separately reviewed Lane B.
            return False
    return True
