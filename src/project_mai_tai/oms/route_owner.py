"""Known shared-account owners; SQL only, used inside the existing admission lock."""
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from decimal import Decimal

from project_mai_tai.db.models import (
    AccountPosition, BrokerOrder, OmsManagedPosition, Strategy, TradeIntent, VirtualPosition,
)
from project_mai_tai.fanout_segment_store import current_session_anchor

logger = logging.getLogger(__name__)


def _utc_row_time(value):
    if value is None:
        return None
    # SQLite drops the timezone from UTC DateTime columns; PostgreSQL retains it.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _stale_row(anchor, *, now, session_start, account, symbol, owner, row_id):
    if anchor is None:
        logger.warning(
            "[ROUTE-OWNER-UNANCHORED-ROW] account=%s symbol=%s owner=%s row_id=%s refusal_kept=1",
            account, symbol, owner, row_id,
        )
        return False
    if anchor >= session_start:
        return False
    logger.info(
        "[ROUTE-OWNER-STALE-ROW] account=%s symbol=%s owner=%s row_id=%s age_s=%.3f session_start=%s",
        account, symbol, owner, row_id, (now - anchor).total_seconds(), session_start.isoformat(),
    )
    return True


def route_owner_conflict(session, *, account_id, account_name, symbol, incoming_strategy, now=None):
    """Return a proven foreign strategy, never release/cancel it or borrow its lot."""
    ticker = symbol.upper()
    held = session.scalar(select(Strategy.code).join(
        VirtualPosition, VirtualPosition.strategy_id == Strategy.id).where(
        VirtualPosition.broker_account_id == account_id, VirtualPosition.symbol == ticker,
        VirtualPosition.quantity != 0, Strategy.code != incoming_strategy).limit(1))
    if held is not None:
        return held
    net = session.scalar(select(AccountPosition.quantity).where(
        AccountPosition.broker_account_id == account_id, AccountPosition.symbol == ticker))
    own = session.scalar(select(VirtualPosition.quantity).join(
        Strategy, Strategy.id == VirtualPosition.strategy_id).where(
        VirtualPosition.broker_account_id == account_id, VirtualPosition.symbol == ticker,
        Strategy.code == incoming_strategy))
    if net is not None and net != (own if own is not None else Decimal(0)):
        logger.info(
            "[ROUTE-OWNER-MANUAL-PRESENT] account=%s symbol=%s incoming=%s net=%s own_vp=%s log_only=1",
            account_name, ticker, incoming_strategy, net, own if own is not None else Decimal(0),
        )
    managed = session.scalar(select(OmsManagedPosition.strategy_code).where(
        OmsManagedPosition.broker_account_name == account_name,
        OmsManagedPosition.symbol == ticker, OmsManagedPosition.status == "open",
        OmsManagedPosition.current_quantity != 0,
        OmsManagedPosition.strategy_code != incoming_strategy).limit(1))
    if managed is not None:
        return managed
    clock = now if now is not None else datetime.now(UTC)
    session_start = current_session_anchor(clock)
    working = session.execute(select(Strategy.code, BrokerOrder, TradeIntent).select_from(Strategy).join(
        BrokerOrder, BrokerOrder.strategy_id == Strategy.id).where(
        BrokerOrder.broker_account_id == account_id, BrokerOrder.symbol == ticker,
        Strategy.code != incoming_strategy,
        BrokerOrder.status.not_in(("filled", "cancelled", "canceled", "rejected", "expired", "aborted")),
    ).outerjoin(TradeIntent, TradeIntent.id == BrokerOrder.intent_id).order_by(BrokerOrder.id)).all()
    for owner, order, parent in working:
        anchor = _utc_row_time(order.submitted_at)
        if (anchor is None and parent is not None and parent.strategy_id == order.strategy_id
                and parent.broker_account_id == account_id and parent.symbol == ticker):
            # Explicit 06:2x provenance rule for schemas without order created_at.
            anchor = _utc_row_time(parent.created_at)
        if not _stale_row(anchor, now=clock, session_start=session_start,
                          account=account_name, symbol=ticker, owner=owner, row_id=order.id):
            return owner
    # MOMLIVE's durable reservation precedes creation of a broker-order row.
    reservations = session.execute(select(Strategy.code, TradeIntent).select_from(Strategy).join(
        TradeIntent, TradeIntent.strategy_id == Strategy.id).where(
        TradeIntent.broker_account_id == account_id, TradeIntent.symbol == ticker,
        Strategy.code != incoming_strategy, TradeIntent.side == "buy",
        TradeIntent.intent_type.in_(("open", "scale")),
        TradeIntent.status.not_in(("filled", "cancelled", "canceled", "rejected", "expired", "aborted", "completed", "superseded")),
    ).order_by(TradeIntent.id)).all()
    for owner, intent in reservations:
        if not _stale_row(_utc_row_time(intent.created_at), now=clock, session_start=session_start,
                          account=account_name, symbol=ticker, owner=owner, row_id=intent.id):
            return owner
    return None
