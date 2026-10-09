"""ALERTS1 A1: close exposure incidents once the symbol is flat at both live brokers.

Two incident kinds are opened elsewhere and were never closed by anything:

* ``schwab_opening_policy_reject`` (OMS ``_record_broker_rejection``): "SCHWAB OPEN REFUSED:
  <SYM> on live:schwab_1m_v2; check Webull-only exposure".
* ``orb_schwab_exit_evidence`` (``orb_schwab_exits.save_context``): "ORB strategy-exit evidence
  unavailable: <SYM>".

Both ask a human to check exposure on one symbol. Once that symbol has NO broker position, NO
live-book row (virtual or OMS-managed), NO working order and NO active intent on any of the live
accounts below, there is nothing left to check and the incident is closed with a recorded reason.
Any exposure keeps it open. Unknown order/intent statuses count as working (fail open = page).

Runs inside the reconciler cycle only (never the OMS order/tick path); reads are bounded by
``MAX_INCIDENTS_PER_CYCLE`` and one indexed query per table per incident.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    OmsManagedPosition,
    SystemIncident,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.strategy_core.time_utils import session_day_eastern_str

AUTO_RESOLVE_SOURCES = ("schwab_opening_policy_reject", "orb_schwab_exit_evidence")
AUTO_RESOLVE_SERVICES = ("oms-risk", "orb-schwab")
# The exposure this incident warns about lives on these two live accounts (Schwab + Webull).
EXPOSURE_ACCOUNT_NAMES = ("live:schwab_1m_v2", "live:orb")
ACTIVE_INCIDENT_STATUSES = ("open", "acknowledged")
TERMINAL_ORDER_STATUSES = ("filled", "cancelled", "canceled", "rejected", "aborted", "expired")
TERMINAL_INTENT_STATUSES = ("filled", "cancelled", "canceled", "rejected", "aborted", "expired")
# A same-day incident is closed only after this age, so the Webull leg that follows a Schwab
# refusal has time to be persisted as a working order before "flat" is judged.
SAME_DAY_MIN_AGE = timedelta(minutes=30)
MAX_INCIDENTS_PER_CYCLE = 100
RESOLUTION_REASON = "auto_resolved_flat_both_brokers"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def symbol_exposure(session: Session, symbol: str) -> dict[str, Any]:
    """Return every source of exposure for ``symbol`` across the live exposure accounts."""
    accounts = session.execute(
        select(BrokerAccount.id, BrokerAccount.name).where(
            BrokerAccount.name.in_(EXPOSURE_ACCOUNT_NAMES)
        )
    ).all()
    account_ids = tuple(account_id for account_id, _ in accounts)
    name_by_id = {account_id: name for account_id, name in accounts}
    exposure: dict[str, Any] = {
        "accounts_found": sorted(name_by_id.values()),
        "broker_positions": [],
        "virtual_positions": [],
        "managed_positions": [],
        "working_orders": [],
        "active_intents": [],
    }
    if account_ids:
        for account_id, quantity in session.execute(
            select(AccountPosition.broker_account_id, AccountPosition.quantity).where(
                AccountPosition.broker_account_id.in_(account_ids),
                AccountPosition.symbol == symbol,
                AccountPosition.quantity != 0,
            )
        ).all():
            exposure["broker_positions"].append(
                {"account": name_by_id[account_id], "quantity": str(quantity)}
            )
        for account_id, quantity in session.execute(
            select(VirtualPosition.broker_account_id, func.sum(VirtualPosition.quantity))
            .where(
                VirtualPosition.broker_account_id.in_(account_ids),
                VirtualPosition.symbol == symbol,
                VirtualPosition.quantity != 0,
            )
            .group_by(VirtualPosition.broker_account_id)
        ).all():
            exposure["virtual_positions"].append(
                {"account": name_by_id[account_id], "quantity": str(quantity)}
            )
        for row in session.execute(
            select(
                BrokerOrder.broker_account_id,
                BrokerOrder.client_order_id,
                BrokerOrder.side,
                BrokerOrder.status,
            )
            .where(
                BrokerOrder.broker_account_id.in_(account_ids),
                BrokerOrder.symbol == symbol,
                BrokerOrder.status.not_in(TERMINAL_ORDER_STATUSES),
            )
            .limit(20)
        ).all():
            exposure["working_orders"].append(
                {
                    "account": name_by_id[row[0]],
                    "client_order_id": row[1],
                    "side": row[2],
                    "status": row[3],
                }
            )
        for account_id, intent_type, status in session.execute(
            select(TradeIntent.broker_account_id, TradeIntent.intent_type, TradeIntent.status)
            .where(
                TradeIntent.broker_account_id.in_(account_ids),
                TradeIntent.symbol == symbol,
                TradeIntent.status.not_in(TERMINAL_INTENT_STATUSES),
            )
            .limit(20)
        ).all():
            exposure["active_intents"].append(
                {"account": name_by_id[account_id], "intent_type": intent_type, "status": status}
            )
    for account_name, quantity in session.execute(
        select(OmsManagedPosition.broker_account_name, OmsManagedPosition.current_quantity).where(
            OmsManagedPosition.broker_account_name.in_(EXPOSURE_ACCOUNT_NAMES),
            OmsManagedPosition.symbol == symbol,
            OmsManagedPosition.status == "open",
        )
    ).all():
        exposure["managed_positions"].append({"account": account_name, "quantity": str(quantity)})
    exposure["flat"] = (
        len(exposure["accounts_found"]) == len(EXPOSURE_ACCOUNT_NAMES)
        and not exposure["broker_positions"]
        and not exposure["virtual_positions"]
        and not exposure["managed_positions"]
        and not exposure["working_orders"]
        and not exposure["active_intents"]
    )
    return exposure


def _old_enough(incident: SystemIncident, now: datetime) -> bool:
    payload = incident.payload or {}
    session_date = payload.get("session_date")
    if isinstance(session_date, str) and session_date < session_day_eastern_str(now):
        return True
    opened_at = _aware(incident.opened_at)
    return opened_at is not None and now - opened_at >= SAME_DAY_MIN_AGE


def candidate_incidents(session: Session, *, limit: int = MAX_INCIDENTS_PER_CYCLE):
    return session.scalars(
        select(SystemIncident)
        .where(
            SystemIncident.service_name.in_(AUTO_RESOLVE_SERVICES),
            SystemIncident.status.in_(ACTIVE_INCIDENT_STATUSES),
            SystemIncident.payload["source"].as_string().in_(AUTO_RESOLVE_SOURCES),
        )
        .order_by(SystemIncident.opened_at)
        .limit(limit)
    ).all()


def resolve_flat_exposure_incidents(session: Session, *, now: datetime) -> list[str]:
    """Close each eligible incident whose symbol is flat; return the closed incident ids."""
    resolved: list[str] = []
    exposure_by_symbol: dict[str, dict[str, Any]] = {}
    for incident in candidate_incidents(session):
        payload = dict(incident.payload or {})
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol or not _old_enough(incident, now):
            continue
        if symbol not in exposure_by_symbol:
            exposure_by_symbol[symbol] = symbol_exposure(session, symbol)
        exposure = exposure_by_symbol[symbol]
        if not exposure["flat"]:
            continue
        payload["resolution"] = {
            "reason": RESOLUTION_REASON,
            "resolved_by": "reconciler",
            "resolved_at": now.isoformat(),
            "checked_accounts": list(EXPOSURE_ACCOUNT_NAMES),
        }
        incident.payload = payload
        incident.status = "closed"
        incident.closed_at = now
        resolved.append(str(incident.id))
    return resolved
