"""Deterministic functional controls; no rate/CPU/performance acceptance run."""
import ast
import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import inspect
import threading
from textwrap import dedent
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Strategy, TradeIntent,
)
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.settings import Settings
from project_mai_tai.v2_restart_rest_store import (
    RETENTION_DAYS, SNAPSHOT_TYPE, RestartCheckpointWriter, RestartRestStore,
)
from tests.unit.test_restartkeep1 import CASES, PRIMARY, WEBULL, case_bot, checkpoint
from tests.unit.test_v2_flip_entry_ownership_store import _factory


def payload(sequence, *, symbols=None):
    return dict(session_anchor_ms=1, captured_at_ms=sequence*60_000, cursor=f'{sequence}-0',
                checkpoint_sequence=sequence, writer_instance_id='controlled-process',
                symbols={'AIXI': dict(value=sequence)} if symbols is None else symbols)


async def reached(event):
    for _ in range(500):
        if event.is_set():
            return
        await asyncio.sleep(.001)
    raise AssertionError('controlled worker did not reach barrier')


@pytest.mark.asyncio
async def test_one_inflight_one_latest_pending_with_no_producer_db_wait():
    started, release = threading.Event(), threading.Event()
    saved, workers = [], []
    loop_thread = threading.get_ident()

    def record(value):
        workers.append(threading.get_ident())
        saved.append(deepcopy(value))
        if len(saved) == 1:
            started.set()
            assert release.wait(2)

    writer = RestartCheckpointWriter(Mock(record=record))
    try:
        assert writer.submit(payload(1))
        await reached(started)
        task = writer.task
        for sequence in range(2, 102):
            assert writer.submit(payload(sequence))
            assert writer.task is task
        assert len(saved) == 1 and not release.is_set()
        assert writer.pending['checkpoint_sequence'] == 101
        assert workers == [workers[0]] and workers[0] != loop_thread
    finally:
        release.set()
        assert await writer.drain()
    assert [row['checkpoint_sequence'] for row in saved] == [1, 101]
    assert writer.pending is None
    assert not writer.submit(payload(102))


def test_actual_bar_never_awaits_checkpoint_and_writer_owns_the_only_db_await():
    source = inspect.getsource(SchwabV2BotService._handle_bar)
    tree = ast.parse(dedent(source))
    checkpoint_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == '_persist_restart_checkpoint']
    assert len(checkpoint_calls) == 1
    assert not inspect.iscoroutinefunction(SchwabV2BotService._persist_restart_checkpoint)
    for node in ast.walk(tree):
        if isinstance(node, ast.Await):
            assert not any(call in checkpoint_calls for call in ast.walk(node))
    producer = inspect.getsource(SchwabV2BotService._persist_restart_checkpoint)
    assert 'to_thread' not in producer and 'json.dumps' not in producer and 'store.record' not in producer


@pytest.mark.timing
@pytest.mark.asyncio
async def test_actual_bar_callback_finishes_while_checkpoint_sql_is_held():
    started, release = threading.Event(), threading.Event()
    bot = SchwabV2BotService(Settings())
    bot._entry_watch_symbols = {'AIXI'}
    bot._watch_start_ms['AIXI'] = bot.strategy._now_ms()
    bot._restart_scanner_ready = True
    def record(value):
        started.set()
        assert release.wait(2)
    bot._restart_rest_store = Mock(record=record)
    # Existing unrelated feed/delivery boundaries are controlled; real bar callback
    # and real checkpoint producer/writer remain under test.
    bot._ensure_atr_massive_seed_before_bar = AsyncMock()
    bot._strategy_on_bar = Mock(return_value=None)
    bot._drain_atr_sell_observations = AsyncMock()
    bot._maybe_emit = AsyncMock()
    bot._drain_direct_strategy_intents = AsyncMock()
    bot._emit_webull_fanout_legs = AsyncMock()
    bot._persist_bar = Mock()
    bar = ChartBar('AIXI', 2, 2.1, 1.9, 2, 100, 0)
    try:
        await bot._handle_bar('AIXI', bar, observation_phase='live')
        await reached(started)
        await asyncio.wait_for(bot._handle_bar('AIXI', bar, observation_phase='live'), timeout=.5)
        assert not release.is_set()
        assert bot._restart_checkpoint_writer.pending is not None
    finally:
        release.set()
        assert await bot._restart_checkpoint_writer.drain()


@pytest.mark.asyncio
async def test_failure_keeps_only_newest_snapshot_and_graceful_close_reports_failure(caplog):
    started, release = threading.Event(), threading.Event()
    calls = []

    def record(value):
        calls.append(value['checkpoint_sequence'])
        if len(calls) == 1:
            started.set()
            assert release.wait(2)
        raise ValueError('controlled SQL failure')

    writer = RestartCheckpointWriter(Mock(record=record))
    try:
        writer.submit(payload(1))
        await reached(started)
        writer.submit(payload(2))
        writer.submit(payload(3))
    finally:
        release.set()
        assert not await writer.drain()
    assert calls == [1, 3] and writer.pending['checkpoint_sequence'] == 3
    assert 'checkpoint_failed=1' in caplog.text and 'checkpoint_drain_unproven=1' in caplog.text
    assert writer.failures == 2 and writer.retry_at > 0


@pytest.mark.asyncio
async def test_drain_attempts_newest_snapshot_after_older_write_failed():
    started, release = threading.Event(), threading.Event()
    saved = []
    def record(value):
        if value['checkpoint_sequence'] == 1:
            started.set()
            assert release.wait(2)
            raise ValueError('old checkpoint failed')
        saved.append(value['checkpoint_sequence'])
    writer = RestartCheckpointWriter(Mock(record=record))
    try:
        writer.submit(payload(1))
        await reached(started)
        writer.submit(payload(2))
    finally:
        release.set()
        assert await writer.drain()
    assert saved == [2] and writer.pending is None and writer.failures == 0


def test_pending_cancel_checkpoint_retains_original_executable_leg_identity():
    from project_mai_tai.v2_restart_rest_store import checkpoint_state
    bot, state, saved, _ = case_bot(CASES[0])
    assert not bot.strategy.restore_restart_rest(state, saved, {})
    assert state.restart_rest_cancel_fields == saved['rest']
    pending = checkpoint_state(state)
    assert pending['cancel_pending'] and pending['rest'] == saved['rest']
    assert pending['rest']['resting_schwab_generation'] == saved['rest']['resting_schwab_generation']


@pytest.mark.asyncio
async def test_bounded_drain_does_not_claim_to_cancel_running_sql(caplog):
    started, release, completed = threading.Event(), threading.Event(), threading.Event()
    def record(value):
        started.set()
        assert release.wait(2)
        completed.set()
    writer = RestartCheckpointWriter(Mock(record=record))
    try:
        writer.submit(payload(1))
        await reached(started)
        assert not await writer.drain(timeout=.001)
        assert writer.task.done() and writer.task.cancelled()
        assert not release.is_set()  # The cancelled waiter did NOT stop the SQL worker.
        assert 'checkpoint_drain_unproven=1' in caplog.text
    finally:
        release.set()
        await asyncio.gather(writer.task, return_exceptions=True)
        assert await asyncio.to_thread(completed.wait, 1)
        assert not await writer.drain()


@pytest.mark.asyncio
async def test_dedup_and_minute_cursor_refresh_run_in_worker_only():
    store = Mock()
    writer = RestartCheckpointWriter(store)
    first = payload(1, symbols={})
    writer.submit(first)
    await writer.task
    second = {**first, 'cursor': '2-0', 'captured_at_ms': first['captured_at_ms']+10}
    writer.submit(second)
    await writer.task
    assert store.record.call_count == 1
    writer.submit({**second, 'captured_at_ms': first['captured_at_ms']+60_000})
    assert await writer.drain()
    assert store.record.call_count == 2 and store.record.call_args.args[0]['cursor'] == '2-0'


@pytest.mark.asyncio
async def test_producer_copies_scalar_provenance_and_removal_tombstone():
    bot = SchwabV2BotService(Settings())
    state = bot.strategy.watchlist_state('BKYI')
    bot._entry_watch_symbols = {'BKYI'}
    bot._watch_start_ms['BKYI'] = bot.strategy._now_ms()
    bot._restart_scanner_ready = True
    bot._restart_rest_store = Mock()
    bot._strategy_state_last_id = '1-0'
    bot._persist_restart_checkpoint()
    first = bot._restart_checkpoint_writer.pending
    state.restart_last_live_sell_ms += 60_000
    assert first['symbols']['BKYI']['state']['sell_ms'] == 0
    bot._entry_watch_symbols = set()
    bot._strategy_state_last_id = '2-0'
    bot._persist_restart_checkpoint()
    assert await bot._restart_checkpoint_writer.drain()
    written = bot._restart_rest_store.record.call_args.args[0]
    assert written['symbols'] == {} and written['cursor'] == '2-0'
    assert written['writer_instance_id'] == first['writer_instance_id']
    assert written['checkpoint_sequence'] > first['checkpoint_sequence']


def test_seven_day_cleanup_is_scoped_and_retains_current_removal_tombstone():
    factory = _factory()
    value, now = checkpoint()
    with factory() as session:
        session.add_all([
            DashboardSnapshot(snapshot_type=SNAPSHOT_TYPE, created_at=now-timedelta(days=RETENTION_DAYS+1), payload={}),
            DashboardSnapshot(snapshot_type=SNAPSHOT_TYPE, created_at=now-timedelta(days=RETENTION_DAYS-1), payload={}),
            DashboardSnapshot(snapshot_type='foreign_checkpoint', created_at=now-timedelta(days=90), payload={}),
        ])
        session.commit()
    store = RestartRestStore(factory)
    store.record({**value, 'symbols': {}}, now=now)
    with factory() as session:
        rows = session.scalars(select(DashboardSnapshot)).all()
    assert len(rows) == 3
    assert any(row.snapshot_type == 'foreign_checkpoint' for row in rows)
    assert store.restore(now=now+timedelta(seconds=1))['symbols'] == {}
    assert store.restore(now=now+timedelta(days=1)) is None


@pytest.mark.parametrize('other_status', ['aborted', 'superseded', 'filled', 'partially_filled', 'working', None])
def test_aixi_terminal_request_sibling_does_not_orphan_exact_owned_order(other_status, caplog):
    # AIXI-shaped ownership control; not reconstruction of the historical broker book.
    bot, state, saved, clock = case_bot(CASES[5])
    saved['rest']['resting_is_broker_order'] = True
    saved['rest']['resting_schwab_generation'] = 'AIXI-controlled-owned-native'
    saved['rest']['resting_schwab_quantity'] = 50
    factory = _factory()
    BrokerOrderEvent.__table__.create(factory.kw['bind'])
    bot.session_factory = factory
    now = datetime.fromtimestamp(clock[0]/1000, UTC)
    with factory() as session:
        strategy = Strategy(code='schwab_1m_v2', name='controlled')
        account = BrokerAccount(name=PRIMARY, provider='schwab', environment='isolated')
        session.add_all([strategy, account])
        session.flush()
        metadata = {'fanout_segment_id': str(saved['opportunity_id']),
                    'rpg_resting_generation': saved['rest']['resting_schwab_generation']}
        actual = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
                             symbol='AIXI', side='buy', intent_type='open', quantity=Decimal('50'),
                             reason='controlled owned generation', status='accepted', created_at=now,
                             payload={'metadata': metadata})
        sibling = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
                              symbol='AIXI', side='buy', intent_type='open', quantity=Decimal('50'),
                              reason='controlled prior attempt', status=other_status, created_at=now,
                              payload={'metadata': {**metadata, 'rpg_resting_generation': 'superseded-old'}})
        session.add_all([actual, sibling])
        session.flush()
        session.add(BrokerOrder(intent_id=actual.id, strategy_id=strategy.id,
                                broker_account_id=account.id, client_order_id='AIXI-controlled',
                                symbol='AIXI', side='buy', order_type='STOP_LIMIT', time_in_force='day',
                                quantity=Decimal('50'), status='accepted', payload={}))
        session.commit()
    value = dict(symbols={'AIXI': {'state': saved}})
    # Exact case anchor derives from the captured AIXI day, not today's actual DB.
    from project_mai_tai.services.schwab_1m_v2_bot import session_start_ts_ms
    value['session_anchor_ms'] = session_start_ts_ms(clock[0])
    legs = bot._read_restart_rest_legs(value)['AIXI']
    if other_status in {'filled', 'partially_filled'}:
        assert legs[PRIMARY] == 'filled'
        bot._restart_rest_legs = {'AIXI': legs}
        before = state.retry_one_closes_in_segment
        assert not bot._restore_restart_rest_after_seed('AIXI')
        assert not state.resting_active and state.cw_resting_taken
        assert PRIMARY in state.flip_owner_fill_accounts
        for _ in range(3):
            bot.strategy._cw_v2_resting_track(state, None)
        assert bot.strategy.drain_pending_intents() == []
        assert state.retry_one_closes_in_segment == before and bot.strategy._retry_one_max_retries == 0
    elif other_status in {'aborted', 'superseded'}:
        assert legs[PRIMARY] == 'owned'
    else:
        assert PRIMARY not in legs
        assert 'symbol=AIXI' in caplog.text and 'other_live_intent' in caplog.text


@pytest.mark.parametrize("proof_state", ["fresh", "incomplete", "mid_episode", "stale", "real", "uncommitted"])
def test_restart_historical_false_fill_defers_only_to_existing_refund_path(proof_state, caplog):
    from dataclasses import replace
    from project_mai_tai.falseflip1_runtime import FalseBudget
    from project_mai_tai.v2_restart_rest_store import checkpoint_state
    from tests.unit.test_falseflip2_release import (
        OPPORTUNITY, SEGMENT, apply_book, release, tcrt_runtime,
    )
    from tests.unit.test_falseflip1_runtime import ack

    strategy, state, clock, proofs, _ = tcrt_runtime()
    budget = FalseBudget("TCRT", SEGMENT).closed(OPPORTUNITY, 1).fully_filled_closed(OPPORTUNITY)
    if proof_state == "mid_episode":
        budget = FalseBudget("TCRT", SEGMENT).closed(OPPORTUNITY, 1)
    strategy.configure_falseflip(
        {} if proof_state == "uncommitted" else {("TCRT", SEGMENT): budget}, readable=True)
    # A stale pre-fill owner/checkpoint, before the real position poll revalidates
    # the already-durable refund. Neither this checkpoint nor a filled status is permission.
    state.flip_owner_phase = "awaiting_fill"
    state.flip_owner_fill_accounts.clear()
    state.flip_owner_position_ids.clear()
    state.flip_owner_position_entry_ms.clear()
    state.retry_one_closes_in_segment = 1
    state.cw_resting_taken = False
    saved = checkpoint_state(state)
    before = state.retry_one_closes_in_segment
    assert not strategy.restore_restart_rest(state, saved, {PRIMARY: "filled", WEBULL: "filled"})
    assert state.restart_rest_evidence_pending and not state.resting_active
    assert state.cw_resting_taken is (proof_state == "uncommitted")
    for _ in range(3):
        strategy._cw_v2_resting_track(state, None)
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
    assert state.retry_one_closes_in_segment == before
    assert strategy._falseflip_budget(state) == (
        FalseBudget("TCRT", SEGMENT) if proof_state == "uncommitted" else budget)
    if proof_state == "uncommitted":
        assert "same_opportunity_filled_consumed" in caplog.text
        return

    assert "falseflip_episode_requires_existing_reconciliation" in caplog.text
    if proof_state == "real":
        proofs = (replace(proofs[0], kind="REAL"), *proofs[1:])
    elif proof_state == "stale":
        clock[0] += 16000
    apply_book(strategy, state, clock, proofs, complete=proof_state not in {"incomplete", "mid_episode"})
    if proof_state == "stale":
        clock[0] += 16000  # Age the actual immutable proof; do not refresh it at validation.
    if proof_state != "fresh":
        assert not any(strategy.falseflip2_release_valid(record)
                       for record in strategy.pending_falseflip2_releases())
        assert state.restart_rest_evidence_pending and not state.resting_active
        assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
        return

    release(strategy, state)
    assert not state.restart_rest_evidence_pending and not state.cw_resting_taken
    strategy._falseflip_restoring_track(state)
    plan, = strategy.pending_falseflip2_rearms()
    assert strategy.acknowledge_falseflip2_rearm(plan)
    ack(strategy)
    assert state.resting_active and state.retry_one_closes_in_segment == before
    assert strategy._falseflip_effective_closes(state, before) == 0
    assert not strategy.pending_falseflip2_rearms()
    from project_mai_tai.strategy_core.schwab_1m_v2 import Quote
    clock[0] += 17000
    quote = Quote("TCRT", 1.92, 1.93, 1.93, clock[0], clock[0])
    draft = strategy.on_quote("TCRT", quote)
    assert draft is not None and draft.intent_type == "open"
    mirror, = strategy.drain_webull_fanout_intents()
    assert draft.metadata["cw_entry_slot"] == mirror.metadata["cw_entry_slot"] == "first"
    assert strategy.on_quote("TCRT", quote) is None
    assert not strategy.drain_webull_fanout_intents()
    assert state.retry_one_closes_in_segment == before


@pytest.mark.parametrize('reason', ['native', 'cancel', 'owner', 'position', 'software', 'fields'])
def test_each_restore_refusal_or_pending_logs_symbol_and_reason(reason, caplog):
    bot, state, saved, _ = case_bot(CASES[3])
    legs = {PRIMARY: 'owned', WEBULL: 'owned'}
    if reason == 'native':
        legs = {}
    elif reason == 'cancel':
        saved['cancel_pending'] = True
        state.flip_owner_phase = 'unknown'
    elif reason == 'owner':
        state.flip_owner_opportunity_id += 1
    elif reason == 'position':
        state.position_qty_held = 1
    elif reason == 'software':
        saved['rest']['resting_is_broker_order'] = False
        legs = {'software_dispatched': True}
    elif reason == 'fields':
        saved['rest'].pop('resting_level')
    assert not bot.strategy.restore_restart_rest(state, saved, legs)
    assert 'symbol=BKYI' in caplog.text and 'reason=' in caplog.text


def test_missing_continuity_and_sell_mismatch_are_not_silent(caplog):
    bot, state, saved, _ = case_bot(CASES[3])
    bot._restart_continuing = {}
    assert not bot._restore_restart_rest_after_seed('BKYI')
    assert 'continuous_watch_not_proven' in caplog.text
    bot._restart_continuing = {'BKYI': {'watch_ms': saved['sell_ms']-60_000, 'state': saved}}
    assert not bot._restore_restart_rest_after_seed('BKYI', sell_ms=saved['sell_ms']+60_000)
    assert 'same_live_sell_not_proven' in caplog.text
