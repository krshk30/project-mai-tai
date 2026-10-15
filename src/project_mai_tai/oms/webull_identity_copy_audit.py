"""Exact-13 metadata repair rehearsal. CLI accepts frozen files, never a database URL.

Production execution is deliberately not exposed. A future execution/rollback needs
independent approval, current preimages and dependency checks, not this copy receipt.
"""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, Strategy, TradeIntent,
)

MANIFEST_SHA = "b2c21ccdae64822944ec7f19442eb8f369e423eb4e4c75595191c65bc6b6ac05"
CAPTURE_SHA = "4f499b0ab4e80e745dfa4a00344b7081920a64228d9c99064401d2af76fd3f90"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def preimage(row):
    return {column.name: getattr(row, column.name) for column in BrokerOrder.__table__.columns}


def validate_proof(snapshot, mapping):
    if (snapshot is None or str(snapshot.id) != mapping["snapshot_id"]
            or snapshot.snapshot_type != "webull_terminal_read_proof"):
        raise ValueError("native snapshot missing")
    p = snapshot.payload
    body = p["body"]
    if len(body.get("items", [])) != 1:
        raise ValueError("ambiguous native body")
    item = body["items"][0]
    version = digest([p["account_id"], p["client_order_id"], {"execution": p["execution"]}])
    valid = (
        p["source"] == "/trade/order/detail"
        and p["version"] == version == mapping["proof_version"]
        and str(uuid5(NAMESPACE_URL, f"webull-terminal:{p['account_id']}:{p['client_order_id']}:{version}")) == str(snapshot.id)
        and digest(body) == mapping["body_sha256"]
        and hashlib.sha256(str(p["account_id"]).encode()).hexdigest() == mapping["proof_account_id_sha256"]
        and body["account_id"] == p["account_id"]
        and body["client_order_id"] == p["client_order_id"] == mapping["client_order_id"]
        and body["order_id"] == p["execution"][1] == mapping["broker_order_id"]
        and item["symbol"] == mapping["symbol"] and item["side"] == "BUY"
        and item["order_status"] == "CANCELLED" and Decimal(str(item["filled_qty"])) == 0
    )
    if not valid:
        raise ValueError("native proof identity/hash mismatch")


def rehearse_exact13(session, manifest):
    """Caller supplies an isolated COPY transaction; lock/check all rows before any change."""
    if (len(manifest["exact_13"]) != 13
            or len({m["order_id"] for m in manifest["exact_13"]}) != 13):
        raise ValueError("not the exact 13-row population")
    if session.bind.dialect.name == "postgresql":
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        session.execute(text("SET LOCAL statement_timeout = '2s'"))
    prepared = []
    for mapping in sorted(manifest["exact_13"], key=lambda m: m["order_id"]):
        row = session.scalar(select(BrokerOrder).where(
            BrokerOrder.id == UUID(mapping["order_id"]),
        ).with_for_update())
        if row is None:
            raise ValueError("order missing")
        account = session.get(BrokerAccount, row.broker_account_id)
        strategy = session.get(Strategy, row.strategy_id)
        if not (account and account.environment == "COPY"
                and account.provider == "webull" and account.name == mapping["account"]
                and strategy and strategy.code == mapping["strategy"]
                and row.client_order_id == mapping["client_order_id"]
                and row.symbol == mapping["symbol"] and row.side == mapping["side"]
                and row.status == mapping["expected_status"] and not row.broker_order_id
                and row.quantity == Decimal(mapping["expected_quantity"])
                and str(row.intent_id) == mapping["expected_intent_id"]
                and utc(row.updated_at) == utc(mapping["reference_updated_at"])
                and digest(row.payload) == mapping["reference_order_payload_sha256"]):
            raise ValueError("current row no longer matches audited preimage")
        if session.scalar(select(Fill.id).where(Fill.order_id == row.id)) is not None:
            raise ValueError("fill exists")
        token = uuid5(NAMESPACE_URL, f"atr-reprice:{account.name}:{row.client_order_id}")
        owner = session.get(DashboardSnapshot, token)
        if owner is not None and owner.snapshot_type == "atr_reprice_handoff":
            raise ValueError("active ownership journal")
        if session.scalar(select(BrokerOrder.id).where(
            BrokerOrder.broker_order_id == mapping["broker_order_id"],
        )) is not None:
            raise ValueError("venue ID already bound")
        validate_proof(session.get(DashboardSnapshot, UUID(mapping["snapshot_id"])), mapping)
        prepared.append((row, mapping, preimage(row)))
    audit = []
    for row, mapping, before in prepared:
        result = session.execute(update(BrokerOrder).where(
            BrokerOrder.id == row.id, BrokerOrder.broker_order_id.is_(None),
            BrokerOrder.updated_at == row.updated_at,
        ).values(broker_order_id=mapping["broker_order_id"], updated_at=row.updated_at))
        if result.rowcount != 1:
            raise ValueError("conditional ID-only update superseded")
        session.refresh(row)
        after = preimage(row)
        if {k for k in before if before[k] != after[k]} != {"broker_order_id"}:
            raise ValueError("non-ID column changed")
        audit.append({"order_id": str(row.id), "account": mapping["account"],
                      "client_order_id": row.client_order_id, "symbol": row.symbol,
                      "body_sha256": mapping["body_sha256"], "snapshot_id": mapping["snapshot_id"],
                      "before": before, "after": after,
                      "original_ack_origin_unchanged": mapping["accepted_origin"],
                      "changed_columns": ["broker_order_id"]})
    return audit


def frozen_copy(capture, manifest):
    """Load retained order/intent metadata and native bodies into a fresh in-memory DB."""
    if hashlib.sha256(capture.read_bytes()).hexdigest() != CAPTURE_SHA:
        raise ValueError("frozen capture hash mismatch")
    records = [json.loads(line) for line in capture.read_text().splitlines()]
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as session:
        accounts, strategies = {}, {}
        for record in records:
            if record["kind"] != "order":
                continue
            source = record["data"]
            if source["account"] not in accounts:
                account = BrokerAccount(name=source["account"], provider="webull", environment="COPY")
                session.add(account)
                session.flush()
                accounts[account.name] = account
            if source["strategy"] not in strategies:
                strategy = Strategy(code=source["strategy"], name=source["strategy"], execution_mode="paper")
                session.add(strategy)
                session.flush()
                strategies[strategy.code] = strategy
            account, strategy = accounts[source["account"]], strategies[source["strategy"]]
            intent = TradeIntent(id=UUID(source["intent_id"]), strategy_id=strategy.id,
                broker_account_id=account.id, symbol=source["symbol"], side=source["side"],
                intent_type=source["intent_type"], quantity=Decimal(str(source["quantity"])),
                status=source["status"], reason=source["intent_reason"], payload=source["intent_payload"])
            if session.get(TradeIntent, intent.id) is None:
                session.add(intent)
                session.flush()
            session.add(BrokerOrder(id=UUID(source["id"]), intent_id=intent.id, strategy_id=strategy.id,
                broker_account_id=account.id, client_order_id=source["client_order_id"],
                broker_order_id=source["broker_order_id"], symbol=source["symbol"], side=source["side"],
                order_type=source["order_type"], time_in_force=source["time_in_force"],
                quantity=Decimal(str(source["quantity"])), status=source["status"], payload=source["payload"],
                submitted_at=utc(source["submitted_at"]), updated_at=utc(source["updated_at"])))
        for record in records:
            if record["kind"] == "retained_proof":
                p = record["data"]
                session.add(DashboardSnapshot(id=UUID(p["id"]), snapshot_type=p["snapshot_type"],
                                               payload=p["payload"], created_at=utc(p["created_at"])))
    try:
        with sessions.begin() as session:
            all_before = {str(o.id): preimage(o) for o in session.scalars(select(BrokerOrder))}
            audit = rehearse_exact13(session, manifest)
            all_after = {str(o.id): preimage(o) for o in session.scalars(select(BrokerOrder))}
            changed = {key for key in all_before if all_before[key] != all_after[key]}
            if changed != {m["order_id"] for m in manifest["exact_13"]}:
                raise ValueError("population changed outside exact 13")
        return {"mode": "LOCAL_IN_MEMORY_COPY_ONLY", "orders_in_copy": len(all_before),
                "changed_rows": len(audit), "unchanged_rows": len(all_before) - len(audit),
                "audit": audit, "API_calls": 0, "production_writes": 0,
                "retained_capture_sha256": CAPTURE_SHA,
                "known_limit": "Copy contains retained order/intent/proof data, not a full live Fill or owner table backup; production revalidation remains mandatory.",
                "lost_original_receipt": "Original CAPTURE-RECEIPT.json was lost in a case-insensitive filename collision; not reconstructed.",
                "excluded": "15 rejected and 8 unproved accepted-NULL rows remain untouched.",
                "rollback": "No production execution or rollback interface; preserve actual run preimages and require separate reviewed CAS rollback approval."}
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if hashlib.sha256(args.manifest.read_bytes()).hexdigest() != MANIFEST_SHA:
        raise ValueError("exact reviewed manifest hash mismatch")
    result = frozen_copy(args.capture, json.loads(args.manifest.read_text()))
    result["manifest_sha256"] = MANIFEST_SHA
    # Exclusive creation prevents overwriting prior audit evidence.
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=2, default=str) + "\n")
    print(f"COPY ONLY: {result['changed_rows']}/13 ID changes; {result['unchanged_rows']} unchanged; API=0 production=0")


if __name__ == "__main__":
    main()
