"""Identical FIFO transaction, cancellation and provenance controls on PostgreSQL."""
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import threading

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import AccountPosition, BrokerOrder, BrokerOrderEvent, TradeIntent, VirtualPosition
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload, TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.oms import cancel_terminal as journal
from project_mai_tai.oms.report_audit_fifo import AuditItem
import test_buy_submission_tokens_runtime as transport
import test_cancel_terminal_runtime as runtime
from tests.unit import test_report_audit_fifo as control
from tests.unit import test_webull_day_cancel_proof as day

sessions = runtime.sessions
sdk = runtime.sdk


@pytest.mark.asyncio
async def test_pg_fifo_parent_fk_copy_and_commit_census(sessions, monkeypatch, caplog):
    await control.test_fifo_parent_fk_copy_order_and_commit_accounting(sessions, monkeypatch, caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_fails", [False, True])
async def test_pg_fifo_cancelled_parent_completion_and_rollback(sessions, monkeypatch, commit_fails):
    await control.test_parent_cancel_completion_or_rollback_before_transfer(sessions, monkeypatch, commit_fails)


@pytest.mark.asyncio
async def test_pg_fifo_real_insert_failure(sessions, caplog):
    await control.test_fifo_actual_fk_insert_failure_counted_after_parent_survives(sessions, caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["fanout_source", "nfq_price_feedback_owned", "clearwait_removal_token", None])
async def test_pg_fifo_old_payload_and_nested_inline(sessions, marker):
    await control.test_old_payload_and_nested_reports_stay_inline(sessions, marker)


@pytest.mark.asyncio
async def test_pg_fifo_proof_does_not_run_before_audit_commit(sessions, monkeypatch):
    await control.test_background_terminal_evidence_waits_for_committed_fifo(sessions, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["canceled", "filled"])
async def test_pg_terminal_refusal_remains_inline(sessions, terminal):
    await control.test_terminal_cancel_refusal_stays_inline(sessions, terminal)


@pytest.mark.asyncio
async def test_pg_concurrent_bounded_waiters(sessions, monkeypatch):
    await control.test_concurrent_capacity_waiters_recheck_no_drop_one_writer(sessions, monkeypatch)


@pytest.mark.asyncio
async def test_pg_whole_shutdown_double_cancel_and_private_commit(sessions, monkeypatch):
    await control.test_whole_shutdown_fences_producer_settlement_and_private_commit(sessions, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,marker", [("close", None), ("cancel", None),
    ("cancel", "fanout_source"), ("cancel", "nfq_price_feedback_owned"),
    ("cancel", "clearwait_removal_token")])
async def test_actual_root_return_with_blocked_insert_or_old_proof_inline(sessions, sdk, monkeypatch, caplog, kind, marker):
    _, leaf, _, guard = await transport.setup_protocol(sessions, monkeypatch)
    async def wire(request):
        return [ExecutionReport("cancelled" if request.intent_type == "cancel" else "accepted",
            request.client_order_id, broker_order_id="controlled-answer",
            symbol=request.symbol, side=request.side, quantity=request.quantity,
            intent_type=request.intent_type, metadata=request.metadata)]
    leaf.submit_order = wire
    service = transport.oms(sessions, leaf, monkeypatch)
    service.broker_adapter = guard
    monkeypatch.setattr(service.logger, "handlers", [*service.logger.handlers, caplog.handler])
    md = {"target_client_order_id": "original-order"} if kind == "cancel" else {"reference_price": "2"}
    event = TradeIntentEvent(source_service="test", payload=TradeIntentPayload(strategy_code="macd_30s",
        broker_account_name="schwab", symbol="DKI", side="sell" if kind == "close" else "buy",
        intent_type=kind, quantity=Decimal(1), reason="CONTROLLED", metadata=md))
    with sessions() as session:
        store = OmsStore()
        strategy = store.ensure_strategy(session, "macd_30s")
        account = store.ensure_broker_account(session, "schwab", provider="schwab", environment="test")
        if kind == "close":
            session.add_all([VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
                symbol="DKI", quantity=Decimal(1), average_price=Decimal(2)),
                AccountPosition(broker_account_id=account.id, symbol="DKI", quantity=Decimal(1), average_price=Decimal(2))])
        else:
            intent = store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
            store.get_or_create_order(session, intent=intent, strategy_id=strategy.id,
                broker_account_id=account.id, client_order_id="original-order", symbol="DKI", side="buy",
                quantity=Decimal(1), status="accepted", metadata={marker: "true"} if marker else {})
        session.commit()
    entered, release = threading.Event(), threading.Event()
    original = OmsStore.append_order_event
    loop_thread = threading.get_ident()
    def blocked(store, session, **kwargs):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5)
        return original(store, session, **kwargs)
    monkeypatch.setattr(OmsStore, "append_order_event", blocked)
    stage = service._stage_ordinary_audit
    def checked_stage(*args, **kwargs):
        result = stage(*args, **kwargs)
        assert result is (marker is None), (args, kwargs)
        return result
    monkeypatch.setattr(service, "_stage_ordinary_audit", checked_stage)
    publications = []
    async def publish(event):
        publications.append(event)
    monkeypatch.setattr(service, "_publish_order_event", publish)
    task = asyncio.create_task(service.process_trade_intent(event))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if marker:
            await asyncio.sleep(0.01)
            assert not task.done() and publications == []
            assert "_ordinary_report_audit_fifo" not in service.__dict__
        else:
            result = await asyncio.wait_for(asyncio.shield(task), 1)
            assert result and publications
            assert service._report_audit_fifo().written == 0
            await asyncio.wait_for(service._handle_quote_tick_event(QuoteTickEvent(source_service="test",
                payload=QuoteTickPayload(symbol="OTHER", bid_price=Decimal(2), ask_price=Decimal("2.01")))), 1)
            service._maybe_emit_order_event_census(interval_seconds=0)
            assert "enqueued=1 committed_written=0 failed=0 pending=0 inflight=1" in caplog.text
            with sessions() as session:
                assert session.scalar(select(BrokerOrder.status)) == ("cancelled" if kind == "cancel" else "accepted")
                assert list(session.scalars(select(BrokerOrderEvent))) == []
    finally:
        release.set()
        await task
        await service._drain_cancel_terminal_evidence()
    with sessions() as session:
        assert len(list(session.scalars(select(BrokerOrderEvent)))) == 1


@pytest.mark.asyncio
async def test_real_day_proof_unknown_without_audit_then_background_retry(sessions, sdk, monkeypatch):
    # Defense-in-depth for a queued receipt; clearwait root callers themselves stay inline.
    sleep = asyncio.sleep
    day.install_sdk(monkeypatch)
    client = day.Client()
    routed = runtime.adapter(client)
    intent_id = runtime.seed(sessions, routed)
    with sessions() as session:
        intent = session.get(TradeIntent, intent_id)
        intent.created_at = datetime.fromtimestamp((day.NOW - 2000) / 1000, UTC)
        intent.updated_at = datetime.fromtimestamp(day.RECEIPT.observed_at_ms / 1000, UTC)
        intent.payload = {**intent.payload, "refusal_origin": "broker_reject", "refusal_code": day.REASON,
            journal.BINDING_KEY: {**intent.payload[journal.BINDING_KEY], "broker_order_id": day.BOUND.broker_order_id}}
        order = BrokerOrder(strategy_id=intent.strategy_id, broker_account_id=intent.broker_account_id,
            client_order_id=day.SCOPE.client_order_id, broker_order_id=day.BOUND.broker_order_id,
            symbol=day.SCOPE.symbol, side="buy", order_type="limit", time_in_force="day",
            quantity=Decimal(1), status="accepted", payload={},
            submitted_at=datetime.fromtimestamp(day.BOUND.submitted_at_ms / 1000, UTC))
        session.add(order)
        session.flush()
        report = ExecutionReport("rejected", order.client_order_id, origin="broker", reason=day.REASON,
            reported_at=datetime.fromtimestamp((day.NOW - 1500) / 1000, UTC))
        item = AuditItem.capture(order, report, {"client_order_id": order.client_order_id,
            "reason": day.REASON, "metadata": {"webull_error_code": "ORDER_CAN_NOT_BE_CANCEL", "webull_http_status": "417"}})
        session.commit()
    day.permit_time(monkeypatch, routed)
    service = control.service_for(sessions)
    service.broker_adapter = routed
    entered, release = threading.Event(), threading.Event()
    original = service.store.append_order_event
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(service.store, "append_order_event", blocked)
    await service._report_audit_fifo().transfer((item,))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert journal._read_request(sessions, intent_id, routed).bound_day_cancel is None
        assert not runtime.read(sessions, intent_id)[1].terminal
        service._schedule_cancel_terminal_evidence("live:orb", intent_id)
        await sleep(0.01)
        assert client.calls == [] and not runtime.read(sessions, intent_id)[1].terminal
    finally:
        release.set()
        await service._drain_cancel_terminal_evidence()
    assert runtime.read(sessions, intent_id)[1].terminal
    assert service._report_audit_fifo().written == 1
