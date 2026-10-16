"""C6 uses the existing OMS wire-read receipt, never broker tradeDate/as_of."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from project_mai_tai.db.models import DashboardSnapshot, VirtualPosition
from project_mai_tai.oms.bot_control import ControlConflict
from project_mai_tai.positions_read_receipt import SNAPSHOT_TYPE


def positions_after_protection(session, *, account_name: str, terminal_at: datetime, now: datetime):
    if terminal_at.tzinfo is None or now.tzinfo is None or terminal_at > now:
        raise ControlConflict("protection_terminal_time_unknown")
    row = session.scalar(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
        DashboardSnapshot.payload["broker_account_name"].as_string() == account_name,
    ).order_by(DashboardSnapshot.created_at.desc()).limit(1))
    payload = row.payload if row is not None else None
    if (not isinstance(payload, dict) or payload.get("writer") != "oms.sync_broker_positions"
            or payload.get("broker_account_name") != account_name
            or type(payload.get("position_count")) is not int or payload["position_count"] < 0):
        raise ControlConflict("positions_wire_receipt_unknown")
    try:
        read_at = datetime.fromisoformat(payload["read_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ControlConflict("positions_wire_receipt_unknown") from exc
    if read_at.tzinfo is None or not 0 <= (now - read_at).total_seconds() <= 30:
        raise ControlConflict("positions_wire_receipt_not_fresh")
    if read_at <= terminal_at:
        raise ControlConflict("positions_wire_receipt_predates_release")
    return read_at


def owned_exit_ceiling(session, *, strategy_id, account_id, symbol, remaining, broker_total):
    if any(not isinstance(value, Decimal) or not value.is_finite() or value < 0
           for value in (remaining, broker_total)):
        raise ControlConflict("owned_or_broker_quantity_unknown")
    positions = session.scalars(select(VirtualPosition).where(
        VirtualPosition.strategy_id == strategy_id, VirtualPosition.broker_account_id == account_id,
        VirtualPosition.symbol == symbol,
    ).limit(2)).all()
    if (len(positions) != 1 or not positions[0].quantity.is_finite()
            or positions[0].quantity < 0):
        raise ControlConflict("strategy_owned_vp_unknown")
    return min(remaining, positions[0].quantity, broker_total)
