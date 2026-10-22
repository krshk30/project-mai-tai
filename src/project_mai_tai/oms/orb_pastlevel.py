"""Durable no-wire proof for same-plan ORB continuation; no broker inference."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, TradeIntent
from project_mai_tai.strategy_core.orb_pastlevel import POLICY


def receipt(intent, plan_id: str, phase: str) -> None:
    intent.payload = {**dict(intent.payload or {}), "orb_pastlevel": {
        "policy": POLICY, "plan_id": plan_id, "phase": phase,
        "filled_quantity": "0" if phase == "local_zero" else "UNKNOWN",
    }}


def day_intents(session, intent, now: datetime):
    start = now.astimezone(ZoneInfo("America/New_York")).replace(
        hour=4, minute=0, second=0, microsecond=0).astimezone(UTC)
    return list(session.scalars(select(TradeIntent).where(
        TradeIntent.strategy_id == intent.strategy_id,
        TradeIntent.broker_account_id == intent.broker_account_id,
        TradeIntent.symbol == intent.symbol, TradeIntent.side == "buy",
        TradeIntent.created_at >= start, TradeIntent.id != intent.id,
    ).order_by(TradeIntent.created_at, TradeIntent.id)))


def unsent_plan(session, intent, now: datetime):
    """Only locally terminal zero-wire attempts may continue a single original plan.

    A historical status/rejection or absent working-parent lookup is not proof.
    Any broker order consumes the existing attempt budget, even if now cancelled.
    """
    priors = day_intents(session, intent, now)
    roots = [row for row in priors if row.intent_type == "open"
             and (row.payload or {}).get("orb_pastlevel", {}).get("plan_id") == str(row.id)]
    if len(roots) != 1:
        raise ValueError("orb_pastlevel_exact_original_plan_missing")
    root = roots[0]
    for row in priors:
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)  # SQLite fixture storage; production is timestamptz.
        if created > now:
            raise ValueError("orb_pastlevel_prior_clock_UNKNOWN")
        proof = (row.payload or {}).get("orb_pastlevel", {})
        if (row.status != "rejected" or proof.get("policy") != POLICY
                or proof.get("plan_id") != str(root.id)
                or proof.get("phase") != "local_zero" or proof.get("filled_quantity") != "0"
                or (row.payload or {}).get("refusal_origin") != "client_abort"):
            raise ValueError("orb_pastlevel_prior_exposure_UNKNOWN")
    if session.scalar(select(BrokerOrder.id).where(
        BrokerOrder.intent_id.in_([row.id for row in priors])).limit(1)) is not None:
        raise ValueError("orb_pastlevel_order_budget_consumed")
    return root
