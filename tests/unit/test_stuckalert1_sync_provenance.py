"""Actual retained provenance census and controlled offline OMS-path evidence."""


from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from tests.unit.oms_sqlite_fixtures import private_session_factory as build_test_session_factory
from tests.unit.oms_sqlite_fixtures import own_service
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent
from project_mai_tai.oms import service as oms
from project_mai_tai.reconciliation import service as reconciler
from project_mai_tai.settings import Settings
from test_oms_risk_service import FakeRedis
from test_stuckalert1 import FIXTURE, HISTORY_FIXTURE, at, methods, seed


EVIDENCE = Path(__file__).parents[2] / "docs/review-artifacts/stuckalert1/OMS_SYNC_PROVENANCE_CAPTURE.json"


def test_actual181_missing_piece_census_has_no_persisted_sync_origin():
    raw = EVIDENCE.read_bytes()
    assert len(raw) < 1024*1024
    assert sum(len(p.read_bytes().splitlines()) for p in (FIXTURE, HISTORY_FIXTURE, EVIDENCE)) <= 500
    data = json.loads(raw)
    original = {row["case_id"]: row for row in json.loads(FIXTURE.read_bytes())["rows"]}
    history = json.loads(HISTORY_FIXTURE.read_bytes())
    retained = {row["case_id"] for row in history["rows"]
                if not (row["accepted"] and row["latest"]
                        and row["latest"]["event_type"] in {"accepted", "partially_filled"})}
    assert data["original_fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert data["broker_history_fixture_sha256"] == hashlib.sha256(HISTORY_FIXTURE.read_bytes()).hexdigest()
    assert data["query_cases"] == len(data["rows"]) == len(retained) == 181
    assert {row["case_id"] for row in data["rows"]} == retained
    assert data["unique_order_ids"] == len({row["order_id"] for row in data["rows"]}) == 91
    assert [p["offset"] for p in data["pages"]] == list(range(0, 181, 16))
    assert [p["count"] for p in data["pages"]] == [16]*11 + [5]
    counts = {}
    for row in data["rows"]:
        before = original[row["case_id"]]
        assert row["order_id"] == before["order_id"] and at(row["replay_at"]) == at(before["replay_at"])
        assert row["provider"] == before["provider"] and row["kind"] == before["kind"]
        assert row["accepted_rows"] == 1
        assert row["accepted_origin_counts"] == {"broker": 0, "unknown": 1, "client": 0, "other": 0}
        ack = row["latest_accepted"]
        assert ack["event_source"] == "unknown" and ack["event_type"] == "accepted"
        assert at(ack["event_at"]) <= at(row["replay_at"])
        assert at(row["latest_any"]["event_at"]) <= at(row["replay_at"])
        assert ack["metadata_source"] == "schwab_1m_v2"
        assert ack["status_source"] is ack["oms_broker_sync"] is ack["broker_status"] is None
        assert ack["webull_broker_place_time"] is ack["webull_broker_filled_time"] is None
        assert not ack["has_webull_list_evidence"] and not ack["has_webull_terminal_proof"]
        count = counts.setdefault(row["provider"], {"retained": 0, "orders": 0, "intents": 0,
            "unknown_accepted_rows": 0, "no_accepted_row": 0, "no_broker_ACK": 0,
            "missing_snapshot_marker": 0, "null_snapshot_broker_id": 0, "terminal": 0,
            "wire_timestamp_present": 0, "persisted_sync_read_marker": 0})
        count["retained"] += 1
        count["orders" if row["kind"] == "stuck_order" else "intents"] += 1
        count["unknown_accepted_rows"] += 1
        count["no_broker_ACK"] += 1
        count["missing_snapshot_marker"] += int(before["metadata"]["resting_entry"] != "true"
            and before["metadata"]["fanout_source"] != "rth_resting_mirror")
        count["null_snapshot_broker_id"] += int(not before["broker_order_id"])
        count["terminal"] += int(row["latest_any"]["event_type"] not in {"accepted", "partially_filled"})
        count["wire_timestamp_present"] += int(bool(ack["webull_wire_submitted_at_utc"]))
    assert counts == data["breakdown"]
    assert {p: (c["retained"], c["orders"], c["intents"], c["unknown_accepted_rows"],
                c["no_accepted_row"], c["missing_snapshot_marker"], c["null_snapshot_broker_id"],
                c["terminal"], c["persisted_sync_read_marker"])
            for p, c in counts.items()} == {
        "schwab": (97, 48, 49, 97, 0, 1, 0, 1, 0),
        "webull": (84, 42, 42, 84, 0, 0, 84, 0, 0),
    }
    print("STUCKALERT1_OMS_SYNC_PROVENANCE="+json.dumps({"evidence_kind": "RECORDED_ASOF_PROVENANCE_CENSUS",
        "sha256": hashlib.sha256(raw).hexdigest(), "cases": 181, "breakdown": counts,
        "proven_legacy_sync_ACKs": 0, "source_extension": "NONE_WITHOUT_POSITIVE_PROVENANCE",
        "suppressed": 61, "population": 242, "suppressed_pct": 61/242*100,
        "target95pct": "BLOCKED_NOT_FABRICATED"}, sort_keys=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("shape", ["same_status", "changed_broker", "changed_unknown"])
async def test_controlled_sync_path_only_preserves_available_origin(monkeypatch, provider, shape):
    now = datetime(2026, 10, 12, 15, tzinfo=UTC)
    monkeypatch.setattr(oms, "utcnow", lambda: now)
    monkeypatch.setattr(reconciler, "utcnow", lambda: now)
    monkeypatch.setattr(oms.OmsRiskService, "_market_is_fillable", lambda self, now=None: True)
    factory = build_test_session_factory()
    with factory() as session:
        _, order = seed(session, provider=provider, now=now,
                        order_status="accepted" if shape == "same_status" else "submitted")
        order.broker_order_id = "CONTROLLED-id"
        order.updated_at = now-timedelta(seconds=182)
        session.add(BrokerOrderEvent(order_id=order.id, event_type="accepted", event_source="unknown",
            event_at=now, payload={"metadata": {"source": "schwab_1m_v2"}}))
        session.commit()

    class OfflineRead:
        calls = 0

        async def fetch_order_update(self, request):
            self.calls += 1
            return ExecutionReport(event_type="accepted", client_order_id=request.client_order_id,
                broker_order_id="CONTROLLED-id", symbol=request.symbol, quantity=Decimal("1"),
                metadata=dict(request.metadata), reported_at=now,
                origin="unknown" if shape == "changed_unknown" else "broker")

        async def submit_order(self, request):
            raise AssertionError("No submission allowed in this controlled read test")

    adapter = OfflineRead()
    service = own_service(oms.OmsRiskService(Settings(_env_file=None, redis_stream_prefix="offline-stuck"),
        redis_client=FakeRedis(), session_factory=factory, broker_adapter=adapter))
    result = await service.sync_broker_orders(account_names=["live:"+provider])
    assert adapter.calls == 1
    assert result == {"orders": 0 if shape == "same_status" else 1, "terminal_orders": 0}
    with factory() as session:
        events = session.scalars(select(BrokerOrderEvent)).all()
        assert len(events) == (1 if shape == "same_status" else 2)
        assert sum(event.event_source == "broker" for event in events) == int(shape == "changed_broker")
        order = session.scalar(select(BrokerOrder))
        account = session.get(reconciler.BrokerAccount, order.broker_account_id)
        assert reconciler._accepted_resting_ids(session, [order], {account.id: account}) == (
            {order.id} if shape == "changed_broker" else set())
        if shape == "same_status":
            orders, intents = methods(session, factory)
            assert (len(orders), len(intents)) == (1, 1)
