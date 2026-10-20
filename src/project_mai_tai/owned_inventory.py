"""Local live-book ownership; never broker-flatness or authority to sell surplus."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_, select

from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, OmsManagedPosition, Strategy, TradeIntent, VirtualPosition,
)
from project_mai_tai.positions_read_receipt import load_receipts

ROW_LIMIT = 8192


def owned_position_present(session, *, symbol, accounts, strategy_id, now, max_age_ms=15_000):
    """Only this strategy's live books hold its local release gate.

    Historical fills and other strategies' books cannot resurrect inventory
    already cleared by the OMS. Native, cancellation and broker-flat proofs
    remain separate consumers; this helper cannot replace them.
    """
    def bounded(query):
        rows = session.scalars(query.limit(ROW_LIMIT + 1)).all()
        if len(rows) > ROW_LIMIT:
            raise ValueError("owned_inventory_history_UNKNOWN")
        return rows

    def quantity(value):
        try:
            result = Decimal(str(value))
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise ValueError("owned_inventory_quantity_UNKNOWN") from exc
        if not result.is_finite() or result < 0:
            raise ValueError("owned_inventory_quantity_UNKNOWN")
        return result

    if (now.tzinfo is None or len(accounts) not in {1, 2}
            or len(set(accounts.values())) != len(accounts)):
        raise ValueError("owned_inventory_accounts_UNKNOWN")
    actual = session.scalars(select(BrokerAccount).where(BrokerAccount.id.in_(accounts))).all()
    if ({row.id: row.name for row in actual} != accounts
            or any(not row.is_active or not row.name.startswith("live:") for row in actual)):
        raise ValueError("owned_inventory_accounts_UNKNOWN")
    strategy = session.get(Strategy, strategy_id)
    if strategy is None or strategy.code != "schwab_1m_v2":
        raise ValueError("owned_inventory_strategy_UNKNOWN")
    for position in bounded(select(VirtualPosition).where(
            VirtualPosition.strategy_id == strategy_id, VirtualPosition.symbol == symbol,
            VirtualPosition.broker_account_id.in_(accounts))):
        if quantity(position.quantity):
            return True
    for row in bounded(select(OmsManagedPosition).where(
            OmsManagedPosition.strategy_code == strategy.code, OmsManagedPosition.symbol == symbol,
            OmsManagedPosition.broker_account_name.in_(tuple(accounts.values())),
            or_(OmsManagedPosition.status.is_(None), OmsManagedPosition.status == "open",
                OmsManagedPosition.current_quantity != 0))):
        amount = quantity(row.current_quantity)
        if row.status not in {"open", "closed"} or row.status == "closed" and amount:
            raise ValueError("owned_inventory_managed_UNKNOWN")
        if row.status == "open":
            return True

    terminal = {"filled", "cancelled", "canceled", "rejected", "expired", "aborted", "replaced"}
    if bounded(select(BrokerOrder).where(
            BrokerOrder.strategy_id == strategy_id, BrokerOrder.broker_account_id.in_(accounts),
            BrokerOrder.symbol == symbol, BrokerOrder.side == "buy",
            or_(BrokerOrder.status.is_(None), BrokerOrder.status.notin_(terminal)))):
        return True
    if bounded(select(TradeIntent).where(
            TradeIntent.strategy_id == strategy_id, TradeIntent.broker_account_id.in_(accounts),
            TradeIntent.symbol == symbol, TradeIntent.side == "buy", TradeIntent.intent_type == "open",
            or_(TradeIntent.status.is_(None), TradeIntent.status.notin_(terminal | {"completed"})))):
        return True

    receipts = load_receipts(session)
    for name in accounts.values():
        receipt = receipts.get(name, {})
        read_at = receipt.get("read_at")
        if (receipt.get("writer") != "oms.sync_broker_positions" or not isinstance(read_at, datetime)
                or read_at.tzinfo is None or not 0 <= (now - read_at).total_seconds() * 1000 <= max_age_ms):
            raise ValueError("owned_inventory_account_read_UNKNOWN")
    return False
