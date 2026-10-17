"""Typed accounting projection must not flatten or alias durable ORB state."""

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BotControlAttempt, BrokerOrder
from project_mai_tai.oms import orb_schwab_eod as owner
from project_mai_tai.oms.bot_control_orb import _cancel_entry, _entry_accounting_metadata
from tests.unit.test_killsw1_legacy import legacy as legacy
from tests.unit.test_orbroute1_driver_cleanup import prepare


def test_accounting_projection_retains_scalars_without_aliasing_or_stringifying_journal():
    metadata = {
        owner._STATE: {"phase": "submitting", "nested": {"exit_coid": "exact-original"}},
        "broker_order_id": "exact-root", "entry_order_id": "exact-entry",
        "orb_country_route": "true", "orb_country_trigger": "6.60",
        "orb_stop_broker_cumulative_quantity": "10", "orb_stop_broker_cumulative_average": "6.6",
        "bot_control_generation": 2, "extended_hours": False, "limit_price": Decimal("6.60"),
        "finite_ratio": 0.5,
    }
    before = deepcopy(metadata)
    projected = _entry_accounting_metadata(metadata)
    assert owner._STATE not in projected and metadata == before
    assert projected == {
        "broker_order_id": "exact-root", "entry_order_id": "exact-entry",
        "orb_country_route": "true", "orb_country_trigger": "6.60",
        "orb_stop_broker_cumulative_quantity": "10", "orb_stop_broker_cumulative_average": "6.6",
        "bot_control_generation": "2", "extended_hours": "false", "limit_price": "6.60",
        "finite_ratio": "0.5",
    }
    projected["entry_order_id"] = "changed-event-copy"
    assert metadata == before


@pytest.mark.parametrize("value", [{"not": "journal"}, ["structured"], None,
                                   float("inf"), Decimal("NaN")])
def test_unknown_structured_or_nonfinite_accounting_values_are_not_coerced(value):
    with pytest.raises(ValueError, match="^orb_stop_entry_structured_or_nonfinite_metadata_unknown$"):
        _entry_accounting_metadata({"route_or_cap": value})


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["schwab_1m_v2"], indirect=True)
@pytest.mark.parametrize("owner_progresses", [False, True])
async def test_webull_cancel_keeps_full_durable_payload_and_isolates_wire_mutation(legacy, owner_progresses):
    service = prepare(legacy, "webull")
    with legacy.sessions.begin() as session:
        entry = session.get(BrokerOrder, legacy.entries[0])
        entry.payload = {**entry.payload,
            owner._STATE: {"phase": "normal_owner_sentinel", "nested": {"binding": "original"}},
            "orb_country_route": "true", "orb_country_trigger": "6.60",
            "entry_order_id": str(entry.id), "bot_control_generation": "1"}
        original_payload = deepcopy(entry.payload)
    expected_payload = deepcopy(original_payload)
    captured = {}
    original_submit = legacy.broker.submit_order
    original_writer = service._record_order_reports

    async def submit(request):
        assert request.metadata[owner._STATE] == original_payload[owner._STATE]
        request.metadata[owner._STATE]["nested"]["binding"] = "mutated-wire-copy"
        if owner_progresses:
            # A concurrent normal-owner write is not owned by entry cancellation.
            expected_payload[owner._STATE] = {"phase": "normal_owner_progressed", "nested": {"binding": "newer"}}
            with legacy.sessions.begin() as session:
                session.get(BrokerOrder, legacy.entries[0]).payload = deepcopy(expected_payload)
        return await original_submit(request)

    async def record(**kwargs):
        event, request = kwargs["intent_event"], kwargs["request"]
        captured["event"] = dict(event.payload.metadata)
        captured["accounting"] = deepcopy(request.metadata)
        assert request.metadata == expected_payload
        assert owner._STATE not in event.payload.metadata
        return await original_writer(**kwargs)

    legacy.broker.submit_order = submit
    service._record_order_reports = record
    await _cancel_entry(service, legacy.lease, legacy.journal, legacy.entries[0], lambda: datetime.now(UTC))
    assert len(legacy.broker.sent) == 1
    assert captured["accounting"] == expected_payload
    assert captured["event"]["broker_order_id"] == "root-0"
    for key in ("orb_country_route", "orb_country_trigger", "entry_order_id", "bot_control_generation"):
        assert captured["event"][key] == original_payload[key]
    with legacy.sessions() as session:
        entry = session.get(BrokerOrder, legacy.entries[0])
        assert entry.status == "cancelled" and entry.payload == expected_payload
        attempt, = session.scalars(select(BotControlAttempt)).all()
        assert attempt.status == "TERMINAL" and attempt.operation == "CANCEL_ENTRY"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["schwab_1m_v2"], indirect=True)
@pytest.mark.parametrize("value", [{"unexpected": "structure"}, ["unexpected"]])
async def test_webull_unknown_structure_refuses_before_cancel_and_preserves_payload(legacy, value):
    service = prepare(legacy, "webull")
    with legacy.sessions.begin() as session:
        entry = session.get(BrokerOrder, legacy.entries[0])
        entry.payload = {**entry.payload, "route_or_cap": value}
        original_payload = deepcopy(entry.payload)
    with pytest.raises(ValueError, match="^orb_stop_entry_structured_or_nonfinite_metadata_unknown$"):
        await _cancel_entry(service, legacy.lease, legacy.journal, legacy.entries[0], lambda: datetime.now(UTC))
    assert not legacy.broker.sent and not legacy.broker.reads
    with legacy.sessions() as session:
        assert session.get(BrokerOrder, legacy.entries[0]).payload == original_payload
        assert session.scalar(select(BotControlAttempt.attempt_id)) is None
