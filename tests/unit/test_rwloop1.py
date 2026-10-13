from datetime import UTC, datetime, timedelta
import logging

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import DashboardSnapshot, Fill, SystemIncident
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.v2_removed_wait import RemovedWaitProof
from tests.unit.test_v2_removed_wait_clear_rules import (
    db as db, NOW, MS, PRIMARY, WEBULL, order_row, request, strategy,
)


def owned_removal(db, account=PRIMARY):
    store, factory, accounts, strategies = db
    req = request(opportunity=0)
    store.record_rules(req, True)
    order = order_row(db, req, account=account, own=True, status="filled", submitted=NOW-timedelta(hours=1))
    with factory() as session:
        session.add(Fill(order_id=order.id, strategy_id=strategies["schwab_1m_v2"],
            broker_account_id=accounts[account], symbol=req.symbol, side="buy", quantity=1, price=1,
            filled_at=NOW-timedelta(hours=1)))
        session.commit()
    strat = strategy(req, store)
    state = strat.watchlist_state(req.symbol)
    state.flip_owner_phase = "consumed"
    state.flip_owner_opportunity_id = state.fanout_segment_id = MS-600000
    state.flip_owner_fill_accounts.add(account)
    state.flip_owner_position_ids[account] = str(order.id)
    state.retry_one_closes_in_segment = 1
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strat, strat.settings, store
    read = store.clear_rule_proofs
    store.clear_rule_proofs = lambda requests, names: read(requests, names, now=NOW)
    return bot, req, state


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [PRIMARY, WEBULL])
async def test_opp_zero_own_fill_request_commits_offloop_then_readd_has_no_removal_gate(db, account, caplog):
    import threading

    bot, req, state = owned_removal(db, account)
    store, factory, _, _ = db
    owner = bot.strategy._flip_owner_record(state)
    calls = []
    commit = store.commit_clear

    def worker(plan, valid):
        calls.append(threading.get_ident())
        assert not plan.proof.clear and plan.proof.reason == "own_fill_stays_owned"
        assert not plan.retirements
        commit(plan, valid)

    store.commit_clear = worker
    caplog.set_level(logging.WARNING)
    await bot._removed_wait_poll()
    assert calls == [calls[0]] and calls[0] != threading.get_ident()
    assert not store.restore() and not bot.strategy._removed_wait_requests
    assert bot.strategy._flip_owner_record(state) == owner
    assert state.retry_one_closes_in_segment == 1
    bot.strategy.scanner_readded(req.symbol)
    assert not bot.strategy._removed_wait_gate_closed(req.symbol)
    assert not caplog.records
    with factory() as session:
        assert session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == "v2_flip_entry_ownership")).all() == []


@pytest.mark.asyncio
async def test_commit_failure_retries_with_backoff_five_attempts_one_warning_one_incident(db, caplog):
    bot, req, state = owned_removal(db)
    store, factory, _, _ = db
    clock = [MS]
    bot.strategy._now_ms = lambda: clock[0]
    read = type(store).clear_rule_proofs
    store.clear_rule_proofs = lambda requests, names: read(store, requests, names,
        now=datetime.fromtimestamp(clock[0]/1000, UTC))
    calls = []

    def unavailable(*_):
        calls.append(clock[0])
        raise RuntimeError("controlled copy-only commit failure")

    store.commit_clear = unavailable
    caplog.set_level(logging.WARNING)
    for _ in range(14):
        await bot._removed_wait_poll()
        clock[0] += 1000
    assert len(calls) == 1
    clock[0] = MS + 15000
    await bot._removed_wait_poll()
    assert len(calls) == 2
    clock[0] = MS + 44000
    await bot._removed_wait_poll()
    assert len(calls) == 2
    for delta in (45000, 105000, 225000, 600000, 1000000):
        clock[0] = MS + delta
        await bot._removed_wait_poll()
    assert len(calls) == 5
    assert len([r for r in caplog.records if "[RWLOOP1]" in r.message]) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert store.restore() == bot.strategy._removed_wait_requests == {req.symbol:req}
    assert state.flip_owner_phase == "consumed" and state.retry_one_closes_in_segment == 1
    with factory() as session:
        incidents = session.scalars(select(SystemIncident)).all()
        assert len(incidents) == 1 and incidents[0].payload["attempts"] == 5


@pytest.mark.asyncio
async def test_failure_then_retry_ack_retires_request_without_changing_owner(db, caplog):
    bot, req, state = owned_removal(db)
    store, _, _, _ = db
    commit = store.commit_clear

    def unavailable(*_):
        raise RuntimeError("controlled failure")

    store.commit_clear = unavailable
    await bot._removed_wait_poll()
    assert req.symbol in bot.strategy._removed_wait_requests
    store.commit_clear = commit
    clock = MS + 15000
    bot.strategy._now_ms = lambda: clock
    read = type(store).clear_rule_proofs
    store.clear_rule_proofs = lambda requests, names: read(store, requests, names,
        now=datetime.fromtimestamp(clock/1000, UTC))
    await bot._removed_wait_poll()
    assert not store.restore() and not bot.strategy._removed_wait_requests
    assert state.flip_owner_phase == "consumed"
    assert state.retry_one_closes_in_segment == 1


def test_bar_receipt_without_offloop_ack_is_quiet_not_a_fleet_error_loop(db, caplog):
    bot, req, state = owned_removal(db)
    caplog.set_level(logging.WARNING)
    proof = RemovedWaitProof(req, MS, False, "own_fill_stays_owned")
    for _ in range(100):
        bot.strategy.apply_removed_wait_proofs((proof,))
    assert bot.strategy._removed_wait_requests == {req.symbol:req}
    assert state.flip_owner_phase == "consumed"
    assert not caplog.records
    assert not bot.strategy.stage_removed_wait_clear(proof, retire_owner=True)
    assert not bot.strategy._removed_wait_pending_clears
