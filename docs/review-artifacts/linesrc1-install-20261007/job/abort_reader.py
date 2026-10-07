"""[codex] Only shared canonical local no-wire proof may retire an aborted row."""
from uuid import UUID

from sqlalchemy import select, text
from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.oms.atr_reprice_handoff import local_rpg_abort_proof


def evidence_budget(session, order_id):
    size = session.execute(text("SELECT coalesce(octet_length(b.payload::text),0) "
        "+coalesce((SELECT sum(octet_length(e.payload::text)) FROM broker_order_events e WHERE e.order_id=b.id),0) "
        "+coalesce((SELECT octet_length(t.payload::text) FROM trade_intents t WHERE t.id=b.intent_id),0) "
        "FROM broker_orders b WHERE b.id=:id"), dict(id=order_id)).scalar_one()
    total, journal_size = session.execute(text("SELECT count(*),coalesce(sum(octet_length(payload::text)),0) "
        "FROM dashboard_snapshots WHERE snapshot_type='atr_reprice_handoff'")).one()
    if size > 1_000_000 or total > 1024 or journal_size > 4_000_000:
        raise ValueError("aborted-row canonical evidence exceeds1MB or journal1024/4MB bound")


def filter_rows(session, working, inflight):
    proven, audit, remaining = set(), [], []
    for row in working:
        if str(row.get("status", "")).lower() != "aborted":
            remaining.append(row)
            continue
        order_id = UUID(str(row["id"]))
        evidence_budget(session, order_id)
        order = session.get(BrokerOrder, order_id)
        proof = local_rpg_abort_proof(session, order) if order is not None else None
        if proof is None:
            remaining.append(row)
            continue
        proven.add(str(order.intent_id))
        audit.append(dict(order_id=str(order.id), intent_id=str(order.intent_id), account=row["account"],
            symbol=row["symbol"], client_order_id=order.client_order_id, refusal_code=proof[0], audit_at=proof[1],
            source="shared_local_rpg_abort_proof", old_ticket_released=False))
    kept = []
    for row in inflight:
        if str(row.get("status", "")).lower() != "aborted" or str(row["id"]) not in proven:
            kept.append(row)
            continue
        orders = list(session.scalars(select(BrokerOrder).where(BrokerOrder.intent_id == UUID(str(row["id"]))).limit(2)))
        if len(orders) != 1 or local_rpg_abort_proof(session, orders[0]) is None:
            kept.append(row)
    return remaining, kept, audit
