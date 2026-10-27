"""Native-control-enabled local refusal controls; all identities are synthetic."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill, TradeIntent, VirtualPosition
from tests.unit.test_l7_orb_webull_tickcap import INCIDENT, incident_event, quote
from tests.unit.test_l8_orb_control_envelope import enable_native_control
from tests.unit.test_orbroute1_oms_chain import SCHWAB, setup as setup


def controlled(setup, monkeypatch):
    service, factory, broker = setup
    event = incident_event(setup, monkeypatch, "1.6501").model_copy(update={"produced_at": INCIDENT})
    control, _strategy, _accounts = enable_native_control(service, factory, broker, monkeypatch)
    return event, control


def change_quote(service, monkeypatch, mutation):
    if mutation == "window":
        monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: INCIDENT + timedelta(minutes=5))
    elif mutation == "missing":
        service._latest_quotes_by_symbol.clear()
    else:
        service._latest_quotes_by_symbol["STAK"] = quote(
            "1.65" if mutation == "changed" else "1.67" if mutation == "above_cap" else "1.6501",
            at=INCIDENT - timedelta(minutes=1) if mutation == "stale" else INCIDENT)


def row_snapshot(factory):
    with factory() as session:
        order = session.scalar(select(BrokerOrder))
        parent = session.scalar(select(TradeIntent))
        return deepcopy((order.id, order.intent_id, order.strategy_id, order.broker_account_id,
            order.symbol, order.side, order.quantity, order.order_type, order.time_in_force,
            order.status, order.broker_order_id, order.submitted_at, order.updated_at, order.payload,
            parent.status, parent.updated_at, parent.payload))


def assert_no_economic_rows(factory):
    with factory() as session:
        assert session.scalar(select(Fill.id)) is None
        assert session.scalar(select(VirtualPosition.id)) is None


def test_fresh_native_control_dispatch_still_records_real_permit(setup, monkeypatch):
    service, factory, broker = setup
    event, _control = controlled(setup, monkeypatch)
    result = asyncio.run(service.process_trade_intent(event))
    assert result[-1].payload.status == "accepted"
    assert len(broker.sent) == 1
    assert "bot_control_permit" in broker.sent[0].metadata
    with factory() as session:
        order = session.scalar(select(BrokerOrder))
        assert order.status == "accepted" and order.broker_order_id == "parent-1"
        assert order.submitted_at is not None
        assert "bot_control_permit" in order.payload
        assert "orb_pastlevel_local_no_wire" not in order.payload
        assert session.get(TradeIntent, order.intent_id).status == "submitted"
    assert_no_economic_rows(factory)


@pytest.mark.parametrize("mutation", ["stale", "changed", "window", "missing", "above_cap"])
def test_final_fence_atomically_settles_exact_new_prepared_row_and_intent(setup, monkeypatch, mutation):
    service, factory, broker = setup
    event, _control = controlled(setup, monkeypatch)
    original = service._prepare_bot_control_entry
    captured = {}

    async def prepare(*args, **kwargs):
        identity = await original(*args, **kwargs)
        assert identity is not None
        with factory() as session:
            row = session.get(BrokerOrder, identity)
            assert row.status == "pending" and row.submitted_at is None and row.broker_order_id is None
            assert "bot_control_permit" not in row.payload
            captured.update(identity=identity, payload=deepcopy(row.payload))
        change_quote(service, monkeypatch, mutation)
        return identity

    monkeypatch.setattr(service, "_prepare_bot_control_entry", prepare)
    result = asyncio.run(service.process_trade_intent(event))
    assert result[-1].payload.status == "rejected"
    assert broker.sent == [] and broker.previews == []
    with factory() as session:
        rows = session.scalars(select(BrokerOrder)).all()
        assert len(rows) == 1 and rows[0].id == captured["identity"]
        row = rows[0]
        parent = session.get(TradeIntent, row.intent_id)
        assert row.status == "aborted" and row.broker_order_id is None and row.submitted_at is None
        assert parent.status == "rejected" and parent.payload["refusal_origin"] == "client_abort"
        assert parent.payload["refusal_code"] == row.payload["reject_reason"] == result[-1].payload.reason
        proof = row.payload["orb_pastlevel_local_no_wire"]
        assert proof["intent_id"] == str(parent.id) and proof["client_order_id"] == row.client_order_id
        assert proof["prepared_at"] == captured["payload"]["bot_control_prepared_at"]
        assert "before_submit_under_permit_lock" in proof["proof"]
        assert "bot_control_proven_not_sent" not in row.payload  # Not a forged STOP proof.
        assert "bot_control_permit" not in row.payload
        for key, value in captured["payload"].items():
            assert row.payload[key] == value
        assert session.scalar(select(BrokerOrderEvent.id)) is None
    assert_no_economic_rows(factory)


@pytest.mark.parametrize("evidence", [
    "actual_permit", "unknown_ack", "broker_id", "submitted_at", "accepted", "unknown",
    "false_permit_key", "request_permit", "broker_event", "existing_unpermitted", "wrong_created_id",
])
def test_dispatched_uncertain_or_existing_row_and_parent_are_never_terminalized(setup, monkeypatch, evidence):
    service, factory, broker = setup
    event, control = controlled(setup, monkeypatch)
    original = service._prepare_bot_control_entry
    expected = {}

    async def prepare(session, intent, request, decided_at):
        identity = await original(session, intent, request, decided_at)
        assert identity is not None
        if evidence in {"actual_permit", "unknown_ack"}:
            # Actual native permit method, not a fake "unsent" marker; no delegate/SDK call.
            await control.authorize(request)
        elif evidence == "existing_unpermitted":
            assert await original(session, intent, request, decided_at) is None
        with factory.begin() as unit:
            row = unit.get(BrokerOrder, identity)
            if evidence == "unknown_ack":
                row.status = "unknown"
                unit.get(TradeIntent, row.intent_id).status = "submitted"
            elif evidence == "broker_id":
                row.broker_order_id = "synthetic-possibly-accepted"
            elif evidence == "submitted_at":
                row.submitted_at = INCIDENT
            elif evidence in {"accepted", "unknown"}:
                row.status = evidence
            elif evidence == "false_permit_key":
                row.payload = {**row.payload, "bot_control_permit": False}
            elif evidence == "request_permit":
                request.metadata["bot_control_permit"] = "UNKNOWN"
            elif evidence == "broker_event":
                unit.add(BrokerOrderEvent(order_id=row.id, event_type="unknown", event_source="broker",
                                         payload={"synthetic": True}))
        expected["snapshot"] = row_snapshot(factory)
        change_quote(service, monkeypatch, "stale")
        return None if evidence == "existing_unpermitted" else uuid4() if evidence == "wrong_created_id" else identity

    monkeypatch.setattr(service, "_prepare_bot_control_entry", prepare)
    with pytest.raises(ValueError, match="orb_pastlevel_prepared_dispatch_UNKNOWN"):
        asyncio.run(service.process_trade_intent(event))
    assert broker.sent == [] and broker.previews == []
    assert row_snapshot(factory) == expected["snapshot"]
    assert_no_economic_rows(factory)


@pytest.mark.parametrize("mutation", ["intent_link", "account", "symbol", "quantity", "side",
                                       "order_type", "tif", "payload", "prepared_stamp", "generation"])
def test_changed_exact_preimage_cannot_be_used_as_unsent_cleanup_authority(setup, monkeypatch, mutation):
    service, factory, broker = setup
    event, _control = controlled(setup, monkeypatch)
    original = service._prepare_bot_control_entry
    expected = {}

    async def prepare(*args, **kwargs):
        identity = await original(*args, **kwargs)
        with factory.begin() as session:
            row = session.get(BrokerOrder, identity)
            if mutation == "intent_link":
                row.intent_id = None
            elif mutation == "account":
                row.broker_account_id = session.scalar(select(BrokerAccount.id).where(BrokerAccount.name == SCHWAB))
            elif mutation in {"symbol", "quantity", "side", "order_type", "tif"}:
                setattr(row, "time_in_force" if mutation == "tif" else mutation,
                        {"symbol": "OTHER", "quantity": Decimal(11), "side": "sell",
                         "order_type": "STOP_LIMIT", "tif": "gtc"}[mutation])
            else:
                payload = dict(row.payload)
                if mutation == "payload":
                    payload["unexpected_evidence"] = "UNKNOWN"
                else:
                    payload.pop("bot_control_prepared_at" if mutation == "prepared_stamp" else "bot_control_generation")
                row.payload = payload
        expected["snapshot"] = row_snapshot(factory)
        change_quote(service, monkeypatch, "changed")
        return identity

    monkeypatch.setattr(service, "_prepare_bot_control_entry", prepare)
    with pytest.raises(ValueError, match="orb_pastlevel_prepared_dispatch_UNKNOWN"):
        asyncio.run(service.process_trade_intent(event))
    assert broker.sent == [] and broker.previews == []
    assert row_snapshot(factory) == expected["snapshot"]
    assert_no_economic_rows(factory)


def test_existing_fill_is_a_hard_refusal_and_is_never_rewritten(setup, monkeypatch):
    service, factory, broker = setup
    event, _control = controlled(setup, monkeypatch)
    original = service._prepare_bot_control_entry
    expected = {}

    async def prepare(*args, **kwargs):
        identity = await original(*args, **kwargs)
        with factory.begin() as session:
            row = session.get(BrokerOrder, identity)
            fill = Fill(order_id=row.id, strategy_id=row.strategy_id, broker_account_id=row.broker_account_id,
                broker_fill_id="synthetic-existing-fill", symbol=row.symbol, side="buy",
                quantity=Decimal(1), price=Decimal("1.66"), filled_at=INCIDENT, payload={"synthetic": True})
            session.add(fill)
            session.flush()
            expected["fill_id"] = fill.id
        expected["snapshot"] = row_snapshot(factory)
        change_quote(service, monkeypatch, "stale")
        return identity

    monkeypatch.setattr(service, "_prepare_bot_control_entry", prepare)
    with pytest.raises(ValueError, match="orb_pastlevel_prepared_dispatch_UNKNOWN"):
        asyncio.run(service.process_trade_intent(event))
    assert broker.sent == [] and row_snapshot(factory) == expected["snapshot"]
    with factory() as session:
        fills = session.scalars(select(Fill)).all()
        assert len(fills) == 1 and fills[0].id == expected["fill_id"]
        assert fills[0].broker_fill_id == "synthetic-existing-fill"
        assert fills[0].quantity == Decimal(1) and fills[0].price == Decimal("1.66")
        assert fills[0].payload == {"synthetic": True}
        assert session.scalar(select(VirtualPosition.id)) is None
