"""Source-authored bounded/fair lifecycle controls; all DB/broker state is synthetic."""

import asyncio
from contextlib import contextmanager
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, TradeIntent
from project_mai_tai.oms import webull_attach_riskoff as wb
from tests.unit.test_wbattach1_riskoff import attach, make_case as make_case, restarted
from tests.unit.test_wbattach1_terminal_bookkeeping import cached_adapter, close_economically, economic_rows


def pending(case, identifiers, account="live:orb"):
    with case[1].begin() as session:
        entry = session.get(BrokerOrder, case[3])
        acct = case[0].store.ensure_broker_account(session, account, provider="webull", environment="production")
        for identifier in identifiers:
            session.add(TradeIntent(id=UUID(int=identifier), strategy_id=entry.strategy_id,
                broker_account_id=acct.id, symbol="ABC", side="sell", intent_type="close",
                quantity=10, reason="WBATTACH1_PROTECTION", status="accepted", payload={"metadata": {}}))


def observe(case, monkeypatch, *, change_status=False):
    seen = []
    case[0].broker_adapter.cached_attach_pair_terminal = lambda *_args: None
    monkeypatch.setattr(wb, "monotonic", lambda: 0.)
    def settle(_service, _session, intent):
        seen.append(intent.id.int)
        if change_status:
            intent.status = "filled"
        return change_status
    monkeypatch.setattr(wb, "settle_cached_protection_intent", settle)
    return seen


@pytest.mark.asyncio
async def test_unknown_front_page_does_not_starve_later_rows_and_sweep_revisits(make_case, monkeypatch):
    case = make_case()
    pending(case, range(1, 20))
    seen = observe(case, monkeypatch)
    before = economic_rows(case[1])
    await wb.reconcile_cached_protection_intents(case[0], None)
    assert seen == list(range(1, 9))
    await wb.reconcile_cached_protection_intents(case[0], None)
    assert seen == list(range(1, 17))
    await wb.reconcile_cached_protection_intents(case[0], None)
    assert seen == list(range(1, 20))
    await wb.reconcile_cached_protection_intents(case[0], None)
    assert seen[19:] == list(range(1, 9))
    with case[1]() as session:
        assert {i.status for i in session.scalars(select(TradeIntent).where(
            TradeIntent.reason == "WBATTACH1_PROTECTION"))} == {"accepted"}
    assert economic_rows(case[1]) == before


@pytest.mark.asyncio
async def test_new_upper_ids_wait_for_next_sweep_without_extending_current_sweep(make_case, monkeypatch):
    case = make_case()
    pending(case, range(1, 20))
    seen = observe(case, monkeypatch)
    await wb.reconcile_cached_protection_intents(case[0], None)
    pending(case, range(20, 25))
    await wb.reconcile_cached_protection_intents(case[0], None)
    await wb.reconcile_cached_protection_intents(case[0], None)
    assert seen == list(range(1, 20))
    for _ in range(3):
        await wb.reconcile_cached_protection_intents(case[0], None)
    assert seen[19:] == list(range(1, 25))


@pytest.mark.asyncio
async def test_insert_below_cursor_is_eligible_on_next_sweep(make_case, monkeypatch):
    case = make_case()
    pending(case, range(2, 12))
    seen = observe(case, monkeypatch)
    await wb.reconcile_cached_protection_intents(case[0], None)
    pending(case, [1])
    await wb.reconcile_cached_protection_intents(case[0], None)
    assert seen == list(range(2, 12))
    await wb.reconcile_cached_protection_intents(case[0], None)
    assert seen[10:] == list(range(1, 9))


@pytest.mark.asyncio
async def test_account_cursors_are_independent_and_scope_is_canonical(make_case, monkeypatch):
    case = make_case()
    pending(case, range(1, 11))
    pending(case, range(11, 21), account="live:other")
    seen = observe(case, monkeypatch)
    await wb.reconcile_cached_protection_intents(case[0], ["live:orb", "live:orb"])
    await wb.reconcile_cached_protection_intents(case[0], ["live:other"])
    assert seen == list(range(1, 9)) + list(range(11, 19))
    await wb.reconcile_cached_protection_intents(case[0], ["live:orb"])
    await wb.reconcile_cached_protection_intents(case[0], ["live:other"])
    assert seen[-4:] == [9, 10, 19, 20]


@pytest.mark.asyncio
async def test_cooperative_budget_advances_past_slow_unknown_instead_of_retrying_first(make_case, monkeypatch):
    case = make_case()
    pending(case, range(1, 10))
    seen = observe(case, monkeypatch)
    clock = [0.]
    monkeypatch.setattr(wb, "monotonic", lambda: clock[0])
    def slow(_service, _session, intent):
        seen.append(intent.id.int)
        clock[0] += 1.
        return False
    monkeypatch.setattr(wb, "settle_cached_protection_intent", slow)
    for _ in range(9):
        previous = len(seen)
        await wb.reconcile_cached_protection_intents(case[0], None)
        assert len(seen) - previous == 1
    assert seen == list(range(1, 10))


@pytest.mark.asyncio
async def test_commit_failure_does_not_advance_cursor_or_publish_terminal_status(make_case, monkeypatch):
    case = make_case()
    pending(case, range(1, 10))
    seen = observe(case, monkeypatch, change_status=True)
    factory = case[1]
    @contextmanager
    def fail_commit():
        with factory() as session:
            def fail():
                raise RuntimeError("controlled commit failure")
            session.commit = fail
            yield session
    case[0].session_factory = fail_commit
    before = economic_rows(factory)
    with pytest.raises(RuntimeError, match="controlled commit failure"):
        await wb.reconcile_cached_protection_intents(case[0], None)
    assert case[0]._wbattach_bookkeeping_sweep.cursors == {}
    with factory() as session:
        assert session.get(TradeIntent, UUID(int=1)).status == "accepted"
    case[0].session_factory = factory
    await wb.reconcile_cached_protection_intents(case[0], None)
    assert seen == list(range(1, 9)) * 2
    assert economic_rows(factory) == before


@pytest.mark.asyncio
async def test_cancelled_waiter_keeps_worker_ownership_through_commit(make_case, monkeypatch):
    case = make_case()
    pending(case, [1])
    seen = observe(case, monkeypatch)
    committing, release = Event(), Event()
    factory = case[1]
    opened = []
    @contextmanager
    def blocked_commit():
        with factory() as session:
            opened.append(1)
            original_commit = session.commit
            def commit():
                committing.set()
                assert release.wait(2.), "controlled commit barrier timed out"
                original_commit()
            session.commit = commit
            yield session
    case[0].session_factory = blocked_commit
    first = asyncio.create_task(wb.reconcile_cached_protection_intents(case[0], None))
    try:
        assert await asyncio.to_thread(committing.wait, 2.)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await wb.reconcile_cached_protection_intents(case[0], None)
        assert opened == [1] and seen == [1]
        assert case[0]._wbattach_bookkeeping_sweep.cursors == {}
    finally:
        release.set()
        async def released():
            while case[0]._wbattach_bookkeeping_sweep.lock.locked():
                await asyncio.sleep(.001)
        await asyncio.wait_for(released(), 2.)
    assert case[0]._wbattach_bookkeeping_sweep.cursors[None] == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [[], ["live:missing"]])
async def test_empty_or_unmatched_selection_never_visits_foreign_intents(make_case, monkeypatch, scope):
    case = make_case()
    pending(case, range(1, 10))
    seen = observe(case, monkeypatch)
    await wb.reconcile_cached_protection_intents(case[0], scope)
    assert seen == []


@pytest.mark.asyncio
async def test_service_restart_resets_only_cursor_and_preserves_pending_eligibility(make_case, monkeypatch):
    case = make_case()
    pending(case, range(1, 10))
    seen = observe(case, monkeypatch)
    await wb.reconcile_cached_protection_intents(case[0], None)
    new_case = restarted(case)
    assert "_wbattach_bookkeeping_sweep" not in new_case[0].__dict__
    await wb.reconcile_cached_protection_intents(new_case[0], None)
    assert seen == list(range(1, 9)) * 2


@pytest.mark.asyncio
async def test_actual_terminal_helper_reaches_valid_episode_after_legacy_backlog_without_economic_writes(make_case, monkeypatch):
    case = make_case()
    assert await attach(case)
    await close_economically(case)
    cached_adapter(case, monkeypatch)
    pending(case, range(1, 10))
    monkeypatch.setattr(wb, "monotonic", lambda: 0.)
    before = economic_rows(case[1])
    # The fixture's real protection intent uses UUID4; the nine synthetic legacy
    # IDs precede it. Their incomplete metadata must not become a permanent head.
    with case[1]() as session:
        actual = session.scalar(select(TradeIntent).where(
            TradeIntent.reason == "WBATTACH1_PROTECTION", TradeIntent.id > UUID(int=9)))
        actual_id = actual.id
    await wb.reconcile_cached_protection_intents(case[0], None)
    with case[1]() as session:
        assert session.get(TradeIntent, actual_id).status == "accepted"
    await wb.reconcile_cached_protection_intents(case[0], None)
    with case[1]() as session:
        assert session.get(TradeIntent, actual_id).status == "filled"
        assert all(session.get(TradeIntent, UUID(int=i)).status == "accepted" for i in range(1, 10))
    assert economic_rows(case[1]) == before
