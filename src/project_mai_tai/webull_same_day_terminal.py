"""Positive stored Webull detail proof; no reads, writes or cancel-status inference.

Caller supplies the COMPLETE bounded order-event set, typed account/order and
strategy identity, and retains parent, replacement, Fill and maturity checks.
Only the shared same-day event-binding flag may enable this additional witness.
"""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal


MAX_EVENTS = 2048  # Existing v2_removed_wait.ROW_LIMIT, not a query/cadence change.
MAX_PROOF_BYTES = 65536


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_proof_key")
        result[key] = value
    return result


def _number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("explicit_quantity_required")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("finite_quantity_required")
    return result


def _ms(value):
    if not isinstance(value, datetime):
        raise ValueError("typed_event_clock_required")
    return int((value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp() * 1000)


def _metadata(row):
    payload = row.payload
    if not isinstance(payload, dict):
        raise ValueError("typed_payload_required")
    result = payload.get("metadata", payload)
    if not isinstance(result, dict):
        raise ValueError("typed_metadata_required")
    return result


def _proof(event):
    raw = _metadata(event).get("_webull_terminal_proof")
    if raw is None:
        return None
    if not isinstance(raw, str) or not 0 < len(raw.encode("utf-8")) <= MAX_PROOF_BYTES:
        raise ValueError("bounded_writer_proof_required")
    def constant(value):
        raise ValueError("nonfinite_proof_constant")
    result = json.loads(raw, object_pairs_hook=_pairs, parse_constant=constant)
    if not isinstance(result, dict):
        raise ValueError("object_proof_required")
    return result


def _zero_detail(event, order, account, proof, observed):
    """Validate the exact _ordinary_order_body proof writer, not a label."""
    outcomes = (event.payload.get("cancel_outcome"), _metadata(event).get("cancel_outcome"))
    if (event.event_source != "broker"
            or event.payload.get("broker_order_id") not in (None, order.broker_order_id)
            or event.payload.get("refusal_origin") == "could_not_tell"
            or any(value not in {None, "confirmed", "requested", "confirmed_after_accepted_request"}
                   for value in outcomes)
            or len({value for value in outcomes if value is not None}) > 1):
        return False
    native = account.external_account_id
    execution = ["cancelled", order.broker_order_id, "0", None, None]
    if (proof.get("source") != "/trade/order/detail"
            or proof.get("account_id") != native
            or proof.get("client_order_id") != order.client_order_id
            or proof.get("execution") != execution):
        return False
    # Byte algorithm of webull_order_reads.terminal_version(account, coid,
    # {"execution": execution}); no importing DB/query-budget machinery here.
    version = hashlib.sha256(json.dumps(
        [native, order.client_order_id, {"execution": execution}],
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if proof.get("version") != version:
        return False
    acquired = datetime.fromisoformat(proof["acquired_at"].replace("Z", "+00:00"))
    # Acquisition and the observer are producer clocks. Do not introduce a
    # millisecond ordering gate against a DB/broker submission clock (C1 15:45).
    # The caller retains its existing submission/event and parent checks.
    if (acquired.tzinfo is None or acquired.utcoffset() is None
            or not 0 <= _ms(acquired) <= observed - 15000):
        return False
    body = proof.get("body")
    if not isinstance(body, dict) or body.get("error_code"):
        return False
    items = body.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return False
    item = items[0]
    if (body.get("account_id") != native or body.get("client_order_id") != order.client_order_id
            or body.get("order_id") != order.broker_order_id
            or item.get("symbol") != order.symbol or item.get("side") != "BUY"):
        return False
    for part in (body, item):
        for keys, expected in (
            (("account_id", "accountId"), native),
            (("client_order_id", "clientOrderId"), order.client_order_id),
            (("order_id", "orderId"), order.broker_order_id),
            (("symbol",), order.symbol), (("side",), "BUY"),
        ):
            if any(key in part and part[key] != expected for key in keys):
                return False
    statuses = [part[key] for part in (body, item)
                for key in ("status", "order_status", "orderStatus") if key in part]
    if not statuses or any(value not in {"CANCELLED", "CANCELED"} for value in statuses):
        return False
    quantities = [part[key] for part in (body, item)
                  for key in ("qty", "quantity", "total_qty") if key in part]
    fills = [part[key] for part in (body, item)
             for key in ("filled_qty", "filledQty", "filled_quantity", "filledQuantity") if key in part]
    return bool(any(key in item for key in ("qty", "quantity", "total_qty"))
        and any(key in item for key in ("filled_qty", "filledQty", "filled_quantity", "filledQuantity"))
        and all(_number(value) == _number(order.quantity) for value in quantities)
        and all(_number(value) == 0 for value in fills))


def zero_fill_terminal_event(order, events, account, *, strategy_code, observed_at_ms, enabled=False):
    """Return the witnessed event or None; NEVER a whole-owner/flat verdict.

    A later rejected CANCEL_VERIFY_RESUBMIT is allowed only as an exact scoped
    request audit after positive native proof. It is not itself terminal proof.
    No event is deleted or status retagged. Missing data fails closed.
    """
    if enabled is not True:
        return None
    try:
        if (type(observed_at_ms) is not int or observed_at_ms < 15000
                or not isinstance(events, (list, tuple)) or not 0 < len(events) <= MAX_EVENTS
                or strategy_code != "schwab_1m_v2" or account.provider != "webull"
                or account.id != order.broker_account_id
                or not isinstance(account.external_account_id, str) or not account.external_account_id
                or not isinstance(order.broker_order_id, str) or not order.broker_order_id
                or order.side != "buy" or _number(order.quantity) <= 0
                or order.status not in {"cancelled", "canceled", "replaced"}):
            return None
        md = _metadata(order)
        segment = md.get("fanout_segment_id")
        if (not isinstance(segment, str) or not segment.isdecimal()
                or md.get("atr_country_account") != account.name
                or md.get("atr_country_broker") != "webull"
                or md.get("atr_country_symbol") != order.symbol):
            return None
        identifiers, candidates = set(), []
        for event in events:
            emd = _metadata(event)
            at = _ms(event.event_at)
            if (event.order_id != order.id or not event.id or str(event.id) in identifiers
                    or not 0 <= at <= observed_at_ms - 15000
                    or event.payload.get("client_order_id") != order.client_order_id
                    or event.event_type in {"filled", "partially_filled"}
                    or emd.get("webull_terminal_with_partial_fill") is not None
                    or event.payload.get("broker_fill_id") is not None
                    or any(key in event.payload and _number(event.payload[key]) != 0
                           for key in ("filled_quantity", "filled_qty"))
                    or any(key in emd and _number(emd[key]) != 0
                           for key in ("rpg_terminal_filled_quantity", "filled_quantity"))):
                return None
            identifiers.add(str(event.id))
            proof = _proof(event)
            if proof is not None:
                execution = proof.get("execution")
                if (not isinstance(execution, list) or len(execution) != 5
                        or _number(execution[2]) != 0):
                    return None
                if (emd.get("fanout_segment_id") != segment
                        or emd.get("atr_country_account") != account.name
                        or emd.get("atr_country_broker") != "webull"
                        or emd.get("atr_country_symbol") != order.symbol):
                    return None
                if not _zero_detail(event, order, account, proof, observed_at_ms):
                    return None
                if (event.event_type in {"cancelled", "canceled"}
                        and event.payload.get("broker_order_id") == order.broker_order_id):
                    candidates.append(event)
                elif not (event.event_type == "rejected"
                        and event.payload.get("internal") == "CANCEL_VERIFY_RESUBMIT"):
                    return None
        if not candidates:
            return None
        terminal = max(candidates, key=lambda event: (_ms(event.event_at), str(event.id)))
        terminal_at = _ms(terminal.event_at)
        for event in events:
            at = _ms(event.event_at)
            if event is terminal or at < terminal_at:
                continue
            # Tied/unknown target-state witnesses are not silently selected.
            emd = _metadata(event)
            if (at == terminal_at or event.event_type != "rejected" or event.event_source != "broker"
                    or event.payload.get("internal") != "CANCEL_VERIFY_RESUBMIT"
                    or emd.get("webull_error_code") != "ORDER_CAN_NOT_BE_CANCEL"
                    or emd.get("cancel_outcome") not in {"requested", "confirmed_after_accepted_request"}
                    or event.payload.get("cancel_outcome") not in (None, emd.get("cancel_outcome"))
                    or event.payload.get("refusal_origin") == "could_not_tell"
                    or event.payload.get("broker_order_id") not in (None, order.broker_order_id)
                    or emd.get("fanout_segment_id") != segment
                    or emd.get("atr_country_account") != account.name
                    or emd.get("atr_country_broker") != "webull"
                    or emd.get("atr_country_symbol") != order.symbol):
                return None
        return terminal
    except (ValueError, TypeError, ArithmeticError, AttributeError, KeyError, UnicodeError, RecursionError):
        return None
