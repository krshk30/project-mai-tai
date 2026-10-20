"""AUTHOR ONLY / UNRUN until16 ET.

Controlled DB/broker replies plus hashed actual AIXI stored payloads/logs.
Both packet parts retain the actual BUY parent, Fill and entry-event rows.
They export the close AFTER abort and positions AFTER external flatten.
Pending/no-event/prior-VP prewire reconstruction below is explicitly CONTROLLED,
not a verbatim earlier database snapshot or a claimed executed b79 replay.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill,
    OmsManagedPosition, Strategy, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.db.base import Base
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import managed_sell_prewire, webull_attach_riskoff as wb
from tests.unit.test_wbattach1_riskoff import closes, make_case as make_case


COID = "schwab_1m_v2-AIXI-open-1b6725ce9e74"
PREPARED = datetime(2026, 10, 20, 17, 27, 7, 428419, tzinfo=UTC)
SUBMITTED = datetime(2026, 10, 20, 17, 27, 7, 464168, tzinfo=UTC)
QTY, PRICE = Decimal(95), Decimal("3.14")
# Upstream retained evidence: task-queues/analysis/aixi-1327-packet/aixi_packet.txt.
# Main copies those exact hashed bytes; no application runtime/file export here.
PACKET = Path(__file__).resolve().parents[1] / "fixtures" / "aixi_1327_packet_20261020.txt"
PACKET_SHA = "d6e9806c14884f7f84cb63512cd973d9f8f536d68f5d1b4591ebb8d53c3687f0"
PART2 = PACKET.with_name("aixi_1327_packet_part2_20261020.txt")
PART2_SHA = "b99d870da34edaf0b7f3c61e119e6af5f3f2bc45fb147bbbaceaee4a2f0ff822"


def actual_packet():
    raw = PACKET.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PACKET_SHA
    sections, heading = {}, None
    for line in raw.decode().splitlines():
        if line.startswith("== "):
            heading = line[3:]
            sections[heading] = []
        elif heading is not None:
            sections[heading].append(line)
    def one(name):
        rows = [json.loads(line, parse_float=Decimal) for line in sections[name] if line.startswith("{")]
        assert len(rows) == 1
        return rows[0]
    return {"close": one("close order"), "intent": one("parent intent"),
            "event": one("close order events"), "entry": one("entry order (wbattach1 journal)"),
            "managed": one("managed row"), "lines": raw.decode()}


def actual_packet_part2():
    # Upstream: task-queues/analysis/aixi-1327-packet/aixi_packet_part2.txt.
    raw = PART2.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PART2_SHA
    sections, heading = {}, None
    for line in raw.decode().splitlines():
        if line.startswith("== "):
            heading = line[3:]
            sections[heading] = []
        elif heading is not None and line.startswith("{"):
            sections[heading].append(json.loads(line, parse_float=Decimal))
    assert len(sections["entry BUY parent intent"]) == 1
    assert sections["entry fills"] and sections["entry order events"]
    return sections


def model_values(row):
    values = deepcopy(row)

    def json_numbers(value):
        # parse_float=Decimal preserves numeric database-column precision.
        # JSON columns, however, use the model's ordinary JSON number types,
        # not Decimal objects (and never decimal-as-string substitutions).
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, dict):
            return {key: json_numbers(item) for key, item in value.items()}
        if isinstance(value, list):
            return [json_numbers(item) for item in value]
        return value

    for key, value in values.items():
        if key in {"payload", "entry_classification"}:
            values[key] = json_numbers(value)
        if value is not None and (key == "id" or key.endswith("_id")):
            # COIDs and native IDs are strings; only canonical model UUID columns.
            if key in {"id", "intent_id", "strategy_id", "broker_account_id", "entry_order_id", "order_id"}:
                values[key] = UUID(value)
        if value is not None and (key.endswith("_at") or key == "entry_time"):
            values[key] = datetime.fromisoformat(value)
    return values


@pytest.mark.parametrize("late_stamp", [False, True], ids=["stored-exact-binding", "reported-request-only-stamp"])
def test_hashed_actual_aixi_payload_through_atr_with_explicit_prewire_reconstruction(
        make_case, monkeypatch, late_stamp):
    packet = actual_packet()
    part2 = actual_packet_part2()
    close, parent, entry, managed = (packet[k] for k in ("close", "intent", "entry", "managed"))
    buy_parent = part2["entry BUY parent intent"][0]
    assert buy_parent["id"] == entry["intent_id"]
    assert buy_parent in part2["protect intents"]  # Actual heading includes the last3h intent census.
    assert close in part2["all AIXI live:orb sell orders today (any status)"]
    for fill in part2["entry fills"]:
        assert fill["order_id"] == entry["id"] and fill["side"] == "buy"
        assert fill["strategy_id"] == entry["strategy_id"]
        assert fill["broker_account_id"] == entry["broker_account_id"]
        assert fill["broker_fill_id"] == entry["broker_order_id"] + ":95"
    assert sum(f["quantity"] for f in part2["entry fills"]) == QTY
    assert sum(f["quantity"] * f["price"] for f in part2["entry fills"]) == QTY * PRICE
    vps = [r for r in part2["VP row (all)"] if r["strategy_id"] == entry["strategy_id"]
           and r["broker_account_id"] == entry["broker_account_id"] and r["symbol"] == "AIXI"]
    assert len(vps) == 1 and vps[0]["quantity"] == 0 and vps[0]["average_price"] == 0
    assert vps[0]["updated_at"] == "2026-10-20T17:34:42.523128+00:00"
    assert close["status"] == parent["status"] == "aborted"
    assert close["submitted_at"] is close["broker_order_id"] is None
    assert close["payload"] == parent["payload"]["metadata"]
    assert "cw_submitted_at" not in close["payload"]
    assert close["payload"]["managed_sell_prepared_at"] == PREPARED.isoformat(timespec="microseconds")
    assert "submitted_at=2026-10-20T17:27:07.464168+00:00" in packet["lines"]
    assert entry["payload"][wb.KEY]["exit_phase"] == "claimed"
    assert packet["event"]["payload"]["proven_not_sent"] is True
    assert packet["event"]["payload"]["metadata"] == close["payload"]
    case = aixi_case(make_case, monkeypatch)
    service, factory, broker, _, _ = case
    with factory.begin() as unit:
        # Only the fixture's throwaway SQLite DB, never production state.
        for table in reversed(Base.metadata.sorted_tables):
            unit.execute(table.delete())
        strategy = Strategy(id=UUID(entry["strategy_id"]), code="schwab_1m_v2", name="actual-payload-controlled-DB", execution_mode="live")
        account = BrokerAccount(id=UUID(entry["broker_account_id"]), name=broker.account, provider="webull", environment="production")
        unit.add_all([strategy, account])
        unit.flush()
        # Part2 now supplies actual complete BUY parent/Fill/event rows. Keep
        # those values unchanged; only the close's lifecycle frame is restored.
        unit.add(TradeIntent(**model_values(buy_parent)))
        for exported in part2["protect intents"]:
            if (exported["strategy_id"] == entry["strategy_id"]
                    and exported["broker_account_id"] == entry["broker_account_id"]
                    and exported["id"] not in {buy_parent["id"], parent["id"]}):
                unit.add(TradeIntent(**model_values(exported)))
        unit.add(TradeIntent(**{**model_values(parent), "status": "pending"}))
        unit.flush()
        unit.add(BrokerOrder(**model_values(entry)))
        # Only lifecycle frame is reconstructed: before abort was pending/noevent.
        unit.add(BrokerOrder(**{**model_values(close), "status": "pending"}))
        unit.flush()
        for fill in part2["entry fills"]:
            unit.add(Fill(**model_values(fill)))
        for event in part2["entry order events"]:
            assert event["order_id"] == entry["id"]
            unit.add(BrokerOrderEvent(**model_values(event)))
        unit.add(OmsManagedPosition(**model_values(managed)))
        # The exported VP is already0 after17:34 external flatten. Preserve its
        # exact identity/cumulative PnL, but CONTROLLED historical quantity/cost
        # are needed to isolate the earlier prewire metadata predicate.
        unit.add(VirtualPosition(**{**model_values(vps[0]), "quantity": QTY, "average_price": PRICE}))
    metadata = deepcopy(close["payload"])
    if late_stamp:
        metadata["cw_submitted_at"] = SUBMITTED.isoformat(timespec="microseconds")
    request = OrderRequest(close["client_order_id"], broker.account, "schwab_1m_v2", "AIXI", "sell", "close",
        QTY, parent["reason"], metadata, close["order_type"], close["time_in_force"])
    with factory() as unit:
        if late_stamp:
            with pytest.raises(ValueError, match="wbattach1_prepared_order_not_this_unposted_close"):
                wb._atr(service, unit, broker.account, "AIXI", COID, prepared=request)
        else:
            actual_entry, actual_managed, _, _, owned = wb._atr(service, unit, broker.account, "AIXI", COID, prepared=request)
            assert str(actual_entry.id) == entry["id"] and str(actual_managed.id) == managed["id"]
            assert owned == QTY
    assert broker.requests == []


def aixi_case(make_case, monkeypatch):
    case = make_case()
    service, factory, broker, entry_id, clock = case
    clock[0] = PREPARED
    entry_time = PREPARED - timedelta(minutes=10)
    with factory.begin() as unit:
        entry = unit.get(BrokerOrder, entry_id)
        entry.symbol, entry.client_order_id, entry.quantity = "AIXI", COID, QTY
        entry.submitted_at = entry_time
        parent = unit.get(TradeIntent, entry.intent_id)
        parent.symbol, parent.quantity, parent.created_at = "AIXI", QTY, entry_time
        for fill in unit.scalars(select(Fill)).all():
            fill.symbol, fill.quantity, fill.price, fill.filled_at = "AIXI", QTY, PRICE, entry_time
        managed = unit.scalar(select(OmsManagedPosition))
        managed.symbol, managed.entry_client_order_id, managed.entry_price = "AIXI", COID, PRICE
        managed.original_quantity = managed.current_quantity = 95
        managed.entry_time = entry_time
        proof = {**entry.payload[wb.BARE_FILL_PROOF], "entry_coid": COID}
        entry.payload = {**entry.payload, wb.BARE_FILL_PROOF: proof}
        vp = unit.scalar(select(VirtualPosition))
        vp.symbol, vp.quantity, vp.average_price = "AIXI", QTY, PRICE
        position = unit.scalar(select(AccountPosition))
        position.symbol, position.quantity, position.average_price = "AIXI", QTY, PRICE
        position.source_updated_at = clock[0]
    broker.total = QTY
    old_positions, old_submit, old_report, old_fetch = (
        broker.list_account_positions, broker.submit_order, broker.report, broker.fetch_order_update)

    async def positions(account):
        return [replace(p, symbol="AIXI", average_price=PRICE) for p in await old_positions(account)]

    def report(*args, **kwargs):
        actual = old_report(*args, **kwargs)
        return replace(actual, symbol="AIXI", quantity=QTY if actual.quantity == 10 else actual.quantity,
                       reported_at=clock[0])

    async def submit(request):
        reports = await old_submit(request)
        return [replace(r, symbol="AIXI", reported_at=clock[0],
                        fill_price=(Decimal("3.35") if request.reason.endswith("CW_TARGET") else Decimal("3.00"))
                        if r.filled_quantity else r.fill_price) for r in reports]

    async def fetch(request):
        actual = await old_fetch(request)
        if actual is None:
            return None
        return replace(actual, symbol="AIXI", reported_at=clock[0],
                       quantity=QTY if request.side == "buy" else actual.quantity,
                       filled_quantity=QTY if request.side == "buy" else actual.filled_quantity,
                       fill_price=PRICE if request.side == "buy" else actual.fill_price)

    monkeypatch.setattr(broker, "list_account_positions", positions)
    monkeypatch.setattr(broker, "report", report)
    monkeypatch.setattr(broker, "submit_order", submit)
    monkeypatch.setattr(broker, "fetch_order_update", fetch)
    return case


async def attach_aixi(case):
    return await case[0]._attach_webull_protection(broker_account_name=case[2].account,
        symbol="AIXI", quantity=95, entry_price=float(PRICE),
        strategy_code="schwab_1m_v2", entry_client_order_id=COID)


async def emit_aixi(case, tag="CW_FLIP", *, shared=False):
    service, factory, broker, _, _ = case
    with factory() as unit:
        row = unit.scalar(select(OmsManagedPosition))
        position = service._hydrate_v2_position(row)
        identity = str(row.id)
        context = service._cw_exit_decision_context(broker.account, "AIXI", tag, 3.0, 3.0)
        if tag == "CW_TARGET":
            context.update(cw_target_bound_row_id=identity,
                cw_target_entry_order_id=str(row.entry_order_id), cw_target_entry_price=str(PRICE),
                cw_target_quantity="95")
    if shared:
        return await service._webull_cw_exit_on_shared_path(broker.account, "AIXI", tag=tag,
            ref=3.35 if tag == "CW_TARGET" else 3.0, bid=3.35 if tag == "CW_TARGET" else 3.0,
            expected_row_id=identity, decision_context=context)
    return await service._emit_v2_exit_on_loop(broker.account, "AIXI", position, float(PRICE),
        kind="HARD", reference_price=3.35 if tag == "CW_TARGET" else 3.0,
        reason="oms_v2_managed_exit:"+tag, bid=3.35 if tag == "CW_TARGET" else 3.0,
        close_on_fill=True, expected_managed_row_id=identity, confirmation_context=context)


async def prepare_exact(case):
    service, factory, broker, _, clock = case
    with factory() as unit:
        identity = str(unit.scalar(select(OmsManagedPosition.id)))
    metadata = {"oms_v2_managed_exit": "true", "order_type": "market", "session": "NORMAL",
                "time_in_force": "day", "cw_decided_at": clock[0].isoformat(), "reference_price": "3.0250"}
    event = TradeIntentEvent(source_service="CONTROLLED AIXI regression", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=broker.account, symbol="AIXI",
        side="sell", intent_type="close", quantity=QTY, reason="oms_v2_managed_exit:CW_FLIP",
        metadata=dict(metadata)))
    request = OrderRequest("controlled-AIXI-prepared-close", broker.account, "schwab_1m_v2",
        "AIXI", "sell", "close", QTY, event.payload.reason, dict(metadata), "market", "day")
    intent_id = await managed_sell_prewire.prepare_managed_sell(service, event=event,
        request=request, managed_row_id=identity, now=clock[0], allow_pending_fill_writer=True)
    assert intent_id is not None
    return request, intent_id


@pytest.mark.asyncio
async def test_aixi_reported_late_stamp_fails_exact_atr_binding_but_unmodified_request_passes(make_case, monkeypatch):
    case = aixi_case(make_case, monkeypatch)
    request, intent_id = await prepare_exact(case)
    with case[1]() as unit:
        wb._atr(case[0], unit, case[2].account, "AIXI", COID, prepared=request)
    request.metadata["cw_submitted_at"] = SUBMITTED.isoformat(timespec="microseconds")
    with case[1]() as unit:
        order = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        assert "cw_submitted_at" not in order.payload
        assert "cw_submitted_at" not in unit.get(TradeIntent, intent_id).payload["metadata"]
        assert order.payload["managed_sell_prepared_at"] == PREPARED.isoformat(timespec="microseconds")
        with pytest.raises(ValueError, match="wbattach1_prepared_order_not_this_unposted_close"):
            wb._atr(case[0], unit, case[2].account, "AIXI", COID, prepared=request)
    request.metadata.pop("cw_submitted_at")
    with case[1]() as unit:
        wb._atr(case[0], unit, case[2].account, "AIXI", COID, prepared=request)
    assert closes(case[2]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["CW_FLIP", "CW_HARD_STOP"])
async def test_aixi_composed_non_target_validates_before_submit_stamp_and_sends_once(make_case, monkeypatch, tag):
    case = aixi_case(make_case, monkeypatch)
    assert await attach_aixi(case) is True
    actual_validator, submit = wb.validate_atr_before_wire, case[2].submit_order
    checked, wired = [], []

    async def validator(service, request, identity):
        assert "cw_submitted_at" not in request.metadata
        with case[1]() as unit:
            order = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
            assert order.payload == request.metadata
            assert unit.get(TradeIntent, order.intent_id).payload["metadata"] == request.metadata
        await actual_validator(service, request, identity)
        checked.append(request.client_order_id)
        case[4][0] = SUBMITTED

    async def sender(request):
        if request.metadata.get("webull_exit_only_pair") != "true":
            assert request.client_order_id in checked
            assert request.metadata["cw_submitted_at"] == SUBMITTED.isoformat(timespec="microseconds")
            wired.append(request.client_order_id)
        return await submit(request)

    monkeypatch.setattr(wb, "validate_atr_before_wire", validator)
    monkeypatch.setattr(case[2], "submit_order", sender)
    assert await emit_aixi(case, tag) == "closed"
    assert checked == wired and len(wired) == len(closes(case[2])) == 1
    assert len(case[2].cancelled) == 1 and case[2].total == 0
    with case[1]() as unit:
        managed = unit.scalar(select(OmsManagedPosition))
        assert managed.status == "closed" and managed.current_quantity == 0


@pytest.mark.asyncio
async def test_target_exit_keeps_existing_late_wire_stamp_not_early_preparation_stamp(make_case, monkeypatch):
    case = aixi_case(make_case, monkeypatch)
    service, factory, broker, _, clock = case
    original_target_submit, submit = service._submit_cw_target_order, broker.submit_order
    stages = []

    async def target_submit(request, guards):
        # CW_TARGET does not enter WBATTACH's software validator. Its actual
        # baseline target-submit boundary must still see unstamped preparation.
        assert "cw_submitted_at" not in request.metadata
        with factory() as unit:
            order = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
            assert order.status == "pending" and order.payload == request.metadata
            assert unit.get(TradeIntent, order.intent_id).payload["metadata"] == request.metadata
        stages.append("prepared-without-early-stamp")
        clock[0] = SUBMITTED
        return await original_target_submit(request, guards)

    async def wire(request):
        assert request.reason == "oms_v2_managed_exit:CW_TARGET"
        assert request.metadata["cw_submitted_at"] == SUBMITTED.isoformat(timespec="microseconds")
        stages.append("existing-target-helper-stamped-wire")
        return await submit(request)

    monkeypatch.setattr(service, "_submit_cw_target_order", target_submit)
    monkeypatch.setattr(broker, "submit_order", wire)
    assert await emit_aixi(case, "CW_TARGET") == "closed"
    assert len(closes(case[2])) == 1
    request = closes(case[2])[0]
    assert request.order_type == "limit" and request.quantity == QTY
    assert Decimal(request.metadata["limit_price"]) >= Decimal("3.35")
    assert "cw_decided_at" in request.metadata
    assert request.metadata["cw_submitted_at"] == SUBMITTED.isoformat(timespec="microseconds")
    assert stages == ["prepared-without-early-stamp", "existing-target-helper-stamped-wire"]


@pytest.mark.asyncio
async def test_foreign_pending_sell_is_not_exempted_as_this_prepared_close(make_case, monkeypatch):
    case = aixi_case(make_case, monkeypatch)
    request, intent_id = await prepare_exact(case)
    with case[1].begin() as unit:
        owned = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        unit.add(BrokerOrder(intent_id=intent_id, strategy_id=owned.strategy_id,
            broker_account_id=owned.broker_account_id, client_order_id="controlled-foreign-pending-close",
            symbol="AIXI", side="sell", quantity=QTY, order_type="market", time_in_force="day",
            status="pending", submitted_at=None, payload={"oms_v2_managed_exit": "true"}))
    with case[1]() as unit:
        with pytest.raises(ValueError, match="wbattach1_existing_own_sell"):
            wb._atr(case[0], unit, case[2].account, "AIXI", COID, prepared=request)
    assert closes(case[2]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("flat", [False, True], ids=["YMAT-class-still-held", "externally-flat-no-short"])
async def test_postrelease_refusal_finishes_covered_or_flat_not_permanently_uncovered(make_case, monkeypatch, flat):
    case = aixi_case(make_case, monkeypatch)
    assert await attach_aixi(case) is True
    actual_validator = wb.validate_atr_before_wire
    refused = []

    async def fail_first(service, request, identity):
        await actual_validator(service, request, identity)
        if not refused:
            refused.append(request.client_order_id)
            if flat:
                case[2].total = Decimal(0)  # CONTROLLED external flatten, not actual broker receipt.
            raise ValueError("CONTROLLED final-wire refusal after original release")

    monkeypatch.setattr(wb, "validate_atr_before_wire", fail_first)
    await emit_aixi(case, shared=True)
    # Drain existing owner recovery, not a fabricated successful callback.
    tasks = tuple(case[0].__dict__.get("_confirmation_exit_recovery_tasks", ()))
    if tasks:
        await asyncio.gather(*tasks)
    assert len(refused) == 1
    with case[1]() as unit:
        managed = unit.scalar(select(OmsManagedPosition))
        journal = unit.get(BrokerOrder, case[3]).payload[wb.KEY]
        if flat:
            assert closes(case[2]) == [] and case[2].total == 0
            assert managed.status == "closed" and managed.current_quantity == 0
        elif managed.status == "closed":
            assert managed.current_quantity == 0 and case[2].total == 0
            assert len(closes(case[2])) == 1  # Fresh owned recovery, never duplicate SELL.
        else:
            # A log/page/cleared latch alone is NOT working broker cover.
            pairs = [r for r in case[2].requests if r.metadata.get("webull_exit_only_pair") == "true"]
            assert managed.current_quantity == 95 and managed.status == "open"
            assert len(pairs) >= 2 and pairs[-1].client_order_id != pairs[0].client_order_id
            assert pairs[-1].quantity == QTY and case[2].total == QTY and closes(case[2]) == []
            assert journal["attempts"][-1]["base"] == pairs[-1].client_order_id
            assert journal["attempts"][-1]["phase"] == "accepted"
            assert not journal.get("exit_phase")
        incidents = unit.scalars(select(SystemIncident)).all()
        assert incidents  # Refusal remains visible even when recovery later succeeds.


async def proven_unsent_case(make_case, monkeypatch):
    case = aixi_case(make_case, monkeypatch)
    assert await attach_aixi(case) is True
    original = wb.validate_atr_before_wire

    async def refuse(service, request, identity):
        await original(service, request, identity)
        raise ValueError("CONTROLLED exact never-sent final proof refusal")

    with monkeypatch.context() as patch:
        patch.setattr(wb, "validate_atr_before_wire", refuse)
        assert await emit_aixi(case) == "refused"
    with case[1]() as unit:
        entry = unit.get(BrokerOrder, case[3])
        journal = entry.payload[wb.KEY]
        assert not journal.get("exit_phase")
        receipt = journal["unsent_handoffs"][-1]
        assert receipt["outcome"] == "PROVEN_NOT_SENT"
        order = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == receipt["client_order_id"]))
        assert order.status == "aborted" and order.submitted_at is None and order.broker_order_id is None
        assert unit.get(TradeIntent, order.intent_id).status == "aborted"
        event = unit.get(BrokerOrderEvent, UUID(receipt["abort_event_id"]))
        assert event.event_type == "aborted" and event.payload["proven_not_sent"] is True
        assert event.payload["metadata"] == order.payload
        assert unit.scalar(select(Fill.id).where(Fill.order_id == order.id)) is None
    assert closes(case[2]) == []
    return case


@pytest.mark.asyncio
@pytest.mark.parametrize("flat", [False, True])
async def test_positive_unsent_abort_retries_fresh_owner_or_flat_without_sell(make_case, monkeypatch, flat):
    case = await proven_unsent_case(make_case, monkeypatch)
    with case[1]() as unit:
        identity = str(unit.scalar(select(OmsManagedPosition.id)))
        receipt = unit.get(BrokerOrder, case[3]).payload[wb.KEY]["unsent_handoffs"][-1]
        aborted_coid = receipt["client_order_id"]
    if flat:
        case[2].total = Decimal(0)
    result = await wb.retry_atr_proven_unsent_exit(case[0], case[2].account, "AIXI", identity)
    assert result == "closed"
    wires = closes(case[2])
    assert len(wires) == (0 if flat else 1)
    if wires:
        assert wires[0].client_order_id != aborted_coid and wires[0].quantity == QTY
        assert wires[0].reason == "oms_v2_managed_exit:CW_FLIP"
    assert case[2].total == 0
    with case[1]() as unit:
        row = unit.scalar(select(OmsManagedPosition))
        assert row.status == "closed" and row.current_quantity == 0
    assert not case[0].__dict__.get("_wbattach_exit_owners")


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["claimed", "unknown-original", "wrong-event", "other-event",
    "wrong-intent", "broker-id", "foreign-pending", "wrong-entry", "abort-before-prepared",
    "future-abort", "naive-prepared"])
async def test_unsent_retry_never_waives_unknown_or_foreign_identity(make_case, monkeypatch, damage):
    case = await proven_unsent_case(make_case, monkeypatch)
    with case[1].begin() as unit:
        row = unit.scalar(select(OmsManagedPosition))
        identity = str(row.id)
        entry = unit.get(BrokerOrder, case[3])
        journal = deepcopy(entry.payload[wb.KEY])
        receipt = journal["unsent_handoffs"][-1]
        order = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == receipt["client_order_id"]))
        if damage == "claimed":
            journal["exit_phase"] = "claimed"
        elif damage == "unknown-original":
            journal["attempts"] = [{**a, "phase": "unknown"} for a in journal["attempts"]]
        elif damage == "wrong-event":
            event = unit.get(BrokerOrderEvent, UUID(receipt["abort_event_id"]))
            event.payload = {**event.payload, "client_order_id": "controlled-foreign-close"}
        elif damage == "other-event":
            unit.add(BrokerOrderEvent(order_id=order.id, event_type="accepted", event_source="broker",
                event_at=PREPARED, payload={"source": "CONTROLLED unknown wire"}))
        elif damage == "wrong-intent":
            intent = unit.get(TradeIntent, order.intent_id)
            intent.payload = {**intent.payload, "metadata": {**intent.payload["metadata"], "foreign": "true"}}
        elif damage == "broker-id":
            order.broker_order_id = "CONTROLLED-possibly-sent"
        elif damage in {"wrong-entry", "naive-prepared"}:
            # Keep all three metadata copies equal so the new episode/time
            # predicate itself must refuse, not an older dict-equality check.
            metadata = deepcopy(order.payload)
            if damage == "wrong-entry":
                metadata["entry_order_id"] = "00000000-0000-0000-0000-000000000001"
            else:
                metadata["managed_sell_prepared_at"] = PREPARED.replace(tzinfo=None).isoformat(timespec="microseconds")
            order.payload = metadata
            intent = unit.get(TradeIntent, order.intent_id)
            intent.payload = {**intent.payload, "metadata": deepcopy(metadata)}
            event = unit.get(BrokerOrderEvent, UUID(receipt["abort_event_id"]))
            event.payload = {**event.payload, "metadata": deepcopy(metadata)}
        elif damage in {"abort-before-prepared", "future-abort"}:
            event = unit.get(BrokerOrderEvent, UUID(receipt["abort_event_id"]))
            event.event_at = (PREPARED - timedelta(microseconds=1)
                              if damage == "abort-before-prepared" else case[4][0] + timedelta(seconds=1))
        else:
            unit.add(BrokerOrder(intent_id=order.intent_id, strategy_id=order.strategy_id,
                broker_account_id=order.broker_account_id, symbol="AIXI", side="sell", quantity=QTY,
                client_order_id="controlled-other-working-close", order_type="market", time_in_force="day",
                status="pending", payload={"oms_v2_managed_exit": "true"}))
        entry.payload = {**entry.payload, wb.KEY: journal}
    before = (len(case[2].requests), len(case[2].cancelled))
    if damage == "foreign-pending":
        # The common _atr owner reader raises before helper qualification;
        # the coordinator catches this UNKNOWN without a retry or latch clear.
        with pytest.raises(ValueError, match="wbattach1_existing_own_sell"):
            await wb.retry_atr_proven_unsent_exit(case[0], case[2].account, "AIXI", identity)
    else:
        assert await wb.retry_atr_proven_unsent_exit(case[0], case[2].account, "AIXI", identity) is None
    assert (len(case[2].requests), len(case[2].cancelled)) == before
    assert closes(case[2]) == [] and case[2].total == QTY
    with case[1]() as unit:
        assert unit.get(BrokerOrder, case[3]).payload[wb.KEY] == journal


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_kind", ["missing", "stale", "future", "nan", "fresh"])
async def test_unsent_extended_hours_retry_requires_fresh_bid_and_uses_limit(make_case, monkeypatch, quote_kind):
    from project_mai_tai.oms import service as oms
    case = await proven_unsent_case(make_case, monkeypatch)
    case[4][0] = PREPARED.replace(hour=21)
    monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: "PM")
    monkeypatch.setattr(case[0], "_market_is_fillable", lambda: True)
    with case[1]() as unit:
        identity = str(unit.scalar(select(OmsManagedPosition.id)))
    quote = {"received_at": case[4][0], "bid": 3.0}
    if quote_kind == "stale":
        quote["received_at"] -= timedelta(seconds=3)
    elif quote_kind == "future":
        quote["received_at"] += timedelta(seconds=1)
    elif quote_kind == "nan":
        quote["bid"] = float("nan")
    case[0]._latest_quotes_by_symbol = {} if quote_kind == "missing" else {"AIXI": quote}
    result = await wb.retry_atr_proven_unsent_exit(case[0], case[2].account, "AIXI", identity)
    if quote_kind == "fresh":
        assert result == "closed" and len(closes(case[2])) == 1
        request = closes(case[2])[0]
        assert request.order_type == "limit" and request.metadata["session"] == "PM"
        assert request.metadata["extended_hours"] == "true"
        assert Decimal(request.metadata["limit_price"]) <= 3
    else:
        assert result == "refused" and closes(case[2]) == [] and case[2].total == QTY


@pytest.mark.asyncio
async def test_retry_eh_quote_aging_during_last_broker_proof_aborts_without_sell(make_case, monkeypatch):
    from project_mai_tai.oms import service as oms
    case = await proven_unsent_case(make_case, monkeypatch)
    service, factory, broker, _, clock = case
    clock[0] = PREPARED.replace(hour=21)
    monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: "PM")
    monkeypatch.setattr(service, "_market_is_fillable", lambda: True)
    quote_at = clock[0]
    service._latest_quotes_by_symbol = {"AIXI": {"received_at": quote_at, "bid": 3.0}}
    with factory() as unit:
        identity = str(unit.scalar(select(OmsManagedPosition.id)))
    original_positions, validator = broker.list_account_positions, wb.validate_atr_before_wire
    reads, failures = [], []

    async def positions(account):
        result = await original_positions(account)
        reads.append(account)
        if len(reads) == 2:
            # First position read is handoff. Second is validator's LAST broker
            # await; quote was valid on entry and ages only during that proof.
            clock[0] += timedelta(seconds=2.6)
        return result

    async def checked(service, request, row_id):
        assert request.metadata["wbattach1_unsent_retry_quote_at"] == quote_at.isoformat()
        with factory() as unit:
            order = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
            assert order.payload == request.metadata
            assert unit.get(TradeIntent, order.intent_id).payload["metadata"] == request.metadata
        try:
            await validator(service, request, row_id)
        except ValueError as exc:
            failures.append(str(exc))
            raise

    monkeypatch.setattr(broker, "list_account_positions", positions)
    monkeypatch.setattr(wb, "validate_atr_before_wire", checked)
    assert await wb.retry_atr_proven_unsent_exit(service, broker.account, "AIXI", identity) == "refused"
    assert len(reads) == 2 and failures == ["wbattach1_unsent_retry_quote_stale_before_wire"]
    assert closes(broker) == [] and broker.total == QTY
    with factory() as unit:
        entry = unit.get(BrokerOrder, case[3])
        journal = entry.payload[wb.KEY]
        assert not journal.get("exit_phase") and len(journal["unsent_handoffs"]) == 2
        row = unit.scalar(select(OmsManagedPosition))
        assert row.status == "open" and row.current_quantity == 95
        last = journal["unsent_handoffs"][-1]
        order = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == last["client_order_id"]))
        assert order.status == "aborted" and order.submitted_at is None and order.broker_order_id is None
        assert unit.scalar(select(Fill.id).where(Fill.order_id == order.id)) is None


@pytest.mark.asyncio
async def test_actual_recovery_coordinator_tries_at_most_three_positive_unsent_owners(make_case, monkeypatch):
    case = await proven_unsent_case(make_case, monkeypatch)
    service, factory, broker, _, _ = case
    validator, retry = wb.validate_atr_before_wire, wb.retry_atr_proven_unsent_exit
    attempts, validations = [], []

    async def refuse(service, request, identity):
        await validator(service, request, identity)
        validations.append(request.client_order_id)
        raise ValueError("CONTROLLED repeat final-wire refusal")

    async def observe_retry(*args):
        attempts.append(args)
        return await retry(*args)

    monkeypatch.setattr(wb, "validate_atr_before_wire", refuse)
    monkeypatch.setattr(wb, "retry_atr_proven_unsent_exit", observe_retry)
    with factory() as unit:
        identity = str(unit.scalar(select(OmsManagedPosition.id)))
    # Real coordinator, real helper, real emitter/prewire/abort transactions.
    from project_mai_tai.oms.service import _ConfirmationFanoutDecision
    decision = _ConfirmationFanoutDecision(symbol="AIXI", source_fill_id="CONTROLLED-repeat-refusal",
        accounts=(broker.account,), exit_tag="CW_FLIP")
    await service._recover_released_confirmation_webull_leg(decision, broker.account, "AIXI",
        expected_row_id=identity, protection="released")
    assert len(attempts) == len(validations) == 3 and len(set(validations)) == 3
    assert closes(broker) == [] and broker.total == QTY
    with factory() as unit:
        journal = unit.get(BrokerOrder, case[3]).payload[wb.KEY]
        assert len(journal["unsent_handoffs"]) == 4  # Initial plus three retry abort receipts.
        assert not journal.get("exit_phase")
        assert unit.scalar(select(SystemIncident.id)) is not None  # Exhaustion is visible, not a fake close.


async def drain_auto_recovery(service):
    """Observe actual spawned coordinator completion, not a success stub."""
    while tasks := tuple(service.__dict__.get("_confirmation_exit_recovery_tasks", ())):
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("flat", [False, True], ids=["fresh-owner-one-sell", "flat-no-sell"])
async def test_riskoff_positive_never_sent_abort_automatically_recovers_once(make_case, monkeypatch, flat):
    case = aixi_case(make_case, monkeypatch)
    service, factory, broker, _, _ = case
    assert await attach_aixi(case) is True
    validator, spawn = wb.validate_atr_before_wire, service._spawn_confirmation_webull_recovery
    validated, spawned = [], []

    async def fail_once(actual_service, request, identity):
        await validator(actual_service, request, identity)
        assert request.reason == wb.REASON
        validated.append(request.client_order_id)
        if len(validated) == 1:
            if flat:
                broker.total = Decimal(0)  # Explicit CONTROLLED external flatten.
            raise ValueError("CONTROLLED canonical riskoff postrelease refusal")

    def observe_spawn(*args, **kwargs):
        before = set(service.__dict__.get("_confirmation_exit_recovery_tasks", ()))
        result = spawn(*args, **kwargs)
        spawned.extend(set(service.__dict__.get("_confirmation_exit_recovery_tasks", ())) - before)
        return result

    monkeypatch.setattr(wb, "validate_atr_before_wire", fail_once)
    monkeypatch.setattr(service, "_spawn_confirmation_webull_recovery", observe_spawn)
    assert await wb.request_atr_exit(service, broker.account, "AIXI", COID,
        "CONTROLLED original attach risk-off trigger") is False
    await drain_auto_recovery(service)
    assert len(spawned) == 1 and all(task.done() for task in spawned)
    assert len(validated) == (1 if flat else 2)
    assert len(set(validated)) == len(validated)
    assert len(closes(broker)) == (0 if flat else 1) and broker.total == 0
    if not flat:
        assert closes(broker)[0].reason == wb.REASON and closes(broker)[0].quantity == QTY
        assert closes(broker)[0].client_order_id == validated[-1]
    with factory() as unit:
        entry = unit.get(BrokerOrder, case[3])
        receipt = entry.payload[wb.KEY]["unsent_handoffs"][0]
        assert receipt["reason"] == wb.REASON
        assert receipt["exit_reason"] == "CONTROLLED original attach risk-off trigger"
        order = unit.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == validated[0]))
        assert order.status == "aborted" and order.submitted_at is None and order.broker_order_id is None
        assert unit.scalar(select(Fill.id).where(Fill.order_id == order.id)) is None
        managed = unit.scalar(select(OmsManagedPosition))
        assert managed.status == "closed" and managed.current_quantity == 0
    assert not service.__dict__.get("_wbattach_exit_owners")
    assert not service._webull_recovery_in_progress(broker.account, "AIXI")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["claimed", "unknown-children", "unproven-abort"])
async def test_riskoff_unknown_or_claimed_never_spawns_another_recovery_sell(make_case, monkeypatch, fault):
    case = aixi_case(make_case, monkeypatch)
    service, factory, broker, _, _ = case
    assert await attach_aixi(case) is True
    validator = wb.validate_atr_before_wire
    spawns = []
    original_spawn = service._spawn_confirmation_webull_recovery

    def observe_spawn(*args, **kwargs):
        spawns.append((args, kwargs))
        return original_spawn(*args, **kwargs)

    async def opaque_abort(actual_service, request, identity):
        await validator(actual_service, request, identity)
        request.metadata["CONTROLLED-unproven-request-change"] = "true"
        raise ValueError("CONTROLLED no exact unsent release receipt")

    monkeypatch.setattr(service, "_spawn_confirmation_webull_recovery", observe_spawn)
    if fault == "claimed":
        with factory.begin() as unit:
            entry = unit.get(BrokerOrder, case[3])
            entry.payload = {**entry.payload, wb.KEY: {**entry.payload[wb.KEY],
                "exit_phase": "claimed", "exit_call_id": "CONTROLLED-other-inflight-call"}}
    elif fault == "unknown-children":
        broker.child_unknown = True
    else:
        monkeypatch.setattr(wb, "validate_atr_before_wire", opaque_abort)
    assert await wb.request_atr_exit(service, broker.account, "AIXI", COID,
        "CONTROLLED uncertain attach risk-off") is False
    await drain_auto_recovery(service)
    assert spawns == [] and closes(broker) == [] and broker.total == QTY
    with factory() as unit:
        state = unit.get(BrokerOrder, case[3]).payload[wb.KEY]
        assert not state.get("unsent_handoffs")
        if fault in {"claimed", "unproven-abort"}:
            assert state["exit_phase"] == "claimed"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["old-receipt-no-reason", "changed-origin", "riskoff-outside-rth"])
async def test_riskoff_retry_requires_new_reason_bound_receipt_and_rth(make_case, monkeypatch, damage):
    from project_mai_tai.oms import service as oms
    case = aixi_case(make_case, monkeypatch)
    service, factory, broker, _, clock = case
    assert await attach_aixi(case) is True
    validator = wb.validate_atr_before_wire
    scheduled = []

    async def refuse(actual_service, request, identity):
        await validator(actual_service, request, identity)
        raise ValueError("CONTROLLED isolate eligible canonical riskoff receipt")

    # Counterfactual preparation only: actual automatic scheduling has dedicated
    # controls above. Here inspect helper denial before any recovery can consume it.
    with monkeypatch.context() as patch:
        patch.setattr(wb, "validate_atr_before_wire", refuse)
        patch.setattr(service, "_spawn_confirmation_webull_recovery", lambda *a, **k: scheduled.append((a, k)))
        await wb.request_atr_exit(service, broker.account, "AIXI", COID, "CONTROLLED origin")
    assert len(scheduled) == 1
    with factory.begin() as unit:
        entry = unit.get(BrokerOrder, case[3])
        state = deepcopy(entry.payload[wb.KEY])
        assert not state.get("exit_phase") and state["unsent_handoffs"][-1]["reason"] == wb.REASON
        if damage == "old-receipt-no-reason":
            state["unsent_handoffs"][-1].pop("reason")
        elif damage == "changed-origin":
            state["exit_reason"] = "CONTROLLED different trigger"
        entry.payload = {**entry.payload, wb.KEY: state}
        identity = str(unit.scalar(select(OmsManagedPosition.id)))
    if damage == "riskoff-outside-rth":
        clock[0] = PREPARED.replace(hour=21)
        monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: "PM")
        monkeypatch.setattr(service, "_market_is_fillable", lambda: True)
        service._latest_quotes_by_symbol = {"AIXI": {"received_at": clock[0], "bid": 3.0}}
    assert await wb.retry_atr_proven_unsent_exit(service, broker.account, "AIXI", identity) is None
    assert closes(broker) == [] and broker.total == QTY


@pytest.mark.asyncio
async def test_riskoff_auto_recovery_coalesces_recursive_refusals_and_stops_at_three(make_case, monkeypatch):
    case = aixi_case(make_case, monkeypatch)
    service, factory, broker, _, _ = case
    assert await attach_aixi(case) is True
    validator, spawn = wb.validate_atr_before_wire, service._spawn_confirmation_webull_recovery
    validated, workers = [], []

    async def always_refuse(actual_service, request, identity):
        await validator(actual_service, request, identity)
        validated.append(request.client_order_id)
        raise ValueError("CONTROLLED repeat canonical riskoff refusal")

    def observed_spawn(*args, **kwargs):
        before = set(service.__dict__.get("_confirmation_exit_recovery_tasks", ()))
        result = spawn(*args, **kwargs)
        workers.extend(set(service.__dict__.get("_confirmation_exit_recovery_tasks", ())) - before)
        return result

    monkeypatch.setattr(wb, "validate_atr_before_wire", always_refuse)
    monkeypatch.setattr(service, "_spawn_confirmation_webull_recovery", observed_spawn)
    await wb.request_atr_exit(service, broker.account, "AIXI", COID, "CONTROLLED bounded riskoff origin")
    await drain_auto_recovery(service)
    assert len(workers) == 1 and workers[0].done()
    assert len(validated) == len(set(validated)) == 4  # Initial +3, no recursive fourth retry.
    assert closes(broker) == [] and broker.total == QTY
    assert not service._webull_recovery_in_progress(broker.account, "AIXI")
    with factory() as unit:
        state = unit.get(BrokerOrder, case[3]).payload[wb.KEY]
        assert len(state["unsent_handoffs"]) == 4 and not state.get("exit_phase")
        assert all(r["reason"] == wb.REASON and r["exit_reason"] == "CONTROLLED bounded riskoff origin"
                   for r in state["unsent_handoffs"])
        assert unit.scalar(select(SystemIncident.id)) is not None  # Exhaustion remains explicitly unresolved.
