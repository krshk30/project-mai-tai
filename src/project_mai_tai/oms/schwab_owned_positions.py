"""Schwab fallback sells only readable, strategy-scoped live inventory."""

from decimal import Decimal

from sqlalchemy import or_, select

from project_mai_tai.db.models import BrokerAccount, OmsManagedPosition, Strategy, VirtualPosition


def own_open_quantity(session, *, strategy_id, account_id, symbol):
    strategy = session.get(Strategy, strategy_id, populate_existing=True)
    account = session.get(BrokerAccount, account_id, populate_existing=True)
    if strategy is None or account is None or account.provider != "schwab" or not account.is_active:
        raise ValueError("schwab_fallback_owner_binding_UNKNOWN")

    def quantity(value):
        try:
            amount = Decimal(str(value))
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise ValueError("schwab_fallback_owned_quantity_UNKNOWN") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError("schwab_fallback_owned_quantity_UNKNOWN")
        return amount

    virtual = session.scalars(select(VirtualPosition).where(
        VirtualPosition.strategy_id == strategy_id, VirtualPosition.broker_account_id == account_id,
        VirtualPosition.symbol == symbol).limit(2).execution_options(populate_existing=True)).all()
    managed = session.scalars(select(OmsManagedPosition).where(
        OmsManagedPosition.strategy_code == strategy.code, OmsManagedPosition.broker_account_name == account.name,
        OmsManagedPosition.symbol == symbol,
        or_(OmsManagedPosition.status.is_(None), OmsManagedPosition.status != "closed",
            OmsManagedPosition.current_quantity != 0)).limit(2).execution_options(populate_existing=True)).all()
    if len(virtual) > 1 or len(managed) > 1 or not virtual and not managed:
        raise ValueError("schwab_fallback_own_book_UNKNOWN")
    vp = quantity(virtual[0].quantity) if virtual else Decimal(0)
    ladder = Decimal(0)
    if managed:
        row = managed[0]
        ladder = quantity(row.current_quantity)
        if row.status != "open":
            raise ValueError("schwab_fallback_own_book_UNKNOWN")
    if vp and ladder and vp != ladder:
        raise ValueError("schwab_fallback_own_book_CHANGED")
    return max(vp, ladder)


def fallback_sell_quantity(session, *, strategy_id, account_id, symbol, requested, broker_available):
    try:
        requested, available = Decimal(str(requested)), Decimal(str(broker_available))
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise ValueError("schwab_fallback_ceiling_UNKNOWN") from exc
    if not requested.is_finite() or not available.is_finite() or requested <= 0 or available < 0:
        raise ValueError("schwab_fallback_ceiling_UNKNOWN")
    owned = own_open_quantity(session, strategy_id=strategy_id, account_id=account_id, symbol=symbol)
    return min(requested, available, owned)
