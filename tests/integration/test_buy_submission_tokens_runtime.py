"""Required real PostgreSQL pre-wire, crash and admission-race controls."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import inspect
import threading
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.schwab import SchwabAccountConfig, SchwabBrokerAdapter
from project_mai_tai.cancel_terminal_proof import (
    UnboundCancelFences, UnboundCancelRequest, evaluate_unbound_cancel_terminal,
)
from project_mai_tai.db.models import AccountPosition, BrokerAccount, BrokerOrder, Strategy, TradeIntent, VirtualPosition
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload, TradeIntentEvent, TradeIntentPayload
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore, current_session_anchor
from project_mai_tai.oms import buy_submission_journal as journal
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
import test_cancel_terminal_runtime as runtime

sessions = runtime.sessions
sdk = runtime.sdk
NOW = 1_791_559_801_000
GENERATION = str(NOW + 999_999)


async def setup_protocol(sessions, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(journal, "now_ms", lambda: clock[0])
    sc = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    sc.accounts_by_name = {"schwab": SchwabAccountConfig(account_hash="actual-hash")}
    wb = broker.broker_binding(runtime.adapter(runtime.Client()), "live:orb")[0]
    routed = RoutingBrokerAdapter(default_provider="schwab",
        provider_by_account={"schwab": "schwab", "live:orb": "webull"},
        factories_by_provider={"schwab": lambda: sc, "webull": lambda: wb})
    guard = journal.DurableBuyAdapter(routed, sessions)
    await guard.start()
    clock[0] += 1000
    for symbol in ("DKI", "FLYE"):
        FanoutSegmentIdentityStore(sessions).record(symbol, int(GENERATION), True, "segment_bind",
            now=datetime.fromtimestamp(clock[0] / 1000, UTC))
    clock[0] += 2000
    return clock, sc, wb, guard


def scope(guard, symbol="DKI", name="schwab", **changes):
    return replace(journal.NeverSentScope(name, "actual-hash" if name == "schwab" else "ACC1",
        symbol, GENERATION, NOW + 1000, "request-id", "request-token", NOW + 2000,
        guard.process_id, current_session_anchor(datetime.fromtimestamp(NOW / 1000, UTC)).isoformat()), **changes)


def buy(symbol="DKI", **md):
    return OrderRequest("exact-coid", "schwab", "schwab_1m_v2", symbol, "buy", "open", Decimal(1),
        "ENTRY", {"fanout_segment_id": GENERATION, **md})


async def noop(*args, **kwargs):
    pass


def oms(sessions, adapter, monkeypatch):
    service = OmsRiskService(settings=Settings(oms_adapter="simulated", broker_default_provider="schwab",
        orb_broker_account_name="unused", strategy_schwab_1m_v2_account_name="schwab"),
        redis_client=SimpleNamespace(), session_factory=sessions, broker_adapter=adapter)
    monkeypatch.setattr(service, "_evaluate_risk", lambda event: (True, "controlled"))
    monkeypatch.setattr(service, "_market_is_fillable", lambda *a: True)
    monkeypatch.setattr(service, "_publish_order_event", noop)
    monkeypatch.setattr(service, "_reconcile_after_intent", noop)
    return service


@pytest.mark.parametrize("symbol", ["DKI", "FLYE"])
@pytest.mark.asyncio
async def test_actual_postcoverage_two_account_journal_never_dispatched_replays(sessions, sdk, monkeypatch, symbol):
    clock, _sc, _wb, guard = await setup_protocol(sessions, monkeypatch)
    sc, wb = scope(guard, symbol), scope(guard, symbol, "live:orb")
    monkeypatch.setattr(broker, "now_ms", lambda: clock[0])
    books = {"live:orb": await broker.acquire_complete_working_book(
        guard, "live:orb", after_ms=sc.requested_at_ms)}
    assert books["live:orb"] is not None
    expected = UnboundCancelRequest(symbol, sc.request_id, sc.request_token, sc.generation,
        "retry_exhausted", sc.requested_at_ms, {s.account_name: s.account_id for s in (sc, wb)},
        {"schwab": "schwab", "live:orb": "webull"}, sc.session_key, sc.opportunity_started_at_ms,
        {s.account_name: str(guard.process_id) for s in (sc, wb)})
    with sessions() as session:
        witnesses = {s.account_name: journal.close_never_sent_admission(session, s, observed_at_ms=clock[0])
                     for s in (sc, wb)}
        assert not evaluate_unbound_cancel_terminal(expected, {},
            fences=UnboundCancelFences(expected, True, True, True, True),
            never_sent_witnesses=witnesses, now_ms=clock[0]).terminal
        assert evaluate_unbound_cancel_terminal(expected, books,
            fences=UnboundCancelFences(expected, True, True, True, True),
            never_sent_witnesses=witnesses, now_ms=clock[0]).terminal
        assert session.execute(text("SELECT CAST(:epoch AS bigint)"),
                               {"epoch": sc.opportunity_started_at_ms}).scalar_one() == NOW + 1000
        session.commit()
    # These are controlled never-dispatched opportunities, NOT historical cards
    # or a filled-owned generation with its BUY token silently omitted.


@pytest.mark.parametrize("kind", ["submit", "replace"])
@pytest.mark.asyncio
async def test_actual_durable_token_visible_before_each_physical_buy_entrypoint(sessions, sdk, monkeypatch, kind):
    _clock, sc, _wb, guard = await setup_protocol(sessions, monkeypatch)
    called = []
    async def wire(req, *args):
        with sessions() as independent:
            token = independent.scalar(select(journal.BuySubmissionToken))
            assert token and token.state == "submitting"
            assert (token.account_id, token.symbol, token.client_order_id, token.wire_kind) == (
                "actual-hash", req.symbol, req.client_order_id, kind)
        called.append(req)
        answer = ExecutionReport("accepted", req.client_order_id, broker_order_id=None)
        return [answer] if kind == "submit" else answer
    sc.submit_order = wire
    sc.replace_bracket_order = wire
    if kind == "submit":
        await guard.submit_order(buy())
    else:
        await guard.replace_bracket_order(buy(), "old-exact-broker-id")
    assert len(called) == 1
    with sessions() as session:
        assert session.scalar(select(journal.BuySubmissionToken)).state == "reported_ambiguous"


@pytest.mark.asyncio
async def test_actual_ordinary_oms_crash_rolls_back_intent_not_durable_attempt(sessions, sdk, monkeypatch):
    clock, sc, _wb, guard = await setup_protocol(sessions, monkeypatch)
    service = oms(sessions, guard.cancel_terminal_delegate, monkeypatch)
    service.broker_adapter = guard
    reached = []
    async def accepted_then_crashed(req):
        with sessions() as independent:
            assert not list(independent.scalars(select(TradeIntent)))
            assert not list(independent.scalars(select(BrokerOrder)))
            assert independent.scalar(select(journal.BuySubmissionToken)).state == "submitting"
            assert not journal.close_never_sent_admission(independent, scope(guard),
                                                         observed_at_ms=clock[0]).never_sent
        reached.append(req)
        raise RuntimeError("controlled crash after broker accepted before answer/ordinary commit")
    sc.submit_order = accepted_then_crashed
    event = TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name="schwab", symbol="DKI", side="buy",
        quantity=Decimal(1), intent_type="open", reason="ENTRY",
        metadata={"reference_price": "2", "entry_size_price": "2", "fanout_segment_id": GENERATION}))
    with pytest.raises(RuntimeError, match="controlled crash"):
        await service.process_trade_intent(event)
    assert reached
    with sessions() as independent:
        assert not list(independent.scalars(select(TradeIntent)))
        assert not list(independent.scalars(select(BrokerOrder)))
        assert independent.scalar(select(journal.BuySubmissionToken)).state == "submitting"
    clock[0] += 4000
    restart = journal.DurableBuyAdapter(guard.cancel_terminal_delegate, sessions)
    await restart.start()
    with sessions() as independent:
        result = journal.close_never_sent_admission(independent,
            replace(scope(guard), coverage_process_id=restart.process_id, requested_at_ms=clock[0]),
            observed_at_ms=clock[0])
        assert not result.never_sent and result.reason == "never_sent_legacy_or_coverage_unknown"


@pytest.mark.parametrize("commit_cas", [True, False])
@pytest.mark.asyncio
async def test_actual_pg_release_lock_serializes_new_submit_and_cas_rollback(sessions, sdk, monkeypatch, commit_cas):
    clock, sc, _wb, guard = await setup_protocol(sessions, monkeypatch)
    holding, finish = threading.Event(), threading.Event()
    called = []
    async def wire(req):
        called.append(req)
        return []
    sc.submit_order = wire
    def consumer():
        with sessions() as session:
            witness = journal.close_never_sent_admission(session, scope(guard), observed_at_ms=clock[0])
            assert witness.never_sent
            holding.set()
            assert finish.wait(5)
            session.commit() if commit_cas else session.rollback()
    release_task = asyncio.create_task(asyncio.to_thread(consumer))
    assert await asyncio.to_thread(holding.wait, 2)
    submit = asyncio.create_task(guard.submit_order(buy()))
    try:
        await asyncio.sleep(0.05)
        assert called == [] and not submit.done()
    finally:
        finish.set()
        await release_task
    if commit_cas:
        with pytest.raises(journal.BuyAdmissionClosed):
            await submit
        assert called == []
    else:
        await submit
        assert len(called) == 1


@pytest.mark.asyncio
async def test_actual_pg_new_attempt_wins_absence_race(sessions, sdk, monkeypatch):
    clock, sc, _wb, guard = await setup_protocol(sessions, monkeypatch)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def wire(req):
        entered.set()
        await finish.wait()
        return []
    sc.submit_order = wire
    submitted = asyncio.create_task(guard.submit_order(buy()))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with sessions() as session:
            assert not journal.close_never_sent_admission(session, scope(guard), observed_at_ms=clock[0]).never_sent
    finally:
        finish.set()
        await submitted


@pytest.mark.parametrize("outcome", ["599", "empty", "timeout"])
@pytest.mark.asyncio
async def test_ambiguous_answer_survives_new_opportunity_and_restart(sessions, sdk, monkeypatch, outcome):
    clock, sc, _wb, guard = await setup_protocol(sessions, monkeypatch)
    async def wire(req):
        if outcome == "timeout":
            raise TimeoutError("after send")
        return [] if outcome == "empty" else [ExecutionReport("rejected", req.client_order_id,
            origin="broker", reason="synthetic599", metadata={"http_status": "599"})]
    sc.submit_order = wire
    if outcome == "timeout":
        with pytest.raises(TimeoutError):
            await guard.submit_order(buy())
    else:
        await guard.submit_order(buy())
    clock[0] += 5000
    new_generation = str(int(GENERATION) + 1)
    FanoutSegmentIdentityStore(sessions).record("DKI", int(new_generation), True, "segment_bind",
        now=datetime.fromtimestamp(clock[0] / 1000, UTC))
    with sessions() as session:
        result = journal.close_never_sent_admission(session,
            replace(scope(guard), generation=new_generation, opportunity_started_at_ms=clock[0],
                    requested_at_ms=clock[0]), observed_at_ms=clock[0])
        assert not result.never_sent and result.reason == "never_sent_attempt_or_ambiguity_present"


@pytest.mark.parametrize("mutation", ["precommit", "closure", "epoch", "reported"])
@pytest.mark.asyncio
async def test_real_pg_current_source_mutations_detected(sessions, sdk, monkeypatch, mutation):
    import textwrap
    target = journal.close_never_sent_admission if mutation == "epoch" else getattr(
        journal.DurableBuyAdapter, "_reported" if mutation == "reported" else "_prepare")
    source = textwrap.dedent(inspect.getsource(target))
    old, new = {
        "precommit": ("session.commit()\n        return token.id", "session.flush()\n        return token.id"),
        "closure": ("if any(c.generation == generation or opportunity <= c.opportunity_started_at_ms for c in closures):", "if False:"),
        "epoch": ("not 0 < coverage <= scope.opportunity_started_at_ms", "False"),
        "reported": ('token.state = "reported_ambiguous"', 'token.state = "broker_terminal"'),
    }[mutation]
    damaged = source.replace(old, new)
    assert damaged != source
    namespace = dict(vars(journal))
    exec(compile(damaged, "<buy-journal-test-mutant>", "exec"), namespace)
    owner = journal if mutation == "epoch" else journal.DurableBuyAdapter
    monkeypatch.setattr(owner, target.__name__, namespace[target.__name__])
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        if mutation == "precommit":
            await test_actual_durable_token_visible_before_each_physical_buy_entrypoint(sessions, sdk, monkeypatch, "submit")
        elif mutation == "closure":
            await test_actual_pg_release_lock_serializes_new_submit_and_cas_rollback(sessions, sdk, monkeypatch, True)
        elif mutation == "reported":
            await test_ambiguous_answer_survives_new_opportunity_and_restart(sessions, sdk, monkeypatch, "599")
        else:
            clock, _sc, _wb, guard = await setup_protocol(sessions, monkeypatch)
            clock[0] += 5000
            restart = journal.DurableBuyAdapter(guard.cancel_terminal_delegate, sessions)
            await restart.start()
            with sessions() as session:
                assert not journal.close_never_sent_admission(session,
                    replace(scope(guard), coverage_process_id=restart.process_id, requested_at_ms=clock[0]),
                    observed_at_ms=clock[0]).never_sent


@pytest.mark.asyncio
async def test_actual_oms_200_events_60_seconds_concurrent_buys_and_30_second_read(sessions, sdk, monkeypatch, capsys):
    # Real OMS entry points + real PG, controlled broker/Redis endpoints only.
    def real_now():
        return int(datetime.now(UTC).timestamp() * 1000)
    monkeypatch.setattr(broker, "now_ms", real_now)
    monkeypatch.setattr(journal, "now_ms", real_now)
    client = runtime.Client(detail=runtime.EMPTY_DETAIL)
    routed = runtime.adapter(client)
    service = OmsRiskService(settings=Settings(oms_adapter="simulated", broker_default_provider="webull",
        orb_broker_account_name="unused"), redis_client=SimpleNamespace(), session_factory=sessions,
        broker_adapter=routed)
    monkeypatch.setattr(service, "_evaluate_risk", lambda event: (True, "controlled"))
    monkeypatch.setattr(service, "_market_is_fillable", lambda *a: True)
    monkeypatch.setattr(service, "_reconcile_after_intent", noop)
    publications = []
    async def publish(event):
        publications.append(event)
    monkeypatch.setattr(service, "_publish_order_event", publish)
    await service.broker_adapter.start()
    started = threading.Event()
    stall_duration = []
    def stall():
        if client.calls[-1][0] == "open":
            assert publications
            started.set()
            begin = monotonic()
            sleep(30)
            stall_duration.append(monotonic() - begin)
    client.on_read = stall
    leaf = broker.broker_binding(routed, "live:orb")[0]
    wires, wire_times = [], {}
    async def wire(req):
        wires.append((req.side, req.intent_type))
        wire_times[req.symbol] = monotonic()
        return [ExecutionReport("accepted", req.client_order_id, broker_order_id="controlled-" + req.client_order_id,
            symbol=req.symbol, side=req.side, intent_type=req.intent_type,
            quantity=req.quantity, metadata=req.metadata)]
    leaf.submit_order = wire
    # Distinct operational positions exercise actual SELL guards, not never-sent
    # ownership certificates. Never fabricate a filled generation for release.
    with sessions() as session:
        strategy = Strategy(code="macd_30s", name="controlled", execution_mode="live")
        account = BrokerAccount(name="live:orb", provider="webull", environment="test")
        session.add_all([strategy, account, Strategy(code="schwab_1m_v2", name="v2", execution_mode="live")])
        session.flush()
        for index in range(1, 200, 2):
            symbol = f"EXIT{index}"
            session.add_all([
                VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
                    symbol=symbol, quantity=Decimal(1), average_price=Decimal(2)),
                AccountPosition(broker_account_id=account.id, symbol=symbol,
                    quantity=Decimal(1), average_price=Decimal(2)),
            ])
        session.commit()
    feedback_start = monotonic()
    await service.process_trade_intent(runtime.cancel_event())
    feedback_ms = (monotonic() - feedback_start) * 1000
    assert await asyncio.to_thread(started.wait, 3)
    def intent(side, index):
        return TradeIntentEvent(source_service="test", payload=TradeIntentPayload(strategy_code="macd_30s",
            broker_account_name="live:orb", symbol=f"BUY{index}" if side == "buy" else f"EXIT{index}", side=side,
            quantity=Decimal(1), intent_type="open" if side == "buy" else "close",
            reason=f"CONTROLLED_{index}", metadata={"reference_price": "2"}))
    async def concurrent_buys():
        for index in range(25):
            await service.process_trade_intent(intent("buy", index))
            await asyncio.sleep(0.4)
    buys = asyncio.create_task(concurrent_buys())
    quote_ms, close_ms, close_before_wire_ms, close_after_wire_ms, slow_closes = [], [], [], [], []
    baseline = monotonic()
    try:
        for index in range(200):
            await asyncio.sleep(max(0, baseline + index * 0.3 - monotonic()))
            begin = monotonic()
            if index % 2 == 0:
                await service._handle_quote_tick_event(QuoteTickEvent(source_service="test",
                    payload=QuoteTickPayload(symbol="DKI", bid_price=Decimal(2), ask_price=Decimal("2.01"))))
                quote_ms.append((monotonic() - begin) * 1000)
            else:
                result = await service.process_trade_intent(intent("sell", index))
                finished = monotonic()
                elapsed = (finished - begin) * 1000
                wire_at = wire_times[f"EXIT{index}"]
                close_ms.append(elapsed)
                close_before_wire_ms.append((wire_at - begin) * 1000)
                close_after_wire_ms.append((finished - wire_at) * 1000)
                if elapsed >= 50:
                    slow_closes.append((index, round(begin - baseline, 3), round(elapsed, 3)))
                assert result and result[0].payload.status == "accepted"
        await asyncio.sleep(max(0, baseline + 60 - monotonic()))
        await buys
    finally:
        await asyncio.gather(buys, return_exceptions=True)
        await service._drain_cancel_terminal_evidence()
    assert len(quote_ms) == len(close_ms) == 100
    assert len([w for w in wires if w == ("buy", "open")]) == 25
    assert len([w for w in wires if w == ("sell", "close")]) == 100
    assert stall_duration and stall_duration[0] >= 30
    metrics = (f"[CANCEL-TOKEN-PG-LATENCY] events=200 seconds={monotonic()-baseline:.3f} buys=25 "
               f"stall_s={stall_duration[0]:.3f} feedback_ms={feedback_ms:.3f} "
               f"quote_max_ms={max(quote_ms):.3f} protective_close_max_ms={max(close_ms):.3f} "
               f"close_before_wire_max_ms={max(close_before_wire_ms):.3f} "
               f"close_after_wire_max_ms={max(close_after_wire_ms):.3f} "
               f"slow_closes_index_seconds_ms={slow_closes} proof_raw_gets={len(client.calls)}")
    with capsys.disabled():
        print(metrics, flush=True)
    assert feedback_ms < 50 and max(quote_ms) < 50 and max(close_ms) < 50, metrics
    assert len(client.calls) == 2  # Detail + list-open; no retry or per-tick proof read.
    with sessions() as session:
        tokens = list(session.scalars(select(journal.BuySubmissionToken)))
        assert len(tokens) == 25 and all(t.state == "reported_ambiguous" for t in tokens)
