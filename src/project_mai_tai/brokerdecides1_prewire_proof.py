"""Version-3 fresh-authority prewire closure, disjoint from native closure v1.

Existing tables only. The original journal and refusal are immutable source
evidence. A missing order is a contradiction check, NEVER the positive proof.
Every SQL read rechecks receipt membership, preimages and conflicting sources.
"""

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import aliased

from project_mai_tai.brokerdecides1_prewire import PROOF_VERSION, REFUSAL_TYPE, STRATEGY
from project_mai_tai.db.models import (
    BrokerAccount, BrokerDecidesDispatchEvidence as Evidence,
    BrokerDecidesDispatchRetirement as Retirement, BrokerOrder, BrokerOrderEvent,
    DashboardSnapshot, Strategy, TradeIntent,
)


def _token(data, token):
    return or_(data["clearwait_dispatch_token"].as_string() == token,
               data["metadata"]["clearwait_dispatch_token"].as_string() == token)


def valid_prewire_ids():
    from project_mai_tai.brokerdecides1_retirement import KINDS, entity_state, row_state

    r, a, refusal = aliased(Retirement), aliased(DashboardSnapshot), aliased(DashboardSnapshot)
    account, strategy = aliased(BrokerAccount), aliased(Strategy)
    data, original = refusal.payload, a.payload
    checks = [r.proof_version == PROOF_VERSION,
        a.snapshot_type == "v2_wait_dispatch", original["schema_version"].as_integer() == 1,
        original["kind"].as_string() == "attempt",
        original["strategy_code"].as_string() == STRATEGY,
        r.attempt_state == row_state(a), r.token == original["attempt_token"].as_string(),
        r.symbol == original["symbol"].as_string(), r.account_name == original["account_name"].as_string(),
        refusal.snapshot_type == REFUSAL_TYPE, data["schema_version"].as_integer() == 2,
        original["prewire_authority_version"].as_integer() == 1,
        data["attempt_id"].as_string() == func.replace(cast(a.id, String), "-", ""),
        data["journal_nonce"].as_string() == original["prewire_authority_nonce"].as_string(),
        func.length(data["journal_nonce"].as_string()) == 32,
        data["phase"].as_string() == "BEFORE_XADD", data["strategy_code"].as_string() == STRATEGY,
        data["receipt_id"].as_string() == func.replace(cast(refusal.id, String), "-", ""),
        data["attempt_token"].as_string() == r.token, data["symbol"].as_string() == r.symbol,
        data["account_name"].as_string() == r.account_name,
        data["opportunity_id"].as_string() == original["opportunity_id"].as_string(),
        cast(data["account_names"], String) == cast(original["account_names"], String),
        func.length(data["draft_sha256"].as_string()) == 64,
        func.length(data["reason"].as_string()) > 0, data["observed_ms"].as_integer() == r.observed_ms,
        r.observed_ms > 0, r.counts["prewire_refusal"].as_integer() == 1,
        account.name == r.account_name, strategy.code == STRATEGY]

    # Duplicate journals or receipt tokens are not one exact owned attempt.
    for snapshot_type in ("v2_wait_dispatch", REFUSAL_TYPE):
        duplicate = aliased(DashboardSnapshot)
        count = select(func.count()).select_from(duplicate).where(
            duplicate.snapshot_type == snapshot_type,
            duplicate.payload["attempt_token"].as_string() == r.token).correlate(r).scalar_subquery()
        checks.append(count == 1)
    for model in (BrokerOrder, TradeIntent, BrokerOrderEvent):
        conflict = aliased(model)
        checks.append(~exists(select(1).select_from(conflict).where(
            _token(conflict.payload, r.token)).correlate(r)))
    for kind in KINDS:
        checks.append(r.counts[kind].as_integer() == (1 if kind in {"account", "strategy"} else 0))

    # Three exact identities, including the full positive refusal row. A missing,
    # changed or extra evidence row invalidates the proof, not just its label.
    evidence = aliased(Evidence)
    for kind, model, state in (("prewire_refusal", refusal, row_state(refusal)),
                               ("account", account, entity_state("account", account)),
                               ("strategy", strategy, entity_state("strategy", strategy))):
        checks.append(exists(select(1).select_from(evidence).where(
            evidence.attempt_id == r.attempt_id, evidence.entity_kind == kind,
            evidence.entity_id == model.id, evidence.original_state == state).correlate(r, model)))
    count = select(func.count()).select_from(evidence).where(
        evidence.attempt_id == r.attempt_id).correlate(r).scalar_subquery()
    checks.append(count == 3)
    return select(r.attempt_id).select_from(r).join(a, a.id == r.attempt_id)\
        .join(refusal, and_(refusal.snapshot_type == REFUSAL_TYPE,
                           refusal.payload["attempt_token"].as_string() == r.token))\
        .join(account, account.name == r.account_name).join(strategy, strategy.code == STRATEGY)\
        .where(*checks)


def project_refusal(session, proof):
    from project_mai_tai.brokerdecides1_retirement import KINDS, entity_state, parse_attempt, row_state

    payload, i = proof.payload(), proof.identity
    rows = session.scalars(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == "v2_wait_dispatch",
        DashboardSnapshot.payload["attempt_token"].as_string() == i.token)
        .limit(2).with_for_update()).all()
    if len(rows) != 1:
        raise ValueError("prewire_journal_identity_UNKNOWN")
    attempt = rows[0]
    if (parse_attempt(attempt, i.accounts) != (i.symbol, i.token, i.account, int(i.opportunity))
            or attempt.payload.get("schema_version") != 1
            or attempt.payload.get("opportunity_id") != i.opportunity
            or attempt.payload.get("account_names") != list(i.accounts)
            or attempt.id != proof._issuer._capability.attempt_id
            or attempt.payload.get("prewire_authority_version") != 1
            or attempt.payload.get("prewire_authority_nonce") != proof._issuer._capability.nonce):
        raise ValueError("prewire_journal_identity_UNKNOWN")
    prior = session.get(Retirement, attempt.id)
    if prior is None:
        refusal = DashboardSnapshot(id=proof.receipt_id, snapshot_type=REFUSAL_TYPE, payload=payload)
        account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == i.account))
        strategy = session.scalar(select(Strategy).where(Strategy.code == STRATEGY))
        if account is None or strategy is None:
            raise ValueError("prewire_reference_identity_UNKNOWN")
        session.add(refusal)
        session.flush()
        attempt_state = session.scalar(select(row_state(DashboardSnapshot)).where(DashboardSnapshot.id == attempt.id))
        counts = {kind: int(kind in {"account", "strategy"}) for kind in KINDS}
        counts["prewire_refusal"] = 1
        session.add(Retirement(attempt_id=attempt.id, proof_version=PROOF_VERSION,
            attempt_state=attempt_state, token=i.token, symbol=i.symbol, account_name=i.account,
            observed_ms=proof.observed_ms, counts=counts))
        session.flush()
        for kind, model, row in (("prewire_refusal", DashboardSnapshot, refusal),
                                  ("account", BrokerAccount, account), ("strategy", Strategy, strategy)):
            state = row_state(model) if kind == "prewire_refusal" else entity_state(kind, model)
            image = session.scalar(select(state).where(model.id == row.id))
            session.add(Evidence(attempt_id=attempt.id, entity_kind=kind, entity_id=row.id, original_state=image))
        session.flush()
    else:
        refusal = session.get(DashboardSnapshot, proof.receipt_id)
        if refusal is None or refusal.payload != payload or prior.proof_version != PROOF_VERSION:
            raise ValueError("prewire_prior_projection_conflict")
    if session.scalar(select(DashboardSnapshot.id).where(
            DashboardSnapshot.id == attempt.id, DashboardSnapshot.id.in_(valid_prewire_ids()))) != attempt.id:
        raise ValueError("prewire_projection_readback_UNKNOWN")
    return attempt.id
