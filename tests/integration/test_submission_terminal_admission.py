"""Exact wired-entry terminal closure; no production broker or database access."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.cancel_terminal_proof import (
    BookOrder, CompleteWorkingBook, UnboundCancelFences, UnboundCancelRequest,
    evaluate_unbound_cancel_terminal,
)
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, Strategy
from project_mai_tai.oms import buy_submission_journal as journal
import test_buy_submission_tokens_runtime as runtime

sessions = runtime.sessions
sdk = runtime.sdk


@pytest.mark.parametrize("case", ["filled_closed", "cancelled", "working", "client_event", "lost_id"])
@pytest.mark.asyncio
async def test_real_pg_wired_terminal_and_unwired_sibling_share_request_fence(sessions, sdk, monkeypatch, case):
    clock, sc, _wb, guard = await runtime.setup_protocol(sessions, monkeypatch)
    async def submit(req):
        return [ExecutionReport("accepted", req.client_order_id, origin="broker",
            broker_order_id=None if case == "lost_id" else "broker-1")]
    sc.submit_order = submit
    await guard.submit_order(runtime.buy("FLYE"))
    with sessions() as session:
        account = BrokerAccount(name="schwab", provider="schwab", environment="test",
                                external_account_id="actual-hash")
        strategy = Strategy(code="schwab_1m_v2", name="v2")
        session.add_all([account, strategy])
        session.flush()
        status = "filled" if case == "filled_closed" else "accepted" if case == "working" else "cancelled"
        order = BrokerOrder(strategy_id=strategy.id, broker_account_id=account.id,
            client_order_id="exact-coid", broker_order_id="broker-1", symbol="FLYE", side="buy",
            order_type="limit", time_in_force="day", quantity=Decimal(1), status=status)
        session.add(order)
        session.flush()
        session.add(BrokerOrderEvent(order_id=order.id, event_type=status,
            event_source="client" if case == "client_event" else "broker", payload={},
            event_at=datetime.fromtimestamp(clock[0] / 1000, UTC)))
        session.commit()
    sc_scope = runtime.scope(guard, "FLYE")
    wb_scope = runtime.scope(guard, "FLYE", "live:orb")
    request = UnboundCancelRequest("FLYE", sc_scope.request_id, sc_scope.request_token,
        sc_scope.generation, "retry_exhausted", sc_scope.requested_at_ms,
        {"schwab": "actual-hash", "live:orb": "ACC1"},
        {"schwab": "schwab", "live:orb": "webull"}, sc_scope.session_key,
        sc_scope.opportunity_started_at_ms, {n: str(guard.process_id) for n in ("schwab", "live:orb")})
    fences = UnboundCancelFences(request, True, True, True, True)
    book = CompleteWorkingBook("live:orb", "ACC1", clock[0], clock[0], True, "all_working", (), "broker")
    with sessions() as session:
        wired = journal.close_terminal_buy_admission(session, sc_scope, observed_at_ms=clock[0])
        unwired = journal.close_never_sent_admission(session, wb_scope, observed_at_ms=clock[0])
        args = dict(never_sent_witnesses={"live:orb": unwired},
                    terminal_submission_witnesses={"schwab": wired}, now_ms=clock[0])
        assert evaluate_unbound_cancel_terminal(request, {"live:orb": book},
            fences=fences, **args).terminal is (case in {"filled_closed", "cancelled"})
        if wired.terminal:
            for field in ("owned_rows_closed", "request_cas_current", "no_inflight_buy", "no_unanswered_cancel"):
                assert not evaluate_unbound_cancel_terminal(request, {"live:orb": book},
                    fences=replace(fences, **{field: False}), **args).terminal
            working = replace(book, orders=(BookOrder("manual", "FLYE", "working", "buy"),))
            assert not evaluate_unbound_cancel_terminal(request, {"live:orb": working}, fences=fences, **args).terminal
            sell = replace(book, orders=(BookOrder("manual", "FLYE", "working", "sell"),))
            assert evaluate_unbound_cancel_terminal(request, {"live:orb": sell}, fences=fences, **args).terminal
            session.rollback()
            assert not list(session.scalars(select(journal.BuyAdmissionClosure)))
            assert session.scalar(select(journal.BuySubmissionToken)).state == "reported_ambiguous"
