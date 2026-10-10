"""F consumes canonical DAY nonworking identity without rewriting orders/fills."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.webull_order_reads import shared_budget
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import buy_submission_journal as tokens
from project_mai_tai.oms import cancel_terminal as journal
from tests.integration.test_cancel_terminal_runtime import sdk as controlled_sdk
from tests.unit import test_clearwait1_never_sent_consumer as covered
from tests.unit.test_clearwait1_session_rollover import ACCOUNTS, PRIMARY, WEBULL, ms
from tests.unit.test_clearwait1_session_rollover import db as local_db
from tests.unit.test_clearwait1_unbound import CONFIGURED_IDS, NOW
from tests.unit.test_webull_day_cancel_proof import Client, REASON, install_sdk, permit_time

db, sdk = local_db, controlled_sdk
pytestmark = pytest.mark.usefixtures("sdk")


async def canonical_nonworking(database, monkeypatch):
    req, guard = await covered.covered_request(database, monkeypatch)
    install_sdk(monkeypatch)
    client = Client()
    leaf = guard.cancel_terminal_delegate._adapter_for_account(WEBULL)
    leaf._get_client = lambda: client
    leaf._query_budget = shared_budget("controlled-F-DAY", uuid4().hex)
    permit_time(monkeypatch, guard.cancel_terminal_delegate)
    clock = [ms(NOW) - 1500]
    monkeypatch.setattr(tokens, "now_ms", lambda: clock[0])
    monkeypatch.setattr(broker, "now_ms", lambda: clock[0])
    wires = []
    async def wire(request):
        wires.append(request)
        return [ExecutionReport("accepted", request.client_order_id, origin="broker", broker_order_id="controlled-DAY")]
    leaf.submit_order = wire
    await guard.submit_order(OrderRequest("controlled-DAY-coid", WEBULL, "schwab_1m_v2", req.symbol,
        "buy", "open", Decimal(1), "controlled entry", {"fanout_segment_id": str(req.opportunity_id)}))
    with database[1]() as session:
        order = BrokerOrder(strategy_id=database[3], broker_account_id=database[2][WEBULL],
            client_order_id=wires[0].client_order_id, broker_order_id="controlled-DAY", symbol=req.symbol,
            side="buy", order_type="limit", time_in_force="day", quantity=1, status="accepted",
            submitted_at=datetime.fromtimestamp(clock[0] / 1000, UTC),
            payload={"fanout_segment_id": str(req.opportunity_id)})
        session.add(order)
        session.flush()
        intent = session.scalar(select(TradeIntent).where(TradeIntent.broker_account_id == database[2][WEBULL]))
        intent.payload = {**intent.payload, "refusal_origin": "broker_reject", "refusal_code": REASON,
            "metadata": {**intent.payload["metadata"], "target_client_order_id": order.client_order_id}}
        event = TradeIntentEvent(event_id=UUID(intent.payload["event_id"]), source_service="schwab-1m-v2",
            payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=WEBULL,
                symbol=req.symbol, side="buy", intent_type="cancel", quantity=1, reason=intent.reason,
                metadata=intent.payload["metadata"]))
        journal.bind_cancel_target(intent, event, guard, order)
        intent.updated_at = NOW
        flag_modified(intent, "updated_at")
        session.add(BrokerOrderEvent(order_id=order.id, event_type="rejected", event_source="broker", event_at=NOW,
            payload={"client_order_id": order.client_order_id, "reason": REASON,
                "metadata": {"webull_error_code": "ORDER_CAN_NOT_BE_CANCEL", "webull_http_status": "417"}}))
        session.commit()
        order_id, intent_id = order.id, intent.id
    clock[0] = ms(NOW)
    producer_request = journal._read_request(database[1], intent_id, guard)
    assert producer_request is not None
    assert producer_request.bound_day_cancel is not None, producer_request
    await journal.acquire_cancel_terminal_evidence(database[1], guard, [intent_id])
    with database[1]() as session:
        intent = session.get(TradeIntent, intent_id)
        evidence = journal.load_cancel_terminal_evidence(session, [intent])
        proof = evidence[intent.payload["event_id"]]
        assert proof.day_absence is not None, (proof, client.calls)
        assert proof.target_status == "" and proof.target_filled_quantity == ""
    return req, order_id, intent_id, client


def assess(database, req, **kwargs):
    return database[0].retire_unbound((req,), ACCOUNTS, publication_closed={req: True},
        expected_bindings={PRIMARY: ("schwab", CONFIGURED_IDS[PRIMARY]), WEBULL: ("webull", CONFIGURED_IDS[WEBULL])},
        now=kwargs.pop("now", NOW), **kwargs)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "working_primary", "working_webull", "fill", "open_owner",
    "pending", "changed_request", "memory_cas", "stale", "assessment_bound", "overflow"])
async def test_canonical_nonworking_exact_identity_and_other_guards(db, sdk, monkeypatch, fault):
    req, order_id, intent_id, client = await canonical_nonworking(db, monkeypatch)
    with db[1]() as session:
        if fault in {"working_primary", "working_webull", "overflow"}:
            name = PRIMARY if fault == "working_primary" else WEBULL
            session.add_all(BrokerOrder(strategy_id=db[3], broker_account_id=db[2][name], symbol=req.symbol,
                side="buy", order_type="limit", time_in_force="day", quantity=1, status="accepted",
                client_order_id=f"other-working-{index}") for index in range(2 if fault == "overflow" else 1))
        elif fault == "fill":
            session.add(Fill(order_id=order_id, strategy_id=db[3], broker_account_id=db[2][WEBULL],
                symbol=req.symbol, side="buy", quantity=1, price=1, filled_at=NOW))
        elif fault == "open_owner":
            session.add(OmsManagedPosition(strategy_code="schwab_1m_v2", broker_account_name=WEBULL,
                symbol=req.symbol, entry_order_id=order_id, entry_price=1, original_quantity=1,
                current_quantity=1, status="open"))
        elif fault == "pending":
            session.add(TradeIntent(strategy_id=db[3], broker_account_id=db[2][PRIMARY], symbol=req.symbol,
                side="buy", intent_type="open", quantity=1, reason="controlled pending", status="pending"))
        elif fault == "changed_request":
            intent = session.get(TradeIntent, intent_id)
            payload = dict(intent.payload)
            payload["metadata"] = {**payload["metadata"], "clearwait_removal_token": "changed"}
            intent.payload = payload
            intent.updated_at = NOW
            flag_modified(intent, "updated_at")
        session.commit()
    if fault == "overflow":
        monkeypatch.setattr("project_mai_tai.v2_removed_wait.ROW_LIMIT", 2)
    kwargs = {}
    if fault == "memory_cas":
        current = iter((True, False))
        kwargs["publication_current"] = lambda _req: next(current)
    elif fault == "assessment_bound":
        kwargs["minimum_book_started_at_ms"] = ms(NOW) + 1
    elif fault == "stale":
        kwargs["now"] = NOW + timedelta(milliseconds=15001)
    proof = assess(db, req, **kwargs)
    assert proof.clear is (fault == "none")
    assert bool(covered.closures(db)) is (fault == "none")
    with db[1]() as session:
        assert session.get(BrokerOrder, order_id).status == "accepted"
        assert len(list(session.scalars(select(Fill)))) == int(fault == "fill")
        token = session.scalar(select(tokens.BuySubmissionToken))
        assert token.state == ("broker_terminal" if fault == "none" else "reported_ambiguous")
    assert len(client.calls) == 4
