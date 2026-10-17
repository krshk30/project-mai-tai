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
HISTORY_FIXTURE = FIXTURE.with_name("stuckalert1_broker_history_asof.json")


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
        _, order = seed(session, provider=provider, now=now, order_type=kind, metadata=metadata,
             broker_event="accepted")  # Deliberately day-old: old freshness policy superseded.
        # TOP171 supersedes v1: original day-old acceptance/NULL native ID is
        # historical shape evidence, not current broker-working proof.
        account = session.get(BrokerAccount, order.broker_account_id)
        assert reconciler._accepted_resting_ids(session, [order], {account.id: account}) == {order.id}
        orders, intents = methods(session, factory)
        assert (len(orders), len(intents)) == (1, 1)


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [12, 15], ids=["PM", "RTH"])
@pytest.mark.parametrize("origin", [None, "client", "unknown"], ids=["no_event", "client_only", "unknown_only"])
def test_accepted_row_requires_positive_broker_accepted_event(monkeypatch, provider, hour, origin):
    now = datetime(2026, 10, 12, hour, tzinfo=UTC)
    monkeypatch.setattr(reconciler, "utcnow", lambda: now)
    factory = build_test_session_factory()
    with factory() as session:
        _, order = seed(session, provider=provider, now=now)
        order.broker_order_id = "CONTROLLED-id-not-positive-ACK"
        order.updated_at = now-timedelta(seconds=182)
        if origin is not None:
            session.add(BrokerOrderEvent(order_id=order.id, event_type="accepted",
                event_source=origin, event_at=now-timedelta(days=1), payload={}))
        session.flush()
        orders, intents = methods(session, factory)
        assert (len(orders), len(intents)) == (1, 1)


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [12, 15], ids=["PM", "RTH"])
@pytest.mark.parametrize("shape", ["pending", "submitted", "orphan", "unknown", "broker_terminal"])
def test_all_five_negative_shapes_alert_both_brokers_sessions(monkeypatch, provider, hour, shape):
    now = datetime(2026, 10, 12, hour, tzinfo=UTC)
    monkeypatch.setattr(reconciler, "utcnow", lambda: now)
    factory = build_test_session_factory()
    with factory() as session:
        _, order = seed(session, provider=provider, now=now,
             order_status=shape if shape in {"pending", "submitted", "unknown"} else "accepted",
             intent_status="unknown" if shape == "unknown" else "submitted",
             broker_event="cancelled" if shape == "broker_terminal" else None,
             orphan=shape == "orphan")
        if shape in {"pending", "submitted"}:
            order.broker_order_id = "CONTROLLED-id-is-not-an-ACK"
            order.updated_at = now-timedelta(seconds=182)
            session.flush()
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
            broker_event="accepted")
        if change == "unknown_broker_event":
            session.add(BrokerOrderEvent(order_id=order.id, event_type="unknown",
                event_source="broker", event_at=now-timedelta(hours=1), payload={}))
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
        # TOP171: neither original sibling has current wire proof. The old
        # accepted shape no longer hides it, even with an UNKNOWN parent.
        assert len(orders) == (2 if change == "mixed_noack_sibling" else 1)


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


@pytest.mark.parametrize("conflict", ["cancelled", "filled", "unknown"])
def test_same_timestamp_broker_conflict_cannot_be_hidden_by_accepted_uuid(monkeypatch, conflict):
    now = datetime(2026, 10, 12, 15, tzinfo=UTC)
    monkeypatch.setattr(reconciler, "utcnow", lambda: now)
    factory = build_test_session_factory()
    with factory() as session:
        _, order = seed(session, now=now)
        when = now-timedelta(days=1)
        session.add_all([
            BrokerOrderEvent(id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
                order_id=order.id, event_type="accepted", event_source="broker", event_at=when, payload={}),
            BrokerOrderEvent(id=UUID("00000000-0000-0000-0000-000000000001"),
                order_id=order.id, event_type=conflict, event_source="broker", event_at=when, payload={}),
        ])
        session.flush()
        orders, intents = methods(session, factory)
        assert len(orders) == len(intents) == 1


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("prior_accept", [False, True])
def test_partial_broker_event_does_not_replace_positive_accepted_event(monkeypatch, provider, prior_accept):
    now = datetime(2026, 10, 12, 15, tzinfo=UTC)
    monkeypatch.setattr(reconciler, "utcnow", lambda: now)
    factory = build_test_session_factory()
    with factory() as session:
        _, order = seed(session, provider=provider, now=now,
                        broker_event="accepted" if prior_accept else None)
        session.add(BrokerOrderEvent(order_id=order.id, event_type="partially_filled",
            event_source="broker", event_at=now-timedelta(hours=1), payload={}))
        session.flush()
        orders, intents = methods(session, factory)
        account = session.get(BrokerAccount, order.broker_account_id)
        assert reconciler._accepted_resting_ids(session, [order], {account.id: account}) == (
            {order.id} if prior_accept else set())
        # Preserve old positive-history distinction above; current live proof
        # is still UNKNOWN for BOTH unchanged original inputs under TOP171.
        assert (len(orders), len(intents)) == (1, 1)


def test_recorded242_incident_reconstruction_preserves_unproven_and_terminal_alerts(monkeypatch):
    replay_recorded242(monkeypatch)


def test_recorded242_with_prior_broker_history_asof_each_incident(monkeypatch):
    raw = HISTORY_FIXTURE.read_bytes()
    assert len(raw) < 1024*1024
    assert len(raw.splitlines()) + len(FIXTURE.read_bytes().splitlines()) <= 500
    history = json.loads(raw)
    assert history["original_fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert history["query_cases"] == len(history["rows"]) == 242
    assert history["unique_order_ids"] == len({row["order_id"] for row in history["rows"]}) == 123
    assert [page["offset"] for page in history["pages"]] == list(range(0, 242, 16))
    assert [page["count"] for page in history["pages"]] == [16]*15 + [2]
    replay_recorded242(monkeypatch, history=history, history_sha256=hashlib.sha256(raw).hexdigest())


def replay_recorded242(monkeypatch, *, history=None, history_sha256=None):
    raw = FIXTURE.read_bytes()
    assert len(raw) < 1024*1024
    assert hashlib.sha256(raw).hexdigest() == "b7858e675311f8fdee79f0abb310d2aa203de087cf7074c2b6f37ab8ee850a2b"
    fixture = json.loads(raw)
    rows = fixture["rows"]
    assert len(rows) == len({row["case_id"] for row in rows}) == 242
    assert fixture["scope"].endswith("paged read-only capture, not atomic")
    assert sum(row["kind"] == "stuck_order" for row in rows) == 119
    evidence = {row["case_id"]: row for row in history["rows"]} if history is not None else {}
    if history is not None:
        assert set(evidence) == {row["case_id"] for row in rows}
        assert sum(row["accepted"] is not None for row in evidence.values()) == 61
        legacy = [evidence[row["case_id"]] for row in rows if row["event_source"] == "unknown"]
        assert len(legacy) == 184
        assert sum(row["accepted"] is not None for row in legacy) == 4
        assert sum(row["broker_event_count"] == 0 for row in legacy) == 180
        assert sum(row["broker_accepted_count"] == 0 for row in legacy) == 180
    factory = build_test_session_factory()
    counts = {kind: {"before": 0, "after": 0} for kind in ("stuck_order", "stuck_intent")}
    historical_v1_counts = {kind: {"before": 0, "after": 0} for kind in counts}
    historical_retained = []
    retained = []
    for row in rows:
        now, updated = at(row["replay_at"]), at(row["alert_updated_at"])
        assert updated < now-timedelta(seconds=180)
        assert row["alert_status"] in {
            "stuck_order": {"pending", "submitted", "accepted", "partially_filled"},
            "stuck_intent": {"pending", "submitted", "accepted"},
        }[row["kind"]]
        assert at(row["event_at"]) <= now
        history_events = {}
        if history is not None:
            captured = evidence[row["case_id"]]
            assert captured["order_id"] == row["order_id"] and at(captured["replay_at"]) == now
            assert 0 <= captured["broker_accepted_count"] <= captured["broker_event_count"]
            assert (captured["accepted"] is not None) == (captured["broker_accepted_count"] > 0)
            assert (captured["latest"] is not None) == (captured["broker_event_count"] > 0)
            if captured["accepted"] is not None:
                assert captured["accepted"]["event_type"] == "accepted"
            for event in (captured["accepted"], captured["latest"]):
                if event is not None:
                    assert event["event_source"] == "broker" and at(event["event_at"]) <= now
                    history_events[event["id"]] = event
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
            for event in history_events.values():
                session.add(BrokerOrderEvent(id=UUID(event["id"]), order_id=order.id,
                    event_type=event["event_type"], event_source=event["event_source"],
                    event_at=at(event["event_at"]), payload={}))
            session.flush()
            orders, intents = methods(session, factory)
            historical_ids = reconciler._accepted_resting_ids(session, [order], {account.id: account})
            historical_exempt = order.id in historical_ids and (
                row["kind"] == "stuck_order" or intent.status in {"pending", "submitted", "accepted"})
            historical_v1_counts[row["kind"]]["before"] += 1
            historical_v1_counts[row["kind"]]["after"] += int(not historical_exempt)
            if not historical_exempt:
                historical_retained.append({key: row[key] for key in
                    ("case_id", "kind", "symbol", "order_status_asof", "event_source")})
            actual = orders if row["kind"] == "stuck_order" else intents
            counts[row["kind"]]["before"] += 1  # Original age/status rule qualifies every recorded case.
            counts[row["kind"]]["after"] += len(actual)
            if actual:
                retained.append({key: row[key] for key in
                                 ("case_id", "kind", "symbol", "order_status_asof", "event_source")})
            session.rollback()
    # TOP171 cannot manufacture missing current wire/session/pair evidence in
    # these ORIGINAL242 retained rows. Preserve the old shape totals separately.
    assert counts == {"stuck_order": {"before": 119, "after": 119},
                      "stuck_intent": {"before": 123, "after": 123}}
    assert historical_v1_counts == {
        "stuck_order": {"before": 119, "after": 90 if history is not None else 92},
        "stuck_intent": {"before": 123, "after": 91 if history is not None else 93}}
    unproven = [row for row in retained if row["event_source"] == "unknown"]
    assert len(unproven) == 184
    assert all(row["order_status_asof"] == "accepted" for row in unproven)
    recorded_broker = [row for row in retained if row["event_source"] != "unknown"]
    assert len(recorded_broker) == 58
    terminal = [row for row in recorded_broker if row["order_status_asof"] not in {"accepted", "partially_filled"}]
    assert terminal == [{"case_id": "1a7fdc71-60e3-410c-8d49-bcea106cb322", "kind": "stuck_intent",
                         "symbol": "GIXI", "order_status_asof": "cancelled", "event_source": "broker"}]
    historical_unproven = [row for row in historical_retained if row["event_source"] == "unknown"]
    assert len(historical_unproven) == (180 if history is not None else 184)
    assert all(row["order_status_asof"] == "accepted" for row in historical_unproven)
    assert [row for row in historical_retained if row["event_source"] != "unknown"] == terminal
    assert len(historical_retained) == (181 if history is not None else 185)
    assert len(retained) == 242
    print("STUCKALERT1_RECORDED_REPLAY="+json.dumps({
        "evidence_kind": "RECORDED_INCIDENT_RECONSTRUCTION_NOT_SOFTWARE_ARRIVAL_REPLAY",
        "fixture_sha256": hashlib.sha256(raw).hexdigest(), "counts": counts,
        "policy": "TOP171_CURRENT_WORKING_PROOF_REQUIRED",
        "historical_v1_counts": historical_v1_counts,
        "history_fixture_sha256": history_sha256,
        "query_denominator": 242 if history is not None else None,
        "legacy_unknown_cases": 184,
        "legacy_now_positive_broker_accept": 4 if history is not None else None,
        "legacy_zero_stored_broker_events": 180 if history is not None else None,
        "suppressed_positive_broker_accept": 0,
        "historical_v1_suppressed_positive_broker_accept": 61 if history is not None else 57,
        "retained_unproven_unknown_origin": len(unproven),
        "retained_terminal": terminal,
        "capture_limitation": (
            "Stored broker history queried ASOF each cutoff; zero stored facts does not prove no real broker ACK"
            if history is not None else
            "Latest as-of event only; unknown origin does not prove absence of an earlier broker ACK"
        ),
        "actual_incident_open_session": "RTH only; PM/RTH breadth separately CONTROLLED",
        "per_order_freshness": "REQUIRED_BY_TOP171_UNAVAILABLE_IN_RETAINED_INPUT",
        "historical_v1_per_order_freshness": "NOT_REQUIRED_BY_APPROVED_V1",
        "per_account_completion_metric": "ABSENT_GAP_ALERT_SKIPPED",
    }, sort_keys=True))
