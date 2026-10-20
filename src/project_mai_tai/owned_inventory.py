"""Local ownership evidence, never broker-flatness or permission to sell surplus."""

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select

from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, Fill, OmsManagedPosition, Strategy, VirtualPosition,
)
from project_mai_tai.positions_read_receipt import load_receipts
from project_mai_tai.reconciliation.ownership import coherent_owned_quantity
from project_mai_tai.settings import Settings

ROW_LIMIT = 8192
# Existing positively verified flat checkpoint, not a rolling expiry of old holdings.
FILL_BALANCE_SINCE = Settings.model_fields["reconciliation_fill_balance_since"].default


def owned_position_present(session, *, symbol, accounts, strategy_id, now, max_age_ms=15_000):
    """Return own holdings; unreadable/incoherent evidence raises and must keep waiting.

    The independently covered strategy books determine ownership. Account surplus
    is non-owned for this consumer only; it is NOT a manual execution attribution.
    """
    def bounded(query):
        rows = session.execute(query.limit(ROW_LIMIT + 1)).all()
        if len(rows) > ROW_LIMIT:
            raise ValueError("owned_inventory_history_UNKNOWN")
        return rows

    def quantity(value):
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError("owned_inventory_quantity_UNKNOWN")
        return result

    if now.tzinfo is None or len(accounts) != 2 or len(set(accounts.values())) != 2:
        raise ValueError("owned_inventory_accounts_UNKNOWN")
    actual = session.scalars(select(BrokerAccount).where(BrokerAccount.id.in_(accounts))).all()
    if ({row.id: row.name for row in actual} != accounts
            or any(not row.is_active or not row.name.startswith("live:") for row in actual)):
        raise ValueError("owned_inventory_accounts_UNKNOWN")
    strategy = session.get(Strategy, strategy_id)
    if strategy is None or strategy.code != "schwab_1m_v2":
        raise ValueError("owned_inventory_strategy_UNKNOWN")
    names = {name: identity for identity, name in accounts.items()}
    virtual = defaultdict(dict)
    managed = defaultdict(lambda: defaultdict(lambda: Decimal(0)))
    fills = defaultdict(lambda: defaultdict(lambda: Decimal(0)))
    for position, code in bounded(select(VirtualPosition, Strategy.code).outerjoin(
            Strategy, Strategy.id == VirtualPosition.strategy_id).where(
            VirtualPosition.symbol == symbol, VirtualPosition.broker_account_id.in_(accounts))):
        if not code:
            raise ValueError("owned_inventory_strategy_UNKNOWN")
        amount = quantity(position.quantity)
        if position.strategy_id == strategy_id and amount:
            return True
        virtual[position.broker_account_id][code] = amount
    for (row,) in bounded(select(OmsManagedPosition).where(
            OmsManagedPosition.symbol == symbol, OmsManagedPosition.broker_account_name.in_(names),
            or_(OmsManagedPosition.status == "open", OmsManagedPosition.current_quantity != 0))):
        amount = quantity(row.current_quantity)
        if row.strategy_code == strategy.code and (row.status == "open" or amount):
            return True
        if not row.strategy_code or row.status != "open" and amount:
            raise ValueError("owned_inventory_managed_UNKNOWN")
        managed[names[row.broker_account_name]][row.strategy_code] += amount

    receipts = load_receipts(session)
    for name in names:
        receipt = receipts.get(name, {})
        read_at = receipt.get("read_at")
        if (receipt.get("writer") != "oms.sync_broker_positions" or not isinstance(read_at, datetime)
                or read_at.tzinfo is None or not 0 <= (now - read_at).total_seconds() * 1000 <= max_age_ms):
            raise ValueError("owned_inventory_account_read_UNKNOWN")
    raw = {identity: Decimal(0) for identity in accounts}
    for (position,) in bounded(select(AccountPosition).where(
            AccountPosition.symbol == symbol, AccountPosition.broker_account_id.in_(accounts))):
        raw[position.broker_account_id] = quantity(position.quantity)

    covered = defaultdict(lambda: Decimal(0))
    for fill, order, code in bounded(select(Fill, BrokerOrder, Strategy.code).outerjoin(
            BrokerOrder, BrokerOrder.id == Fill.order_id).outerjoin(
            Strategy, Strategy.id == Fill.strategy_id).where(
            Fill.symbol == symbol, Fill.broker_account_id.in_(accounts),
            Fill.filled_at >= FILL_BALANCE_SINCE)):
        amount = quantity(fill.quantity)
        if (not amount or not code or order is None or fill.strategy_id != order.strategy_id
                or fill.broker_account_id != order.broker_account_id or fill.symbol != order.symbol
                or fill.side not in {"buy", "sell"} or fill.side != order.side):
            raise ValueError("owned_inventory_fill_binding_UNKNOWN")
        fills[fill.broker_account_id][code] += amount if fill.side == "buy" else -amount
        covered[fill.order_id] += amount
    terminal = {"filled", "cancelled", "canceled", "rejected", "expired", "aborted", "replaced"}
    for (order,) in bounded(select(BrokerOrder).where(
            BrokerOrder.strategy_id == strategy_id, BrokerOrder.broker_account_id.in_(accounts),
            BrokerOrder.symbol == symbol, func.lower(BrokerOrder.side) == "buy",
            or_(BrokerOrder.submitted_at >= FILL_BALANCE_SINCE, BrokerOrder.submitted_at.is_(None),
                BrokerOrder.status.is_(None), BrokerOrder.status.notin_(terminal)))):
        if order.status not in terminal:
            return True  # No age limit can turn an old working/NULL BUY into absence.
        if order.status == "filled" and covered[order.id] != quantity(order.quantity):
            raise ValueError("owned_inventory_fill_coverage_UNKNOWN")

    for identity in accounts:
        total = coherent_owned_quantity(virtual=virtual[identity], managed=managed[identity],
                                       fills=fills[identity], tolerance=Decimal(0))
        own = coherent_owned_quantity(
            virtual={strategy.code: virtual[identity].get(strategy.code, Decimal(0))},
            managed={strategy.code: managed[identity].get(strategy.code, Decimal(0))},
            fills={strategy.code: fills[identity].get(strategy.code, Decimal(0))}, tolerance=Decimal(0))
        if total is None or own is None or total > raw[identity]:
            raise ValueError("owned_inventory_books_UNKNOWN")
        surplus = raw[identity] - total
        other_strategies = total - own
        non_bot = surplus + other_strategies
        if raw[identity] - non_bot != own:
            raise ValueError("owned_inventory_arithmetic_UNKNOWN")
        if own:
            return True
    return False
