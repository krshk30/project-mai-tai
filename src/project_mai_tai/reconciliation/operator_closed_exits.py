"""Entry-specific, reviewed operator closes: notification policy, NEVER ledger repair.

Flat books/VIRTUAL-CLEAR alone cannot distinguish a manual close from a missing
bot exit. Only an explicit reviewed ruling supplies that missing positive fact.
Future rulings must bind the native entry and retain both flat/clear evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, Fill, OmsManagedPosition,
    SystemIncident, TradeIntent, VirtualPosition,
)


@dataclass(frozen=True)
class ReviewedIncident:
    row_id: str
    service: str
    opened_at: datetime
    payload: dict


@dataclass(frozen=True)
class ReviewedOperatorClose:
    allowance_id: str
    account: str
    symbol: str
    native_entry_id: str
    quantity: Decimal
    price: Decimal
    entry_second: datetime
    virtual_clear_at: datetime
    broker_flat_proof: str
    operator_ruling: str
    managed_row_id: str = ""
    incident_minutes: tuple[datetime, ...] = ()
    incident_bindings: tuple[ReviewedIncident, ...] = ()


REVIEWED_CLOSES = (
    ReviewedOperatorClose(
        allowance_id="ALERTS1-SAIQ-OPERATOR-CLOSED-20261022", account="live:orb", symbol="SAIQ",
        native_entry_id="7HG0RUDTB5R0DFMUFRELAST5G8", quantity=Decimal("39"), price=Decimal("7.62"),
        entry_second=datetime(2026, 10, 22, 15, 22, 4, tzinfo=UTC),
        virtual_clear_at=datetime(2026, 10, 22, 15, 28, 36, tzinfo=UTC),
        broker_flat_proof="Claude/operator14:59:39: operator closed Webull by hand; broker flat; OMS VIRTUAL-CLEAR11:28:36ET",
        operator_ruling="Claude LATEST2026-10-22T14:59:39ET explicit manual-close allowance, not a broker exit fill",
        managed_row_id="6e641bb1-b7c2-4a3c-b41f-2cfcb0ba940b",
        incident_minutes=tuple(datetime(2026, 10, 22, 15, minute, tzinfo=UTC) for minute in (22, 28, 35)),
        # Original row_to_json packet reviewed by Claude15:31:49; no inferred
        # order-id mapping for reconciler's deliberately aggregate payload.
        incident_bindings=(
            ReviewedIncident("463793e7-cda3-4801-821f-5b486dd4fd30", "oms-risk",
                datetime(2026, 10, 22, 15, 22, 35, 761580, tzinfo=UTC),
                {"source": "oms_v2_webull_uncovered_share", "broker_account_name": "live:orb",
                 "symbol": "SAIQ", "managed_row_id": "6e641bb1-b7c2-4a3c-b41f-2cfcb0ba940b",
                 "cause": "never_protected", "uncovered_seconds": 30.0}),
            ReviewedIncident("67fcff97-864b-4606-94fd-3fb9ca16c290", "reconciler",
                datetime(2026, 10, 22, 15, 28, 46, 793286, tzinfo=UTC),
                {"fingerprint": "position-quantity:live:orb:SAIQ", "finding_type": "position_quantity_mismatch",
                 "symbol": "SAIQ", "account_name": "live:orb", "account_quantity": "0",
                 "virtual_quantity": "0", "managed_quantity": "0", "our_quantity": "0",
                 "quantity_delta": "0", "net_fill_balance": "39.00000000", "fill_delta": "39.00000000",
                 "direction": "broker_missing_owned_position", "ownership": "ours_or_conflicting",
                 "operator_allowance": None, "configured_entry_quantity": None,
                 "configured_entry_notional_usd": 300, "strategy_codes": ["schwab_1m_v2"]}),
            ReviewedIncident("d2711858-e7ef-43b9-a8ce-15039ddb5b8e", "oms-risk",
                datetime(2026, 10, 22, 15, 35, 4, 980607, tzinfo=UTC),
                {"source": "wbattach1", "account": "live:orb", "symbol": "SAIQ",
                 "entry_id": "065aa1dc-aade-4abc-acc9-93aff27dc0a5",
                 "entry_client_order_id": "schwab_1m_v2-SAIQ-open-2a3bc04ec248",
                 "entry_identity": "canonical", "entry_reference": "schwab_1m_v2-SAIQ-open-2a3bc04ec248",
                 "reason": "broker flat before software exit; execution ledger reconciliation required",
                 "sell": "BLOCKED"}),
        ),
    ),
)


def utc(value):
    # Existing DB columns are UTC; SQLite fixtures drop tzinfo.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def match_ruling(fill, order, account, *, now, rulings=REVIEWED_CLOSES):
    if (fill.broker_account_id != order.broker_account_id or fill.broker_account_id != account.id
            or fill.strategy_id != order.strategy_id):
        return None
    for ruling in rulings:
        if not ruling.operator_ruling or not ruling.broker_flat_proof or not ruling.native_entry_id:
            continue
        if (ruling.virtual_clear_at <= ruling.entry_second or utc(now) < ruling.virtual_clear_at):
            continue
        if (account.name == ruling.account and account.provider == "webull"
                and fill.side == "buy" and order.side == "buy"
                and fill.symbol == order.symbol == ruling.symbol
                and order.broker_order_id == ruling.native_entry_id
                and fill.quantity == ruling.quantity and fill.price == ruling.price
                and ruling.entry_second <= utc(fill.filled_at) < ruling.entry_second + timedelta(seconds=1)):
            return ruling
    return None


def bot_sell_or_book_contradiction(session, fill, ruling):
    """Even a sell with a missing Fill must KEEP the phone alert."""
    for model, clock in ((BrokerOrder, BrokerOrder.updated_at), (TradeIntent, TradeIntent.created_at),
                         (Fill, Fill.filled_at)):
        if session.scalar(select(model.id).where(
            model.broker_account_id == fill.broker_account_id, model.symbol == fill.symbol,
            model.side == "sell", clock >= fill.filled_at,
        ).limit(1)) is not None:
            return True
    for model in (AccountPosition, VirtualPosition):
        if session.scalar(select(model.id).where(
            model.broker_account_id == fill.broker_account_id, model.symbol == fill.symbol,
            model.quantity != 0,
        ).limit(1)) is not None:
            return True
    return session.scalar(select(OmsManagedPosition.id).where(
        OmsManagedPosition.broker_account_name == ruling.account,
        OmsManagedPosition.symbol == ruling.symbol, OmsManagedPosition.current_quantity != 0,
    ).limit(1)) is not None


def tag_bound_incidents(session, fill, order, ruling):
    """No symbol-only or time-only suppression; missing payload binding stays visible."""
    if ruling.incident_bindings:
        from uuid import UUID

        bindings = {binding.row_id: binding for binding in ruling.incident_bindings}
        incidents = session.scalars(select(SystemIncident).where(
            SystemIncident.id.in_(tuple(UUID(value) for value in bindings)),
        )).all()
        changed = []
        for incident in incidents:
            binding = bindings[str(incident.id)]
            payload = dict(incident.payload or {})
            if (incident.service_name != binding.service or utc(incident.opened_at) != binding.opened_at):
                continue
            if any(key in payload and payload[key] != value for key, value in (
                ("operator_closed_allowance", ruling.allowance_id),
                ("operator_closed_entry_fill_id", str(fill.id)),
            )):
                continue
            original = {key: value for key, value in payload.items()
                        if key not in ("operator_closed_allowance", "operator_closed_entry_fill_id")}
            if original != binding.payload:
                continue  # Changed/foreign evidence cannot inherit the historical ruling.
            client_id = original.get("entry_client_order_id")
            if client_id is not None and client_id != order.client_order_id:
                continue
            incident.severity = "info"
            if "operator-closed" not in incident.title:
                incident.title = f"{incident.title[:200]} - operator-closed"[:255]
            incident.payload = {**payload, "operator_closed_allowance": ruling.allowance_id,
                                "operator_closed_entry_fill_id": str(fill.id)}
            changed.append(str(incident.id))
        return changed
    aliases = {str(order.id), order.client_order_id, order.broker_order_id}
    if ruling.managed_row_id:
        aliases.add(ruling.managed_row_id)
    candidates = []
    if not ruling.incident_minutes:
        return []
    incidents = session.scalars(select(SystemIncident).where(
        SystemIncident.service_name.in_(("oms-risk", "reconciler", "orb-schwab")),
        SystemIncident.opened_at >= min(ruling.incident_minutes),
        SystemIncident.opened_at < max(ruling.incident_minutes) + timedelta(minutes=1),
    )).all()
    for incident in incidents:
        payload = dict(incident.payload or {})
        if utc(incident.opened_at).replace(second=0, microsecond=0) not in ruling.incident_minutes:
            continue
        if payload.get("symbol") != ruling.symbol:
            continue
        if payload.get("broker_account_name", payload.get("account")) != ruling.account:
            continue
        references = {str(payload[key]) for key in (
            "managed_row_id", "entry_id", "entry_order_id", "entry_client_order_id",
        ) if payload.get(key)}
        if not references or not references <= aliases:
            continue
        if payload.get("base_client_order_ids"):
            continue  # A multi-handle incident is not proved to be this one entry.
        candidates.append((incident, payload))
    if len(candidates) > len(ruling.incident_minutes):
        return []  # Unknown extra incidents cannot borrow the three-row disposition.
    changed = []
    for incident, payload in candidates:
        incident.severity = "info"
        if "operator-closed" not in incident.title:
            incident.title = f"{incident.title[:200]} - operator-closed"[:255]
        incident.payload = {**payload, "operator_closed_allowance": ruling.allowance_id,
                            "operator_closed_entry_fill_id": str(fill.id)}
        changed.append(str(incident.id))
    return changed


def exclude_reviewed_operator_closes(session, unpaired_ids, *, now, rulings=REVIEWED_CLOSES):
    """Only subtract IDs already selected by the original unpaired SQL predicate.

    Historical reviewed rows are audited even after the original CURRENT_DATE
    phone window ends; ONLY intersection with unpaired_ids changes its count.
    Caller commits before subtracting. Matching Fill row NOWAIT locks serialize
    once-only audit creation across concurrent cron runs on PostgreSQL. No schema,
    OMS order, fill, position or incident-status mutation occurs here.
    """
    from uuid import UUID

    if len(unpaired_ids) > 2000:
        raise ValueError("operator-close candidate bound exceeded; keep all alerts")
    ids = {UUID(str(value)) for value in unpaired_ids}
    matches = []
    fills = session.scalars(select(Fill).join(BrokerOrder, BrokerOrder.id == Fill.order_id).where(
        BrokerOrder.broker_order_id.in_(tuple(ruling.native_entry_id for ruling in rulings)),
    ).limit(2001)).all()
    if len(fills) > 2000:
        raise ValueError("reviewed entry fill bound exceeded")
    for fill in fills:
        order = session.get(BrokerOrder, fill.order_id)
        account = session.get(BrokerAccount, fill.broker_account_id)
        if order is None or account is None:
            continue
        ruling = match_ruling(fill, order, account, now=now, rulings=rulings)
        if ruling is not None:
            matches.append((fill, order, ruling))
    excluded = []
    for fill, order, ruling in matches:
        if sum(other.allowance_id == ruling.allowance_id for _, _, other in matches) != 1:
            continue  # Multiple fills cannot borrow one reviewed39-share decision.
        session.scalar(select(Fill).where(Fill.id == fill.id).with_for_update(nowait=True))
        if bot_sell_or_book_contradiction(session, fill, ruling):
            continue
        audit = session.scalar(select(SystemIncident).where(
            SystemIncident.service_name == "oco-capture",
            SystemIncident.payload["source"].as_string() == "alerts1_operator_closed",
            SystemIncident.payload["entry_fill_id"].as_string() == str(fill.id),
        ).limit(1))
        if audit is None:
            audit = SystemIncident(service_name="oco-capture", severity="info", status="open",
                title=f"operator-closed: {ruling.symbol} on {ruling.account}", opened_at=utc(now),
                payload={"source": "alerts1_operator_closed", "entry_fill_id": str(fill.id),
                         "allowance_id": ruling.allowance_id, "symbol": ruling.symbol,
                         "broker_account_name": ruling.account, "operator_ruling": ruling.operator_ruling,
                         "broker_flat_proof": ruling.broker_flat_proof,
                         "virtual_clear_at": ruling.virtual_clear_at.isoformat()})
            session.add(audit)
        changed = tag_bound_incidents(session, fill, order, ruling)
        if audit is not None and changed:
            audit.payload = {**audit.payload, "tagged_incident_ids": changed}
        if fill.id in ids:
            excluded.append(str(fill.id))
    return excluded
