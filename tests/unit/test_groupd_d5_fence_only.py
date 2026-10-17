"""TOP211 D5 rework: existing receipt fence only, no accounting/recovery owner."""

from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExitPairReleaseResult
from project_mai_tai.db.models import Fill, OmsManagedPosition, SystemIncident
from tests.unit.test_legacy_cancelled_partial_handover import (
    fake_sdk as fake_sdk, handover, legacy_case, make_case as make_case,
)
from tests.unit.test_wbattach1_riskoff import closes, restarted


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["open", "closed", "absent"])
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
async def test_partial_fence_has_one_incident_and_never_reenters_release(
        make_case, fake_sdk, monkeypatch, prior, account):
    case = make_case(account=account)
    service, factory, broker, entry_id, _ = case
    row_id, client, _ = legacy_case(case, monkeypatch, 2)
    old_id = None
    old_payload = None
    if prior != "absent":
        with factory.begin() as session:
            service._sync_exit_release_incident(session, session.get(OmsManagedPosition, UUID(row_id)),
                ExitPairReleaseResult("unanswerable"), reason="V2_EOD_OCO_TRANSITION", protective=False)
            session.flush()
            old_id = session.scalar(select(SystemIncident.id))
            if prior == "closed":
                session.get(SystemIncident, old_id).status = "closed"
                old_payload = deepcopy(session.get(SystemIncident, old_id).payload)
    assert await handover(case, "ordinary", row_id) == "unanswerable"
    assert client.seen
    reads = len(client.seen)
    with factory.begin() as session:
        service._sync_exit_release_incident(session, session.get(OmsManagedPosition, UUID(row_id)),
            ExitPairReleaseResult("unanswerable"), reason="V2_EOD_OCO_TRANSITION", protective=False)
    for actual in (case, restarted(case)):
        assert await handover(actual, "ordinary", row_id) == "unanswerable"
        assert len(client.seen) == reads and not closes(broker)
    with factory() as session:
        incident, = session.scalars(select(SystemIncident).where(SystemIncident.status == "open")).all()
        assert incident.payload["source"] == service._EXIT_RELEASE_INCIDENT_SOURCE
        assert incident.payload["fence"] == "unaccounted_child"
        assert incident.payload["owner_proof"] == "UNKNOWN"
        assert incident.payload["entry_order_id"] == str(entry_id)
        assert incident.payload["unaccounted_child_state"]["managed_row_id"] == row_id
        assert incident.payload["execution_binding"] == "ORIGINAL_PAIR"
        assert incident.payload["execution_binding"] == incident.payload["unaccounted_child_state"]["execution_binding"]
        assert session.get(OmsManagedPosition, UUID(row_id)).current_quantity == 10
        assert not list(session.scalars(select(Fill).where(Fill.side == "sell")))
        if prior == "open":
            assert incident.id == old_id
        elif prior == "closed":
            assert incident.id != old_id and session.get(SystemIncident, old_id).status == "closed"
    with factory.begin() as session:
        for incident in session.scalars(select(SystemIncident)):
            incident.status = "closed"
    assert await handover(restarted(case), "ordinary", row_id) == "unanswerable"
    assert len(client.seen) == reads and not closes(broker)
    with factory() as session:
        incidents = list(session.scalars(select(SystemIncident)))
        fenced = [item for item in incidents if item.payload.get("fence") == "unaccounted_child"]
        assert fenced
        assert {item.id for item in incidents if item not in fenced} == (
            {old_id} if prior == "closed" else set())
        for incident in fenced:
            assert incident.payload["execution_binding"] == "ORIGINAL_PAIR"
            assert incident.payload["execution_binding"] == incident.payload["unaccounted_child_state"]["execution_binding"]
        if prior == "closed":
            original = session.get(SystemIncident, old_id)
            assert original.status == "closed" and original.payload == old_payload
