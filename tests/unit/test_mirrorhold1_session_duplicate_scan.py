"""Recorded Oct8 DB rows/shape quotes; local acknowledgements are CONTROLLED."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.dialects import postgresql

from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms.mirror_retained_hold import row_id
from tests.unit.test_mirrorhold1_retained_hold import event_for, quote, state
from tests.unit.test_mirrorhold1_retained_hold import lane as lane


RECORDED = json.loads((Path(__file__).parents[1] / "fixtures/mirrorhold1_session_1008_recorded.json").read_text())
INTENTS = {"AIXI": "34af52a6-b13d-44b7-80ca-dafa699863a6",
           "FLYE": "5446a17f-af09-4d49-a91b-80c4d066a82f"}


def seed_order(lane, event, *, status="pending", submitted_at="current", metadata=None,
               account=None, symbol=None, side="buy", tif="day", order_id=None, client=None):
    service, _, factory, clock = lane
    with factory() as session:
        broker = service.store.ensure_broker_account(session, account or event.payload.broker_account_name,
                                                     provider="webull", environment="live")
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2")
        timestamp = clock[0] if submitted_at == "current" else submitted_at
        order = BrokerOrder(id=order_id or uuid4(), strategy_id=strategy.id, broker_account_id=broker.id,
                            client_order_id=client or "CONTROLLED-" + str(uuid4()), symbol=symbol or event.payload.symbol,
                            side=side, order_type="STOP_LIMIT", time_in_force=tif, quantity=Decimal("1"),
                            status=status, payload=metadata if metadata is not None else {
                                "fanout_segment_id": "1791388800000", "fanout_slot_id": "CONTROLLED-other"},
                            submitted_at=timestamp)
        session.add(order)
        session.commit()
        return order.id


def prepare(lane, *, account="live:orb", now="2026-10-08T13:35:05.075008+00:00"):
    service, _, factory, clock = lane
    clock[0] = datetime.fromisoformat(now)
    service.settings.strategy_schwab_1m_v2_webull_account_name = account
    event = event_for(lane, symbol="AIXI", segment="1791466502449", stop="2.36")
    event.payload.broker_account_name = account
    quote(lane, event, "2.27")
    with factory() as session:
        service.store.ensure_broker_account(session, account, provider="webull", environment="live")
        service._mirrorhold_upsert(session, event, no_wire=True)
        session.commit()
    return event


def dispatch(lane, event):
    with lane[2]() as session:
        reason = lane[0]._mirrorhold_dispatch(session, event)
        session.commit()
    return reason


def recorded_setup(lane, symbol):
    service, client, factory, clock = lane
    row = next(r for r in RECORDED["intents"] if r["id"] == INTENTS[symbol])
    md = row["payload"]["metadata"]
    # Intent creation is a proxy for produced_at; preserve the recorded quote clock.
    produced = datetime.fromisoformat(row["created_at"])
    quote_at = datetime.fromisoformat(md["webull_shape_market_at_utc"])
    clock[0] = max(produced, quote_at)
    event = TradeIntentEvent(event_id=UUID(row["payload"]["event_id"]), source_service=row["payload"]["source_service"],
        produced_at=produced, payload=TradeIntentPayload(strategy_code=row["strategy"],
            broker_account_name=row["account"], symbol=symbol, side=row["side"], intent_type=row["intent_type"],
            quantity=Decimal(str(row["quantity"])), reason=row["reason"], metadata=dict(md)))
    histories = [r for r in RECORDED["orders"] if r["account"] == "live:orb" and r["symbol"] == symbol
                 and r["submitted_at"] < row["created_at"]]
    for order in histories:
        seed_order(lane, event, status=order["status"], submitted_at=datetime.fromisoformat(order["submitted_at"]),
                   metadata=order["payload"], tif=order["time_in_force"], order_id=UUID(order["id"]),
                   client=order["client_order_id"])
    # Restore actual historical quantities/audits; no invented historical replies.
    with factory() as session:
        historical_intents = {r["id"]: r for r in RECORDED["historical_intents"]}
        added = set()
        for order in histories:
            persisted = session.get(BrokerOrder, UUID(order["id"]))
            persisted.quantity = Decimal(str(order["quantity"]))
            recorded_intent = historical_intents.get(order["intent_id"])
            if recorded_intent is not None:
                intent_id = UUID(recorded_intent["id"])
                if intent_id not in added:
                    session.add(TradeIntent(id=intent_id, strategy_id=persisted.strategy_id,
                        broker_account_id=persisted.broker_account_id, symbol=recorded_intent["symbol"],
                        side=recorded_intent["side"], intent_type=recorded_intent["intent_type"],
                        quantity=Decimal(str(recorded_intent["quantity"])), reason=recorded_intent["reason"],
                        status=recorded_intent["status"], payload=recorded_intent["payload"],
                        created_at=datetime.fromisoformat(recorded_intent["created_at"]),
                        updated_at=datetime.fromisoformat(recorded_intent["updated_at"])))
                    added.add(intent_id)
                persisted.intent_id = intent_id
        ids = {r["id"] for r in histories}
        for audit in RECORDED["audits"]:
            if audit["order_id"] in ids and audit["event_at"] < row["created_at"]:
                session.add(BrokerOrderEvent(id=UUID(audit["id"]), order_id=UUID(audit["order_id"]),
                    event_type=audit["event_type"], event_source=audit["event_source"],
                    event_at=datetime.fromisoformat(audit["event_at"]), payload=audit["payload"]))
        for fill in RECORDED["fills"]:
            if fill["order_id"] in ids and fill["filled_at"] < row["created_at"]:
                order = session.get(BrokerOrder, UUID(fill["order_id"]))
                session.add(Fill(id=UUID(fill["id"]), order_id=order.id, strategy_id=order.strategy_id,
                    broker_account_id=order.broker_account_id, symbol=fill["symbol"], side=fill["side"],
                    quantity=Decimal(str(fill["quantity"])), price=Decimal(str(fill["price"])),
                    filled_at=datetime.fromisoformat(fill["filled_at"])))
        owner = service._mirrorhold_upsert(session, event, no_wire=True)
        if md.get("mirrorhold_token"):
            service._mirrorhold_write(session, owner, {**owner.payload, "phase": "queued", "token": md["mirrorhold_token"]})
        session.commit()
    FanoutSegmentIdentityStore(factory).record(symbol, int(md["fanout_segment_id"]), True,
                                               "CONTROLLED recorded identity binding", now=clock[0])
    service._latest_quotes_by_symbol[symbol] = {"ask": Decimal(md["webull_shape_market_price"]), "received_at": quote_at}
    client._bodies["place"] = {"client_order_id": "CONTROLLED local ACK"}
    service.broker_adapter._instrument_cache[symbol] = "CONTROLLED-" + symbol
    return event, histories


def test_recorded_scope_is_complete_and_day_only():
    assert RECORDED["complete"] is True
    assert len(RECORDED["orders"]) == 135
    assert len(RECORDED["intents"]) == 25
    assert len(RECORDED["audits"]) == 237
    assert RECORDED["prior_working_or_null"] == []
    assert {r["time_in_force"] for r in RECORDED["orders"]} == {"day"}
    assert all(not v["truncated"] for v in RECORDED["limits"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,count", [("AIXI", 48), ("FLYE", 38)])
async def test_recorded_dispatch_replay(lane, symbol, count):
    event, histories = recorded_setup(lane, symbol)
    assert len(histories) == count
    if symbol == "AIXI":
        assert any(r["submitted_at"] == "2026-10-08T11:50:23.331252+00:00" and r["status"] == "filled"
                   and r["payload"]["fanout_segment_id"] == "1791459182644" for r in histories)
    with lane[2]() as session:
        if symbol == "AIXI":
            assert lane[0]._mirrorhold_gate(session, event) == "dispatch_uncertain"
        else:
            assert lane[0]._mirrorhold_gate(session, event) is None
    assert dispatch(lane, event) is None
    assert state(lane, event)["phase"] == "dispatching"
    # Run the real adapter against the existing fake SDK, never a live endpoint.
    from project_mai_tai.broker_adapters.protocols import OrderRequest
    request = OrderRequest(lane[0]._build_client_order_id(event), event.payload.broker_account_name,
                           event.payload.strategy_code, symbol, "buy", "open", event.payload.quantity,
                           event.payload.reason, metadata=event.payload.metadata)
    reports = await lane[0].broker_adapter.submit_order(request)
    assert reports[0].event_type == "accepted"
    assert lane[1].calls["place"] == 1
    assert reports[0].metadata["webull_wire_submitted_at_utc"]


@pytest.mark.parametrize("account", ["live:orb", "live:schwab_1m_v2"])
@pytest.mark.parametrize("clock,old,current", [
    ("2026-10-08T13:35:05+00:00", "2026-10-08T07:59:59+00:00", "2026-10-08T08:00:00+00:00"),
    ("2026-10-08T07:59:59+00:00", "2026-10-07T07:59:59+00:00", "2026-10-07T08:00:00+00:00"),
])
def test_session_anchor_both_clocks_and_accounts(lane, monkeypatch, caplog, account, clock, old, current):
    event = prepare(lane, account=account, now=clock)
    # Pre-04:00 controls isolate the scan while holding the unrelated RTH window open.
    monkeypatch.setattr(lane[0], "_nfq_window_open", lambda event: True)
    seed_order(lane, event, status="rejected", submitted_at=datetime.fromisoformat(old))
    seed_order(lane, event, status="pending", submitted_at=datetime.fromisoformat(current))
    assert dispatch(lane, event) == "mirrorhold_dispatch_uncertain"
    assert any(r.levelname == "WARNING" and r.message ==
               f"[OMS-MIRRORHOLD1] account={account} symbol=AIXI phase=refused reason=mirrorhold_dispatch_uncertain orders_considered=1"
               for r in caplog.records)
    assert lane[1].calls.get("place", 0) == 0


@pytest.mark.parametrize("status,reason", [("pending", "dispatch_uncertain"), ("accepted", "duplicate_buy"),
                                          ("cancelled", "dispatch_uncertain"), ("aborted", "dispatch_uncertain")])
def test_current_orders_still_refuse_and_warn(lane, caplog, status, reason):
    event = prepare(lane)
    seed_order(lane, event, status=status)
    assert dispatch(lane, event) == "mirrorhold_" + reason
    assert any(r.levelname == "WARNING" and r.message.endswith("reason=mirrorhold_" + reason + " orders_considered=1")
               for r in caplog.records)
    assert state(lane, event)["wire_submissions"] == 0


@pytest.mark.parametrize("tif", ["day", "gtc"])
@pytest.mark.parametrize("status", ["pending", "accepted", "partially_filled", "aborted"])
def test_prior_working_orders_stay_conservative_across_tif(lane, tif, status):
    event = prepare(lane)
    seed_order(lane, event, status=status, tif=tif, submitted_at=datetime(2026, 8, 25, 15, tzinfo=UTC))
    assert dispatch(lane, event) in {"mirrorhold_dispatch_uncertain", "mirrorhold_duplicate_buy"}
    assert lane[1].calls.get("place", 0) == 0


@pytest.mark.parametrize("slot", ["same", "different", "absent"])
@pytest.mark.parametrize("fill_evidence", ["filled", "partially_filled", "fill_row"])
def test_only_same_slot_fill_across_age_retires_before_pending(lane, slot, fill_evidence):
    event = prepare(lane)
    seed_order(lane, event)  # Unrelated pending must not mask a known mirror fill.
    md = {"fanout_segment_id": event.payload.metadata["fanout_segment_id"]}
    if slot != "absent":
        md["fanout_slot_id"] = event.payload.metadata["fanout_slot_id"] if slot == "same" else "CONTROLLED-other"
    order_id = seed_order(lane, event, status="cancelled" if fill_evidence == "fill_row" else fill_evidence,
                          metadata=md, submitted_at=datetime(2026, 8, 25, 15, tzinfo=UTC))
    if fill_evidence == "fill_row":
        with lane[2]() as session:
            order = session.get(BrokerOrder, order_id)
            session.add(Fill(order_id=order_id, broker_account_id=order.broker_account_id, strategy_id=order.strategy_id,
                             symbol="AIXI", side="buy", quantity=Decimal("1"), price=Decimal("2.27"),
                             filled_at=lane[3][0]))
            session.commit()
    if slot == "same":
        assert dispatch(lane, event) == "mirrorhold_mirror_filled"
        assert state(lane, event)["phase"] == "retired"
    else:
        assert dispatch(lane, event) == "mirrorhold_dispatch_uncertain"
        assert state(lane, event)["phase"] == "held"
    assert state(lane, event)["wire_submissions"] == 0


@pytest.mark.parametrize("age", ["current", "old"])
def test_unrelated_filled_segment_does_not_retire(lane, age):
    event = prepare(lane)
    seed_order(lane, event, status="filled", submitted_at="current" if age == "current"
               else datetime(2026, 8, 25, 15, tzinfo=UTC))
    assert dispatch(lane, event) is None
    assert state(lane, event)["phase"] == "dispatching"


@pytest.mark.parametrize("metadata", [{"fanout_segment_id": None}, {"fanout_segment_id": "bad"},
                                    {"fanout_segment_id": False}, {"fanout_segment_id": True},
                                    {"fanout_segment_id": "0"}, {"fanout_segment_id": "00"},
                                    {"fanout_segment_id": 0}, {"fanout_segment_id": ""},
                                    {"fanout_slot_id": "orphan"}, ["unreadable"]])
def test_unreadable_historical_identity_is_considered_and_warned(lane, caplog, metadata):
    event = prepare(lane)
    seed_order(lane, event, status="rejected", metadata=metadata, submitted_at=datetime(2026, 8, 25, 15, tzinfo=UTC))
    assert dispatch(lane, event) == "mirrorhold_dispatch_uncertain"
    assert any(r.levelname == "WARNING" and "reason=mirrorhold_dispatch_uncertain orders_considered=1" in r.message
               for r in caplog.records)


@pytest.mark.parametrize("status", ["pending", "rejected", "filled"])
def test_null_timestamp_cannot_age_out_even_with_old_updated_at(lane, caplog, status):
    event = prepare(lane)
    order_id = seed_order(lane, event, status=status, submitted_at=None)
    with lane[2]() as session:
        session.get(BrokerOrder, order_id).updated_at = datetime(2026, 8, 25, 15, tzinfo=UTC)
        session.commit()
    assert dispatch(lane, event) == "mirrorhold_dispatch_uncertain"
    assert any("orders_considered=1" in r.message for r in caplog.records)


def test_production_naive_and_unreadable_timestamp_are_not_old(lane):
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    for timestamp in (None, "2026-08-25", datetime(2026, 8, 25)):
        assert lane[0]._mirrorhold_timestamp_readable(session, timestamp) is False


def test_integer_same_segment_survives_sql_bound(lane):
    event = prepare(lane)
    seed_order(lane, event, status="filled", submitted_at=datetime(2026, 8, 25, 15, tzinfo=UTC),
               metadata={"fanout_segment_id": 1791466502449, "fanout_slot_id": event.payload.metadata["fanout_slot_id"]})
    assert dispatch(lane, event) == "mirrorhold_mirror_filled"


@pytest.mark.parametrize("slot", ["different", "absent", "null", "empty"])
def test_other_or_unreadable_slot_fill_never_retires_this_slot(lane, slot):
    event = prepare(lane)
    md = {"fanout_segment_id": event.payload.metadata["fanout_segment_id"]}
    if slot != "absent":
        md["fanout_slot_id"] = {"different": "CONTROLLED-other", "null": None, "empty": ""}[slot]
    seed_order(lane, event, status="filled", metadata=md, submitted_at=datetime(2026, 8, 25, 15, tzinfo=UTC))
    if slot == "different":
        assert dispatch(lane, event) is None
        assert state(lane, event)["phase"] == "dispatching"
    else:
        assert dispatch(lane, event) == "mirrorhold_dispatch_uncertain"
        assert state(lane, event)["phase"] == "held"


@pytest.mark.parametrize("proven", [True, False])
def test_current_legacy_fill_before_segment_requires_actual_fill_proof(lane, proven):
    event = prepare(lane)
    order_id = seed_order(lane, event, status="filled", metadata={}, submitted_at=datetime(2026, 10, 8, 11, tzinfo=UTC))
    if proven:
        with lane[2]() as session:
            order = session.get(BrokerOrder, order_id)
            session.add(Fill(order_id=order_id, broker_account_id=order.broker_account_id, strategy_id=order.strategy_id,
                             symbol="AIXI", side="buy", quantity=Decimal("1"), price=Decimal("2.27"),
                             filled_at=datetime(2026, 10, 8, 11, 1, tzinfo=UTC)))
            session.commit()
    if proven:
        assert dispatch(lane, event) is None
    else:
        assert dispatch(lane, event) == "mirrorhold_dispatch_uncertain"
    assert state(lane, event)["phase"] == ("dispatching" if proven else "held")


def test_sql_does_not_materialize_unrelated_old_terminal_rows(lane):
    event = prepare(lane)
    old_id = seed_order(lane, event, status="rejected", submitted_at=datetime(2026, 8, 25, 15, tzinfo=UTC))
    loaded = []

    def load(session, instance):
        if isinstance(instance, BrokerOrder):
            loaded.append(instance.id)

    sqlalchemy_event.listen(lane[2], "loaded_as_persistent", load)
    try:
        assert dispatch(lane, event) is None
    finally:
        sqlalchemy_event.remove(lane[2], "loaded_as_persistent", load)
    assert old_id not in loaded


def test_postgresql_compiled_query_preserves_bounds_and_ambiguity(lane):
    event = prepare(lane)
    captured = []

    class CaptureSession:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def scalars(self, query):
            captured.append(query)
            return SimpleNamespace(all=lambda: [])

    assert lane[0]._mirrorhold_session_orders(CaptureSession(), SimpleNamespace(id=UUID("3bf71604-e975-4339-9ac1-6ce82117b1ea")), event) == []
    compiled = str(captured[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "broker_orders.submitted_at >= '2026-10-08 08:00:00+00:00'" in compiled
    assert "broker_orders.submitted_at IS NULL" in compiled
    assert "broker_orders.status IS NULL" in compiled
    assert "1791466502449" in compiled
    assert "jsonb_typeof" in compiled and " ? " in compiled and " ~ " in compiled
    assert "live:schwab" not in compiled


@pytest.mark.parametrize("foreign", ["account", "symbol", "side"])
def test_only_this_account_symbol_buy_can_block(lane, foreign):
    event = prepare(lane)
    seed_order(lane, event, account="live:schwab_1m_v2" if foreign == "account" else None,
               symbol="FLYE" if foreign == "symbol" else None, side="sell" if foreign == "side" else "buy")
    assert dispatch(lane, event) is None


def test_queue_gate_remains_all_history_while_dispatch_is_session_bound(lane):
    event = prepare(lane)
    seed_order(lane, event, status="rejected", submitted_at=datetime(2026, 8, 25, 15, tzinfo=UTC))
    with lane[2]() as session:
        assert lane[0]._mirrorhold_gate(session, event) == "dispatch_uncertain"
    assert lane[0]._mirrorhold_prepare_queue("AIXI", [row_id(event)]) == []
    assert state(lane, event)["phase"] == "held"
    assert dispatch(lane, event) is None


@pytest.mark.parametrize("failure,code", [("missing", "owner_missing"), ("shutdown", "shutdown"),
                                          ("cap", "actual_submission_cap"), ("quantity", "stale_generation_or_quantity"),
                                          ("client", "duplicate_client"), ("phase", "owner_uncertain"),
                                          ("quote", "fresh_in_band_required"), ("segment", "segment_ended")])
def test_every_dispatch_refusal_is_warning_with_actual_scan_count(lane, caplog, failure, code):
    event = prepare(lane)
    with lane[2]() as session:
        row = session.get(DashboardSnapshot, row_id(event))
        data = dict(row.payload)
        if failure == "missing":
            session.delete(row)
        elif failure == "cap":
            row.payload = {**data, "wire_submissions": 4}
        elif failure == "client":
            row.payload = {**data, "wire_clients": [lane[0]._build_client_order_id(event)]}
        elif failure == "phase":
            row.payload = {**data, "phase": "uncertain"}
        session.commit()
    if failure == "shutdown":
        lane[0]._symbol_tick_work_closing = True
    elif failure == "quantity":
        event.payload.quantity = Decimal("2")
    elif failure == "quote":
        quote(lane, event, "1")
    elif failure == "segment":
        FanoutSegmentIdentityStore(lane[2]).record("AIXI", 1791466502449, False, "CONTROLLED end",
                                                 now=lane[3][0] + timedelta(milliseconds=1))
    assert dispatch(lane, event) == "mirrorhold_" + code
    assert any(r.levelname == "WARNING" and r.message ==
               f"[OMS-MIRRORHOLD1] account=live:orb symbol=AIXI phase=refused reason=mirrorhold_{code} orders_considered=0"
               for r in caplog.records)
