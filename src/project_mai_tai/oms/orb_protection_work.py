"""Set-based ORB work selection, retaining historical and unrouted supersession."""

from sqlalchemy import and_, func, or_, select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, Strategy, VirtualPosition


UNRESOLVED_PHASES = {"submitting", "accepted", "working", "unknown"}


def routed_buys(account_name):
    return select(
        BrokerOrder.id, BrokerOrder.strategy_id, BrokerOrder.broker_account_id,
        BrokerOrder.symbol, BrokerOrder.submitted_at,
        BrokerOrder.payload["orbroute1_protection"]["phase"].as_string().label("phase"),
    ).join(Strategy).join(
        BrokerAccount, BrokerAccount.id == BrokerOrder.broker_account_id,
    ).where(
        Strategy.code == "orb_schwab", BrokerAccount.name == account_name,
        BrokerOrder.side == "buy",
        BrokerOrder.payload["orb_country_route"].as_string() == "true",
    )


def protection_work_query(account_name):
    candidates = routed_buys(account_name).cte("orb_routed_candidates")
    keys = select(
        candidates.c.strategy_id, candidates.c.broker_account_id, candidates.c.symbol,
    ).distinct().cte("orb_candidate_keys")
    # The ranking universe includes UNROUTED newer BUYs, just like the old EXISTS.
    latest = select(
        BrokerOrder.id,
        func.row_number().over(
            partition_by=(BrokerOrder.strategy_id, BrokerOrder.broker_account_id, BrokerOrder.symbol),
            order_by=(BrokerOrder.submitted_at.desc(), BrokerOrder.id.desc()),
        ).label("rank"),
    ).join(keys, and_(
        BrokerOrder.strategy_id == keys.c.strategy_id,
        BrokerOrder.broker_account_id == keys.c.broker_account_id,
        BrokerOrder.symbol == keys.c.symbol,
    )).where(
        BrokerOrder.side == "buy", BrokerOrder.submitted_at.is_not(None),
    ).cte("orb_latest_dated_buys")
    positive_fills = select(Fill.order_id).join(
        candidates, candidates.c.id == Fill.order_id,
    ).where(Fill.side == "buy", Fill.quantity > 0).distinct().cte("orb_positive_buy_fills")
    held = or_(
        VirtualPosition.quantity > 0,
        and_(VirtualPosition.quantity.is_(None), positive_fills.c.order_id.is_not(None)),
    )
    # NULL timestamps were never superseded by SQL comparisons in the old query.
    newest = or_(candidates.c.submitted_at.is_(None), latest.c.id.is_not(None))
    return select(candidates.c.id).outerjoin(
        latest, and_(latest.c.id == candidates.c.id, latest.c.rank == 1),
    ).outerjoin(VirtualPosition, and_(
        VirtualPosition.strategy_id == candidates.c.strategy_id,
        VirtualPosition.broker_account_id == candidates.c.broker_account_id,
        VirtualPosition.symbol == candidates.c.symbol,
    )).outerjoin(
        positive_fills, positive_fills.c.order_id == candidates.c.id,
    ).where(or_(candidates.c.phase.in_(UNRESOLVED_PHASES), and_(newest, held))).distinct()


def protection_guard_query(account_name):
    return routed_buys(account_name).outerjoin(VirtualPosition, and_(
        VirtualPosition.strategy_id == BrokerOrder.strategy_id,
        VirtualPosition.broker_account_id == BrokerOrder.broker_account_id,
        VirtualPosition.symbol == BrokerOrder.symbol,
    )).with_only_columns(BrokerOrder.id).where(or_(
        VirtualPosition.quantity.is_(None), VirtualPosition.quantity != 0,
        BrokerOrder.payload["orbroute1_protection"]["phase"].as_string().in_(UNRESOLVED_PHASES),
    )).limit(1)


def load_protection_work(session, account_name):
    # Missing VP stays UNKNOWN here; the full selector proves its fill fallback.
    if session.scalar(protection_guard_query(account_name)) is None:
        return []
    return session.scalars(protection_work_query(account_name)).all()
