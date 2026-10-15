"""Webull account holdings are a ceiling, never evidence of strategy ownership."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from project_mai_tai.db.models import BrokerAccount, OmsManagedPosition, Strategy, VirtualPosition


def bot_owned_quantity(session: Session, *, account_name: str, symbol: str) -> Decimal:
    # Do not net opposing strategies or double-count the same holding in both
    # ledgers. An open managed row backs ownership when virtual quantity is zero.
    quantities = session.scalars(
        select(VirtualPosition.quantity)
        .join(BrokerAccount, BrokerAccount.id == VirtualPosition.broker_account_id)
        .where(BrokerAccount.name == account_name, VirtualPosition.symbol == symbol,
               VirtualPosition.quantity != 0)
    )
    virtual_quantity = sum((abs(quantity) for quantity in quantities), Decimal(0))
    managed_quantities = session.scalars(select(OmsManagedPosition.current_quantity).where(
        OmsManagedPosition.broker_account_name == account_name,
        OmsManagedPosition.symbol == symbol,
        OmsManagedPosition.status == "open",
    ))
    managed_quantity = sum((abs(quantity) for quantity in managed_quantities), Decimal(0))
    return max(virtual_quantity, managed_quantity)


def fallback_sell_quantity(
    session: Session, *, strategy_id: UUID, account_id: UUID, symbol: str,
    requested: Decimal, broker_available: Decimal,
) -> Decimal:
    virtual_quantities = session.scalars(select(VirtualPosition.quantity).where(
        VirtualPosition.strategy_id == strategy_id,
        VirtualPosition.broker_account_id == account_id,
        VirtualPosition.symbol == symbol,
    ))
    virtual_quantity = sum(virtual_quantities, Decimal(0))
    managed_quantities = session.scalars(
        select(OmsManagedPosition.current_quantity)
        .join(Strategy, Strategy.code == OmsManagedPosition.strategy_code)
        .join(BrokerAccount, BrokerAccount.name == OmsManagedPosition.broker_account_name)
        .where(
            Strategy.id == strategy_id,
            BrokerAccount.id == account_id,
            OmsManagedPosition.symbol == symbol,
            OmsManagedPosition.status == "open",
        )
    )
    managed_quantity = sum(managed_quantities, Decimal(0))
    owned = max(virtual_quantity, managed_quantity)
    return max(Decimal(0), min(requested, broker_available, owned))
