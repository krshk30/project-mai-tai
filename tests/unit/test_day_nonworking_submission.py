"""Actual durable wire and DAY-proof admission; no invented zero-fill report."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.models import BrokerOrder, BrokerOrderEvent, Fill, TradeIntent
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore, current_session_anchor
from project_mai_tai.oms import buy_submission_journal as tokens
from project_mai_tai.oms import cancel_terminal as journal
from tests.unit.test_webull_day_cancel_proof import (
    BOUND, Client, NOW, REASON, SCOPE, install_sdk, permit_time,
)
from tests.unit.test_buy_submission_journal import sessions as local_sessions
from tests.integration import test_cancel_terminal_runtime as runtime

sdk = runtime.sdk
sessions = local_sessions


def at(epoch):
    return datetime.fromtimestamp(epoch / 1000, UTC)


async def setup(sessions, monkeypatch):
    install_sdk(monkeypatch)
    routed = runtime.adapter(Client())
    guard = tokens.DurableBuyAdapter(routed, sessions)
    clock = [NOW - 65_000]
    monkeypatch.setattr(tokens, "now_ms", lambda: clock[0])
    await guard.ensure_coverage(SCOPE.account_name)
    FanoutSegmentIdentityStore(sessions).record(SCOPE.symbol, NOW, True,
        "flip_owned_opportunity_v2_bind", now=at(NOW - 60_000))
    wires = []

    async def wire(request):
        wires.append(request)
        return [ExecutionReport("accepted", request.client_order_id, origin="broker",
                                broker_order_id=BOUND.broker_order_id)]

    routed._adapter_for_account(SCOPE.account_name).submit_order = wire
    clock[0] = NOW - 59_000
    await guard.submit_order(OrderRequest(SCOPE.client_order_id, SCOPE.account_name,
        "schwab_1m_v2", SCOPE.symbol, "buy", "open", Decimal(1), "controlled entry",
        {"fanout_segment_id": str(NOW)}))
    intent_id = runtime.seed(sessions, routed)
    with sessions() as session:
        intent = session.get(TradeIntent, intent_id)
        md = {**intent.payload["metadata"], "clearwait_buy_only": "true",
              "buy_submission_process_id": str(guard.process_id), "fanout_segment_id": str(NOW)}
        intent.created_at, intent.updated_at = at(NOW - 2000), at(NOW - 1000)
        intent.payload = {**intent.payload, "metadata": md, "source_service": "schwab-1m-v2",
            "refusal_origin": "broker_reject", "refusal_code": REASON,
            journal.BINDING_KEY: {**intent.payload[journal.BINDING_KEY],
                                 "broker_order_id": BOUND.broker_order_id}}
        order = BrokerOrder(strategy_id=intent.strategy_id, broker_account_id=intent.broker_account_id,
            client_order_id=SCOPE.client_order_id, broker_order_id=BOUND.broker_order_id,
            symbol=SCOPE.symbol, side="buy", order_type="limit", time_in_force="day",
            quantity=Decimal(1), status="accepted", submitted_at=at(clock[0]))
        session.add(order)
        session.flush()
        session.add(BrokerOrderEvent(order_id=order.id, event_type="rejected", event_source="broker",
            event_at=at(NOW - 1500), payload={"client_order_id": SCOPE.client_order_id,
                "reason": REASON, "metadata": {"webull_error_code": "ORDER_CAN_NOT_BE_CANCEL",
                                               "webull_http_status": "417"}}))
        session.commit()
    permit_time(monkeypatch, routed)
    await journal.acquire_cancel_terminal_evidence(sessions, routed, [intent_id])
    scope = tokens.NeverSentScope(SCOPE.account_name, SCOPE.account_id, SCOPE.symbol, str(NOW),
        NOW - 60_000, "exact-token", "exact-token", NOW - 2500, guard.process_id,
        current_session_anchor(at(NOW)).isoformat())
    return intent_id, scope, wires


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "new_receipt", "stale", "fill", "partial", "gtc",
    "other_token", "other_process", "other_target", "later_event", "source", "submitting",
    "late_coverage", "lost_id", "other_generation", "real_fill", "valid_other_request"])
async def test_nonworking_wire_admission_exact_and_rollback_safe(sessions, sdk, monkeypatch, fault):
    intent_id, scope, wires = await setup(sessions, monkeypatch)
    with sessions() as session:
        intent = session.get(TradeIntent, intent_id)
        order = session.scalar(select(BrokerOrder))
        token = session.scalar(select(tokens.BuySubmissionToken))
        payload = dict(intent.payload)
        md = dict(payload["metadata"])
        if fault == "other_token":
            md["clearwait_removal_token"] = "other"
        elif fault == "valid_other_request":
            md["clearwait_removal_token"] = "other"
            payload[journal.BINDING_KEY] = {**payload[journal.BINDING_KEY], "token": "other"}
            payload[journal.JOURNAL_KEY] = {**payload[journal.JOURNAL_KEY],
                                          "binding": payload[journal.BINDING_KEY]}
        elif fault == "other_process":
            md["buy_submission_process_id"] = str(uuid4())
        elif fault == "other_target":
            md["target_client_order_id"] = "other"
        elif fault == "source":
            payload["source_service"] = "other"
        elif fault in {"fill", "real_fill"}:
            session.add(Fill(order_id=order.id, strategy_id=order.strategy_id,
                broker_account_id=order.broker_account_id, symbol=order.symbol, side="buy",
                filled_at=at(NOW - 3000), quantity=1, price=1))
            if fault == "real_fill":
                order.status = "filled"
                session.add(BrokerOrderEvent(order_id=order.id, event_type="filled", event_source="broker",
                                            event_at=at(NOW - 1100), payload={}))
        elif fault == "partial":
            order.status = "partially_filled"
        elif fault == "gtc":
            order.time_in_force = "gtc"
        elif fault == "submitting":
            token.state = "submitting"
        elif fault == "late_coverage":
            session.get(tokens.BuyCoverageEpoch, (token.process_id, token.account_id)).started_at_ms = NOW
        elif fault == "lost_id":
            token.answers = [{**answer, "broker_order_id": None} for answer in token.answers]
        elif fault == "other_generation":
            token.generation = "other"
        elif fault == "later_event":
            session.add(BrokerOrderEvent(order_id=order.id, event_type="accepted", event_source="broker",
                                        event_at=at(NOW), payload={}))
        elif fault == "new_receipt":
            session.add(TradeIntent(strategy_id=intent.strategy_id, broker_account_id=intent.broker_account_id,
                symbol=intent.symbol, side="buy", intent_type="cancel", quantity=1,
                reason="new unanswered cancellation", status="pending", created_at=at(NOW), updated_at=at(NOW)))
        payload["metadata"] = md
        intent.payload = payload
        # Keep the controlled receipt clock when payload mutation causes UPDATE.
        intent.updated_at = at(NOW - 1000)
        flag_modified(intent, "updated_at")
        session.commit()
    with sessions() as session:
        observed = NOW + 15_001 if fault == "stale" else NOW
        if fault == "valid_other_request":
            intent = session.get(TradeIntent, intent_id)
            assert journal.load_cancel_terminal_evidence(session, [intent])
        working_filter = tokens.nonworking_buy_submission_witnesses(session, scope, observed_at_ms=observed)
        assert bool(working_filter) is (fault == "none")
        witness = tokens.close_terminal_buy_admission(session, scope, observed_at_ms=observed)
        assert witness.terminal is (fault in {"none", "real_fill"})
        if fault == "none":
            order = session.scalar(select(BrokerOrder))
            token = session.scalar(select(tokens.BuySubmissionToken))
            assert order.status == "accepted" and not list(session.scalars(select(Fill)))
            assert token.state == "broker_terminal"
            typed = tokens._day_nonworking_token(session, token, order, scope, observed)
            assert isinstance(typed, tokens.NonworkingBuySubmissionWitness)
            assert typed.client_order_id == wires[0].client_order_id
        elif fault == "real_fill":
            assert session.scalar(select(BrokerOrder)).status == "filled"
            assert len(list(session.scalars(select(Fill)))) == 1
        session.rollback()
    with sessions() as session:
        assert not list(session.scalars(select(tokens.BuyAdmissionClosure)))
        assert session.scalar(select(tokens.BuySubmissionToken)).state == (
            "submitting" if fault == "submitting" else "reported_ambiguous")
    assert len(wires) == 1
