"""Durable ORB no-wire proofs and guarded local settlement; no broker inference."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill, Strategy, TradeIntent
from project_mai_tai.strategy_core.orb_pastlevel import POLICY


def settle_prepared_webull_refusal(session, *, intent, request, prepared_order_id, control_store, reason):
    """Only this invocation's new, locked, permit-free order may become aborted.

    The caller has not entered submit_order. That fact alone cannot clear a row
    from an earlier invocation, or one whose durable dispatch evidence changed.
    The same state->order lock order as authorize_entry serializes the proof.
    The caller settles the intent and commits both changes in this transaction.
    """
    if request.strategy_code != "orb_schwab" or request.side != "buy" or request.intent_type != "open":
        raise ValueError("orb_pastlevel_cleanup_scope_UNKNOWN")
    if control_store is not None:
        control_store._lock(session, intent.strategy_id)
    order = session.scalar(select(BrokerOrder).where(
        BrokerOrder.client_order_id == request.client_order_id,
    ).with_for_update().execution_options(populate_existing=True))
    session.refresh(intent, with_for_update=True)
    if (order is None and prepared_order_id is None and intent.status == "pending"
            and "bot_control_permit" not in request.metadata):
        return  # No bot-control preparation and no durable order to terminalize.
    account = session.get(BrokerAccount, intent.broker_account_id)
    strategy = session.get(Strategy, intent.strategy_id)
    payload = dict(order.payload or {}) if order is not None else {}
    prepared_at = payload.get("bot_control_prepared_at")
    if (order is None or control_store is None or prepared_order_id != order.id
            or intent.status != "pending" or intent.intent_type != "open" or intent.side != "buy"
            or intent.symbol != request.symbol or intent.quantity != request.quantity
            or account is None or account.provider != "webull" or account.name != request.broker_account_name
            or strategy is None or strategy.code != request.strategy_code or strategy.execution_mode != "live"
            or order.intent_id != intent.id or order.strategy_id != intent.strategy_id
            or order.broker_account_id != intent.broker_account_id or order.symbol != request.symbol
            or order.client_order_id != request.client_order_id or order.side != request.side
            or order.quantity != request.quantity or order.order_type != request.order_type
            or order.time_in_force != request.time_in_force or order.status != "pending"
            or order.broker_order_id is not None or order.submitted_at is not None
            or "bot_control_permit" in payload or "bot_control_permit" in request.metadata
            or "bot_control_generation" not in payload or not isinstance(prepared_at, str) or not prepared_at
            or payload != {**request.metadata, "bot_control_prepared_at": prepared_at}
            or session.scalar(select(Fill.id).where(Fill.order_id == order.id).limit(1)) is not None
            or session.scalar(select(BrokerOrderEvent.id).where(
                BrokerOrderEvent.order_id == order.id).limit(1)) is not None):
        raise ValueError("orb_pastlevel_prepared_dispatch_UNKNOWN")
    order.status = "aborted"
    order.payload = {**payload, "reject_reason": reason, "orb_pastlevel_local_no_wire": {
        "proof": "current_call_created_order_final_fence_before_submit_under_permit_lock",
        "intent_id": str(intent.id), "client_order_id": request.client_order_id,
        "prepared_at": prepared_at, "reason": reason,
    }}


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
