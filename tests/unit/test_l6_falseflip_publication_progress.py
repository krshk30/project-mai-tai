"""L6 phase 1: retain UNKNOWN obligations, avoid cross-symbol starvation.

Real RemovedWaitStore writes/rollbacks over controlled SQLite rows. No broker
working-order proof is fabricated; no case authorizes stale-request retirement.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.falseflip1_runtime import FalseBudget
from project_mai_tai.services import schwab_1m_v2_bot as service_module
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.v2_removed_wait import (
    RemovedWait, RemovedWaitPublicationChanged, RemovedWaitStore, SNAPSHOT_TYPE,
)
from tests.unit.test_falseflip1_runtime import database


ACCOUNTS = ("live:schwab_1m_v2", "live:orb")
NOW_MS = 1792499280000


@pytest.fixture
def case():
    engine, factory = database()
    strategy = SchwabV2Strategy(Settings(_env_file=None,
        strategy_schwab_1m_v2_false_flip_enabled=True,
        strategy_schwab_1m_v2_false_flip2_enabled=True,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_retry_one_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_removed_wait_clear_enabled=True,
        strategy_schwab_1m_v2_account_name=ACCOUNTS[0],
        strategy_schwab_1m_v2_webull_account_name=ACCOUNTS[1]))
    strategy._now_ms = lambda: NOW_MS
    store = RemovedWaitStore(factory)
    strategy.configure_removed_wait(store.record_rules, restored={}, readable=True)
    strategy.configure_falseflip({}, readable=True)
    requests = []
    for index, symbol in enumerate(("SAIQ", "WORX")):
        request = RemovedWait(symbol, NOW_MS-60000+index, "controlled-"+symbol,
                              NOW_MS-1000, ACCOUNTS, "false_flip_restore")
        state = strategy.watchlist_state(symbol)
        state.flip_owner_opportunity_id = state.fanout_segment_id = request.opportunity_id
        state.flip_owner_phase = "consumed"
        strategy._falseflip_cancel_publications[symbol] = request
        requests.append(request)
    bot = SchwabV2BotService.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot._removed_wait_store = strategy, strategy.settings, store
    yield SimpleNamespace(engine=engine, factory=factory, strategy=strategy, bot=bot,
                          store=store, first=requests[0], second=requests[1])
    engine.dispose()


def publications(case):
    with case.factory() as session:
        return [row.payload for row in session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE)).all()]


def assert_retained(case, request):
    assert case.strategy._falseflip_cancel_publications[request.symbol] == request
    assert case.strategy._falseflip_readable and case.strategy._removed_wait_restore_readable
    assert not case.strategy.pending_falseflip2_releases()
    assert not case.strategy.pending_falseflip2_rearms()


@pytest.mark.asyncio
async def test_valid_publications_use_real_writer_and_keep_existing_cancel_barriers(case):
    await case.bot._falseflip_poll()
    assert not case.strategy.pending_falseflip_cancel_publications()
    assert case.strategy._removed_wait_requests == {r.symbol:r for r in (case.first,case.second)}
    assert {p["token"] for p in publications(case)} == {case.first.token,case.second.token}
    assert all(p["active"] is True for p in publications(case))
    assert all(case.strategy._removed_wait_gate_closed(r.symbol) for r in (case.first,case.second))
    drafts = case.strategy.drain_pending_intents()+case.strategy.drain_webull_direct_intents()
    assert len(drafts) == 4 and all(d.intent_type == "cancel" for d in drafts)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["owner", "removed", "state_missing", "open_emit", "store_missing"])
async def test_invalid_request_is_retained_and_does_not_starve_later_symbol(case, invalid, caplog):
    state = case.strategy._symbol_states[case.first.symbol]
    budget = FalseBudget(case.first.symbol, NOW_MS-120000).closed(1).closed(2)
    state.retry_one_segment_id = budget.segment
    case.strategy._falseflip_budgets[(budget.symbol,budget.segment)] = budget
    if invalid == "owner":
        state.flip_owner_opportunity_id = state.fanout_segment_id = case.first.opportunity_id+10
    elif invalid == "removed":
        current = replace(case.first, token="new-scanner-token", purpose="scanner_removal")
        case.strategy._removed_wait_requests[case.first.symbol] = current
    elif invalid == "state_missing":
        case.strategy._symbol_states.pop(case.first.symbol)
    elif invalid == "open_emit":
        case.bot._clearwait_open_emits = {case.first.symbol:1}
    else:
        case.bot._removed_wait_store = None
    before_owner = (state.flip_owner_phase,state.fanout_segment_id,state.flip_owner_opportunity_id)
    caplog.set_level("WARNING")
    await case.bot._falseflip_poll()
    assert_retained(case, case.first)
    assert (state.flip_owner_phase,state.fanout_segment_id,state.flip_owner_opportunity_id) == before_owner
    assert case.strategy._falseflip_budgets[(budget.symbol,budget.segment)] == budget
    assert case.strategy._falseflip_entry_blocked(state)
    assert "cancel_publication_waiting" in caplog.text
    assert "cancel_barrier_write_failed" not in caplog.text
    assert "stale_publication_retired" not in caplog.text
    if invalid == "store_missing":
        assert_retained(case, case.second)
        assert publications(case) == []
    else:
        assert case.second.symbol not in case.strategy._falseflip_cancel_publications
        assert case.strategy._removed_wait_requests[case.second.symbol] == case.second
        assert [p["token"] for p in publications(case)] == [case.second.token]
    if invalid == "removed":
        assert case.strategy._removed_wait_requests[case.first.symbol] == current
        assert case.strategy._removed_wait_gate_closed(case.first.symbol)


@pytest.mark.asyncio
async def test_warning_is_once_per_exact_request_at_strict_five_minute_boundary(case, monkeypatch, caplog):
    clock = [10.0]
    monkeypatch.setattr(service_module, "time", SimpleNamespace(monotonic=lambda:clock[0]))
    case.bot._clearwait_open_emits = {case.first.symbol:1,case.second.symbol:1}
    caplog.set_level("WARNING")
    await case.bot._falseflip_poll()
    for instant in (10.0,11.0,200.0,309.999):
        clock[0] = instant
        await case.bot._falseflip_poll()
    assert caplog.text.count("request_token="+case.first.token) == 1
    clock[0] = 310.0
    await case.bot._falseflip_poll()
    assert caplog.text.count("request_token="+case.first.token) == 2
    new = replace(case.first, token="replacement-exact-request")
    case.strategy._falseflip_cancel_publications[case.first.symbol] = new
    await case.bot._falseflip_poll()
    assert caplog.text.count("request_token="+new.token) == 1
    assert_retained(case, new)
    assert publications(case) == []
    assert all(record.levelname == "WARNING" for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["generation", "open_emit", "owner"])
@pytest.mark.parametrize("stage", ["before_write", "before_commit", "after_commit"])
async def test_transient_fence_races_keep_request_and_progress_other_symbol(case, change, stage, caplog):
    original = case.store.record_rules

    def mutate():
        if change == "generation":
            case.bot._clearwait_emit_generation_by_symbol = {case.first.symbol:1}
        elif change == "open_emit":
            case.bot._clearwait_open_emits = {case.first.symbol:1}
        else:
            case.strategy._symbol_states[case.first.symbol].flip_owner_opportunity_id += 10

    def race(request, active, valid):
        if request != case.first:
            return original(request, active, valid)
        checks = [0]

        def fenced():
            checks[0] += 1
            if (stage == "before_write" and checks[0] == 1
                    or stage == "before_commit" and checks[0] == 2):
                mutate()
            return valid()

        result = original(request, active, fenced)
        if stage == "after_commit":
            mutate()
        return result

    case.store.record_rules = race
    caplog.set_level("WARNING")
    await case.bot._falseflip_poll()
    assert_retained(case, case.first)
    assert case.first.symbol not in case.strategy._removed_wait_requests
    assert case.strategy._removed_wait_requests[case.second.symbol] == case.second
    assert "cancel_barrier_write_failed" not in caplog.text
    assert "cancel_publication_waiting" in caplog.text
    own = [p for p in publications(case) if p["token"] == case.first.token]
    assert len(own) == (1 if stage == "after_commit" else 0)
    assert all(p["active"] for p in own)  # No inactive/retirement row is manufactured.


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("removal publication changed"), OSError("controlled DB outage")])
async def test_genuine_write_error_stays_visible_despite_concurrent_invalidity_and_does_not_starve(case, error, caplog):
    original = case.store.record_rules

    def failed(request, active, valid):
        if request == case.first:
            case.bot._clearwait_emit_generation_by_symbol = {request.symbol:1}
            raise error
        return original(request, active, valid)

    case.store.record_rules = failed
    caplog.set_level("WARNING")
    await case.bot._falseflip_poll()
    assert_retained(case, case.first)
    assert case.strategy._removed_wait_requests[case.second.symbol] == case.second
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and errors[0].exc_info is not None
    assert "cancel_barrier_write_failed" in errors[0].getMessage()
    assert "stale_publication_retired" not in caplog.text
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert [p["token"] for p in publications(case)] == [case.second.token]


def test_real_store_fence_exception_is_typed_and_second_check_rolls_back(case):
    with pytest.raises(RemovedWaitPublicationChanged):
        case.store.record_rules(case.first, True, lambda:False)
    answers = iter((True,False))
    with pytest.raises(RemovedWaitPublicationChanged):
        case.store.record_rules(case.first, True, lambda:next(answers))
    assert publications(case) == []
