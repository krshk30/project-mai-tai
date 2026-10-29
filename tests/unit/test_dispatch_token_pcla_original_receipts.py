"""Original B-full operands; author SOURCE ONLY, Main executes after review.

Packet: three current order rows/events + nine wait rows, not parent intents or
an account-wide native book. Receipt-first metadata reconstruction is explicit;
the first placement event's UNKNOWN provenance is never upgraded to broker.
No historical row is repaired: all writer probes use detached private objects.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot
from project_mai_tai.oms.store import OmsStore
from tests.unit.test_brokerdecides1_local_census import census


PACKET = Path("/Users/velkris/.codex/task-queues/analysis/OCT29-C6-ORIGINAL-CASE-RECEIPTS/"
              "bfull_pcla_orders_and_attempts_1029.jsonl")
PACKET_SHA = "666e1ae7c7d68c2cd90335af2765c463b1abfc50c55d0e5124bc93fb96479ac3"
ORDER_LINES = {
    "5a204ec96e9e": (1, "a1033f783336a2bb43f5234bbad38eb30f6b23c2811f86fe1042ea4a4428f884"),
    "a1eaa924ed97": (2, "f942111f87ae6792c05562c0b2e9757233411f19f1cb293ad2212c9260ec9820"),
    "ee98086794eb": (3, "6f5907d4de4e2a134bb1af6326dcebf66a2a9f779d29bb5be20197ae56d2d0df"),
}
ORIGINAL = {
    "5a204ec96e9e": ("02dd3eab-2053-46e0-b6ef-faeebaf6942b", "ffc58d09-e9ed-43c3-b76e-1a3a4223f8fb",
                      "3757974d-555d-4135-b6b3-1c73b724ee68", "ac6f143c-bcf5-4cfc-9205-d04c3ebc400d"),
    "a1eaa924ed97": ("b9225b77-9c8f-4519-9be4-4b868496d3fc", "05627c3e-20fc-413b-979d-a6930ebb96e7",
                      "023547e0-6844-4a65-8a97-311c4ffde029", "1171daa8-0174-4262-bc09-eb6cb4db8a23"),
    "ee98086794eb": ("df215d0b-c61a-45bb-ab35-9cc90a0eca88", "d407a0ab-8f8f-4e7e-b951-6f4b31c9679a",
                      "2a97c8f0-dbe7-4a56-b45e-91cebf443d48", "554e29d9-491e-484c-a1a1-c7dfce26c20f"),
}
SEGMENT = "1793281982992"
SOURCE_ACCOUNTS = ("live:schwab_1m_v2", "live:orb")


def retained_packet():
    # Exactly one bounded existing file. Decimal retains NUMERIC operands.
    with PACKET.open("rb") as source:
        raw = source.read(128 * 1024 + 1)
    assert len(raw) == 33924 and hashlib.sha256(raw).hexdigest() == PACKET_SHA
    lines = raw.splitlines(keepends=True)
    assert len(lines) == 12 and all(len(line) <= 32 * 1024 for line in lines)
    for number, digest in ORDER_LINES.values():
        assert hashlib.sha256(lines[number - 1]).hexdigest() == digest
    assert hashlib.sha256(b"".join(lines[3:])).hexdigest() == "8fb3f7b1bac018d3914104acd0a51cff014fb5a9bf8df0be50bbee6e6301bac6"
    return [json.loads(line, parse_float=Decimal) for line in lines]


def case_from_packet(suffix):
    packet = retained_packet()
    row = packet[ORDER_LINES[suffix][0] - 1]
    order_id, token, placement_id, rewrite_id = ORIGINAL[suffix]
    assert row["id"] == order_id and row["client_order_id"] == "schwab_1m_v2-PCLA-open-" + suffix
    assert row["symbol"] == "PCLA" and row["side"] == "buy" and row["status"] == "cancelled"
    assert isinstance(row["quantity"], Decimal)
    events = sorted(row["events"], key=lambda event: (datetime.fromisoformat(event["event_at"]), event["id"]))
    assert all(event["order_id"] == order_id for event in events)
    placement = events[0]
    assert placement["id"] == placement_id and placement["event_type"] == "accepted"
    assert placement["event_source"] == "unknown"  # Never relabelled native acceptance.
    metadata = placement["payload"]["metadata"]
    assert metadata["clearwait_dispatch_token"] == token and metadata["fanout_segment_id"] == SEGMENT
    rewrites = [event for event in events if event["payload"].get("metadata", {}).get("resting_entry_cancel") == "true"]
    assert rewrites[0]["id"] == rewrite_id
    assert all("clearwait_dispatch_token" not in event["payload"]["metadata"] for event in rewrites)
    assert "clearwait_dispatch_token" not in row["payload"] and row["payload"]["fanout_segment_id"] == SEGMENT
    matches = [wait for wait in packet[3:] if wait["payload"].get("attempt_token") == token]
    assert len(matches) == 1
    wait = matches[0]
    assert wait["payload"]["kind"] == "attempt" and wait["payload"]["symbol"] == row["symbol"]
    assert wait["payload"]["account_name"] == metadata["atr_country_account"] == "live:orb"
    assert wait["payload"]["opportunity_id"] == metadata["fanout_segment_id"]
    assert datetime.fromisoformat(wait["created_at"]) < datetime.fromisoformat(placement["event_at"])
    return packet, row, placement, rewrites, wait


def private_order(row, payload, *, status):
    """Detached object, not an insert and not a claimed full before-write row.

    Column IDs/quantity/clocks are retained captured values. Only payload/status
    are deliberately selected for this writer probe; updated_at is the captured
    FINAL clock, NOT invented as an earlier row timestamp. Parent payload UNKNOWN.
    """
    return BrokerOrder(id=UUID(row["id"]), intent_id=UUID(row["intent_id"]),
        strategy_id=UUID(row["strategy_id"]), broker_account_id=UUID(row["broker_account_id"]),
        client_order_id=row["client_order_id"], broker_order_id=row["broker_order_id"],
        symbol=row["symbol"], side=row["side"], order_type=row["order_type"], time_in_force=row["time_in_force"],
        quantity=row["quantity"], status=status, payload=deepcopy(payload),
        submitted_at=datetime.fromisoformat(row["submitted_at"]), updated_at=datetime.fromisoformat(row["updated_at"]))


def report_envelope(row, event, *, metadata=None):
    """CONTROLLED ExecutionReport carrier; exact retained event time/body/provenance.

    Symbol/side/quantity come from the retained order because event rows don't
    contain those fields. No native fill quantity is inferred from default0.
    """
    payload = event["payload"]
    assert payload["client_order_id"] == row["client_order_id"]
    assert payload.get("broker_order_id") in (None, row["broker_order_id"])
    return ExecutionReport(event["event_type"], payload["client_order_id"],
        broker_order_id=payload.get("broker_order_id"), broker_fill_id=payload.get("broker_fill_id"),
        symbol=row["symbol"], side=row["side"], intent_type="cancel", quantity=row["quantity"],
        reason=payload.get("reason", ""), metadata=deepcopy(payload["metadata"] if metadata is None else metadata),
        reported_at=datetime.fromisoformat(event["event_at"]), origin=event["event_source"])


def binding(order):
    return (order.id, order.intent_id, order.strategy_id, order.broker_account_id,
            order.client_order_id, order.broker_order_id, order.symbol, order.side,
            order.quantity, order.submitted_at, order.updated_at)


@pytest.mark.parametrize("suffix", tuple(ORIGINAL))
def test_original_pcla_receipt_first_metadata_survives_actual_report_writer(suffix, record_property):
    packet, row, placement, rewrites, wait = case_from_packet(suffix)
    retained_before = deepcopy(packet)
    # Explicit receipt-first reconstruction. The parent-intent payload and a
    # full actual pre-rewrite order payload are not supplied by the packet.
    target = private_order(row, placement["payload"]["metadata"], status=placement["event_type"])
    original_binding = binding(target)
    token = placement["payload"]["metadata"]["clearwait_dispatch_token"]
    store = OmsStore()
    applied = []
    for event in rewrites:
        # The direct cancel verifier records rejected CANCEL_VERIFY_RESUBMIT
        # events but does not update the target order for those refusals.
        if event["event_type"] == "rejected":
            continue
        assert event["event_type"] in {"accepted", "cancelled"}
        report = report_envelope(row, event)
        result = store.update_order_from_report(target, report=report, metadata=report.metadata)
        applied.append(event["id"])
        observation = dict(case=suffix, order_id=row["id"], event_id=event["id"],
            event_at=event["event_at"], event_source=event["event_source"],
            expected_original_token=token, actual_after_token=target.payload.get("clearwait_dispatch_token"),
            actual_after_segment=target.payload.get("fanout_segment_id"), packet_sha256=PACKET_SHA,
            raw_line_sha256=ORDER_LINES[suffix][1], reconstruction="RECEIPT_FIRST_METADATA",
            parent_payload="UNKNOWN_NOT_IN_PACKET")
        record_property("original_pcla_step_" + str(len(applied)), json.dumps(observation, sort_keys=True))
        print("BFULL_ORIGINAL_WRITER_STEP " + json.dumps(observation, sort_keys=True), flush=True)
        assert result is target and binding(target) == original_binding
        assert target.payload.get("clearwait_dispatch_token") == token
        assert target.payload.get("fanout_segment_id") == SEGMENT
        assert target.status == event["event_type"]
    assert target.status == row["status"] and applied
    assert packet == retained_before  # Full event metadata/provenance/clock unchanged.
    witness = dict(case=suffix, order_id=row["id"], parent_intent_id=row["intent_id"],
        parent_payload="UNKNOWN_NOT_IN_PACKET", payload_preimage="RECEIPT_FIRST_RECONSTRUCTION",
        placement_event_id=placement["id"], placement_event_source=placement["event_source"],
        wait_id=wait["id"], original_token=token, applied_event_ids=applied,
        recorded_current_payload_missing_token=True, recorded_current_segment_present=True,
        packet_sha256=PACKET_SHA, raw_line_sha256=ORDER_LINES[suffix][1], native_permission="UNKNOWN")
    record_property("original_pcla_witness", json.dumps(witness, sort_keys=True))
    print("BFULL_ORIGINAL_RECEIPT_FIRST " + json.dumps(witness, sort_keys=True), flush=True)


@pytest.mark.parametrize("suffix", tuple(ORIGINAL))
@pytest.mark.parametrize("target_state", ["as_recorded_missing", "CONTROLLED_null", "CONTROLLED_empty", "CONTROLLED_integer"])
def test_original_current_payload_never_acquires_token_from_receipt_metadata(suffix, target_state):
    packet, row, placement, rewrites, _wait = case_from_packet(suffix)
    before = deepcopy(packet)
    current_payload = deepcopy(row["payload"])
    if target_state != "as_recorded_missing":
        current_payload["clearwait_dispatch_token"] = {
            "CONTROLLED_null": None, "CONTROLLED_empty": "", "CONTROLLED_integer": 123,
        }[target_state]
    target = private_order(row, current_payload, status=row["status"])
    original_binding = binding(target)
    terminal = next(event for event in reversed(rewrites) if event["event_type"] == "cancelled")
    # Deliberately wrong-source incoming metadata: a retained placement receipt
    # is NOT an authorized repair of an already-lost existing target binding.
    report = report_envelope(row, terminal, metadata=placement["payload"]["metadata"])
    OmsStore().update_order_from_report(target, report=report, metadata=report.metadata)
    assert binding(target) == original_binding and target.status == row["status"]
    assert target.payload["fanout_segment_id"] == row["payload"]["fanout_segment_id"]
    if target_state == "as_recorded_missing":
        assert "clearwait_dispatch_token" not in target.payload
    else:
        assert target.payload["clearwait_dispatch_token"] == current_payload["clearwait_dispatch_token"]
    assert packet == before


def test_original_today_wait_rows_stay_unknown_without_proven_own_order_binding(census, record_property):
    packet = retained_packet()
    retained_before = deepcopy(packet)
    session, accounts, _strategy = census
    # Existing private SQLite fixture's account IDs/provider configuration are
    # CONTROLLED. Only account NAMES and journal IDs/payloads/clocks are original.
    for account, name in zip(accounts, SOURCE_ACCOUNTS, strict=True):
        account.name = name
    journals = packet[3:]
    assert len(journals) == 9
    attempts = [row for row in journals if row["payload"]["kind"] == "attempt"]
    assert len(attempts) == 6 and len(journals) - len(attempts) == 3
    for row in journals:
        assert row["payload"]["symbol"] == "PCLA"
        session.add(DashboardSnapshot(id=UUID(row["id"]), snapshot_type="v2_wait_dispatch",
            created_at=datetime.fromisoformat(row["created_at"]), payload=deepcopy(row["payload"])))
    session.flush()
    # No fabricated parent/order/native rows added to manufacture positive closure.
    inflight, fills, terminals = local_evidence(session, SOURCE_ACCOUNTS)
    assert inflight == frozenset({"PCLA"}) and not fills and not terminals
    for row in journals:
        stored = session.get(DashboardSnapshot, UUID(row["id"]))
        assert stored.payload == row["payload"]
        stored_clock = stored.created_at
        if stored_clock.tzinfo is None:
            stored_clock = stored_clock.replace(tzinfo=UTC)  # SQLite transport, same original instant.
        assert stored_clock == datetime.fromisoformat(row["created_at"])
    assert packet == retained_before
    record_property("original_today_unknown", json.dumps(dict(packet_sha256=PACKET_SHA,
        journal_ids=[row["id"] for row in journals], attempts=6, begin_rows=3,
        local_inflight=["PCLA"], native_book="UNKNOWN_NOT_IN_PACKET", retirement="UNCHANGED"), sort_keys=True))
