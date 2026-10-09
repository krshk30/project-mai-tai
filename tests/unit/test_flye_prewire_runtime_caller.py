"""Real token/OMS/F caller negatives, not historical FLYE terminal certification."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.models import Base, BrokerAccount, BrokerOrder, OmsManagedPosition, Strategy, TradeIntent
from project_mai_tai.events import TradeIntentEvent
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore, current_session_anchor
from project_mai_tai.oms import buy_submission_journal as journal
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.unbound_cancel_book import JOURNAL_KEY
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2IntentEmitter
from project_mai_tai.v2_flip_entry_ownership import FlipPositionLeg
from project_mai_tai.v2_removed_wait import RemovedWaitStore
from tests.integration.test_cancel_terminal_runtime import sdk as controlled_sdk
from tests.unit.test_clearwait1_runtime_caller import poll
from tests.unit.test_clearwait1_session_rollover import service
from tests.unit.test_flye_bound_owner_target_close import FLYE, PRIMARY, WEBULL, book, sell
from tests.unit.test_flye_prewire_consumer_contract import synthetic_opportunity
from tests.unit.test_flye_unbound_owner_release import controlled_routing

sdk = controlled_sdk


def at(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, UTC)


@pytest.fixture
def runtime_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'flye_caller.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    ids, strategy_id = {name: uuid4() for name in (PRIMARY, WEBULL)}, uuid4()
    with sessions() as session:
        session.add(Strategy(id=strategy_id, code="schwab_1m_v2", name="synthetic caller"))
        for name, ident in ids.items():
            session.add(BrokerAccount(id=ident, name=name, environment="test",
                provider="webull" if name == WEBULL else "schwab", external_account_id=None))
        session.commit()
    yield RemovedWaitStore(sessions), sessions, ids, strategy_id
    engine.dispose()


class OmsReceipt:
    """Use the emitted UUID and actual OMS transaction, not an injected certificate."""
    def __init__(self, oms):
        self.oms, self.events = oms, []

    async def xadd(self, _stream, fields, **_kwargs):
        envelope = TradeIntentEvent.model_validate_json(fields["data"])
        self.events.append(envelope)
        await self.oms.process_trade_intent(envelope)
        return "controlled-stream-id"


@pytest.mark.asyncio
@pytest.mark.usefixtures("sdk")
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("case", ["filled_target", "filled_stop", "operator_sell", "working_schwab",
                                  "working_webull", "open_owned", "unknown_rows", "same_real_segment", "legacy"])
async def test_actual_wired_owner_cannot_be_certified_never_sent(runtime_db, monkeypatch, pm, account, case):
    store, sessions, ids, strategy_id = runtime_db
    strategy, state, record, clock, _ = synthetic_opportunity(account, pm)
    strategy.settings = strategy.settings.model_copy(update={
        "strategy_schwab_1m_v2_broker_provider": "schwab", "environment": "test",
        "oms_cancel_verify_enabled": False,
    })
    strategy._removed_wait_persist = store.record
    monkeypatch.setattr(journal, "now_ms", lambda: clock[0])
    # The imported SDK fixture freezes an older bound-request clock. This actual
    # OMS receipt uses wall time; the physical book must start after that receipt.
    monkeypatch.setattr(broker, "now_ms", lambda: int(datetime.now(UTC).timestamp() * 1000))
    routing = controlled_routing()
    book_calls = []
    webull = routing._adapter_for_account(WEBULL)

    def controlled_book(request):
        assert request.values == {"account_id": "TEST-WEBULL", "page_size": 100}
        book_calls.append(request.values)
        orders = []
        if case in {"working_webull", "operator_sell"}:
            orders = [{"client_order_id": "controlled-operator-order", "order_id": "controlled-working",
                "symbol": "FLYE", "order_status": "SUBMITTED",
                "side": "SELL" if case == "operator_sell" else "BUY"}]
        return SimpleNamespace(status_code=200, body={"hasNext": False, "orders": orders})

    webull._get_client = lambda: SimpleNamespace(_auto_retry=False, get_response=controlled_book)
    webull._body = lambda response: response.body
    webull._query_budget = SimpleNamespace(claim=lambda *_args, **_kwargs: None)
    primary = routing._adapter_for_account(PRIMARY)
    primary._authorized_request_json = AsyncMock(side_effect=AssertionError("Schwab book is not permitted"))
    oms = OmsRiskService(strategy.settings, SimpleNamespace(xadd=AsyncMock()),
                         session_factory=sessions, broker_adapter=routing)
    assert isinstance(oms.broker_adapter, journal.DurableBuyAdapter)
    wires = []

    async def controlled_wire(request):
        with sessions() as independent:
            tokens = list(independent.scalars(select(journal.BuySubmissionToken)))
            assert len(tokens) == 1 and tokens[0].state == "submitting"
            assert tokens[0].client_order_id == request.client_order_id
        wires.append(request)
        return [ExecutionReport("filled", request.client_order_id, broker_order_id="controlled-entry",
            symbol="FLYE", quantity=request.quantity, filled_quantity=request.quantity,
            fill_price=Decimal("2.34"), origin="broker")]

    routing._adapter_for_account(account).submit_order = controlled_wire
    bind_time = clock[0] - 60_000
    clock[0] = bind_time + 1 if case == "legacy" else bind_time - 1
    await oms.broker_adapter.start()
    FanoutSegmentIdentityStore(sessions).record("FLYE", record.opportunity_id, True,
        "flip_owned_opportunity_v2_bind", now=at(bind_time))
    clock[0] = bind_time + 2
    entry = OrderRequest("controlled-covered-entry", account, "schwab_1m_v2", "FLYE", "buy", "open",
        Decimal(280), "synthetic entry", {"fanout_segment_id": str(record.opportunity_id)})
    await oms.broker_adapter.submit_order(entry)
    with sessions() as session:
        order = BrokerOrder(strategy_id=strategy_id, broker_account_id=ids[account], symbol="FLYE",
            side="buy", order_type="market", time_in_force="day", quantity=280, status="filled",
            client_order_id=entry.client_order_id, broker_order_id="controlled-entry",
            payload=entry.metadata)
        session.add(order)
        session.flush()
        session.add(OmsManagedPosition(id=UUID(record.position_ids[account]), strategy_code="schwab_1m_v2",
            broker_account_name=account, symbol="FLYE", entry_order_id=order.id,
            entry_client_order_id=entry.client_order_id, entry_price=Decimal("2.34"), original_quantity=280,
            current_quantity=1 if case == "open_owned" else 0,
            status="open" if case == "open_owned" else "closed", entry_time=at(bind_time + 2)))
        if case in {"working_schwab", "working_webull", "operator_sell"}:
            venue = PRIMARY if case == "working_schwab" else WEBULL
            session.add(BrokerOrder(strategy_id=uuid4(), broker_account_id=ids[venue], symbol="FLYE",
                side="sell" if case == "operator_sell" else "buy", order_type="limit", time_in_force="day",
                quantity=1000, status="accepted", client_order_id="controlled-operator-order", payload={}))
        session.commit()

    clock[0] = FLYE["fresh_sell_poll_ms"] - 60_000
    book(strategy, record, clock, reason="CW_HARD_STOP" if case == "filled_stop" else "OCO_RESOLVED_FLAT")
    request = strategy._removed_wait_requests["FLYE"]
    assert request.purpose == "retry_exhausted"
    bot = service(strategy, store)
    bot._removed_wait_roll_anchor = current_session_anchor(at(clock[0]))
    bot._removed_wait_adapter = routing
    redis = OmsReceipt(oms)
    bot.intent_emitter = SchwabV2IntentEmitter(strategy.settings, redis, broker_account_name=PRIMARY)
    bot.webull_intent_emitter = SchwabV2IntentEmitter(strategy.settings, redis, broker_account_name=WEBULL)
    assert not book_calls
    await bot._drain_direct_strategy_intents()
    await oms._drain_cancel_terminal_evidence()
    primary._authorized_request_json.assert_not_awaited()
    assert len(redis.events) == 2 and all(e.payload.intent_type == "cancel" for e in redis.events)
    with sessions() as session:
        receipts = list(session.scalars(select(TradeIntent)))
        assert len(receipts) == 2
        assert len(book_calls) == 1, [(row.reason, row.payload) for row in receipts]
        assert all(row.payload["metadata"]["buy_submission_process_id"] == str(oms.broker_adapter.process_id)
                   for row in receipts)
        assert all(row.status == "rejected" and row.payload["refusal_code"] == "cancel_target_not_found"
                   for row in receipts)
        assert all(row.external_account_id is None for row in session.scalars(select(BrokerAccount)))
        webull_receipt, = [row for row in receipts if row.broker_account_id == ids[WEBULL]]
        assert JOURNAL_KEY in webull_receipt.payload
        assert webull_receipt.payload[JOURNAL_KEY]["binding"]["event_id"] == webull_receipt.payload["event_id"]
        token, = session.scalars(select(journal.BuySubmissionToken)).all()
        assert token.state == "reported_ambiguous" and token.answers[0]["status"] == "filled"
    assert store.request_publication_closed(request, last_publications={
        name: tuple(bot._clearwait_last_publications["FLYE", name]) for name in (PRIMARY, WEBULL)},
        now=at(clock[0]))

    if case == "operator_sell":
        state.position_qty = state.position_qty_held = 1000
    if case != "same_real_segment":
        clock[0] = FLYE["fresh_sell_poll_ms"]
        book(strategy, record, clock)
        sell(strategy, state, clock)
        assert strategy._removed_wait_evidence_wakes == {request}
    legs = (FlipPositionLeg(account, record.position_ids[account], bind_time + 2, 1),) if case == "open_owned" else ()
    book(strategy, record, clock, legs=legs, readable=case != "unknown_rows")
    await poll(bot)
    book(strategy, record, clock, legs=legs, readable=case != "unknown_rows")
    assert state.flip_owner_phase != "idle" and not strategy._strict_first_rest_admitted(state, slot="first")
    assert store.restore() == {"FLYE": request} and not store.restore_terminal_proofs()
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
    assert len(wires) == 1
    if case == "operator_sell":
        assert state.position_qty == state.position_qty_held == 1000
    with sessions() as session:
        assert not session.scalars(select(journal.BuyAdmissionClosure)).all()
