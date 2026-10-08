"""Review guards through runtime entry points; later orders/clocks are controlled.

Only the old-parent strict read uses the retained broker body. Extra live BUYs
are explicit ledger race scenarios, never represented as recorded broker data.
"""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import object_session

from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal
from project_mai_tai.settings import Settings
from tests.unit.test_rpg1_buy_readback import recorded
from tests.unit.test_rpg1_runtime import runtime, begin, feedback


def configured_window(h, start, end):
    settings = h.service.settings
    settings.strategy_schwab_1m_v2_entry_window_start_hour_et = start[0]
    settings.strategy_schwab_1m_v2_entry_window_start_minute_et = start[1]
    settings.strategy_schwab_1m_v2_entry_window_end_hour_et = end[0]
    settings.strategy_schwab_1m_v2_entry_window_end_minute_et = end[1]


def move_clock(h, hour, minute, second=0):
    h.clock[0] = h.clock[0].replace(hour=hour + 4, minute=minute, second=second, microsecond=0)
    now = h.strategy._now_ms()
    h.state.last_quote = replace(h.state.last_quote, quote_time_ms=now)
    h.state.bars[-1] = replace(h.state.bars[-1], timestamp_ms=now - 60_000)


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("hour,minute,allowed", [(9, 59, False), (10, 0, True), (15, 50, True), (16, 0, False)])
async def test_current_reprice_callback_uses_configured_window_intersect_rth(monkeypatch, broker, hour, minute, allowed):
    h = await runtime(monkeypatch, broker)
    token, _ = await begin(h, broker)
    configured_window(h, (10, 0), (16, 30))
    move_clock(h, hour, minute)
    # P3 must fail here if v2 authorizes outside the gate, even if OMS would veto.
    decision = h.strategy.rpg_handoff_authorization(str(token), HandoffJournal(h.factory).read(token))
    assert decision["verdict"] == ("ready" if allowed else "expired")
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == ("placed" if allowed else "expired")
    assert len(h.adapter.opens) == int(allowed)


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_final_wire_guard_rechecks_configured_cutoff_inside_authorization_second(monkeypatch, broker):
    h = await runtime(monkeypatch, broker)
    token, _ = await begin(h, broker)
    configured_window(h, (9, 30), (14, 0))
    move_clock(h, 13, 59, 59)
    h.clock[0] += timedelta(microseconds=500_000)
    original = h.service._finalize_v2_entry_quantity

    def cross_cutoff(event, intent):
        result = original(event, intent)
        h.clock[0] += timedelta(milliseconds=500)
        return result

    monkeypatch.setattr(h.service, "_finalize_v2_entry_quantity", cross_cutoff)
    await feedback(h)
    assert not h.adapter.opens
    assert HandoffJournal(h.factory).read(token)["phase"] == "refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("tagged", [True, False])
async def test_existing_oms_dedup_refuses_another_live_buy_despite_generation_filter(monkeypatch, broker, tagged):
    h = await runtime(monkeypatch, broker)
    with h.factory() as session:
        old = session.scalar(select(BrokerOrder))
        if not tagged:
            old.payload = {k: v for k, v in old.payload.items() if k != "rpg_resting_generation"}
            h.state.resting_schwab_generation = h.state.resting_webull_generation = ""
        session.add(BrokerOrder(
            id=uuid4(), strategy_id=old.strategy_id, broker_account_id=old.broker_account_id,
            intent_id=old.intent_id, client_order_id="CONTROLLED-other-live-buy",
            broker_order_id="CONTROLLED-other-parent", symbol=old.symbol, side="buy",
            quantity=Decimal(2), order_type="STOP_LIMIT", time_in_force="day", status="accepted",
            payload={**old.payload, "rpg_resting_generation": "CONTROLLED-other-generation", "stop_price": "9.99"},
        ))
        session.commit()
    token, _ = await begin(h, broker)
    await feedback(h)
    assert not h.adapter.opens
    # This is the existing OMS dedup backstop, not an additional RPG target rule.
    assert len(h.adapter.cancels) == len(h.adapter.reads) == 1
    assert HandoffJournal(h.factory).read(token)["phase"] == "refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_p4_wrong_dispatch_token_refuses_submitting_replacement(monkeypatch, broker):
    h = await runtime(monkeypatch, broker)
    token, _ = await begin(h, broker)
    journal = HandoffJournal(h.factory)
    # Controlled second ticket: both jobs are submitting, but only the other
    # token owns this serial dispatch. No second broker response is invented.
    other = journal.prepare(replace(h.old, client_order_id="CONTROLLED-other-dispatch"),
        slot="first", segment_id=h.state.fanout_segment_id, now=h.clock[0].timestamp())
    journal.change(other, journal.read(other)["revision"], phase="submitting")
    original = h.service._finalize_v2_entry_quantity

    def wrong_dispatch(event, intent):
        result = original(event, intent)
        session = object_session(intent)
        assert journal.read(token, session=session)["phase"] == journal.read(other, session=session)["phase"] == "submitting"
        assert event.payload.metadata["rpg_handoff_token"] == str(token)
        h.service._rpg_dispatch_token = str(other)
        return result

    # Move dispatch ownership after the entry guard but before the wire guard.
    # The earlier external-retry guard must not mask P4's final check.
    monkeypatch.setattr(h.service, "_finalize_v2_entry_quantity", wrong_dispatch)
    await feedback(h)
    assert not h.adapter.opens
    assert journal.read(token)["phase"] == "refused"
    assert "rpg_unclaimed_or_expired_replacement" in journal.read(token)["replacement_reasons"]


@pytest.mark.parametrize("broker", ["schwab", "webull"])
def test_p8_dataclass_requires_terminal_even_for_cancelled_empty_label(broker):
    request, body, decode = recorded(broker)
    actual = decode(request, body)
    assert actual.can_replace and actual.terminal_cancel
    # An inconsistent helper value is synthetic, not an observed broker response.
    assert not replace(actual, terminal_cancel=False).can_replace


def test_rpg_flag_defaults_off_and_exact_environment_name_enables(monkeypatch):
    key = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_REPRICE_HANDOFF_ENABLED"
    monkeypatch.delenv(key, raising=False)
    assert not Settings(_env_file=None).strategy_schwab_1m_v2_atr_reprice_handoff_enabled
    monkeypatch.setenv(key, "true")
    assert Settings(_env_file=None).strategy_schwab_1m_v2_atr_reprice_handoff_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_rpg_flag_off_new_reprice_retains_legacy_cancel_then_next_pass(monkeypatch, slot):
    h = await runtime(monkeypatch, "schwab", slot=slot)
    h.service.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled = False
    track = (lambda: h.strategy._cw_v2_resting_track(h.state, None)) if slot == "first" else (
        lambda: h.strategy._cw_v2_reclaim_resting_track(h.state))
    track()
    cancel, = h.strategy.drain_pending_intents()
    mirror, = h.strategy.drain_webull_direct_intents()
    assert cancel.intent_type == mirror.intent_type == "cancel"
    assert "atr_reprice" not in cancel.metadata and "atr_reprice" not in mirror.metadata
    assert not h.strategy._rpg_entry_owned(h.state)
    track()
    opening, = h.strategy.drain_pending_intents()
    assert opening.intent_type == "open"
    assert not HandoffJournal(h.factory).jobs()


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("working", [False, True])
async def test_switch_off_leaves_inflight_journal_dormant(monkeypatch, broker, working):
    h = await runtime(monkeypatch, broker)
    if working:
        h.adapter.override = AtrBuyReadback("working", "CONTROLLED pending cancel", Decimal(0))
    token, _ = await begin(h, broker)
    journal = HandoffJournal(h.factory)
    before = journal.read(token)
    h.service.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled = False
    h.service.__dict__.pop("_atr_reprice_controller")
    h.strategy._rpg_handoffs.clear()
    h.strategy._rpg_feedback_applied.clear()
    await feedback(h)
    for _ in range(2):
        await h.service._rpg_advance(token)
        await feedback(h)
    assert journal.read(token) == before
    assert len(h.adapter.cancels) == 1
    assert not h.strategy._rpg_entry_owned(h.state)
    assert not h.adapter.opens
