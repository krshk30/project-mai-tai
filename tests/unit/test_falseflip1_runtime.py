from __future__ import annotations

import asyncio
import json
import inspect
import logging
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import Base, BrokerAccount, BrokerOrder, Fill, OmsManagedPosition, Strategy
from project_mai_tai.falseflip1 import Classification, classify_entry
from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.falseflip1_runtime import FalseBudget, FalseFlipStore, classify_managed_entries, record_bar
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.services.control_plane import (
    ControlPlaneRepository, _build_closed_trade_rows_v2, _collect_completed_position_rows,
)
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, Quote, SchwabV2IntentEmitter, SchwabV2Strategy
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipRecord, FlipPositionBook, FlipPositionClose
from project_mai_tai.v2_removed_wait import RemovedWaitProof
from tests.unit.test_falseflip1_step0 import CASES, FALSE_SLOTS, REAL_CASES, _evidence
from tests.unit.test_retryleft1 import strategy_for_record
from tests.unit.test_v2_retry_one import _bar, _sell_segment


FLYE = [row for row in CASES if row["symbol"] == "FLYE" and row["classification"] == "FALSE_FLIP"]


def controlled_proof(row):
    """Retained scalar/fill facts; legacy missing bindings are explicit controlled mappings."""
    identity, bar = _evidence(row)
    identity = replace(identity, managed_row_id=identity.managed_row_id or str(uuid4()),
                       entry_order_id=row["order_id"], entry_client_order_id=row["fill_client_order_id"],
                       managed_account=row["account"], managed_symbol=row["symbol"])
    return classify_entry(identity, bar)


def runtime(rows=FLYE, *, pm=False, budgets=None, installed_policy=False):
    proofs = tuple(controlled_proof(row) for row in rows)
    first = proofs[0]
    # FLYE's recorded causal SELL; other fixtures test the new runtime over recorded
    # entry facts, not a claim that the synthetic owner segment was historically observed.
    segment = 1791466440000 if first.identity.symbol == "FLYE" else first.bar.bar_ms - 600000
    record = FlipEntryOwnershipRecord(first.identity.symbol, first.identity.opportunity_id,
        "consumed", 0, 0, tuple(p.identity.account for p in proofs),
        {p.identity.account: p.identity.managed_row_id for p in proofs},
        {p.identity.account: p.identity.fill_ms for p in proofs}, segment, 0)
    at = datetime.fromtimestamp(max(p.bar.observed_at_ms for p in proofs) / 1000, UTC)
    strategy, state, _, clock, writes = strategy_for_record(record, at, pm=pm, quantities=(197, 98))
    if installed_policy:
        strategy = SchwabV2Strategy(strategy.settings.model_copy(update={
            "strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct": .5,
            "strategy_schwab_1m_v2_cw_v2_resting_entry_band_pct": .5,
            "strategy_schwab_1m_v2_resting_buy_round_up_enabled": True,
            "strategy_schwab_1m_v2_pm_print_ask_confirm_enabled": True,
            "strategy_schwab_1m_v2_pm_flip_wait_enabled": True,
            "strategy_schwab_1m_v2_pm_rest_reprice_enabled": True,
            "strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled": True,
            "strategy_schwab_1m_v2_gap_hold_enabled": True,
            "strategy_schwab_1m_v2_atr_reprice_handoff_enabled": False,
            "oms_v2_webull_mirror_fresh_price_enabled": True,
            "oms_v2_eh_fresh_price_enabled": True,
        }))
        strategy._now_ms = lambda: clock[0]
        strategy.configure_fanout_identity_persistence(lambda *_: None)
        strategy.configure_flip_entry_ownership(lambda *_: None, restored={record.symbol: record},
            active_segments={record.symbol: record.opportunity_id}, retry_budget_persist=lambda *_: None,
            restored_retry_budgets={record.symbol: (record.retry_segment_id, 1)})
        strategy.configure_removed_wait(lambda r, active: writes.append((r, active)), restored={}, readable=True)
        state = strategy.watchlist_state(record.symbol)
        state.fanout_segment_id = record.opportunity_id
        state.atr_short_flip_bar_ts = record.retry_segment_id
        state.resting_active = state.webull_resting_active = True
        state.resting_is_broker_order = not pm
        state.resting_schwab_quantity, state.resting_webull_quantity = 197, 98
        state.cw_armed = pm
    strategy.settings.strategy_schwab_1m_v2_false_flip_enabled = True
    strategy.configure_falseflip(budgets or {}, readable=True)
    strategy._resting_in_window = lambda now=None: True
    strategy._resting_session_is_eh = lambda now=None: pm
    strategy._eh_resting_enabled = pm
    strategy._liquidity_floor_ok = lambda _state: True
    strategy._resting_stop_ask_allows = lambda *_args, **_kwargs: True
    state.atr_state = "short"
    state.atr_trail = float(first.bar.trail)
    state.bars.append(_bar(clock[0]))
    return strategy, state, clock, proofs, writes


def book(strategy, state, clock, proofs, *, closed=True):
    strategy.apply_flip_position_book(FlipPositionBook(clock[0], True, {},
        closes_by_symbol={state.symbol: tuple(FlipPositionClose(p.identity.account,
            p.identity.managed_row_id, "CW_HARD_STOP") for p in proofs)} if closed else {},
        entry_classifications={state.symbol: tuple(p.as_payload() for p in proofs)},
        filled_opportunities={state.symbol: tuple((p.identity.account, p.identity.opportunity_id) for p in proofs)},
        closed_entry_rows=frozenset((p.identity.account, p.identity.managed_row_id) for p in proofs) if closed else frozenset()))


def ack(strategy):
    for before, after in strategy.pending_falseflip_budgets():
        strategy.acknowledge_falseflip_budget(before, after)


def close_and_cancel(strategy, state, clock, proofs):
    book(strategy, state, clock, proofs)
    assert len(strategy.pending_falseflip_budgets()) == 1
    assert strategy.drain_pending_intents() == []
    ack(strategy)
    book(strategy, state, clock, proofs)
    request, = strategy.pending_falseflip_cancel_publications()
    strategy._removed_wait_persist(request, True)
    strategy.acknowledge_falseflip_cancel_publication(request)
    request = strategy._removed_wait_requests[state.symbol]
    assert request.purpose == "false_flip_restore"
    primary, = strategy.drain_pending_intents()
    mirror, = strategy.drain_webull_direct_intents()
    assert primary.intent_type == mirror.intent_type == "cancel"
    strategy.apply_removed_wait_proofs((RemovedWaitProof(request, clock[0], True,
        "false_flip_leftovers_cancelled_owner_kept"),))
    book(strategy, state, clock, proofs)
    before, after = strategy.pending_falseflip_budgets()[0]
    assert after.cancelled == (*before.cancelled, proofs[0].identity.opportunity_id)
    ack(strategy)
    book(strategy, state, clock, proofs)
    assert state.flip_owner_phase == "idle"


@pytest.mark.parametrize("slot", FALSE_SLOTS)
@pytest.mark.parametrize("pm", [False, True], ids=["broker", "software"])
def test_recorded_false_slot_controlled_runtime_restores_only_after_exact_close_and_cancel(slot, pm):
    rows = [row for row in CASES if row["slot"] == slot]
    strategy, state, clock, proofs, _ = runtime(rows, pm=pm)
    close_and_cancel(strategy, state, clock, proofs)
    assert state.retry_one_closes_in_segment == 1  # no destructive decrement of the old ledger
    assert strategy._falseflip_effective_closes(state, 1) == 0
    clock[0] += 60000
    state.bars.append(_bar(clock[0]))
    book(strategy, state, clock, proofs)
    strategy._falseflip_restoring_track(state)
    assert state.resting_active
    primary = strategy.drain_pending_intents()
    mirror = strategy.drain_webull_direct_intents()
    if pm:
        assert not primary and not mirror and not state.resting_is_broker_order
    else:
        assert len(primary) == len(mirror) == 1
        assert primary[0].intent_type == mirror[0].intent_type == "open"
        assert primary[0].metadata["fanout_segment_id"] == mirror[0].metadata["fanout_segment_id"]
    assert len(strategy._falseflip_budget(state).episodes) == 1


@pytest.mark.parametrize("problem", ["unknown", "real", "held", "missing", "unreadable"])
def test_mixed_or_unproven_sibling_never_refunds_or_restores(problem):
    strategy, state, clock, proofs, _ = runtime()
    if problem in {"unknown", "real"}:
        p = proofs[1]
        bar = replace(p.bar, state="long" if problem == "real" else "unknown")
        proofs = (proofs[0], classify_entry(p.identity, bar))
    if problem == "missing":
        proofs = proofs[:1]
        state.flip_owner_fill_accounts.add("live:orb")
    if problem == "unreadable":
        strategy.configure_falseflip({}, readable=False)
    book(strategy, state, clock, proofs, closed=problem != "held")
    assert not strategy.pending_falseflip_budgets()
    strategy._falseflip_restoring_track(state)
    assert not [i for i in strategy.drain_pending_intents() if i.intent_type == "open"]


@pytest.mark.parametrize("row", REAL_CASES, ids=lambda row: row["fill_id"])
def test_recorded_real_close_still_consumes_zero_retry_segment(row):
    strategy, state, clock, proofs, _ = runtime([row])
    assert proofs[0].kind == "REAL_FLIP"
    assert strategy._falseflip_effective_closes(state, 1) == 1
    book(strategy, state, clock, proofs)
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    clock[0] += 60000
    state.bars.append(_bar(clock[0]))
    strategy._queue_resting_place(state, state.atr_trail, slot="first")
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
    assert state.flip_owner_phase == "consumed"
    assert not strategy.pending_falseflip_budgets()


def database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


@pytest.mark.parametrize("kind", ["REAL_FLIP", "UNKNOWN"])
def test_synthetic_sibling_filling_a_later_bar_is_not_refunded_with_the_first_false_leg(kind):
    strategy, state, clock, proofs, _ = runtime()
    later = proofs[1]
    identity = replace(later.identity, fill_ms=later.identity.fill_ms+60000)
    bar = replace(later.bar, bar_ms=later.bar.bar_ms+60000, observed_at_ms=later.bar.observed_at_ms+60000,
                  close=Decimal("3.5"), state="long" if kind == "REAL_FLIP" else "unknown")
    later = classify_entry(identity, bar)
    assert later.kind == kind
    clock[0] = later.bar.observed_at_ms
    book(strategy, state, clock, (proofs[0], later))
    assert not strategy.pending_falseflip_budgets()
    assert strategy._falseflip_effective_closes(state, 1) == 1
    strategy._falseflip_restoring_track(state)
    assert not [draft for draft in strategy.drain_pending_intents() if draft.intent_type == "open"]


def populate(factory, row=FLYE[0], *, quantity=197, price=Decimal("3.05")):
    proof = controlled_proof(row)
    at = datetime.fromtimestamp(proof.identity.fill_ms / 1000, UTC)
    with factory() as session:
        account = BrokerAccount(name=row["account"], provider="schwab", environment="live")
        strategy = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        if strategy is None:
            strategy = Strategy(code="schwab_1m_v2", name="v2")
            session.add(strategy)
        session.add(account)
        session.flush()
        order = BrokerOrder(id=UUID(row["order_id"]), broker_account_id=account.id, strategy_id=strategy.id,
            client_order_id=row["fill_client_order_id"], symbol=row["symbol"], side="buy",
            order_type="stop_limit", time_in_force="day", quantity=quantity, status="filled", submitted_at=at,
            payload={"fanout_segment_id": proof.identity.opportunity_id, "fanout_slot_id": proof.identity.slot_id},
            updated_at=at)
        managed = OmsManagedPosition(id=UUID(proof.identity.managed_row_id), strategy_code="schwab_1m_v2",
            broker_account_name=account.name, symbol=order.symbol, entry_order_id=order.id,
            entry_client_order_id=order.client_order_id, entry_price=price, original_quantity=quantity,
            current_quantity=0, status="closed", entry_time=at, updated_at=at + timedelta(seconds=10))
        fill = Fill(order_id=order.id, strategy_id=strategy.id, broker_account_id=account.id,
                    symbol=order.symbol, side="buy", quantity=quantity, price=price, filled_at=at)
        session.add_all((order, managed, fill))
        session.commit()
    return proof, at, managed.id


def test_flye_already_closed_row_and_owning_order_receive_same_proof_atomically_without_new_close():
    engine, factory = database()
    proof, at, row_id = populate(factory)
    now = datetime.fromtimestamp(proof.bar.observed_at_ms / 1000, UTC)
    assert classify_managed_entries(factory, now=now) == 0
    record_bar(factory, {"symbol": proof.bar.symbol, "bar_ms": proof.bar.bar_ms,
        "observed_at_ms": proof.bar.observed_at_ms, "close": str(proof.bar.close),
        "trail": str(proof.bar.trail), "state": "short", "phase": "live"}, now=now)
    assert classify_managed_entries(factory, now=now) == 1
    assert classify_managed_entries(factory, now=now) == 0
    with factory() as session:
        managed = session.get(OmsManagedPosition, row_id)
        order = session.get(BrokerOrder, UUID(proof.identity.entry_order_id))
        assert managed.entry_classification == order.payload["entry_classification"] == proof.as_payload()
        assert managed.status == "closed" and managed.current_quantity == 0
        assert managed.updated_at.replace(tzinfo=UTC) == at + timedelta(seconds=10)
        assert order.updated_at.replace(tzinfo=UTC) == at
        assert len(session.scalars(select(Fill)).all()) == 1
    engine.dispose()


def test_late_conflicting_bar_revokes_both_proofs_and_blocks_even_a_refunded_episode():
    engine, factory = database()
    proof, _, row_id = populate(factory)
    now = datetime.fromtimestamp(proof.bar.observed_at_ms/1000, UTC)
    payload = {"symbol": "FLYE", "bar_ms": proof.bar.bar_ms, "observed_at_ms": proof.bar.observed_at_ms,
               "close": str(proof.bar.close), "trail": str(proof.bar.trail), "state": "short", "phase": "live"}
    record_bar(factory, payload, now=now)
    assert classify_managed_entries(factory, now=now) == 1
    record_bar(factory, {**payload, "state": "long"}, now=now)
    with factory() as session:
        row = session.get(OmsManagedPosition, row_id)
        order = session.get(BrokerOrder, UUID(proof.identity.entry_order_id))
        assert row.entry_classification == order.payload["entry_classification"]
        revoked = Classification.from_payload(row.entry_classification)
        assert revoked.kind == "UNKNOWN"
    strategy, state, clock, _, _ = runtime([FLYE[0]])
    budget = FalseBudget("FLYE", state.retry_one_segment_id).closed(proof.identity.opportunity_id, 1)
    strategy.configure_falseflip({(budget.symbol, budget.segment): budget}, readable=True)
    state.flip_owner_phase = "idle"
    book(strategy, state, clock, (revoked,))
    strategy._falseflip_rearm.add("FLYE")
    strategy._falseflip_restoring_track(state)
    assert strategy._falseflip_entry_blocked(state)
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
    engine.dispose()


def test_budget_cas_once_skip_and_confirmed_rearm_restore_across_restart():
    engine, factory = database()
    store = FalseFlipStore(factory)
    at = datetime(2026, 10, 8, 14, 1, tzinfo=UTC)
    initial = FalseBudget("FLYE", 1791466440000)
    one = initial.closed(1791466682474, 1)
    two = one.closed(1791468000000, 2)
    cancelled = two.cancellation_confirmed(1791468000000)
    skipped = cancelled.skip()
    for before, after in ((initial, one), (one, two), (two, cancelled), (cancelled, skipped)):
        store.commit(before, after, now=at)
        store.commit(before, after, now=at)
    restored = store.restore(now=at)[("FLYE", initial.segment)]
    assert restored == skipped and not restored.skip_due
    assert restored.awaiting_rearm == 1791468000000
    with pytest.raises(ValueError, match="compare-and-set"):
        store.commit(initial, initial.closed(99), now=at)
    store.commit(skipped, skipped.rest_drafted(restored.awaiting_rearm), now=at)
    assert store.restore(now=at)[("FLYE", initial.segment)].awaiting_rearm == 0
    assert not store.restore(now=at + timedelta(days=1))
    engine.dispose()


@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("installed_policy", [False, True], ids=["scalar-control", "installed-offset-roundup"])
def test_second_false_exit_has_no_fill_capable_rest_on_skipped_cross_then_same_segment_rearms(pm, installed_policy, caplog):
    rows = ([row for row in CASES if row["symbol"] == "CLRO" and row["classification"] == "FALSE_FLIP"]
            if pm and installed_policy else FLYE)
    strategy, state, clock, proofs, _ = runtime(rows, pm=pm, installed_policy=installed_policy)
    first = FalseBudget(state.symbol, state.retry_one_segment_id).closed(state.retry_one_segment_id + 60000, 1)
    strategy.configure_falseflip({(first.symbol, first.segment): first}, readable=True)
    state.retry_one_closes_in_segment = 2
    state.flip_owner_retry_closes_at_place = 1
    close_and_cancel(strategy, state, clock, proofs)
    segment = state.retry_one_segment_id
    clock[0] += 60000
    state.bars.append(_bar(clock[0]))
    book(strategy, state, clock, proofs)
    strategy._falseflip_restoring_track(state)
    assert not state.resting_active
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
    level = strategy._resting_trigger_for_line(state.atr_trail)
    if installed_policy:
        assert level == (5.53 if pm else 3.03)
        assert strategy._resting_offset_pct_value() == strategy._resting_band_pct_value() == .5
    quote = Quote(state.symbol, level - 0.01, level, level, clock[0], clock[0])
    caplog.set_level("INFO")
    assert strategy.on_quote(state.symbol, quote) is None
    strategy.on_stream_trade(state.symbol, price=level, event_ts_ms=clock[0],
                             ask_price=level, ask_age_ms=0)
    assert len(strategy.pending_falseflip_budgets()) == 1
    ack(strategy)
    assert "false_flips=2 skipped=1" in caplog.text
    assert strategy.on_quote(state.symbol, quote) is None
    strategy._falseflip_restoring_track(state)
    assert not state.resting_active and not strategy.drain_pending_intents()
    # Restart cannot count the duplicate above-level delivery as the next cross.
    budgets = dict(strategy._falseflip_budgets)
    restarted, fresh, new_clock, new_proofs, _ = runtime(rows, pm=pm, budgets=budgets, installed_policy=installed_policy)
    fresh.flip_owner_phase = "idle"
    fresh.flip_owner_opportunity_id = fresh.fanout_segment_id = 0
    fresh.flip_owner_fill_accounts.clear()
    fresh.flip_owner_position_ids.clear()
    fresh.resting_active = fresh.webull_resting_active = False
    book(restarted, fresh, new_clock, new_proofs)
    restarted.on_quote(fresh.symbol, replace(quote, quote_time_ms=new_clock[0], trade_time_ms=new_clock[0]))
    restarted._falseflip_restoring_track(fresh)
    assert not fresh.resting_active
    restarted.on_quote(fresh.symbol, Quote(fresh.symbol, level - .02, level - .01,
                                         level - .01, new_clock[0], new_clock[0]))
    restarted._falseflip_restoring_track(fresh)
    assert fresh.resting_active and fresh.retry_one_segment_id == segment
    if not pm:
        primary, = restarted.drain_pending_intents()
        mirror, = restarted.drain_webull_direct_intents()
        if installed_policy:
            for draft in (primary, mirror):
                assert Decimal(draft.metadata["stop_price"]) == Decimal("3.03")
                assert Decimal(draft.metadata["limit_price"]) == Decimal("3.05")
    else:
        assert not fresh.resting_is_broker_order
    ack(restarted)
    assert not restarted._falseflip_budget(fresh).awaiting_rearm
    if pm and installed_policy:
        draft = restarted.on_quote(fresh.symbol, Quote(fresh.symbol, level-.01, level, level,
                                                       new_clock[0], new_clock[0]))
        mirror, = restarted.drain_webull_fanout_intents()
        assert draft is not None and draft.intent_type == mirror.intent_type == "open"
        assert draft.quantity == 108 and mirror.quantity == 54
        for leg in (draft, mirror):
            assert leg.metadata["pm_confirming_ask"] == "5.53"
            assert Decimal(leg.metadata["entry_size_price"]) == Decimal("5.53")
        assert draft.metadata["entry_size_price_basis"] == "pm_confirming_ask"
        assert mirror.metadata["entry_size_price_basis"] == "trigger_quote_ask"
        assert draft.metadata["cw_entry_slot"] == mirror.metadata["cw_entry_slot"] == "first"


def test_fresh_sell_starts_clean_false_budget_without_releasing_a_real_old_position():
    strategy, state, clock, proofs, _ = runtime()
    budget = FalseBudget(state.symbol, state.retry_one_segment_id).closed(1, 1).closed(2, 2)
    strategy.configure_falseflip({(budget.symbol, budget.segment): budget}, readable=True)
    clock[0] += 60000
    _sell_segment(strategy, clock, state.symbol)
    assert state.retry_one_segment_id != budget.segment
    assert strategy._falseflip_budget(state).episodes == ()
    assert strategy._falseflip_effective_closes(state, 0) == 0


@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_cross_before_latest_cancel_receipt_never_spends_skip_pending_unknown_or_restart(pm, restart):
    strategy, state, clock, proofs, _ = runtime(pm=pm)
    prior = state.retry_one_segment_id + 60000
    budget = FalseBudget(state.symbol, state.retry_one_segment_id).closed(prior, 1).cancellation_confirmed(prior)
    strategy.configure_falseflip({(budget.symbol, budget.segment): budget}, readable=True)
    state.retry_one_closes_in_segment = 2
    state.flip_owner_retry_closes_at_place = 1
    book(strategy, state, clock, proofs)
    ack(strategy)
    if restart:
        strategy, state, clock, proofs, _ = runtime(pm=pm, budgets=dict(strategy._falseflip_budgets))
    level = strategy._resting_trigger_for_line(state.atr_trail)

    def cross():
        assert strategy.on_quote(state.symbol, Quote(state.symbol, level-.01, level, level,
                                                     clock[0], clock[0])) is None
        assert strategy.on_stream_trade(state.symbol, price=level, event_ts_ms=clock[0],
                                       ask_price=level, ask_age_ms=0) is None
        assert not strategy.pending_falseflip_budgets()
        assert not strategy._falseflip_budget(state).skipped
        assert strategy._falseflip_entry_blocked(state)

    cross()
    book(strategy, state, clock, proofs)
    request, = strategy.pending_falseflip_cancel_publications()
    strategy._removed_wait_persist(request, True)
    strategy.acknowledge_falseflip_cancel_publication(request)
    assert all(d.intent_type == "cancel" for d in
               strategy.drain_pending_intents() + strategy.drain_webull_direct_intents())
    strategy.apply_removed_wait_proofs((RemovedWaitProof(request, clock[0], False, "cancel_unknown"),))
    cross()
    strategy.apply_removed_wait_proofs((RemovedWaitProof(request, clock[0], True,
        "false_flip_leftovers_cancelled_owner_kept"),))
    book(strategy, state, clock, proofs)
    ack(strategy)
    book(strategy, state, clock, proofs)
    strategy.on_quote(state.symbol, Quote(state.symbol, level-.01, level, level, clock[0], clock[0]))
    before, after = strategy.pending_falseflip_budgets()[0]
    assert not before.skipped and after.skipped


@pytest.mark.parametrize("pm", [False, True], ids=["broker", "software"])
@pytest.mark.parametrize("installed_policy", [False, True], ids=["scalar-control", "installed-policy"])
def test_synthetic_three_false_episodes_skip_exactly_once_and_keep_same_causal_segment(pm, installed_policy):
    """Lifecycle control, not three fabricated historical FLYE fills."""
    rows = ([row for row in CASES if row["symbol"] == "CLRO" and row["classification"] == "FALSE_FLIP"]
            if pm and installed_policy else FLYE)
    strategy, state, clock, proofs, _ = runtime(rows, pm=pm, installed_policy=installed_policy)
    segment = state.retry_one_segment_id
    for number in range(1, 4):
        if number > 1:
            opportunity = state.fanout_segment_id
            assert opportunity > 0
            clock[0] += 60000
            minute = clock[0] // 60000 * 60000 - 60000
            proofs = tuple(classify_entry(replace(p.identity,
                managed_row_id=str(uuid4()), entry_order_id=str(uuid4()),
                fill_ms=minute + 41000, opportunity_id=opportunity,
                slot_id=fanout_slot_id(strategy_code="schwab_1m_v2", symbol=state.symbol,
                                      segment_id=opportunity, slot="resting")),
                replace(p.bar, bar_ms=minute, observed_at_ms=clock[0])) for p in proofs)
            # Exact counterpart order/fill identity in this controlled new episode.
            proofs = tuple(classify_entry(replace(p.identity, fill_order_id=p.identity.entry_order_id), p.bar)
                           for p in proofs)
            assert all(p.kind == "FALSE_FLIP" for p in proofs)
            state.flip_owner_phase = "consumed"
            state.flip_owner_opportunity_id = opportunity
            state.flip_owner_position_ids = {p.identity.account: p.identity.managed_row_id for p in proofs}
            state.flip_owner_fill_accounts = {p.identity.account for p in proofs}
            state.flip_owner_retry_segment_id = segment
            state.flip_owner_retry_closes_at_place = number - 1
            state.retry_one_closes_in_segment = number
        close_and_cancel(strategy, state, clock, proofs)
        assert len(strategy._falseflip_budget(state).episodes) == number
        assert strategy._falseflip_effective_closes(state, number) == 0
        clock[0] += 60000
        state.bars.append(_bar(clock[0]))
        book(strategy, state, clock, proofs)
        if number == 2:
            strategy._falseflip_restoring_track(state)
            assert not state.resting_active
            level = strategy._resting_trigger_for_line(state.atr_trail)
            strategy.on_quote(state.symbol, Quote(state.symbol, level-.01, level, level, clock[0], clock[0]))
            assert len(strategy.pending_falseflip_budgets()) == 1
            assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
            ack(strategy)
            assert strategy._falseflip_budget(state).skipped
            strategy.on_quote(state.symbol, Quote(state.symbol, level-.02, level-.01, level-.01, clock[0], clock[0]))
        strategy._falseflip_restoring_track(state)
        assert state.resting_active and state.retry_one_segment_id == segment
        if pm:
            assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
        else:
            assert len(strategy.drain_pending_intents()) == len(strategy.drain_webull_direct_intents()) == 1
        ack(strategy)
    assert len(strategy._falseflip_budget(state).episodes) == 3
    assert strategy._falseflip_budget(state).skipped
    assert not strategy._falseflip_budget(state).skip_due


def test_false_restoration_uses_existing_manual_stop_risk_gate_without_blocking_cancel():
    from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
    strategy, state, clock, proofs, _ = runtime()
    close_and_cancel(strategy, state, clock, proofs)
    clock[0] += 60000
    state.bars.append(_bar(clock[0]))
    book(strategy, state, clock, proofs)
    strategy._falseflip_restoring_track(state)
    drafts = strategy.drain_pending_intents() + strategy.drain_webull_direct_intents()
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings = Settings(strategy_schwab_1m_v2_false_flip_enabled=True)
    service._manual_stop_symbols = {"FLYE"}
    for account, draft in zip(("live:schwab_1m_v2", "live:orb"), drafts, strict=True):
        payload = TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=account,
            symbol=draft.symbol, side="buy", quantity=Decimal(draft.quantity),
            intent_type="open", reason=draft.reason, metadata=draft.metadata)
        event_ = TradeIntentEvent(source_service="schwab-1m-v2", payload=payload)
        assert service._evaluate_risk(event_) == (False, "manual_stop:FLYE")
        cancel = event_.model_copy(update={"payload": payload.model_copy(update={"intent_type": "cancel"})})
        assert service._evaluate_risk(cancel) == (True, "ok")


def test_exit_before_bar_and_restart_before_proof_refunds_only_the_exact_episode_once():
    strategy, state, clock, proofs, _ = runtime()
    # Already consumed at boot. No completed-bar proof means no permission to undo it.
    book(strategy, state, clock, ())
    assert state.flip_owner_phase == "consumed" and not strategy.pending_falseflip_budgets()
    book(strategy, state, clock, proofs)
    ack(strategy)
    budget = strategy._falseflip_budget(state)
    restarted, fresh, new_clock, same_proofs, _ = runtime(budgets={(budget.symbol, budget.segment): budget})
    book(restarted, fresh, new_clock, same_proofs)
    assert not restarted.pending_falseflip_budgets()
    assert len(restarted._falseflip_budget(fresh).episodes) == 1
    assert fresh.flip_owner_phase == "consumed"  # cancellation is still unconfirmed
    request, = restarted.pending_falseflip_cancel_publications()
    restarted._removed_wait_persist(request, True)
    restarted.acknowledge_falseflip_cancel_publication(request)
    restarted.apply_removed_wait_proofs((RemovedWaitProof(request, new_clock[0], False, "cancel_pending"),))
    assert fresh.symbol in restarted._removed_wait_requests
    assert not restarted.pending_falseflip_budgets()


def test_flye_repository_to_existing_page_uses_durable_owning_entry_proof(monkeypatch, tmp_path):
    engine, factory = database()
    proof, at, _ = populate(factory, next(row for row in FLYE if row["account"] == "live:schwab_1m_v2"))
    now = datetime.fromtimestamp(proof.bar.observed_at_ms / 1000, UTC)
    record_bar(factory, {"symbol": proof.bar.symbol, "bar_ms": proof.bar.bar_ms,
        "observed_at_ms": proof.bar.observed_at_ms, "close": str(proof.bar.close),
        "trail": str(proof.bar.trail), "state": "short", "phase": "live"}, now=now)
    classify_managed_entries(factory, now=now)
    with factory() as session:
        buy = session.get(BrokerOrder, UUID(proof.identity.entry_order_id))
        sell = BrokerOrder(strategy_id=buy.strategy_id, broker_account_id=buy.broker_account_id,
            client_order_id=buy.client_order_id + "-ocoexit-1008219030471", broker_order_id="1008219030471",
            symbol="FLYE", side="sell", order_type="stop", time_in_force="day", quantity=197,
            status="filled", submitted_at=at + timedelta(seconds=1), updated_at=at + timedelta(seconds=1),
            payload={"reason": "CW_HARD_STOP"})
        session.add(sell)
        session.flush()
        session.add(Fill(order_id=sell.id, strategy_id=sell.strategy_id, broker_account_id=sell.broker_account_id,
            symbol="FLYE", side="sell", quantity=197, price=Decimal("2.78"),
            filled_at=at + timedelta(seconds=1), payload={"reason": "CW_HARD_STOP"}))
        session.commit()
    monkeypatch.setattr("project_mai_tai.services.control_plane.utcnow", lambda: now)
    repository = ControlPlaneRepository(Settings(), session_factory=factory, redis=None)
    source = repository._load_database_state(lightweight=True)
    assert not source["errors"]
    entry, = [item for item in source["recent_fills"] if item["side"] == "buy"]
    assert entry["entry_classification"] == proof.as_payload()
    rows = _collect_completed_position_rows({"strategy_code": "schwab_1m_v2", "account_name": proof.identity.account},
                                            source["recent_orders"], source["recent_fills"])
    assert len(rows) == 1 and rows[0]["summary"].startswith("False flip / ")
    assert "stop" in rows[0]["summary"].lower()
    html = _build_closed_trade_rows_v2(rows)
    assert "False flip" in html and "FLYE" in html
    (tmp_path / "controlled-flye-false-flip-row.html").write_text(html)
    engine.dispose()


def test_classification_sql_is_batched_and_executed_only_on_its_worker_thread():
    engine, factory = database()
    proof, _, _ = populate(factory)
    now = datetime.fromtimestamp(proof.bar.observed_at_ms / 1000, UTC)
    record_bar(factory, {"symbol": proof.bar.symbol, "bar_ms": proof.bar.bar_ms,
        "observed_at_ms": proof.bar.observed_at_ms, "close": str(proof.bar.close),
        "trail": str(proof.bar.trail), "state": "short", "phase": "live"}, now=now)
    threads = []
    event.listen(engine, "before_cursor_execute", lambda *_: threads.append(threading.get_ident()))
    current = threading.get_ident()

    async def run():
        return await asyncio.to_thread(classify_managed_entries, factory, now=now)

    assert asyncio.run(run()) == 1
    assert len(threads) == 6 and current not in threads
    engine.dispose()


@pytest.mark.asyncio
async def test_stalled_classification_worker_never_delays_later_protective_intent_or_quote(monkeypatch):
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings = SimpleNamespace(strategy_schwab_1m_v2_false_flip_enabled=True)
    service.session_factory = None
    service.logger = logging.getLogger("falseflip-test")
    entered, release = threading.Event(), threading.Event()
    worker_thread = []

    def stalled(*_args):
        worker_thread.append(threading.get_ident())
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr("project_mai_tai.oms.service.record_bar", stalled)
    monkeypatch.setattr("project_mai_tai.oms.service.classify_managed_entries", lambda *_: 0)
    service._rpg_advance = AsyncMock()
    service._rpg_external_retry = lambda _: False
    service._claim_webull_mirror_deferred_resubmit = lambda _: True
    service.process_trade_intent = AsyncMock()
    service._finish_webull_mirror_deferred_resubmit = lambda _: None
    service._finish_nfq_retry = lambda *_, **__: None
    service._finish_nfq2_retry = lambda *_, **__: None
    service._handle_quote_tick_event = AsyncMock()
    stop = asyncio.Event()
    worker = asyncio.create_task(service._run_falseflip_worker(stop))
    try:
        await service._handle_stream_message({"data": json.dumps({"event_type": "v2_entry_bar_close"})})
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set() and worker_thread == [worker_thread[0]]
        assert worker_thread[0] != threading.get_ident()
        # A later protective cancel/readback remains on the ordinary serial path.
        await asyncio.wait_for(service._handle_stream_message({"data": json.dumps({
            "event_type": "atr_reprice_tick", "token": str(uuid4())})}), timeout=0.05)
        service._rpg_advance.assert_awaited_once()
        from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload, TradeIntentEvent, TradeIntentPayload
        cancel = TradeIntentEvent(source_service="schwab-1m-v2", payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name="live:orb", symbol="FLYE",
            side="buy", quantity=Decimal("98"), intent_type="cancel", reason="ATR_FLIP"))
        quote = QuoteTickEvent(source_service="market-data", payload=QuoteTickPayload(
            symbol="FLYE", bid_price=Decimal("2.78"), ask_price=Decimal("2.79")))
        for message in (cancel, quote):
            await asyncio.wait_for(service._handle_stream_message({"data": message.model_dump_json()}), 0.05)
        service.process_trade_intent.assert_awaited_once()
        service._handle_quote_tick_event.assert_awaited_once()
    finally:
        release.set()
        stop.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True, "mock"], ids=["default-off", "enabled", "unconfigured-mock"])
async def test_oms_run_creates_falseflip_worker_only_for_explicit_on(monkeypatch, enabled):
    engine, factory = database()
    service = OmsRiskService.__new__(OmsRiskService)
    service.session_factory = factory
    service.settings = SimpleNamespace(strategy_schwab_1m_v2_false_flip_enabled=(
        MagicMock() if enabled == "mock" else enabled), oms_adapter_label="test", active_broker_providers=())
    service.logger = logging.getLogger("falseflip-run-test")
    service.redis = SimpleNamespace(aclose=AsyncMock())
    service.seed_runtime_metadata = lambda: {"strategies": 0, "broker_accounts": 0}
    for name in ("_rehydrate_managed_v2_symbols", "_restore_nfq_holds", "_restore_nfq2_holds", "_restore_mirrorhold"):
        setattr(service, name, lambda: None)
    for name in ("_refresh_drift_working_cache", "_rehydrate_armed_hard_stops", "_publish_heartbeat",
                 "_reconcile_protection_before_serving", "_shutdown_symbol_tick_work"):
        setattr(service, name, AsyncMock())
    service._run_tick_consumer = AsyncMock()
    service._run_rpg_retry_loop = AsyncMock()
    service._run_falseflip_worker = AsyncMock()
    monkeypatch.setattr("project_mai_tai.oms.service._install_signal_handlers", lambda _: None)

    async def control(stop):
        await asyncio.sleep(0)
        stop.set()

    service._run_control_loop = control
    try:
        await asyncio.wait_for(service.run(), 1)
        assert service._run_falseflip_worker.await_count == int(enabled is True)
        service.redis.aclose.assert_awaited_once()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_worker_shutdown_keeps_ownership_of_its_inflight_database_thread():
    service = OmsRiskService.__new__(OmsRiskService)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def write():
        entered.set()
        assert release.wait(2)
        finished.set()

    work = asyncio.create_task(service._falseflip_database_work(write))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        work.cancel()
        await asyncio.sleep(0)
        assert not work.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(work, 1)
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_stalled_budget_write_is_offloop_and_actual_quote_and_bar_callbacks_stay_under_50ms():
    engine, factory = database()
    strategy, state, clock, proofs, _ = runtime()
    book(strategy, state, clock, proofs)
    assert len(strategy.pending_falseflip_budgets()) == 1
    entered, release = threading.Event(), threading.Event()
    sql_threads = []
    event.listen(engine, "before_cursor_execute", lambda *_: sql_threads.append(threading.get_ident()))
    store = FalseFlipStore(factory)

    class SlowStore:
        def commit(self, before, after):
            entered.set()
            assert release.wait(2)
            store.commit(before, after, now=datetime.fromtimestamp(clock[0]/1000, UTC))

    bot = SchwabV2BotService.__new__(SchwabV2BotService)
    bot.strategy, bot.settings = strategy, strategy.settings
    bot._falseflip_store = SlowStore()
    bot.intent_emitter = SimpleNamespace(emit_entry_bar_close=AsyncMock())
    worker = asyncio.create_task(bot._falseflip_poll())
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.001)
        assert entered.is_set()
        start = time.perf_counter()
        strategy.on_quote(state.symbol, Quote(state.symbol, 2.78, 2.79, 2.79, clock[0], clock[0]))
        bot._strategy_on_bar(state.symbol, _bar(clock[0]), observation_phase="live")
        assert time.perf_counter()-start < .05
        assert not sql_threads  # stalled writer has not reached SQL; neither callback queried
        release.set()
        await asyncio.wait_for(worker, 1)
        assert sql_threads and threading.get_ident() not in sql_threads
    finally:
        release.set()
        await asyncio.gather(worker, return_exceptions=True)
        engine.dispose()


def test_nullable_classification_leaves_old_code_projection_compatible():
    engine, factory = database()
    _, _, row_id = populate(factory)
    with engine.connect() as connection:
        value = connection.execute(text("SELECT status, current_quantity, entry_order_id FROM oms_managed_positions"))
        assert value.one()[0:2] == ("closed", 0)
    with factory() as session:
        assert session.get(OmsManagedPosition, row_id).entry_classification is None
    engine.dispose()


def test_pre_column_orm_mapping_reads_nullable_and_classified_rows_without_schema_rollback():
    from project_mai_tai.db import models
    engine, factory = database()
    proof, _, row_id = populate(factory)
    old_source = inspect.getsource(models)
    column = "    entry_classification: Mapped[dict | None] = mapped_column(JSON, nullable=True)\n"
    assert old_source.count(column) == 1  # models.py's sole delta from the pinned base
    old_source = old_source.replace(column, "").replace("from project_mai_tai.db.base import Base", "")
    name = "falseflip_rollback_models_"+uuid4().hex
    module = ModuleType(name)
    module.Base = type("RollbackBase", (DeclarativeBase,), {})
    sys.modules[name] = module
    try:
        exec(compile(old_source, "<pre-0023-mapping>", "exec"), module.__dict__)
        with factory() as session:
            old_row = session.get(module.OmsManagedPosition, row_id)
            assert old_row.status == "closed" and old_row.current_quantity == 0
            assert not hasattr(old_row, "entry_classification")
        now = datetime.fromtimestamp(proof.bar.observed_at_ms/1000, UTC)
        record_bar(factory, {"symbol": "FLYE", "bar_ms": proof.bar.bar_ms,
            "observed_at_ms": proof.bar.observed_at_ms, "close": str(proof.bar.close),
            "trail": str(proof.bar.trail), "state": "short", "phase": "live"}, now=now)
        assert classify_managed_entries(factory, now=now) == 1
        with factory() as session:
            old_row = session.get(module.OmsManagedPosition, row_id)
            assert old_row.status == "closed" and old_row.entry_order_id == UUID(proof.identity.entry_order_id)
            assert not hasattr(old_row, "entry_classification")
    finally:
        sys.modules.pop(name, None)
        engine.dispose()


@pytest.mark.asyncio
async def test_recorded_flye_completed_bar_database_book_next_live_bar_and_existing_emitter(monkeypatch):
    """Recorded post-entry scalar snapshot and stored arrivals, not simulated broker fills."""
    stored = json.loads((Path(__file__).parents[2] / "tests/fixtures/falseflip1/"
                         "flye-bars-20261008.json").read_text())
    bars = [OHLCVBar(int(datetime.fromisoformat(row["bar_time"]).timestamp()*1000),
        *(float(row[key]) for key in ("open_price", "high_price", "low_price", "close_price")),
        row["volume"]) for row in stored["bars"]]
    engine, factory = database()
    strategy, state, clock, proofs, _ = runtime()
    for row in FLYE:
        populate(factory, row, quantity=98 if row["account"] == "live:orb" else 197,
                 price=Decimal("3.04") if row["account"] == "live:orb" else Decimal("3.05"))
    entry_bar = next(bar for bar in bars if bar.timestamp_ms == proofs[0].bar.bar_ms)
    assert entry_bar.close == float(proofs[0].bar.close)
    state.bars.clear()
    state.bars.append(entry_bar)
    state.atr_session_anchor_ms = 1791446400000  # Oct 8 04:00 ET, unchanged session boundary
    state.atr_prev_bar = entry_bar
    state.atr_prev_state = state.atr_state = "short"
    state.atr_prev_trail = state.atr_trail = 3.006326
    state.atr_state_age = 26
    state.atr_wilders = .600184 / strategy._atr_factor  # recorded entry-close loss, not an oracle reseed
    state.atr_hl.clear()
    state.atr_hl.extend(bar.high-bar.low for bar in bars if bar.timestamp_ms <= entry_bar.timestamp_ms)
    bot = SchwabV2BotService.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot.session_factory = strategy, strategy.settings, factory
    emitted = []

    class Redis:
        async def xadd(self, _stream, fields, **_kwargs):
            emitted.append(json.loads(fields["data"]))
            return "test"

    bot.intent_emitter = SchwabV2IntentEmitter(strategy.settings, Redis(), "live:schwab_1m_v2")
    mirror_emitter = SchwabV2IntentEmitter(strategy.settings, Redis(), "live:orb")
    bot._strategy_on_bar("FLYE", entry_bar, observation_phase="live")
    await bot._falseflip_poll()
    event_, = emitted
    assert event_["event_type"] == "v2_entry_bar_close"
    assert Decimal(event_["close"]) == Decimal("2.7901") and Decimal(event_["trail"]) == Decimal("3.006326")
    now = datetime.fromtimestamp(clock[0]/1000, UTC)
    record_bar(factory, event_, now=now)
    assert classify_managed_entries(factory, now=now) == 2
    from project_mai_tai import services
    module = services.schwab_1m_v2_bot

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0]/1000, tz or UTC)

    monkeypatch.setattr(module, "datetime", ClockDatetime)
    monkeypatch.setattr(module, "_current_scanner_session_start_utc",
                        lambda: datetime(2026, 10, 8, 8, tzinfo=UTC))
    measured_book = bot._fetch_flip_position_book()
    assert measured_book.readable and len(measured_book.entry_classifications["FLYE"]) == 2
    strategy.apply_flip_position_book(measured_book)
    ack(strategy)
    strategy.apply_flip_position_book(bot._fetch_flip_position_book())
    await bot._falseflip_poll()  # durable request publication is off-loop
    request = strategy._removed_wait_requests["FLYE"]
    assert len(strategy.drain_pending_intents()) == len(strategy.drain_webull_direct_intents()) == 1
    strategy.apply_removed_wait_proofs((RemovedWaitProof(request, clock[0], True,
        "false_flip_leftovers_cancelled_owner_kept"),))
    strategy.apply_flip_position_book(bot._fetch_flip_position_book())
    ack(strategy)
    strategy.apply_flip_position_book(bot._fetch_flip_position_book())
    next_bar = next(bar for bar in bars if bar.timestamp_ms == entry_bar.timestamp_ms+60000)
    clock[0] = next_bar.timestamp_ms+62000
    strategy.apply_flip_position_book(bot._fetch_flip_position_book())
    bot._strategy_on_bar("FLYE", next_bar, observation_phase="live")
    primary, = strategy.drain_pending_intents()
    mirror, = strategy.drain_webull_direct_intents()
    for emitter, draft in ((bot.intent_emitter, primary), (mirror_emitter, mirror)):
        assert draft.intent_type == "open" and draft.metadata["cw_entry_slot"] == "first"
        await emitter.emit(draft)
    orders = [item for item in emitted if item["event_type"] == "trade_intent"]
    assert {item["payload"]["broker_account_name"] for item in orders} == {"live:orb", "live:schwab_1m_v2"}
    assert state.retry_one_segment_id == 1791466440000
    assert state.atr_state == "short"
    # The subsequent real flip is a recorded mathematical outcome; no later broker
    # fill is invented by this controlled restoration/emitter replay.
    flips = []
    for bar in bars:
        if next_bar.timestamp_ms < bar.timestamp_ms <= FLYE[0]["next_buy"]["minute"]:
            signal = strategy._update_atr_state(state, bar, state_only=True)
            if signal and signal["flip"]:
                flips.append((bar.timestamp_ms, signal["flip"]))
    assert flips == [(1791472020000, "BUY")]
    assert state.atr_state == "long"
    assert state.atr_trail == pytest.approx(2.237570, abs=.000001)
    engine.dispose()
