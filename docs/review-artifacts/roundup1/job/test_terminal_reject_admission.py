"""Four actual October6 records: gate admission, never ownership release."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
import census_readonly as gate


def recorded():
    return json.loads(Path(__file__).with_name(
        "TERMINAL_REJECTS_RECORDED_2026-10-06.json").read_text())


def admit(data, row=None):
    return gate.no_broker_id_terminal(row or data["orders"][-1],
        data["events"], data["intents"], data["fills"])


def test_all_four_recorded_rejects_are_terminal_without_fabricated_broker_events():
    data = recorded()
    assert len(data["orders"]) == 4 and len(data["events"]) == 3
    assert [admit(data, row)["kind"] for row in data["orders"]] == [
        "broker_submit_reject_event"] * 3 + ["client_abort_intent"]
    assert data == recorded()


@pytest.mark.parametrize("field,value", [("status", "accepted"), ("broker_order_id", "present"),
    ("client_order_id", "foreign"), ("side", "sell"), ("strategy", "orb"), ("account", "foreign")])
def test_recorded_client_abort_needs_exact_rejected_no_id_buy_identity(field, value):
    data = recorded()
    data["orders"][-1][field] = value
    with pytest.raises(gate.Stop):
        admit(data)


@pytest.mark.parametrize("field,value", [("status", "accepted"), ("account", "live:orb"),
    ("symbol", "MOBX"), ("side", "sell"), ("strategy", "orb")])
def test_recorded_client_abort_intent_must_match_order(field, value):
    data = recorded()
    data["intents"][0][field] = value
    with pytest.raises(gate.Stop):
        admit(data)


@pytest.mark.parametrize("field,value", [("refusal_origin", "broker"), ("refusal_code", "")])
def test_recorded_intent_requires_explicit_local_abort(field, value):
    data = recorded()
    data["intents"][0]["payload"][field] = value
    with pytest.raises(gate.Stop):
        admit(data)


def test_missing_proof_or_any_recorded_fill_blocks():
    data = recorded()
    data["fills"] = [dict(order_id=data["orders"][-1]["id"], quantity="1")]
    with pytest.raises(gate.Stop):
        admit(data)
    data["fills"] = []
    data["intents"] = []
    with pytest.raises(gate.Stop):
        admit(data)


@pytest.mark.parametrize("field,value", [("order_id", "foreign"), ("event_type", "accepted"),
    ("event_source", "client")])
def test_recorded_broker_reject_event_must_match(field, value):
    data = recorded()
    event = next(event for event in data["events"] if event["order_id"] == data["orders"][0]["id"])
    event[field] = value
    with pytest.raises(gate.Stop):
        admit(data, data["orders"][0])


@pytest.mark.parametrize("field,value", [("client_order_id", "foreign"),
    ("broker_order_id", "present"), ("broker_fill_id", "present")])
def test_broker_reject_payload_has_no_order_or_fill_and_matches_client(field, value):
    data = recorded()
    event = next(event for event in data["events"] if event["order_id"] == data["orders"][0]["id"])
    event["payload"][field] = value
    with pytest.raises(gate.Stop):
        admit(data, data["orders"][0])


@pytest.mark.parametrize("field,value", [("webull_http_status", "200"),
    ("webull_http_status", "unknown"), ("webull_wire_submitted_at_utc", None)])
def test_recorded_submit_rejection_needs_4xx_submit_evidence(field, value):
    data = recorded()
    event = next(event for event in data["events"] if event["order_id"] == data["orders"][0]["id"])
    event["payload"]["metadata"][field] = value
    with pytest.raises(gate.Stop):
        admit(data, data["orders"][0])


def test_client_attempt_identity_cannot_be_borrowed_from_another_intent():
    data = deepcopy(recorded())
    data["intents"][0]["payload"]["metadata"]["fanout_attempt_id"] = "foreign"
    with pytest.raises(gate.Stop):
        admit(data)
