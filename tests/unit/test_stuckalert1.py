"""Reconciler-only v1 controls and recorded-incident reconstruction, not broker I/O."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, Strategy, TradeIntent
from project_mai_tai.reconciliation import service as reconciler
from project_mai_tai.settings import Settings
from test_reconciliation_service import FakeRedis, build_test_session_factory

FIXTURE = Path(__file__).parents[1] / "fixtures" / "stuckalert1_actual242.json"


def at(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seed(session, *, provider="schwab", now, order_status="accepted", intent_status="submitted",
         order_type="STOP_LIMIT", metadata=None, broker_event=None, orphan=False):
    account = BrokerAccount(name="live:"+provider, provider=provider, environment="production")
    strategy = Strategy(code="schwab_1m_v2", name="v2", execution_mode="live", metadata_json={})
    session.add_all([account, strategy])
    session.flush()
    intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
        symbol="CONTROL", side="buy", intent_type="open", quantity=Decimal("1"),
        reason="CONTROLLED fleet944 orphan" if orphan else "CONTROLLED resting entry",
        status=intent_status, payload={}, updated_at=now-timedelta(seconds=181))
    session.add(intent)
    session.flush()
    order = None
    if not orphan:
        order = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id="CONTROLLED-one", broker_order_id=None,
            symbol="CONTROL", side="buy", order_type=order_type, time_in_force="day",
            quantity=Decimal("1"), status=order_status,
            payload=metadata if metadata is not None else {"resting_entry": "true"},
            submitted_at=now-timedelta(days=1), updated_at=now-timedelta(seconds=181))
        session.add(order)
        session.flush()
        if broker_event:
            session.add(BrokerOrderEvent(order_id=order.id, event_type=broker_event,
                event_source="broker", event_at=now-timedelta(days=1), payload={}))
            session.flush()
    return intent, order


def methods(session, factory):
    svc = reconciler.ReconciliationService(
        Settings(_env_file=None), redis_client=FakeRedis(), session_factory=factory,
    )
    return svc._build_stuck_order_findings(session), svc._build_stuck_intent_findings(session)


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [12, 15], ids=["PM", "RTH"])
@pytest.mark.parametrize("kind", ["STOP", "STOP_LIMIT"])
@pytest.mark.parametrize("marker", ["resting_entry", "fanout_source"])
def test_accepted_resting_marker_no_id_or_fresh_read_required(monkeypatch, provider, hour, kind, marker):
    now = datetime(2026, 10, 12, hour, tzinfo=UTC)
    monkeypatch.setattr(reconciler, "utcnow", lambda: now)
    factory = build_test_session_factory()
    with factory() as session:
        metadata = {marker: "true" if marker == "resting_entry" else "rth_resting_mirror"}
        seed(session, provider=provider, now=now, order_type=kind, metadata=metadata,
             broker_event="accepted")  # Deliberately day-old: old freshness policy superseded.
        assert methods(session, factory) == ([], [])


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [12, 15], ids=["PM", "RTH"])
@pytest.mark.parametrize("shape", ["pending", "submitted", "orphan", "unknown", "broker_terminal"])
def test_all_five_negative_shapes_alert_both_brokers_sessions(monkeypatch, provider, hour, shape):
    now = datetime(2026, 10, 12, hour, tzinfo=UTC)
    monkeypatch.setattr(reconciler, "utcnow", lambda: now)
    factory = build_test_session_factory()
    with factory() as session:
        seed(session, provider=provider, now=now,
             order_status=shape if shape in {"pending", "submitted", "unknown"} else "accepted",
             intent_status="unknown" if shape == "unknown" else "submitted",
             broker_event="cancelled" if shape == "broker_terminal" else None,
             orphan=shape == "orphan")
        orders, intents = methods(session, factory)
        assert len(orders) == (0 if shape == "orphan" else 1)
        assert len(intents) == 1


@pytest.mark.parametrize("change", ["unknown_broker_event", "sell", "wrong_type", "wrong_marker",
                                   "mixed_noack_sibling", "unknown_intent", "other_provider", "bad_metadata"])
def test_no_exemption_for_ambiguous_shape_or_intent_chain(monkeypatch, change):
    now = datetime(2026, 10, 12, 15, tzinfo=UTC)
    monkeypatch.setattr(reconciler, "utcnow", lambda: now)
    factory = build_test_session_factory()
    with factory() as session:
        intent, order = seed(session, now=now,
            provider="other" if change == "other_provider" else "schwab",
            intent_status="unknown" if change == "unknown_intent" else "submitted",
            broker_event="unknown" if change == "unknown_broker_event" else None)
        if change == "sell":
            order.side = intent.side = "sell"
        if change == "wrong_type":
            order.order_type = "LIMIT"
        if change == "wrong_marker":
            order.payload = {"resting_entry": "false", "fanout_source": "other"}
        if change == "bad_metadata":
            order.payload = ["CONTROLLED-invalid"]
        if change == "mixed_noack_sibling":
            session.add(BrokerOrder(intent_id=intent.id, strategy_id=intent.strategy_id,
                broker_account_id=intent.broker_account_id, client_order_id="CONTROLLED-two",
                symbol="CONTROL", side="buy", order_type="STOP_LIMIT", time_in_force="day",
                quantity=Decimal("1"), status="submitted", payload={"resting_entry": "true"},
                updated_at=now-timedelta(seconds=181)))
        order.updated_at = intent.updated_at = now-timedelta(seconds=182)
        session.flush()
        orders, intents = methods(session, factory)
        assert len(intents) == 1
        assert len(orders) == (0 if change == "unknown_intent" else 1)


def test_exact_180s_noack_boundary_remains_strict(monkeypatch):
    now = datetime(2026, 10, 12, 15, tzinfo=UTC)
    monkeypatch.setattr(reconciler, "utcnow", lambda: now)
    factory = build_test_session_factory()
    with factory() as session:
        intent, order = seed(session, now=now, order_status="submitted")
        intent.updated_at = order.updated_at = now-timedelta(seconds=180)
        session.flush()
        assert methods(session, factory) == ([], [])
        monkeypatch.setattr(reconciler, "utcnow", lambda: now+timedelta(microseconds=1))
        orders, intents = methods(session, factory)
        assert len(orders) == len(intents) == 1


def test_recorded242_incident_reconstruction_preserves_real_terminal_intent(monkeypatch):
    raw = FIXTURE.read_bytes()
    assert len(raw) < 1024*1024
    fixture = json.loads(raw)
    rows = fixture["rows"]
    assert len(rows) == len({row["case_id"] for row in rows}) == 242
    assert fixture["scope"].endswith("paged read-only capture, not atomic")
    assert sum(row["kind"] == "stuck_order" for row in rows) == 119
    factory = build_test_session_factory()
    counts = {kind: {"before": 0, "after": 0} for kind in ("stuck_order", "stuck_intent")}
    retained = []
    for row in rows:
        now, updated = at(row["replay_at"]), at(row["alert_updated_at"])
        assert updated < now-timedelta(seconds=180)
        assert at(row["event_at"]) <= now
        monkeypatch.setattr(reconciler, "utcnow", lambda now=now: now)
        with factory() as session:
            account = BrokerAccount(name=row["account_name"], provider=row["provider"], environment="production")
            strategy = Strategy(code=row["strategy_code"], name="v2", execution_mode="live", metadata_json={})
            session.add_all([account, strategy])
            session.flush()
            intent = TradeIntent(id=UUID(row["order_intent_id"]), strategy_id=strategy.id,
                broker_account_id=account.id, symbol=row["symbol"], side=row["side"],
                intent_type="open", quantity=Decimal(str(row["quantity"])), reason="RECORDED reconstruction",
                status=row["alert_status"] if row["kind"] == "stuck_intent" else "submitted",
                payload={}, updated_at=updated)
            session.add(intent)
            session.flush()
            order = BrokerOrder(id=UUID(row["order_id"]), intent_id=intent.id,
                strategy_id=strategy.id, broker_account_id=account.id, symbol=row["symbol"],
                side=row["side"], order_type=row["order_type"], time_in_force="day",
                client_order_id=row["client_order_id"], broker_order_id=row["broker_order_id"],
                quantity=Decimal(str(row["quantity"])), payload=row["metadata"],
                status=row["alert_status"] if row["kind"] == "stuck_order" else row["order_status_asof"],
                updated_at=updated, submitted_at=updated)
            session.add(order)
            session.flush()
            session.add(BrokerOrderEvent(order_id=order.id, event_type=row["order_status_asof"],
                event_source=row["event_source"], event_at=at(row["event_at"]), payload={}))
            session.flush()
            orders, intents = methods(session, factory)
            actual = orders if row["kind"] == "stuck_order" else intents
            counts[row["kind"]]["before"] += 1  # Original age/status rule qualifies every recorded case.
            counts[row["kind"]]["after"] += len(actual)
            if actual:
                retained.append({key: row[key] for key in ("case_id", "kind", "symbol", "order_status_asof")})
            session.rollback()
    assert counts == {"stuck_order": {"before": 119, "after": 0}, "stuck_intent": {"before": 123, "after": 1}}
    assert retained == [{"case_id": "1a7fdc71-60e3-410c-8d49-bcea106cb322", "kind": "stuck_intent",
                         "symbol": "GIXI", "order_status_asof": "cancelled"}]
    print("STUCKALERT1_RECORDED_REPLAY="+json.dumps({
        "evidence_kind": "RECORDED_INCIDENT_RECONSTRUCTION_NOT_SOFTWARE_ARRIVAL_REPLAY",
        "fixture_sha256": hashlib.sha256(raw).hexdigest(), "counts": counts, "retained": retained,
        "actual_incident_open_session": "RTH only; PM/RTH breadth separately CONTROLLED",
        "per_order_freshness": "NOT_REQUIRED_BY_APPROVED_V1",
        "per_account_completion_metric": "ABSENT_GAP_ALERT_SKIPPED",
    }, sort_keys=True))
