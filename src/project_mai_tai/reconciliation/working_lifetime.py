"""TOP171: alert-only lifetime of exact, actively owned broker work."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from project_mai_tai.db.models import DashboardSnapshot, OmsManagedPosition, Strategy, TradeIntent
from project_mai_tai.reconciliation.order_read_evidence import SNAPSHOT_TYPE, fresh_working
from project_mai_tai.strategy_core.order_routing import extended_hours_session
from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS

ET = ZoneInfo("America/New_York")
ACTIVE = {"pending", "submitted", "accepted", "partially_filled"}


def recorded_session_matches(order, now):
    metadata = order.payload if isinstance(order.payload, dict) else {}
    tokens = []
    for key in ("session", "market_session"):
        if key in metadata:
            value = str(metadata[key]).upper()
            value = {"RTH": "NORMAL", "PRE": "AM", "POST": "PM"}.get(value, value)
            if value not in {"NORMAL", "AM", "PM"}:
                return False
            tokens.append(value)
    if not tokens or len(set(tokens)) != 1 or now.tzinfo is None:
        return False
    if now.astimezone(ET).weekday() >= 5 or now.astimezone(ET).date() in US_MARKET_HOLIDAYS:
        return False
    current = extended_hours_session(now) or "NORMAL"
    if tokens[0] != current:
        return False
    extended = str(metadata.get("extended_hours", "")).lower()
    if (current == "NORMAL" and extended in {"true", "1", "yes"}
            or current != "NORMAL" and extended in {"false", "0", "no"}):
        return False
    # DAY means THIS recorded trading date, not an invented fixed expiry hour.
    submitted = order.submitted_at
    if str(order.time_in_force).lower() != "day" or not isinstance(submitted, datetime):
        return False
    # SQLAlchemy SQLite returns naive UTC for the timestamp column.
    if submitted.tzinfo is None:
        from datetime import UTC
        submitted = submitted.replace(tzinfo=UTC)
    return submitted <= now and submitted.astimezone(ET).date() == now.astimezone(ET).date()


def _parent_matches(order, intent):
    return bool(intent is not None and intent.id == order.intent_id
        and isinstance(intent.payload, dict)
        and intent.strategy_id == order.strategy_id and intent.broker_account_id == order.broker_account_id
        and intent.symbol == order.symbol and intent.side == order.side
        and intent.status in ACTIVE | {"filled"})


def live_working_ids(session, orders, accounts, *, now, cadence_seconds):
    """No side effects. Unknown shape/session/owner/partial accounting stays alert."""
    receipts = {}
    duplicates = set()
    for row in session.scalars(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
    )).all():
        data = row.payload if isinstance(row.payload, dict) else {}
        key = data.get("account_id")
        if key in receipts:
            duplicates.add(key)
        receipts[key] = data.get("orders")
    live = set()
    from project_mai_tai.reconciliation.order_read_evidence import _normal_cadence
    cadence = _normal_cadence(cadence_seconds)
    if cadence is None:
        return live
    for order in orders:
        account = accounts.get(order.broker_account_id)
        values = receipts.get(str(order.broker_account_id))
        if account is None or str(account.id) in duplicates or not isinstance(values, dict):
            continue
        proof = values.get(str(order.id))
        if (not isinstance(proof, dict) or not fresh_working(proof, order=order, account=account,
                now=now, cadence_seconds=cadence_seconds) or not recorded_session_matches(order, now)):
            continue
        parent = session.get(TradeIntent, order.intent_id) if order.intent_id else None
        strategy = session.get(Strategy, order.strategy_id)
        if not _parent_matches(order, parent) or strategy is None or strategy.execution_mode != "live":
            continue
        metadata = order.payload if isinstance(order.payload, dict) else {}
        parent_metadata = (parent.payload or {}).get("metadata", {})
        if not isinstance(parent_metadata, dict):
            continue
        regular_resting = (str(order.order_type).upper() in {"STOP", "STOP_LIMIT"}
            and (str(metadata.get("resting_entry", "")).lower() == "true"
                 or metadata.get("fanout_source") == "rth_resting_mirror"))
        eh_resting = (str(order.order_type).upper() == "LIMIT"
            and str(metadata.get("oms_v2_eh_resting_entry", "")).lower() == "true"
            and extended_hours_session(now) in {"AM", "PM"})
        resting = (order.side == "buy" and parent.intent_type == "open" and parent.status in ACTIVE
            and (regular_resting or eh_resting)
            and all(parent_metadata.get(key) == metadata.get(key) for key in (
                "resting_entry", "fanout_source", "oms_v2_eh_resting_entry")))
        if resting:
            rows = session.scalars(select(OmsManagedPosition).where(
                OmsManagedPosition.entry_order_id == order.id,
            )).all()
            if any(row.status != "open" or row.current_quantity <= 0
                    or row.strategy_code != strategy.code or row.broker_account_name != account.name
                    or row.symbol != order.symbol for row in rows):
                continue
            live.add(order.id)
    return live
