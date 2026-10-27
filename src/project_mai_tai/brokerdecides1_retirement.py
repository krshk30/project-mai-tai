"""Bounded positive-closure projection; never deletes or changes source history.

Retirement is not a terminal status label. Existing CANCELACK native/replacement
proof must pass, and every contributing durable row is sealed. SQL revalidates
the complete sealed row set before excluding the original dispatch attempt.
"""

from sqlalchemy import String, and_, cast, exists, func, inspect, literal, or_, select, text
from sqlalchemy.orm import aliased

from project_mai_tai.brokerdecides1 import MAX_SYMBOLS
from project_mai_tai.brokerdecides1_accounts import local_accounts
from project_mai_tai.db.models import (
    BrokerAccount, BrokerDecidesDispatchEvidence as Evidence,
    BrokerDecidesDispatchRetirement as Retirement, BrokerOrder, BrokerOrderEvent,
    DashboardSnapshot, Fill, OmsManagedPosition, Strategy, TradeIntent,
)

BATCH = 32
MAX_EVIDENCE_ROWS = 64
MAX_EVIDENCE_BYTES = 64 * 1024
PROOF_VERSION = 1
KINDS = ("order", "intent", "event", "fill", "managed", "replacement", "account", "strategy")


def row_state(model, columns=None):
    """Length-prefixed full DB representation, including all JSON/scalar columns.

    Capture and comparison use the SAME database casts, not Python datetime/JSON
    serialization. NULL and empty string are distinct; delimiters cannot collide.
    """
    value = literal("")
    inspection = inspect(model)
    mapper = getattr(inspection, "mapper", inspection)
    selected_columns = model.__table__.columns if columns is None else columns
    for column in selected_columns:
        raw = cast(getattr(model, mapper.get_property_by_column(column).key), String)
        value = value + func.coalesce(cast(func.length(raw), String) + literal(":") + raw, literal("N:"))
    return value


def entity_state(kind, model):
    # Closure depends on these reference identities, NOT unrelated configuration
    # metadata/updated_at. Economic/attempt/event/job preimages remain whole-row.
    if kind == "strategy":
        return row_state(model, (model.__table__.c.id, model.__table__.c.code))
    if kind == "account":
        return row_state(model, tuple(model.__table__.c[key] for key in
            ("id", "name", "provider", "environment", "external_account_id")))
    return row_state(model)


def scope(names, model=DashboardSnapshot):
    data = model.payload
    return and_(model.snapshot_type == "v2_wait_dispatch",
        or_(data["kind"].as_string() == "attempt", data["kind"].as_string().is_(None)),
        or_(data["strategy_code"].as_string() == "schwab_1m_v2", data["strategy_code"].as_string().is_(None)),
        or_(data["account_name"].as_string().in_(names), data["account_name"].as_string().is_(None)))


def entity_set(kind, token):
    order_token = BrokerOrder.payload["clearwait_dispatch_token"].as_string() == token
    orders = select(BrokerOrder.id).where(order_token).correlate_except(BrokerOrder)
    if kind == "order":
        model = aliased(BrokerOrder)
        predicate = model.payload["clearwait_dispatch_token"].as_string() == token
    elif kind == "intent":
        model = aliased(TradeIntent)
        predicate = model.id.in_(select(BrokerOrder.intent_id).where(order_token).correlate_except(BrokerOrder))
    elif kind == "event":
        model = aliased(BrokerOrderEvent)
        predicate = model.order_id.in_(orders)
    elif kind == "fill":
        model = aliased(Fill)
        predicate = model.order_id.in_(orders)
    elif kind == "managed":
        model = aliased(OmsManagedPosition)
        predicate = model.entry_order_id.in_(orders)
    elif kind == "replacement":
        model = aliased(DashboardSnapshot)
        clients = select(BrokerOrder.client_order_id).where(order_token).correlate_except(BrokerOrder)
        predicate = and_(model.snapshot_type == "atr_reprice_handoff", or_(
            model.payload["old"]["client_order_id"].as_string().in_(clients),
            model.payload["replacement"]["client_order_id"].as_string().in_(clients)))
    elif kind == "account":
        model = aliased(BrokerAccount)
        predicate = model.id.in_(select(BrokerOrder.broker_account_id).where(order_token).correlate_except(BrokerOrder))
    elif kind == "strategy":
        model = aliased(Strategy)
        predicate = model.id.in_(select(BrokerOrder.strategy_id).where(order_token).correlate_except(BrokerOrder))
    else:
        raise ValueError("retirement_entity_UNKNOWN")
    return model, predicate


def valid_retirement(attempt=DashboardSnapshot):
    """Correlated SQL: changed/new/missing evidence reopens the attempt as UNKNOWN."""
    receipt = aliased(Retirement)
    checks = [receipt.attempt_id == attempt.id,
        receipt.proof_version == PROOF_VERSION, receipt.attempt_state == row_state(attempt),
        receipt.token == attempt.payload["attempt_token"].as_string(),
        receipt.symbol == attempt.payload["symbol"].as_string(),
        receipt.account_name == attempt.payload["account_name"].as_string()]
    for kind in KINDS:
        model, predicate = entity_set(kind, receipt.token)
        count = select(func.count().label(f"live_{kind}_count")).select_from(model).where(predicate).correlate(receipt).scalar_subquery()
        checks.append(receipt.counts[kind].as_integer() == count)
        evidence = aliased(Evidence)
        unchanged = exists(select(1).select_from(model).where(
            model.id == evidence.entity_id, predicate, entity_state(kind, model) == evidence.original_state)
            .correlate(evidence, receipt))
        changed = exists(select(1).where(evidence.attempt_id == receipt.attempt_id,
            evidence.entity_kind == kind, ~unchanged).correlate(receipt))
        evidence_count = select(func.count()).select_from(evidence).where(
            evidence.attempt_id == receipt.attempt_id, evidence.entity_kind == kind).correlate(receipt).scalar_subquery()
        # Both counts must equal the sealed count. Do not duplicate the costly
        # current entity-set subquery merely to express transitive equality.
        checks.extend((~changed, evidence_count == receipt.counts[kind].as_integer()))
    from project_mai_tai.brokerdecides1_prewire_proof import valid_prewire_ids

    return or_(exists(select(1).where(*checks).correlate(attempt)),
               attempt.id.in_(valid_prewire_ids()))


def validated_retirement_ids():
    from project_mai_tai.brokerdecides1_set_proof import validated_retirement_ids

    return validated_retirement_ids()


def table_entity_state(kind, table):
    from project_mai_tai.brokerdecides1_set_proof import table_entity_state

    return table_entity_state(kind, table)


def unresolved_query(names):
    return select(DashboardSnapshot).where(scope(names),
        DashboardSnapshot.id.not_in(validated_retirement_ids()))


def unresolved_jobs_query(names):
    attempt = aliased(DashboardSnapshot)
    sealed = exists(select(1).select_from(Evidence)
        .join(Retirement, Retirement.attempt_id == Evidence.attempt_id)
        .join(attempt, attempt.id == Retirement.attempt_id)
        .where(Evidence.entity_kind == "replacement", Evidence.entity_id == DashboardSnapshot.id,
            Evidence.original_state == row_state(DashboardSnapshot), scope(names, attempt),
            Retirement.attempt_id.in_(validated_retirement_ids())).correlate(DashboardSnapshot))
    return select(DashboardSnapshot).where(DashboardSnapshot.snapshot_type == "atr_reprice_handoff", ~sealed)


def parse_attempt(row, names):
    data = row.payload
    try:
        opportunity = int(data.get("opportunity_id", "0"))
    except (TypeError, ValueError, AttributeError):
        opportunity = 0
    if (not isinstance(data, dict) or data.get("kind") != "attempt"
            or data.get("strategy_code") != "schwab_1m_v2"
            or data.get("account_name") not in names or opportunity <= 0
            or not isinstance(data.get("account_names"), list)
            or data["account_name"] not in data["account_names"]
            or not isinstance(data.get("symbol"), str) or not data["symbol"]
            or not isinstance(data.get("attempt_token"), str) or not data["attempt_token"]
            or len(data["attempt_token"]) > 128):
        raise ValueError("dispatch_attempt_identity_UNKNOWN")
    return data["symbol"], data["attempt_token"], data["account_name"], opportunity


def retire_batch(session, names, cursor, observed_ms):
    return _retire_batch(session, names, cursor, observed_ms)


def _retire_batch(session, names, cursor, observed_ms, *, install_bounds=None):
    """One finite background batch; cursor advances PAST UNKNOWN, then wraps.

    Caller commits only these NEW projections. A lost commit reply does not
    advance its in-memory cursor. Original rows and existing receipts are immutable.
    """
    from project_mai_tai.cancelack1 import _closed_with_replacements

    if session.get_bind().dialect.name == "postgresql":
        if session.in_transaction():
            raise ValueError("retirement_requires_fresh_repeatable_read_session")
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        # A bounded output is not a bounded SQL plan. Timeout means UNKNOWN,
        # never proof; main must still qualify the real PostgreSQL plan/cost.
        session.execute(text("SET LOCAL statement_timeout = '750ms'"))
        if install_bounds is not None:
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
    account_rows = local_accounts(session, names)
    scope_names = tuple(account.name for account in account_rows)
    if install_bounds is None:
        query = unresolved_query(scope_names)
    else:
        after_id, upper_id = install_bounds
        # Install traversal visits source PKs, not the global correlated proof
        # before LIMIT. UNKNOWN and existing receipts still get their own rows.
        query = select(DashboardSnapshot).where(scope(scope_names), DashboardSnapshot.id <= upper_id)
        if after_id is not None:
            query = query.where(DashboardSnapshot.id > after_id)
    if install_bounds is None and cursor is not None:
        stamp, identity = cursor
        query = query.where(or_(DashboardSnapshot.created_at > stamp,
            and_(DashboardSnapshot.created_at == stamp, DashboardSnapshot.id > identity)))
    ordering = (DashboardSnapshot.created_at, DashboardSnapshot.id) if install_bounds is None else (DashboardSnapshot.id,)
    rows = session.scalars(query.order_by(*ordering).limit(BATCH)).all()
    if not rows:
        return None, 0, 0
    accounts = {account.name: account.id for account in account_rows}
    strategy_id = session.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
    if strategy_id is None:
        raise ValueError("retirement_scope_UNKNOWN")
    retired = 0
    for row in rows:
        try:
            symbol, token, account, opportunity = parse_attempt(row, scope_names)
        except ValueError:
            continue  # Remains in unresolved census; cannot block cursor progress.
        if session.get(Retirement, row.id) is not None:
            continue  # Changed source after proof: UNKNOWN, never refresh the old receipt.
        orders = session.scalars(select(BrokerOrder).where(
            BrokerOrder.payload["clearwait_dispatch_token"].as_string() == token)
            .limit(MAX_EVIDENCE_ROWS + 1)).all()
        if (not orders or len(orders) > MAX_EVIDENCE_ROWS or any(
                order.symbol != symbol or order.strategy_id != strategy_id
                or order.broker_account_id != accounts[account] or order.side != "buy"
                or order.status not in {"cancelled", "canceled", "rejected", "expired", "filled"}
                or not isinstance(order.payload, dict)
                or str(order.payload.get("fanout_segment_id")) != str(opportunity)
                for order in orders)):
            continue
        # First projection version does not flatten a replacement graph or
        # promote local-abort/control-permit proof. Those remain unresolved.
        job_model, job_predicate = entity_set("replacement", token)
        jobs = session.scalars(select(job_model).where(job_predicate).limit(MAX_EVIDENCE_ROWS + 1)).all()
        if len(jobs) > MAX_EVIDENCE_ROWS or any(job.payload.get("replacement") for job in jobs):
            continue

        def closed(order):
            try:
                return _closed_with_replacements(session, order, observed_ms, jobs=jobs)
            except (ValueError, TypeError, AttributeError, ArithmeticError):
                return False

        if not all(closed(order) for order in orders):
            continue
        sealed, counts, size = [], {}, 0
        for kind in KINDS:
            model, predicate = entity_set(kind, token)
            found = session.execute(select(model.id, entity_state(kind, model)).where(predicate)
                .limit(MAX_EVIDENCE_ROWS + 1)).all()
            counts[kind] = len(found)
            for identity, state in found:
                size += len(state.encode())
                sealed.append((kind, identity, state))
        attempt_state = session.scalar(select(row_state(DashboardSnapshot)).where(DashboardSnapshot.id == row.id))
        size += len(attempt_state.encode())
        if len(sealed) > MAX_EVIDENCE_ROWS or size > MAX_EVIDENCE_BYTES:
            continue
        # Re-evaluate closure after all preimages are captured. Commit/readback and
        # every future SQL exclusion compare those exact full-row images again.
        if not all(closed(order) for order in orders):
            continue
        session.add(Retirement(attempt_id=row.id, proof_version=PROOF_VERSION,
            attempt_state=attempt_state, token=token, account_name=account, symbol=symbol,
            observed_ms=observed_ms, counts=counts))
        session.flush()
        session.add_all(Evidence(attempt_id=row.id, entity_kind=kind, entity_id=identity,
            original_state=state) for kind, identity, state in sealed)
        session.flush()
        if session.scalar(select(valid_retirement()).select_from(DashboardSnapshot).where(DashboardSnapshot.id == row.id)) is not True:
            raise ValueError("retirement_readback_UNKNOWN")
        retired += 1
    return (rows[-1].created_at, rows[-1].id), retired, len(rows)


def load_terminal_tokens(session, names, tokens):
    """Bounded consumer requests ONLY its exact outstanding reservations."""
    if len(tokens) > MAX_SYMBOLS:
        raise ValueError("reservation_request_overflow")
    if not tokens:
        return frozenset()
    rows = session.execute(select(Retirement.symbol, Retirement.token)
        .join(DashboardSnapshot, DashboardSnapshot.id == Retirement.attempt_id)
        .where(scope(names), Retirement.token.in_(tokens), valid_retirement())
        .limit(MAX_SYMBOLS + 1)).all()
    if len(rows) > MAX_SYMBOLS:
        raise ValueError("terminal_receipt_overflow")
    return frozenset(rows)
