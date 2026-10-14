"""L1 shared-account rules and runtime fences; controlled books are labelled."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import (
    AccountPosition, Base, BrokerAccount, BrokerOrder, BrokerOrderEvent,
    Fill, OmsManagedPosition, Strategy, TradeIntent, VirtualPosition,
)
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy, TradeIntentDraft
from project_mai_tai.v2_removed_wait import RemovedWait, RemovedWaitProof, RemovedWaitStore, assess_clear_rules

PRIMARY, WEBULL = "live:schwab_1m_v2", "live:orb"
NAMES = {PRIMARY, WEBULL}
NOW = datetime(2026, 10, 9, 19, 4, tzinfo=UTC)
MS = int(NOW.timestamp() * 1000)


@pytest.fixture
def db(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "l1.sqlite"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    accounts = {n: uuid4() for n in NAMES}
    strategies = {n: uuid4() for n in ("schwab_1m_v2", "orb_schwab")}
    with factory() as session:
        for name, ident in accounts.items():
            session.add(BrokerAccount(id=ident, name=name, environment="live", external_account_id=name,
                                      provider="webull" if name == WEBULL else "schwab"))
        for name, ident in strategies.items():
            session.add(Strategy(id=ident, code=name, name=name))
        session.commit()
    yield RemovedWaitStore(factory), factory, accounts, strategies
    engine.dispose()


def request(*, purpose="scanner_removal", opportunity=MS - 60_000):
    return RemovedWait("JZ", opportunity, str(uuid4()), MS - 40_000, (PRIMARY, WEBULL), purpose)


def strategy(req, store, *, enabled=True, now=MS):
    strat = SchwabV2Strategy(Settings(_env_file=None,
        strategy_schwab_1m_v2_removed_wait_clear_rules_enabled=enabled,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_account_name=PRIMARY,
        strategy_schwab_1m_v2_webull_account_name=WEBULL))
    strat._now_ms = lambda: now
    strat.configure_fanout_identity_persistence(lambda *_: None)
    strat.configure_flip_entry_ownership(lambda *_: None, restore_readable=True)
    strat.configure_removed_wait(store.record_rules if enabled else store.record,
                                 restored={req.symbol: req}, readable=True)
    return strat


def apply_committed(strat, store, proof):
    assert not strat.stage_removed_wait_clear(proof)
    plan = strat._removed_wait_pending_clears[proof.request]
    store.commit_clear(plan, lambda: strat.removed_wait_clear_valid(plan))
    strat._removed_wait_committed_clears = {proof.request: plan}
    strat.apply_removed_wait_proofs((proof,))


def order_row(db, req, *, account=PRIMARY, own=False, status="accepted", side="buy", updated=None,
              submitted=None, broker_order_id=None):
    _, factory, accounts, strategies = db
    row = BrokerOrder(id=uuid4(), strategy_id=strategies["schwab_1m_v2" if own else "orb_schwab"],
        broker_account_id=accounts[account], symbol=req.symbol, side=side, order_type="limit",
        time_in_force="gtc", quantity=100, status=status, client_order_id=str(uuid4()),
        broker_order_id=broker_order_id,
        submitted_at=submitted or NOW - timedelta(days=2), updated_at=updated or NOW - timedelta(seconds=20),
        payload={"fanout_segment_id": str(req.opportunity_id)} if own else {})
    with factory() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
    return row


@pytest.mark.parametrize("opportunity", [0, MS - 60_000])
def test_scanner_empty_settled_snapshot_clears_without_dispatch_journal(db, opportunity):
    store, _, _, _ = db
    req = request(opportunity=opportunity)
    store.record_rules(req, True)
    proof, = store.clear_rule_proofs((req,), NAMES, now=NOW)
    assert proof.clear and proof.reason == "scanner_removal_flat"
    strat = strategy(req, store)
    strat.scanner_readded(req.symbol)
    apply_committed(strat, store, proof)
    assert store.restore() == {} and not strat._removed_wait_requests
    assert strat.line_buy_ready(req.symbol)


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("own", [False, True])
@pytest.mark.parametrize("status", ["accepted", "pending", "unknown", "aborted", "partially_filled"])
def test_any_strategy_and_account_working_buy_blocks_including_old_gtc(db, account, own, status):
    store, _, _, _ = db
    req = request()
    store.record_rules(req, True)
    order_row(db, req, account=account, own=own, status=status,
              broker_order_id="controlled-broker-id" if status == "aborted" else None)
    proof, = store.clear_rule_proofs((req,), NAMES, now=NOW)
    assert not proof.clear


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
def test_other_strategy_pending_open_blocks(db, account):
    store, factory, accounts, strategies = db
    req = request()
    store.record_rules(req, True)
    with factory() as session:
        session.add(TradeIntent(strategy_id=strategies["orb_schwab"], broker_account_id=accounts[account],
            symbol=req.symbol, side="buy", intent_type="open", quantity=10, reason="controlled ORB",
            status="pending", created_at=NOW - timedelta(days=2), updated_at=NOW - timedelta(days=2)))
        session.commit()
    assert not store.clear_rule_proofs((req,), NAMES, now=NOW)[0].clear


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("kind", ["account", "virtual", "managed"])
def test_position_in_either_account_and_any_strategy_blocks(db, account, kind):
    store, factory, accounts, strategies = db
    req = request()
    store.record_rules(req, True)
    row = {
        "account": lambda: AccountPosition(broker_account_id=accounts[account], symbol=req.symbol, quantity=1),
        "virtual": lambda: VirtualPosition(broker_account_id=accounts[account], strategy_id=strategies["orb_schwab"],
                                            symbol=req.symbol, quantity=1),
        "managed": lambda: OmsManagedPosition(broker_account_name=account, strategy_code="orb_schwab",
            symbol=req.symbol, entry_price=1, original_quantity=1, current_quantity=0, status="open"),
    }[kind]()
    with factory() as session:
        session.add(row)
        session.commit()
    assert not store.clear_rule_proofs((req,), NAMES, now=NOW)[0].clear


@pytest.mark.parametrize("purpose", ["scanner_removal", "retry_exhausted", "false_flip_restore"])
@pytest.mark.parametrize("outcome", ["already_absent", "confirmed_after_accepted_request", "could_not_tell", "not_confirmed"])
def test_cancel_outcomes_and_cancelled_ticket_fill_fence(db, purpose, outcome):
    store, factory, accounts, strategies = db
    req = request(purpose=purpose)
    store.record_rules(req, True)
    row = order_row(db, req, own=True, status="cancelled")
    with factory() as session:
        session.add(BrokerOrderEvent(order_id=row.id, event_type="cancelled", event_source="broker",
            event_at=NOW - timedelta(seconds=20), payload={"cancel_outcome": outcome}))
        for account in NAMES:
            session.add(TradeIntent(strategy_id=strategies["schwab_1m_v2"], broker_account_id=accounts[account],
                symbol=req.symbol, side="buy", intent_type="cancel", quantity=2, reason="controlled receipt",
                status="cancelled", created_at=NOW - timedelta(seconds=30), updated_at=NOW - timedelta(seconds=20),
                payload={"metadata": {"fanout_segment_id": str(req.opportunity_id),
                    "clearwait_removal_token": req.token, "clearwait_opportunity_id": str(req.opportunity_id),
                    "clearwait_buy_only": "true", "reason": "false_flip_restore" if purpose == "false_flip_restore"
                    else "retry_budget_exhausted"}}))
        session.commit()
    proof, = store.clear_rule_proofs((req,), NAMES, now=NOW)
    assert proof.clear is (outcome in {"already_absent", "confirmed_after_accepted_request"})
    if proof.clear:
        with factory() as session:
            session.add(Fill(order_id=row.id, strategy_id=strategies["schwab_1m_v2"],
                             broker_account_id=accounts[PRIMARY], symbol=req.symbol,
                             side="buy", quantity=1, price=1, filled_at=NOW - timedelta(seconds=25)))
            session.commit()
        assert not store.clear_rule_proofs((req,), NAMES, now=NOW)[0].clear


@pytest.mark.parametrize("age_ms", [14_999, 15_000])
def test_settle_boundary(db, age_ms):
    store, _, _, _ = db
    req = replace(request(), requested_at_ms=MS - age_ms)
    store.record_rules(req, True)
    proof, = store.clear_rule_proofs((req,), NAMES, now=NOW)
    assert proof.clear is (age_ms >= 15_000)


@pytest.mark.parametrize("purpose", ["scanner_removal", "retry_exhausted", "false_flip_restore"])
@pytest.mark.parametrize("opportunity", [0, MS - 86_400_000])
def test_prior_session_all_purposes_retire_only_request(db, purpose, opportunity):
    store, _, _, _ = db
    req = replace(request(purpose=purpose, opportunity=opportunity), requested_at_ms=MS - 86_000_000)
    store.record_rules(req, True)
    strat = strategy(req, store)
    state = strat.watchlist_state(req.symbol)
    state.flip_owner_opportunity_id = state.fanout_segment_id = MS
    state.flip_owner_phase = "consumed"
    state.flip_owner_fill_accounts.add(PRIMARY)
    before = (state.flip_owner_phase, state.fanout_segment_id, set(state.flip_owner_fill_accounts))
    proof, = store.clear_rule_proofs((req,), NAMES, now=NOW)
    assert proof.clear and proof.reason == "previous_session_flat"
    apply_committed(strat, store, proof)
    assert store.restore() == {} and not strat._removed_wait_requests
    assert before == (state.flip_owner_phase, state.fanout_segment_id, state.flip_owner_fill_accounts)


def test_previous_session_working_sell_blocks(db):
    store, _, _, _ = db
    req = request(opportunity=MS - 86_400_000)
    store.record_rules(req, True)
    order_row(db, req, side="sell")
    assert store.clear_rule_proofs((req,), NAMES, now=NOW)[0].reason == "working_order"


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
@pytest.mark.parametrize("status", ["filled", "cancelled"])
def test_zero_op_same_session_own_fill_blocks_even_before_removal(db, status, account):
    store, factory, accounts, strategies = db
    req = request(opportunity=0)
    store.record_rules(req, True)
    row = order_row(db, req, own=True, status=status, account=account, submitted=NOW - timedelta(hours=1))
    with factory() as session:
        session.add(Fill(order_id=row.id, strategy_id=strategies["schwab_1m_v2"],
            broker_account_id=accounts[account], symbol=req.symbol, side="buy", quantity=1, price=1,
            filled_at=NOW - timedelta(hours=1)))
        session.commit()
    assert store.clear_rule_proofs((req,), NAMES, now=NOW)[0].reason == "own_fill_stays_owned"


@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
def test_zero_op_unrelated_old_terminal_own_fill_does_not_block(db, account):
    store, factory, accounts, strategies = db
    req = request(opportunity=0)
    store.record_rules(req, True)
    old = NOW - timedelta(days=50)
    row = order_row(db, req, own=True, account=account, status="filled", submitted=old, updated=old)
    with factory() as session:
        saved = session.get(BrokerOrder, row.id)
        saved.payload = {"fanout_segment_id": str(MS - 50 * 86_400_000)}
        session.flush()
        saved.updated_at = old
        session.add(Fill(order_id=row.id, strategy_id=strategies["schwab_1m_v2"],
            broker_account_id=accounts[account], symbol=req.symbol, side="buy", quantity=1, price=1,
            filled_at=old))
        session.commit()
    assert store.clear_rule_proofs((req,), NAMES, now=NOW)[0].reason == "scanner_removal_flat"


@pytest.mark.parametrize("purpose", ["scanner_removal", "retry_exhausted", "false_flip_restore"])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_zero_op_previous_session_any_age_working_order_blocks_all_purposes(db, purpose, side):
    store, _, _, _ = db
    req = replace(request(opportunity=0, purpose=purpose), requested_at_ms=MS - 86_400_000)
    store.record_rules(req, True)
    order_row(db, req, side=side, submitted=NOW - timedelta(days=50))
    assert store.clear_rule_proofs((req,), NAMES, now=NOW)[0].reason == "working_order"


@pytest.mark.parametrize("purpose", ["scanner_removal", "retry_exhausted", "false_flip_restore"])
@pytest.mark.parametrize("outcome", ["could_not_tell", "not_confirmed"])
@pytest.mark.parametrize("opportunity", [0, MS - 86_400_000])
def test_old_unknown_cancel_outside_request_anchor_remains_open(db, purpose, outcome, opportunity):
    store, factory, _, _ = db
    req = request(purpose=purpose, opportunity=opportunity)
    store.record_rules(req, True)
    row = order_row(db, req, account=WEBULL, status="cancelled", side="sell",
                    updated=NOW - timedelta(days=2))
    with factory() as session:
        session.add(BrokerOrderEvent(order_id=row.id, event_type="cancelled", event_source="broker",
            event_at=NOW - timedelta(days=2), payload={"cancel_outcome": outcome}))
        session.commit()
    assert store.clear_rule_proofs((req,), NAMES, now=NOW)[0].reason == "cancel_unknown_or_refused"


@pytest.mark.asyncio
async def test_pending_clear_commit_is_offloop_and_fenced(db, monkeypatch):
    store, _, _, _ = db
    req = request(opportunity=0)
    store.record_rules(req, True)
    strat = strategy(req, store)
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strat, strat.settings, store
    store.clear_rule_proofs = lambda *_: (RemovedWaitProof(req, MS, True, "scanner_removal_flat"),)
    entered, release = threading.Event(), threading.Event()
    real_commit = store.commit_clear
    loop_thread = threading.get_ident()
    def slow_commit(plan, valid):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5)
        real_commit(plan, valid)
    monkeypatch.setattr(store, "commit_clear", slow_commit)
    task = asyncio.create_task(bot._removed_wait_poll())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert strat._removed_wait_requests and store.restore()
        assert req.symbol in bot._clearwait_committing_symbols
        await asyncio.wait_for(asyncio.sleep(0), .05)
        bot._clearwait_emit_generation_by_symbol = {req.symbol: 1}
    finally:
        release.set()
    await asyncio.wait_for(task, 5)
    assert strat._removed_wait_requests and store.restore()
    assert not strat.__dict__.get("_removed_wait_committed_clears")
    assert strat._removed_wait_restore_readable
    assert strat._removed_wait_pending_publications == {req.symbol: req}
    await bot._removed_wait_poll()
    assert not strat._removed_wait_requests and store.restore() == {}


def test_stale_zero_and_exact_token_cas(db):
    store, _, _, _ = db
    req = request(opportunity=0)
    store.record_rules(req, True)
    strat = strategy(req, store)
    proof, = store.clear_rule_proofs((req,), NAMES, now=NOW)
    strat.apply_removed_wait_proofs((replace(proof, observed_at_ms=MS - 15_001),))
    assert store.restore() == {req.symbol: req}
    newer = replace(req, token="new-token")
    store.record_rules(newer, True)
    assert store.clear_rule_proofs((req,), NAMES, now=NOW)[0].reason == "active_request_changed"
    assert not strat.stage_removed_wait_clear(proof)
    plan = strat._removed_wait_pending_clears[req]
    with pytest.raises(ValueError, match="changed"):
        store.commit_clear(plan, lambda: strat.removed_wait_clear_valid(plan))
    strat.apply_removed_wait_proofs((proof,))
    assert store.restore() == {req.symbol: newer} and strat._removed_wait_requests == {req.symbol: req}
    with pytest.raises(ValueError, match="changed"):
        store.record_rules(req, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["scanner_removal", "retry_exhausted"])
async def test_slow_same_symbol_clear_and_publication_leave_bar_quote_loop_free(db, monkeypatch, purpose):
    """Controlled symbol lock models PG contention; writes use a real disposable DB."""
    store, _, _, _ = db
    old = request(opportunity=MS - 86_400_000)
    store.record_rules(old, True)
    strat = strategy(old, store)
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strat, strat.settings, store
    store.clear_rule_proofs = lambda requests, _: tuple(RemovedWaitProof(
        r, MS, r == old, "previous_session_flat" if r == old else "settling") for r in requests)
    clear_entered, publication_entered, release = threading.Event(), threading.Event(), threading.Event()
    symbol_lock = threading.Lock()
    real_clear, real_publish = store.commit_clear, store.record_rules
    loop_thread = threading.get_ident()
    def slow_clear(plan, valid):
        with symbol_lock:
            clear_entered.set()
            assert release.wait(5)
            real_clear(plan, valid)
    def blocked_publication(req, active, valid=None):
        assert threading.get_ident() != loop_thread
        publication_entered.set()
        with symbol_lock:
            real_publish(req, active, valid)
    monkeypatch.setattr(store, "commit_clear", slow_clear)
    monkeypatch.setattr(store, "record_rules", blocked_publication)
    state = strat.watchlist_state(old.symbol)
    state.flip_owner_opportunity_id = state.fanout_segment_id = old.opportunity_id
    clear_task = asyncio.create_task(bot._removed_wait_poll())
    publication_task = None
    try:
        assert await asyncio.to_thread(clear_entered.wait, 5)
        state.cw_armed = True
        if purpose == "scanner_removal":
            strat.release_and_drop_symbol(old.symbol)
        else:
            strat._cancel_retry_leftovers(state)
        assert strat._removed_wait_requests == {old.symbol: old}
        assert not strat._removed_wait_pending_publications and not publication_entered.is_set()
        # Adversarial reconfiguration tests fencing, not a claimed runtime reset/lock incident.
        strat.configure_removed_wait(store.record_rules, restored={}, readable=True)
        state = strat.watchlist_state(old.symbol)
        state.cw_armed = True
        state.flip_owner_opportunity_id = state.fanout_segment_id = MS - 60_000
        started = asyncio.get_running_loop().time()
        if purpose == "scanner_removal":
            strat.release_and_drop_symbol(old.symbol)
        else:
            strat._cancel_retry_leftovers(state)
        assert asyncio.get_running_loop().time() - started < .05
        new = strat._removed_wait_requests[old.symbol]
        assert new.token != old.token and new.purpose == purpose
        assert strat._removed_wait_pending_publications == {new.symbol: new}
        assert strat._removed_wait_gate_closed(new.symbol)
        assert not strat.stage_removed_wait_clear(RemovedWaitProof(new, MS, True, "scanner_removal_flat"))
        if purpose == "scanner_removal":
            assert not any(d.metadata.get("clearwait_removal_token") == new.token
                           for d in strat._pending_intents)  # new barriers await durable publication
        publication_task = asyncio.create_task(bot._removed_wait_poll())
        assert await asyncio.to_thread(publication_entered.wait, 5)
        quote = Quote(new.symbol, last_price=4.8, bid_price=4.79, ask_price=4.81, quote_time_ms=MS)
        loop = asyncio.get_running_loop()
        started = previous = loop.time()
        while loop.time() - started < .12:
            assert strat.on_bar(new.symbol, ChartBar(new.symbol, 4.8, 4.9, 4.7, 4.8, 10, MS)) is None
            assert strat.on_quote(new.symbol, quote) is None
            await asyncio.sleep(.005)
            assert loop.time() - previous < .05
            previous = loop.time()
        assert not publication_task.done() and not clear_task.done()
        assert store.restore() == {old.symbol: old}
    finally:
        release.set()
        await asyncio.wait_for(clear_task, 5)
        if publication_task is not None:
            await asyncio.wait_for(publication_task, 5)
    assert store.restore() == {new.symbol: new}
    assert not strat._removed_wait_pending_publications
    assert strat._removed_wait_requests == {new.symbol: new}
    assert not strat.__dict__.get("_removed_wait_committed_clears")
    assert strat._removed_wait_gate_closed(new.symbol)
    assert any(d.metadata.get("clearwait_removal_token") == new.token for d in strat._pending_intents)


def test_publication_precommit_fence_rolls_back_and_stale_ack_stays_closed(db):
    store, _, _, _ = db
    req = request()
    answers = iter((True, False))
    with pytest.raises(ValueError, match="before commit"):
        store.record_rules(req, True, lambda: next(answers))
    assert store.restore() == {}
    strat = strategy(req, store)
    strat._publish_removed_wait(req)
    newer = replace(req, token="replacement-token")
    strat._removed_wait_requests[req.symbol] = newer
    strat.acknowledge_removed_wait_publication(req)
    assert strat._removed_wait_requests == {req.symbol: newer}
    assert strat._removed_wait_pending_publications == {req.symbol: newer}
    assert strat._removed_wait_restore_readable
    assert strat._removed_wait_gate_closed(req.symbol)


@pytest.mark.asyncio
async def test_failed_publication_is_retryable_without_clear_or_entry(db):
    store, _, _, _ = db
    req = request()
    strat = strategy(req, store)
    strat._publish_removed_wait(req)
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strat, strat.settings, store
    def unavailable(*_):
        raise RuntimeError("controlled publication failure")
    store.record_rules = unavailable
    store.clear_rule_proofs = lambda *_: pytest.fail("unpublished request was assessed for clear")
    await bot._removed_wait_poll()
    assert store.restore() == {}
    assert strat._removed_wait_pending_publications == {req.symbol: req}
    assert strat._removed_wait_requests == {req.symbol: req}
    assert strat._removed_wait_gate_closed(req.symbol)


@pytest.mark.asyncio
async def test_boot_retirement_and_offloop_symbol_generation_fence(db, monkeypatch):
    store, _, _, _ = db
    req = request(opportunity=MS - 86_400_000)
    store.record_rules(req, True)
    strat = strategy(req, store)
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot.session_factory = strat, strat.settings, store.session_factory
    monkeypatch.setattr(RemovedWaitStore, "clear_rule_proofs", lambda self, r, a: (RemovedWaitProof(
        req, MS, True, "previous_session_flat"),))
    await bot._configure_removed_wait_store()
    assert not strat._removed_wait_requests and store.restore() == {}
    assert not strat._pending_intents and not strat._pending_webull_direct_intents
    store.record_rules(req, True)
    strat._removed_wait_requests[req.symbol] = req
    entered, release = threading.Event(), threading.Event()
    def paused(*_):
        entered.set()
        assert release.wait(5)
        return (RemovedWaitProof(req, MS, True, "previous_session_flat"),)
    bot._removed_wait_store.clear_rule_proofs = paused
    task = asyncio.create_task(bot._removed_wait_poll())
    assert await asyncio.to_thread(entered.wait, 5)
    bot._clearwait_emit_generation_by_symbol = {req.symbol: 1}  # same-symbol completed emit (ABA)
    release.set()
    await asyncio.wait_for(task, 5)
    assert strat._removed_wait_requests and store.restore()
    assert strat._removed_wait_restore_readable


class RecordingEmitter:
    broker_account_name = PRIMARY

    def __init__(self):
        self.drafts = []

    async def emit(self, draft):
        self.drafts.append(draft)


async def emit_other_symbol_buy(bot, emitter):
    state = bot.strategy.watchlist_state("Y")
    state.fanout_segment_id = state.flip_owner_opportunity_id = MS
    assert bot.strategy.line_buy_ready("Y")
    draft = TradeIntentDraft("Y", "buy", "open", Decimal(1), "controlled unrelated BUY",
                             {"fanout_segment_id": str(MS)})
    assert await bot._emit_removal_tracked(emitter, draft)
    assert emitter.drafts[-1] is draft
    assert not bot._clearwait_open_emits
    assert bot._clearwait_emit_generation_by_symbol["Y"] == len(emitter.drafts)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["publication", "clear"])
@pytest.mark.parametrize("supersede", ["none", "before_commit", "after_commit"])
async def test_symbol_write_fence_y_buy_and_x_aba_retry(db, monkeypatch, caplog, boundary, supersede):
    """Real bot Y emit; controlled already-admitted X ABA, before or after a real DB commit."""
    store, _, _, _ = db
    req = request(opportunity=0)
    store.record_rules(req, True)
    strat = strategy(req, store)
    strat.scanner_readded(req.symbol)
    if boundary == "publication":
        strat._publish_removed_wait(req)
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strat, strat.settings, store
    store.clear_rule_proofs = lambda *_: (RemovedWaitProof(req, MS, True, "scanner_removal_flat"),)
    entered, release = threading.Event(), threading.Event()
    method = "record_rules" if boundary == "publication" else "commit_clear"
    real = getattr(store, method)
    loop_thread = threading.get_ident()
    def paused(*args):
        assert threading.get_ident() != loop_thread
        if supersede == "after_commit":
            real(*args)
        entered.set()
        assert release.wait(5)
        if supersede != "after_commit":
            real(*args)
    monkeypatch.setattr(store, method, paused)
    task = asyncio.create_task(bot._removed_wait_poll())
    emitter = RecordingEmitter()
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert strat._removed_wait_gate_closed(req.symbol)
        await emit_other_symbol_buy(bot, emitter)
        if supersede != "none":
            # Mirror a same-symbol emit that was already admitted: active count
            # returns to zero, but its monotonic symbol generation must survive.
            bot._clearwait_open_emits[req.symbol] = 1
            generations = bot._clearwait_emit_generation_by_symbol
            generations[req.symbol] = generations.get(req.symbol, 0) + 1
            bot._clearwait_open_emits.pop(req.symbol)
    finally:
        release.set()
    await asyncio.wait_for(task, 5)
    assert strat._removed_wait_restore_readable
    await emit_other_symbol_buy(bot, emitter)
    if supersede == "none":
        assert not strat._removed_wait_requests and store.restore() == {}
        assert "reason=superseded_retry" not in caplog.text
    else:
        assert strat._removed_wait_requests == {req.symbol: req}
        assert strat._removed_wait_pending_publications == {req.symbol: req}
        assert strat._removed_wait_gate_closed(req.symbol)
        assert not strat._removed_wait_committed_clears
        assert not strat._removed_wait_pending_clears  # next poll must obtain fresh proof
        assert f"{req.symbol} reason=superseded_retry" in caplog.text
        monkeypatch.setattr(store, method, real)
        await bot._removed_wait_poll()
        assert not strat._removed_wait_requests and store.restore() == {}
        assert strat._removed_wait_restore_readable


@pytest.mark.asyncio
async def test_stale_publication_ack_retries_current_token_without_fleet_freeze(db, caplog):
    store, _, _, _ = db
    old = request(opportunity=0)
    store.record_rules(old, True)
    strat = strategy(old, store)
    strat.scanner_readded(old.symbol)
    strat._publish_removed_wait(old)
    current = replace(old, token="current-request")
    strat._removed_wait_requests[old.symbol] = current
    strat.acknowledge_removed_wait_publication(old)
    assert strat._removed_wait_restore_readable
    assert strat._removed_wait_pending_publications == {current.symbol: current}
    assert strat._removed_wait_gate_closed(current.symbol)
    assert f"{current.symbol} reason=superseded_retry" in caplog.text
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strat, strat.settings, store
    await emit_other_symbol_buy(bot, RecordingEmitter())
    store.clear_rule_proofs = lambda requests, _: tuple(RemovedWaitProof(
        r, MS, True, "scanner_removal_flat") for r in requests)
    await bot._removed_wait_poll()
    assert strat._removed_wait_restore_readable and not strat._removed_wait_requests
    assert store.restore() == {}


@pytest.mark.asyncio
async def test_superseded_symbol_read_does_not_discard_other_symbol_proofs(db, monkeypatch):
    store, _, _, _ = db
    x = request(opportunity=0)
    z = replace(x, symbol="Z", token="z-request")
    store.record_rules(x, True)
    store.record_rules(z, True)
    strat = strategy(x, store)
    strat._removed_wait_requests[z.symbol] = z
    strat.scanner_readded(x.symbol)
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strat, strat.settings, store
    entered, release = threading.Event(), threading.Event()
    def paused(requests, _):
        entered.set()
        assert release.wait(5)
        return tuple(RemovedWaitProof(r, MS, True, "scanner_removal_flat") for r in requests)
    monkeypatch.setattr(store, "clear_rule_proofs", paused)
    task = asyncio.create_task(bot._removed_wait_poll())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        bot._clearwait_emit_generation_by_symbol = {x.symbol: 1}
        await emit_other_symbol_buy(bot, RecordingEmitter())
    finally:
        release.set()
    await asyncio.wait_for(task, 5)
    assert strat._removed_wait_restore_readable
    assert strat._removed_wait_requests == {x.symbol: x}
    assert store.restore() == {x.symbol: x}
    assert strat._removed_wait_gate_closed(x.symbol) and not strat._removed_wait_gate_closed(z.symbol)
    assert strat._removed_wait_pending_publications == {x.symbol: x}
    await bot._removed_wait_poll()
    assert not strat._removed_wait_requests and store.restore() == {}


@pytest.mark.asyncio
async def test_active_symbol_emit_does_not_hold_other_request(db):
    store, _, _, _ = db
    x = request(opportunity=0)
    z = replace(x, symbol="Z", token="z-request")
    for req in (x, z):
        store.record_rules(req, True)
    strat = strategy(x, store)
    strat._removed_wait_requests[z.symbol] = z
    strat.scanner_readded(x.symbol)
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strat, strat.settings, store
    bot._clearwait_open_emits = {x.symbol: 1}
    store.clear_rule_proofs = lambda requests, _: tuple(RemovedWaitProof(
        r, MS, True, "scanner_removal_flat") for r in requests)
    await bot._removed_wait_poll()
    assert strat._removed_wait_restore_readable
    assert strat._removed_wait_requests == store.restore() == {x.symbol: x}
    assert strat._removed_wait_gate_closed(x.symbol) and not strat._removed_wait_gate_closed(z.symbol)
    bot._clearwait_open_emits.clear()
    await bot._removed_wait_poll()
    assert not strat._removed_wait_requests and store.restore() == {}


@pytest.mark.asyncio
async def test_superseded_clear_requires_new_proof_when_working_buy_appears(db, monkeypatch):
    store, _, _, _ = db
    req = request(opportunity=0)
    store.record_rules(req, True)
    strat = strategy(req, store)
    strat.scanner_readded(req.symbol)
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strat, strat.settings, store
    read = store.clear_rule_proofs
    store.clear_rule_proofs = lambda requests, accounts: read(requests, accounts, now=NOW)
    real_commit = store.commit_clear
    def superseded(plan, valid):
        # Controlled completed same-symbol emit; no admission or provider override.
        bot._clearwait_emit_generation_by_symbol = {req.symbol: 1}
        real_commit(plan, valid)
    monkeypatch.setattr(store, "commit_clear", superseded)
    await bot._removed_wait_poll()
    assert strat._removed_wait_pending_publications == {req.symbol: req}
    assert strat._removed_wait_requests == store.restore() == {req.symbol: req}
    monkeypatch.setattr(store, "commit_clear", real_commit)
    order_row(db, req)  # actual shared-account working BUY in the disposable DB
    await bot._removed_wait_poll()
    assert strat._removed_wait_restore_readable
    assert strat._removed_wait_requests == store.restore() == {req.symbol: req}
    assert strat._removed_wait_gate_closed(req.symbol)
    assert not strat._removed_wait_committed_clears and not strat._removed_wait_pending_clears


def test_null_status_unknown_is_not_empty():
    req = request()
    rows = dict(now=NOW, accounts={PRIMARY: PRIMARY, WEBULL: WEBULL}, intents=[], filled_order_ids=set(),
        order_events=[], snapshots=[SimpleNamespace(snapshot_type="v2_removed_wait", payload=req.payload(active=True))],
        has_position=False, strategy_id="v2")
    for column in ("orders", "intents"):
        row = SimpleNamespace(status=None, intent_type="open", side="buy", updated_at=NOW)
        assert not assess_clear_rules(req, orders=[row] if column == "orders" else [],
                                      **{**rows, "intents": [row] if column == "intents" else []}).clear


def test_flag_default_catalog_owner():
    flag = "strategy_schwab_1m_v2_removed_wait_clear_rules_enabled"
    assert getattr(Settings(_env_file=None), flag) is True
    catalog = json.loads((Path(__file__).parents[2] / "ops/health/expected_flags.json").read_text())
    row, = [r for r in catalog["flags"] if r["name"] == flag]
    assert row["expected"] is True and row["owning_service"] == "schwab-1m-v2"
    assert "also_check_services" not in row


@pytest.mark.parametrize("value, expected", [(True, True), (False, False), ("false", False), (None, False)])
def test_clear_rule_flag_uses_boolean_permission(value, expected):
    settings = SimpleNamespace(**Settings(_env_file=None).model_dump())
    settings.strategy_schwab_1m_v2_removed_wait_clear_rules_enabled = value
    assert SchwabV2Strategy(settings)._removed_wait_clear_rules_enabled is expected


def test_legacy_constructor_omitted_flag_keeps_default_on():
    settings = SimpleNamespace(**Settings(_env_file=None).model_dump())
    del settings.strategy_schwab_1m_v2_removed_wait_clear_rules_enabled
    assert SchwabV2Strategy(settings)._removed_wait_clear_rules_enabled is True


@pytest.mark.asyncio
async def test_atomic_owner_retirement_does_not_call_sync_callbacks(db):
    store, factory, _, _ = db
    req = request()
    store.record_rules(req, True)
    strat = strategy(req, store)
    state = strat.watchlist_state(req.symbol)
    state.flip_owner_opportunity_id = state.fanout_segment_id = req.opportunity_id
    state.flip_owner_phase = "resting"
    state.flip_owner_evidence_readable = True
    state.flip_owner_evidence_at_ms = MS
    def forbidden(*_):
        pytest.fail("a new proof application invoked synchronous persistence")
    strat._fanout_identity_persist = strat._flip_owner_persist = strat._removed_wait_persist = forbidden
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strat, strat.settings, store
    proof = RemovedWaitProof(req, MS, True, "scanner_removal_flat")
    store.clear_rule_proofs = lambda *_: (proof,)
    strat.apply_removed_wait_proofs((proof,))
    assert state.fanout_segment_id == req.opportunity_id and strat._removed_wait_requests
    await bot._removed_wait_poll()
    assert not strat._removed_wait_requests and state.fanout_segment_id == 0
    assert state.flip_owner_phase == "idle" and store.restore() == {}
    from project_mai_tai.db.models import DashboardSnapshot
    from sqlalchemy import select
    with factory() as session:
        rows = session.scalars(select(DashboardSnapshot)).all()
        assert {r.snapshot_type for r in rows if r.payload.get("active") is False} == {
            "v2_removed_wait", "v2_fanout_segment_identity", "v2_flip_entry_ownership"}


def test_later_sell_bar_ack_requires_fresh_proof_not_second_commit(db):
    store, _, _, _ = db
    req = request(purpose="retry_exhausted")
    store.record_rules(req, True)
    clock = [MS]
    strat = strategy(req, store)
    strat._now_ms = lambda: clock[0]
    state = strat.watchlist_state(req.symbol)
    state.flip_owner_opportunity_id = state.fanout_segment_id = req.opportunity_id
    state.flip_owner_phase = "consumed"
    state.fresh_sell_owner_release_segment_ms = MS
    proof = RemovedWaitProof(req, MS, True, "controlled_fresh_sell_flat")
    assert not strat.stage_removed_wait_clear(proof, retire_owner=True, reason="fresh_sell_cancelled_flat")
    plan = strat._removed_wait_pending_clears.pop(req)
    store.commit_clear(plan, lambda: strat.removed_wait_clear_valid(plan))
    strat._removed_wait_committed_clears = {req: plan}
    assert store.restore() == {} and strat._removed_wait_requests
    clock[0] += 60_000
    assert not strat.stage_removed_wait_clear(proof, retire_owner=True, reason="fresh_sell_cancelled_flat")
    fresh = replace(proof, observed_at_ms=clock[0])
    assert strat.stage_removed_wait_clear(fresh, retire_owner=True, reason="fresh_sell_cancelled_flat")
    assert not strat.stage_removed_wait_clear(fresh, retire_owner=True, reason="fresh_sell_cancelled_flat",
                                             validate=lambda: False)
    state.fresh_sell_owner_release_segment_ms += 60_000
    assert not strat.stage_removed_wait_clear(fresh, retire_owner=True, reason="fresh_sell_cancelled_flat")


def test_atomic_retirement_precommit_mismatch_rolls_back_all_rows(db):
    store, factory, _, _ = db
    req = request()
    store.record_rules(req, True)
    strat = strategy(req, store)
    state = strat.watchlist_state(req.symbol)
    state.flip_owner_opportunity_id = state.fanout_segment_id = req.opportunity_id
    proof = RemovedWaitProof(req, MS, True, "scanner_removal_flat")
    strat.stage_removed_wait_clear(proof, retire_owner=True)
    plan = strat._removed_wait_pending_clears[req]
    answers = iter((True, False))
    with pytest.raises(ValueError, match="before commit"):
        store.commit_clear(plan, lambda: next(answers))
    from sqlalchemy import func, select
    from project_mai_tai.db.models import DashboardSnapshot
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(DashboardSnapshot)) == 1
    assert store.restore() == {req.symbol: req}
