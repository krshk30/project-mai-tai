"""Transactional adversaries on a synthetic 13-row copy; real frozen copy is a separate receipt."""


from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, TradeIntent
from project_mai_tai.oms.webull_identity_copy_audit import (
    MANIFEST_SHA, digest, frozen_copy, preimage, rehearse_exact13,
)
from tests.unit.test_v2_exit_paper_isolation import _make_sf
from tests.unit.test_wbnullid1_identity import setup


def copy_setup():
    service, sessions, _, _ = setup()
    # Use a new empty copy, not setup's single runtime row.
    sessions = _make_sf()
    now = datetime(2026, 10, 8, tzinfo=UTC)
    mappings = []
    with sessions.begin() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2")
        account = service.store.ensure_broker_account(session, "live:orb", provider="webull", environment="COPY")
        for i in range(13):
            coid, venue = f"CONTROLLED-client-{i}", f"CONTROLLED-venue-{i}"
            intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
                symbol="FLYE", side="buy", intent_type="open", quantity=Decimal(2),
                reason="CONTROLLED", status="cancelled", payload={})
            session.add(intent)
            session.flush()
            payload = {"origin": "unknown" if i > 8 else "broker"}
            order = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id, broker_account_id=account.id,
                client_order_id=coid, symbol="FLYE", side="buy", order_type="STOP_LIMIT",
                time_in_force="day", quantity=Decimal(2), status="cancelled", payload=payload,
                updated_at=now, submitted_at=now)
            session.add(order)
            session.flush()
            body = {"account_id": "CONTROLLED-account", "client_order_id": coid, "order_id": venue,
                    "items": [{"symbol": "FLYE", "side": "BUY", "order_status": "CANCELLED", "filled_qty": "0"}]}
            proof = {"account_id": body["account_id"], "client_order_id": coid,
                     "body": body, "execution": ["cancelled", venue, "0"], "source": "/trade/order/detail"}
            version = digest([proof["account_id"], coid, {"execution": proof["execution"]}])
            proof["version"] = version
            proof_id = uuid5(NAMESPACE_URL, f"webull-terminal:{proof['account_id']}:{coid}:{version}")
            session.add(DashboardSnapshot(id=proof_id, snapshot_type="webull_terminal_read_proof", payload=proof))
            import hashlib

            mappings.append({"order_id": str(order.id), "snapshot_id": str(proof_id),
                "account": "live:orb", "strategy": "schwab_1m_v2", "client_order_id": coid,
                "broker_order_id": venue, "symbol": "FLYE", "side": "buy", "expected_status": "cancelled",
                "expected_quantity": "2", "expected_intent_id": str(intent.id),
                "reference_updated_at": now.isoformat(), "reference_order_payload_sha256": digest(payload),
                "proof_version": version, "body_sha256": digest(body),
                "proof_account_id_sha256": hashlib.sha256(body["account_id"].encode()).hexdigest(),
                "accepted_origin": payload["origin"]})
    return sessions, {"exact_13": mappings}


def rows(sessions):
    with sessions() as session:
        return {str(row.id): preimage(row) for row in session.scalars(select(BrokerOrder))}


def test_exact13_only_id_changes_preserve_unknown_origin_and_times():
    sessions, manifest = copy_setup()
    before = rows(sessions)
    with sessions.begin() as session:
        audit = rehearse_exact13(session, manifest)
    after = rows(sessions)
    assert len(audit) == 13
    for order_id in before:
        assert {k for k in before[order_id] if before[order_id][k] != after[order_id][k]} == {"broker_order_id"}
    assert sum(row["original_ack_origin_unchanged"] == "unknown" for row in audit) == 4


@pytest.mark.parametrize("fault", ["status", "quantity", "payload", "updated_at", "client",
    "filled", "owned", "venue_conflict", "proof", "native_type", "production", "missing"])
def test_any_preflight_failure_aborts_all13_before_updates(fault):
    sessions, manifest = copy_setup()
    with sessions.begin() as session:
        mapping = manifest["exact_13"][-1]
        from uuid import UUID

        row = session.get(BrokerOrder, UUID(mapping["order_id"]))
        if fault == "status":
            row.status = "filled"
        elif fault == "quantity":
            row.quantity = 3
        elif fault == "payload":
            row.payload = {"tampered": True}
        elif fault == "updated_at":
            row.updated_at = datetime.now(UTC)
        elif fault == "client":
            row.client_order_id = "wrong-client"
        elif fault == "filled":
            session.add(Fill(order_id=row.id, strategy_id=row.strategy_id, broker_account_id=row.broker_account_id,
                symbol=row.symbol, side="buy", quantity=Decimal(1), price=Decimal(3), payload={}))
        elif fault == "owned":
            token = uuid5(NAMESPACE_URL, f"atr-reprice:live:orb:{row.client_order_id}")
            session.add(DashboardSnapshot(id=token, snapshot_type="atr_reprice_handoff", payload={}))
        elif fault == "venue_conflict":
            first = session.scalar(select(BrokerOrder).where(BrokerOrder.id != row.id))
            first.broker_order_id = mapping["broker_order_id"]
        elif fault == "proof":
            proof = session.get(DashboardSnapshot, UUID(mapping["snapshot_id"]))
            proof.payload = {**proof.payload, "version": "tampered"}
        elif fault == "native_type":
            session.get(DashboardSnapshot, UUID(mapping["snapshot_id"])).snapshot_type = "unrelated_snapshot"
        elif fault == "production":
            session.get(BrokerAccount, row.broker_account_id).environment = "live"
        else:
            session.delete(row)
    before = rows(sessions)
    with pytest.raises(ValueError), sessions.begin() as session:
        rehearse_exact13(session, manifest)
    assert rows(sessions) == before


def test_unapproved_capture_rejected_before_loading(tmp_path):
    capture = tmp_path / "wrong.jsonl"
    capture.write_text("{}\n")
    with pytest.raises(ValueError, match="capture hash"):
        frozen_copy(capture, {})


def test_manifest_is_exact_frozen_plan_not_reconstructed_receipt():
    import hashlib

    manifest = Path(__file__).parents[1] / "fixtures/wbnullid1_exact13.json"
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == MANIFEST_SHA
    mappings = json.loads(manifest.read_text())["exact_13"]
    assert len({m["order_id"] for m in mappings}) == 13
    assert len({m["broker_order_id"] for m in mappings}) == 13


def test_cli_default_is_copy_only_and_never_overwrites_existing_output(tmp_path, monkeypatch):
    import hashlib
    import sys

    from project_mai_tai.oms import webull_identity_copy_audit as tool

    capture = tmp_path / "retained.jsonl"
    capture.write_text("CONTROLLED retained input")
    before_capture = capture.read_bytes()
    manifest = Path(__file__).parents[1] / "fixtures/wbnullid1_exact13.json"
    before_manifest = manifest.read_bytes()
    output = tmp_path / "audit.json"
    output.write_text("CONTROLLED existing evidence")
    calls = []

    def copy_receipt(path, mapping):
        calls.append((path, len(mapping["exact_13"])))
        return {"mode": "LOCAL_IN_MEMORY_COPY_ONLY", "changed_rows": 13, "unchanged_rows": 336}

    monkeypatch.setattr(tool, "frozen_copy", copy_receipt)
    monkeypatch.setattr(sys, "argv", ["copy-tool", "--capture", str(capture),
        "--manifest", str(manifest), "--output", str(output)])
    with pytest.raises(FileExistsError):
        tool.main()
    assert output.read_text() == "CONTROLLED existing evidence"
    assert capture.read_bytes() == before_capture and manifest.read_bytes() == before_manifest
    assert calls == [(capture, 13)]
    assert hashlib.sha256(before_manifest).hexdigest() == MANIFEST_SHA


@pytest.mark.parametrize("option", ["--apply", "--database-url", "--production", "--rollback"])
def test_cli_has_no_execution_or_production_connection_interface(option, monkeypatch):
    import sys

    from project_mai_tai.oms import webull_identity_copy_audit as tool

    monkeypatch.setattr(sys, "argv", ["copy-tool", "--capture", "not-read",
        "--manifest", "not-read", "--output", "not-created", option])
    with pytest.raises(SystemExit) as exc:
        tool.main()
    assert exc.value.code == 2
