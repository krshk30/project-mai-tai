"""Passive, bounded broker-read diagnostics; never execution permission.

Only the existing exact broker read may produce these receipts. Retaining a
receipt does not establish an order's canonical lifetime or pair coverage.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import math

from sqlalchemy import select

from project_mai_tai.db.models import DashboardSnapshot

SNAPSHOT_TYPE = "reconciliation_order_reads_v1"
MAX_ORDERS = 512


def _aware(stamp):
    return isinstance(stamp, datetime) and stamp.tzinfo is not None and stamp.utcoffset() is not None


def _normal_cadence(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(1.0, seconds) if math.isfinite(seconds) else None


def invalidate_order_read(session, *, order_id, account_id, receipt_rows=None) -> bool:
    """A failed/disagreeing existing read cannot reuse an earlier working proof."""
    changed = False
    rows = receipt_rows if receipt_rows is not None else session.scalars(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
    )).all()
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if payload.get("account_id") != str(account_id):
            continue
        raw_orders = payload.get("orders", {})
        orders = dict(raw_orders) if isinstance(raw_orders, dict) else {}
        if not isinstance(raw_orders, dict):
            row.payload = {**payload, "orders": {}}
            changed = True
        if str(order_id) in orders:
            orders.pop(str(order_id))
            row.payload = {**payload, "orders": orders}
            changed = True
    return changed


def record_order_read(session, *, order, account, report, wire_started_at,
                      wire_completed_at, cadence_seconds, broker_account_native_id=None,
                      receipt_rows=None) -> bool:
    """Persist separately from order timestamps, in caller's isolated transaction.

    Caller must not invoke this for cached reads without their original wire
    clock. Unknown/terminal evidence replaces an earlier positive receipt.
    """
    cadence = _normal_cadence(cadence_seconds)
    if (cadence is None or not _aware(wire_started_at)
            or not _aware(wire_completed_at) or wire_completed_at < wire_started_at
            or account.id != order.broker_account_id
            or account.provider not in {"schwab", "webull"}
            or not account.name.startswith("live:")):
        return False
    if account.provider == "webull":
        # list-primary can return a cached row. Its original acquired_at is the
        # only retained wire clock; the surrounding fetch await is not a GET.
        try:
            evidence = json.loads(report.metadata["_webull_list_evidence"])
            if not isinstance(evidence, dict):
                return invalidate_order_read(session, order_id=order.id, account_id=account.id, receipt_rows=receipt_rows)
            original = datetime.fromisoformat(evidence["acquired_at"])
            if (evidence["client_order_id"] != order.client_order_id
                    or not broker_account_native_id
                    or evidence.get("account_id") != broker_account_native_id
                    or evidence.get("truncated") is not False
                    or not _aware(original) or original > wire_completed_at):
                return invalidate_order_read(session, order_id=order.id, account_id=account.id, receipt_rows=receipt_rows)
            wire_started_at = min(original, wire_started_at)
        except (KeyError, TypeError, ValueError):
            return invalidate_order_read(session, order_id=order.id, account_id=account.id, receipt_rows=receipt_rows)
    try:
        quantity = Decimal(str(report.quantity))
        filled = Decimal(str(report.filled_quantity))
        bound = Decimal(str(order.quantity))
        valid_quantity = (all(q.is_finite() for q in (quantity, filled, bound))
                          and bound > 0 and 0 <= filled <= bound
                          and quantity == bound)
    except (InvalidOperation, ValueError, TypeError):
        valid_quantity = False
    exact = bool(
        order.broker_order_id and report.broker_order_id == order.broker_order_id
        and report.client_order_id == order.client_order_id
        and report.symbol == order.symbol and report.side == order.side
        and report.origin == "broker" and valid_quantity
    )
    proof = {
        "version": 1, "account_id": str(account.id), "account_name": account.name,
        "provider": account.provider, "order_id": str(order.id),
        "intent_id": str(order.intent_id), "strategy_id": str(order.strategy_id),
        "coid": order.client_order_id, "native_id": order.broker_order_id,
        "symbol": order.symbol, "side": order.side,
        "status": report.event_type if exact else "UNKNOWN",
        "working": exact and report.event_type in {"accepted", "partially_filled"},
        "quantity": str(quantity) if valid_quantity else None,
        "filled_quantity": str(filled) if valid_quantity else None,
        "wire_read_at": wire_started_at.isoformat(),
        "wire_completed_at": wire_completed_at.isoformat(), "cadence_seconds": cadence,
    }
    rows = receipt_rows if receipt_rows is not None else session.scalars(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
    )).all()
    matching = [row for row in rows if isinstance(row.payload, dict)
                and row.payload.get("account_id") == str(account.id)]
    # Duplicate storage is ambiguous, not a reason to choose a convenient proof.
    if len(matching) > 1:
        return False
    row = matching[0] if matching else None
    raw_orders = row.payload.get("orders", {}) if row else {}
    if not isinstance(raw_orders, dict):
        return False
    orders = dict(raw_orders)
    previous = orders.get(str(order.id))
    if previous is not None:
        try:
            previous_stamp = datetime.fromisoformat(previous["wire_read_at"])
            if not _aware(previous_stamp) or wire_started_at < previous_stamp:
                return False
            if (wire_started_at == previous_stamp and previous.get("working") is not True
                    and proof["working"]):
                return False
        except (KeyError, TypeError, ValueError):
            return False
    cutoff = wire_completed_at.timestamp() - 2 * cadence
    retained = {}
    for key, value in orders.items():
        if not isinstance(value, dict):
            continue
        try:
            stamp = datetime.fromisoformat(value["wire_read_at"])
            if _aware(stamp) and cutoff <= stamp.timestamp() <= wire_completed_at.timestamp():
                retained[key] = value
        except (KeyError, TypeError, ValueError):
            continue
    old = retained.get(str(order.id))
    if (old and old.get("working") == proof["working"] and old.get("status") == proof["status"]
            and old.get("pair") == proof.get("pair")):
        try:
            same_identity = all(old.get(key) == proof[key] for key in (
                "account_id", "account_name", "provider", "intent_id", "strategy_id",
                "coid", "native_id", "symbol", "side", "cadence_seconds",
                "quantity", "filled_quantity",
            ))
            age = (wire_started_at - datetime.fromisoformat(old["wire_read_at"])).total_seconds()
            if same_identity and 0 <= age < cadence:
                return False
        except (TypeError, ValueError):
            pass
    retained[str(order.id)] = proof
    retained = dict(sorted(retained.items(), key=lambda item: (
        item[1]["wire_read_at"], item[0]), reverse=True)[:MAX_ORDERS])
    payload = {"account_id": str(account.id), "orders": retained, "version": 1}
    if row is None:
        row = DashboardSnapshot(snapshot_type=SNAPSHOT_TYPE, payload=payload, created_at=wire_completed_at)
        session.add(row)
        if receipt_rows is not None:
            receipt_rows.append(row)
    else:
        row.payload = payload
        row.created_at = wire_completed_at
    return True


def fresh_working(proof, *, order, account, now, cadence_seconds) -> bool:
    """Identity/freshness only. Caller MUST separately prove lifetime/pair scope."""
    try:
        start = datetime.fromisoformat(proof["wire_read_at"])
        end = datetime.fromisoformat(proof["wire_completed_at"])
        cadence = _normal_cadence(cadence_seconds)
        quantity = Decimal(proof["quantity"])
        filled = Decimal(proof["filled_quantity"])
        return bool(
            _aware(now) and _aware(start) and _aware(end) and cadence is not None
            and proof["cadence_seconds"] == cadence
            and start <= end <= now and (now-start).total_seconds() <= 2*cadence
            and proof["version"] == 1 and proof["working"] is True
            and proof["status"] in {"accepted", "partially_filled"}
            and quantity.is_finite() and filled.is_finite()
            and quantity == order.quantity and 0 <= filled <= quantity
            and proof["account_id"] == str(account.id)
            and order.broker_account_id == account.id
            and proof["account_name"] == account.name and account.name.startswith("live:")
            and proof["provider"] == account.provider and account.provider in {"schwab", "webull"}
            and proof["order_id"] == str(order.id)
            and proof["intent_id"] == str(order.intent_id)
            and proof["strategy_id"] == str(order.strategy_id)
            and proof["coid"] == order.client_order_id
            and bool(order.broker_order_id) and proof["native_id"] == order.broker_order_id
            and proof["symbol"] == order.symbol and proof["side"] == order.side
            and getattr(order, "status", None) in {"pending", "submitted", "accepted", "partially_filled"}
        )
    except (KeyError, TypeError, ValueError, OverflowError, InvalidOperation):
        return False
