"""Synthetic live-account tickets in isolated SQLite; no production/broker reads."""
import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, TradeIntent
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal
from tests.unit.test_rpg1_runtime import runtime


def unreadable(*args, **kwargs):
    pytest.fail("flag OFF consulted RPG journal/cache")


class UnreadableJobs(dict):
    values = items = get = unreadable


def ticket(h, phase="held_unknown"):
    journal = HandoffJournal(h.factory)
    token = journal.prepare(h.old, slot="first", segment_id=h.state.fanout_segment_id,
                            now=h.clock[0].timestamp())
    job = journal.read(token)
    job = journal.change(token, job["revision"], phase=phase)
    return token, job


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("phase", ["prepared", "waiting", "fills_waiting", "held_unknown", "clear",
                                   "submitting", "submit_unknown", "price_wait", "placed"])
async def test_live_looking_ticket_flag_off_open_proceeds_and_on_refuses(monkeypatch, broker, enabled, phase):
    h = await runtime(monkeypatch, broker)
    token, job = ticket(h, phase)
    # The broker parent is terminal; only the stale handoff claims a live BUY.
    with h.factory() as session:
        parent = session.scalar(select(BrokerOrder))
        parent.status = "cancelled"
        session.get(TradeIntent, parent.intent_id).status = "cancelled"
        session.commit()
    h.service.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled = enabled
    opening = h.opening.model_copy(deep=True, update={"event_id": uuid4()})
    opening.payload.metadata["fanout_slot_id"] = str(uuid4())
    if not enabled:
        monkeypatch.setattr(HandoffJournal, "jobs", unreadable)
        monkeypatch.setattr(HandoffJournal, "read", unreadable)
    result = await h.service.process_trade_intent(opening)
    assert len(h.adapter.opens) == (0 if enabled else 1)
    assert result and result[0].payload.status == ("aborted" if enabled else "accepted")
    if enabled:
        assert result[0].payload.reason == "rpg_old_buy_still_owned"
    # Inspect only after restoring the read spies: OFF must not mutate the ticket.
    monkeypatch.undo()
    assert HandoffJournal(h.factory).read(token) == job


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", [False, True])
async def test_flag_off_restore_and_cached_ownership_do_not_read_or_project(monkeypatch, refresh):
    h = await runtime(monkeypatch, "schwab")
    token, job = ticket(h)
    h.bot._rpg_known_jobs = {token: job}
    h.strategy._rpg_handoffs = UnreadableJobs({str(token): job})
    h.service.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled = False
    monkeypatch.setattr(HandoffJournal, "jobs", unreadable)
    monkeypatch.setattr(HandoffJournal, "read", unreadable)
    before = (h.state.resting_active, h.state.resting_schwab_generation,
              h.state.resting_webull_generation, h.state.cw_resting_taken)
    await h.bot._rpg_handoff_pass(refresh=refresh)
    assert not h.strategy._rpg_entry_owned(h.state)
    assert not h.strategy._rpg_leg_owned(h.state, h.old.broker_account_name)
    assert h.strategy._rpg_refused_legs(h.state, slot="first") == set()
    assert h.strategy.rpg_handoff_authorization(str(token), job)["reason"] == "disabled"
    assert before == (h.state.resting_active, h.state.resting_schwab_generation,
                      h.state.resting_webull_generation, h.state.cw_resting_taken)
    h.strategy._queue_resting_place(h.state, 3.10)
    assert len(h.strategy.drain_pending_intents()) == 1
    assert len(h.strategy.drain_webull_direct_intents()) == 1


@pytest.mark.asyncio
async def test_flag_on_restore_projects_ownership(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    token, _ = ticket(h)
    await h.bot._rpg_handoff_pass()
    assert str(token) in h.strategy._rpg_handoffs
    assert h.strategy._rpg_entry_owned(h.state)
    h.strategy._queue_resting_place(h.state, 3.10)
    assert not h.strategy.drain_pending_intents()
    assert not h.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
async def test_flag_off_oms_restore_ticks_evidence_and_token_do_not_read(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    token, _ = ticket(h)
    h.service.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled = False
    monkeypatch.setattr(h.service, "_rpg_journal", unreadable)
    monkeypatch.setattr(h.service, "session_factory", unreadable)
    opening = h.opening.model_copy(deep=True)
    opening.payload.metadata["rpg_handoff_token"] = str(token)
    assert h.service._rpg_open_refusal(opening) is None
    assert not h.service._rpg_external_retry(opening)
    assert not h.service._rpg_owns_old_order(object(), h.old, h.old.broker_account_name)
    assert h.service._rpg_retry_jobs(include_unknown=True) == []
    assert h.service._rpg_mark_committed_evidence(object()) == []
    assert await h.service._rpg_begin_cancel(opening) == []
    await h.service._handle_stream_message({"data": json.dumps(
        {"event_type": "atr_reprice_tick", "token": str(token)})})
    await h.service._run_rpg_retry_loop(asyncio.Event())
    assert h.service._rpg_retry_started().is_set()
    assert not h.adapter.opens and not h.adapter.cancels and not h.adapter.reads


@pytest.mark.asyncio
async def test_missing_flag_defaults_off_without_journal_access(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    monkeypatch.delattr(h.service.settings, "strategy_schwab_1m_v2_atr_reprice_handoff_enabled")
    monkeypatch.setattr(h.service, "_rpg_journal", unreadable)
    monkeypatch.setattr(HandoffJournal, "jobs", unreadable)
    assert h.service._rpg_open_refusal(h.opening) is None
    await h.bot._rpg_handoff_pass()
    assert not h.strategy._rpg_entry_owned(h.state)


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_flag_off_flagged_cancel_uses_ordinary_cancel_without_journal(monkeypatch, broker):
    h = await runtime(monkeypatch, broker)
    h.service.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled = False
    h.strategy._cw_v2_resting_track(h.state, None)
    primary, = h.strategy.drain_pending_intents()
    mirror, = h.strategy.drain_webull_direct_intents()
    draft = primary if broker == "schwab" else mirror
    cancel = h.opening.model_copy(deep=True, update={"event_id": uuid4()})
    cancel.payload.intent_type = "cancel"
    cancel.payload.metadata = {**draft.metadata, "atr_reprice": "true"}
    monkeypatch.setattr(h.service, "_rpg_journal", unreadable)
    await h.service.process_trade_intent(cancel)
    assert len(h.adapter.cancels) == 1
    assert not h.adapter.opens and not h.adapter.reads


@pytest.mark.asyncio
async def test_running_retry_loop_stops_before_scan_after_switch_off(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    scans = []

    def scan(*, include_unknown):
        assert h.service._rpg_enabled(), "flag OFF retry loop scanned tickets"
        scans.append(include_unknown)
        return []

    async def pause(stop_event, seconds):
        assert h.service._rpg_enabled(), "flag OFF retry loop kept running"
        h.service.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled = False
        h.service._rpg_evidence_pending = {uuid4()}

    monkeypatch.setattr(h.service, "_rpg_retry_jobs", scan)
    monkeypatch.setattr(h.service, "_rpg_retry_pause", pause)
    monkeypatch.setattr(h.service, "_rpg_journal", unreadable)
    await h.service._run_rpg_retry_loop(asyncio.Event())
    assert scans == [True]


@pytest.mark.asyncio
async def test_recorded_tickets_real_startup_off_leaves_journal_and_ownership_dormant(monkeypatch):
    from tests.unit.test_rpgstuck1_startup import (
        real_bot_startup, real_oms_startup, startup_harness,
    )

    h = await startup_harness(monkeypatch, with_deferred=True, handoff_enabled=False)
    journal = HandoffJournal(h.factory)
    before = journal.jobs()
    monkeypatch.setattr(HandoffJournal, "jobs", unreadable)
    monkeypatch.setattr(HandoffJournal, "read", unreadable)
    await real_bot_startup(monkeypatch, h)
    await real_oms_startup(monkeypatch, h)
    assert not h.strategy._rpg_handoffs
    assert all(not h.strategy._rpg_entry_owned(state)
               for state in h.strategy._symbol_states.values())
    assert not h.adapter.opens and not h.adapter.cancels and not h.adapter.reads
    monkeypatch.undo()
    assert journal.jobs() == before
