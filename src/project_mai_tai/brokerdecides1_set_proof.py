"""Statement-local Core membership proof for background runtime censuses.

No cache/write/clock/filtering shortcut. Scalar single-ID proof remains the
reference; every statement rebuilds all eight current source sets.
"""

from functools import lru_cache

from sqlalchemy import String, and_, case, cast, func, literal, or_, select, union, union_all
from sqlalchemy.sql.visitors import cloned_traverse

from project_mai_tai.brokerdecides1_retirement import KINDS, PROOF_VERSION
from project_mai_tai.db.models import (
    BrokerAccount, BrokerDecidesDispatchEvidence, BrokerDecidesDispatchRetirement,
    BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, OmsManagedPosition, Strategy, TradeIntent,
)


def table_entity_state(kind, table):
    """Same column order, DB casts and NULL encoding as scalar entity_state."""
    if kind == "strategy":
        columns = (table.c.id, table.c.code)
    elif kind == "account":
        columns = tuple(table.c[key] for key in
            ("id", "name", "provider", "environment", "external_account_id"))
    else:
        columns = tuple(table.c)
    value = literal("")
    for column in columns:
        raw = cast(column, String)
        value = value + func.coalesce(cast(func.length(raw), String) + literal(":") + raw, literal("N:"))
    return value


def validated_retirement_ids():
    # Cache construction only. Each caller owns fresh mutable CTE objects and
    # every execution still checks the current database rows and preimages.
    return cloned_traverse(_retirement_graph_template(), {}, {})


@lru_cache(maxsize=1)
def _retirement_graph_template():
    # Keep one Core proof graph per statement. Name CTEs explicitly: prefix_with
    # copies an anonymous CTE whose original object identity can be recycled.
    source = BrokerDecidesDispatchRetirement.__table__.alias()
    members = []
    for kind in KINDS:
        order = BrokerOrder.__table__.alias()
        model = order
        query = select().select_from(source).join(order,
            order.c.payload["clearwait_dispatch_token"].as_string() == source.c.token)
        if kind == "intent":
            model = TradeIntent.__table__.alias()
            query = query.join(model, model.c.id == order.c.intent_id)
        elif kind == "event":
            model = BrokerOrderEvent.__table__.alias()
            query = query.join(model, model.c.order_id == order.c.id)
        elif kind == "fill":
            model = Fill.__table__.alias()
            query = query.join(model, model.c.order_id == order.c.id)
        elif kind == "managed":
            model = OmsManagedPosition.__table__.alias()
            query = query.join(model, model.c.entry_order_id == order.c.id)
        elif kind == "replacement":
            model = DashboardSnapshot.__table__.alias()
            query = query.join(model, and_(model.c.snapshot_type == "atr_reprice_handoff", or_(
                model.c.payload["old"]["client_order_id"].as_string() == order.c.client_order_id,
                model.c.payload["replacement"]["client_order_id"].as_string() == order.c.client_order_id)))
        elif kind == "account":
            model = BrokerAccount.__table__.alias()
            query = query.join(model, model.c.id == order.c.broker_account_id)
        elif kind == "strategy":
            model = Strategy.__table__.alias()
            query = query.join(model, model.c.id == order.c.strategy_id)
        # Match IN-set semantics: a shared parent/account/job is one entity,
        # even when several token orders join it. Kinds remain disjoint.
        members.append(query.add_columns(source.c.attempt_id.label("attempt_id"),
            literal(kind).label("kind"), model.c.id.label("entity_id"),
            table_entity_state(kind, model).label("state")).distinct())
    current = union_all(*members).cte("l12_current_members").prefix_with("MATERIALIZED", dialect="postgresql")

    def counts(kind):
        return [func.sum(case((kind == name, 1), else_=0)).label(name) for name in KINDS]

    live_counts = select(current.c.attempt_id, *counts(current.c.kind))\
        .group_by(current.c.attempt_id).cte("l12_live_counts")
    evidence = BrokerDecidesDispatchEvidence.__table__
    evidence_counts = select(evidence.c.attempt_id, *counts(evidence.c.entity_kind))\
        .where(evidence.c.entity_kind.in_(KINDS)).group_by(evidence.c.attempt_id).cte("l12_evidence_counts")
    membership = and_(current.c.attempt_id == evidence.c.attempt_id,
        current.c.kind == evidence.c.entity_kind, current.c.entity_id == evidence.c.entity_id)
    changed_or_unsealed = select(current.c.attempt_id).select_from(current.outerjoin(evidence, membership))\
        .where(or_(evidence.c.entity_id.is_(None), current.c.state != evidence.c.original_state))
    missing_current = select(evidence.c.attempt_id).select_from(evidence.outerjoin(current, membership))\
        .where(evidence.c.entity_kind.in_(KINDS), current.c.entity_id.is_(None))
    invalid = union(changed_or_unsealed, missing_current).cte("l12_invalid_ids")
    receipt = BrokerDecidesDispatchRetirement.__table__.alias()
    attempt = DashboardSnapshot.__table__.alias()
    checks = [receipt.c.proof_version == PROOF_VERSION,
        receipt.c.attempt_state == table_entity_state("attempt", attempt),
        receipt.c.token == attempt.c.payload["attempt_token"].as_string(),
        receipt.c.symbol == attempt.c.payload["symbol"].as_string(),
        receipt.c.account_name == attempt.c.payload["account_name"].as_string(),
        receipt.c.attempt_id.not_in(select(invalid.c.attempt_id))]
    for kind in KINDS:
        sealed = receipt.c.counts[kind].as_integer()
        checks.extend((sealed == func.coalesce(live_counts.c[kind], 0),
            sealed == func.coalesce(evidence_counts.c[kind], 0)))
    valid = select(receipt.c.attempt_id).join(attempt, attempt.c.id == receipt.c.attempt_id)\
        .outerjoin(live_counts, live_counts.c.attempt_id == receipt.c.attempt_id)\
        .outerjoin(evidence_counts, evidence_counts.c.attempt_id == receipt.c.attempt_id)\
        .where(*checks).cte("l12_valid_ids").prefix_with("MATERIALIZED", dialect="postgresql")
    from project_mai_tai.brokerdecides1_prewire_proof import valid_prewire_ids

    return union(select(valid.c.attempt_id), valid_prewire_ids())
