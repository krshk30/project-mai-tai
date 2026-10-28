"""T6: controlled scanner/owned-order inputs, never native cancel/fill proof.

Exercise the real service snapshot transition, strategy removal/cancel builder
and direct drains. Only external account/exclusion reads, wall clock and emitters
are controlled. No line/entry/removal/cancel guard is replaced with a PASS stub.
"""

from datetime import UTC, datetime

import pytest

from project_mai_tai.events import StrategyStateSnapshotEvent, StrategyStateSnapshotPayload
from project_mai_tai.services import schwab_1m_v2_bot as service
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar


ANCHOR = 1_793_174_400_000  # Oct28 04:00 ET, controlled session.
NOW = ANCHOR + 6 * 3_600_000  # RTH 10:00 ET.
SYMBOL = "GCTK"


class RecordingEmitter:
    def __init__(self):
        self.drafts = []

    async def emit(self, draft):
        self.drafts.append(draft)


def snapshot(symbols):
    event = StrategyStateSnapshotEvent(
        source_service="controlled-scanner",
        produced_at=datetime.fromtimestamp(NOW / 1000, UTC),
        payload=StrategyStateSnapshotPayload(watchlist=list(symbols)),
    )
    return {"data": event.model_dump_json()}


def fixture_bot(monkeypatch, *, population, mirror=True, removed_wait=False):
    class WallClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromtimestamp(NOW / 1000, UTC)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(service, "datetime", WallClock)
    monkeypatch.setattr(service, "_current_scanner_session_start_utc",
                        lambda: datetime.fromtimestamp(ANCHOR / 1000, UTC))
    bot = service.SchwabV2BotService(Settings(
        _env_file=None,
        strategy_schwab_1m_v2_keepwatch1_enabled=True,
        strategy_schwab_1m_v2_line_chart_restoration_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=mirror,
        strategy_schwab_1m_v2_account_name="paper:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="paper:orb" if mirror else "",
        strategy_schwab_1m_v2_country_route_enabled=False,
        strategy_schwab_1m_v2_removed_wait_clear_enabled=removed_wait,
        strategy_schwab_1m_v2_removed_wait_clear_rules_enabled=removed_wait,
    ))
    bot.strategy._now_ms = lambda: NOW
    # Explicit controlled external read results; never an installed flatness claim.
    monkeypatch.setattr(bot, "_fetch_open_positions", lambda: (
        {SYMBOL: 100} if population in {"held", "inflight"} else {}))
    monkeypatch.setattr(bot, "_schwab_ineligible_symbols", lambda: set())
    monkeypatch.setattr(bot, "_webull_ineligible_symbols", lambda: set())
    bot._apply_strategy_state_event(snapshot([SYMBOL]), max_watchlist=20)
    state = bot.strategy.watchlist_state(SYMBOL)
    state.position_qty = 100 if population in {"held", "inflight"} else 0
    state.position_qty_held = 100 if population == "held" else 0
    state.bars.append(OHLCVBar(timestamp_ms=NOW - 60_000,
        open=2.0, high=2.1, low=1.9, close=2.0, volume=20_000))
    state.atr_state = "short"
    state.atr_trail = 2.12
    state.cw_armed = True
    state.cw_arm_bar_ts = NOW - 120_000
    state.fanout_segment_id = NOW - 120_000
    state.flip_owner_opportunity_id = NOW - 120_000
    state.cw_resting_taken = True
    state.resting_active = True
    state.resting_is_broker_order = True
    state.resting_level = 2.10
    state.resting_trigger = 2.12
    state.resting_schwab_quantity = 100
    state.resting_schwab_generation = "controlled-existing-schwab-generation"
    state.webull_resting_active = mirror
    state.resting_webull_quantity = 200 if mirror else 0
    state.webull_resting_generation_id = "controlled-existing-webull-generation" if mirror else ""
    state.resting_webull_generation = "controlled-existing-webull-rest" if mirror else ""
    bot.intent_emitter = RecordingEmitter()
    bot.webull_intent_emitter = RecordingEmitter() if mirror else None
    return bot, state


@pytest.mark.asyncio
@pytest.mark.parametrize("population", ["flat", "held", "inflight"])
@pytest.mark.parametrize("removed_wait", [False, True])
async def test_scanner_drop_queues_and_drains_rth_broker_and_mirror_cancels(
        monkeypatch, population, removed_wait):
    bot, state = fixture_bot(monkeypatch, population=population, removed_wait=removed_wait)
    ledger = bot._line_sessions[SYMBOL]
    bars = tuple(state.bars)
    first_seen = dict(bot._keepwatch.first_seen_ms)
    owner = (state.flip_owner_opportunity_id, state.fanout_segment_id,
             state.cw_resting_taken, state.position_qty, state.position_qty_held)
    watchlist_before = set(bot._watchlist)
    bot._apply_strategy_state_event(snapshot([]), max_watchlist=20)
    assert bot._watchlist == (watchlist_before if population != "flat" else set())
    # Held/inflight exercises selected==_watchlist; cancellation must still occur.
    assert not bot._entry_watch_symbols and not bot._keepwatch.allows_entry(SYMBOL)
    assert SYMBOL in bot._observation_symbols()
    assert bot._keepwatch.first_seen_ms == first_seen
    assert bot._line_sessions[SYMBOL] is ledger and tuple(state.bars) == bars
    assert bot.strategy._symbol_states[SYMBOL] is state
    assert owner == (state.flip_owner_opportunity_id, state.fanout_segment_id,
                     state.cw_resting_taken, state.position_qty, state.position_qty_held)
    assert not state.cw_armed and not state.resting_active and not state.webull_resting_active
    primary = tuple(bot.strategy._pending_intents)
    mirror = tuple(bot.strategy._pending_webull_direct_intents)
    assert len(primary) == len(mirror) == 1
    for draft in (*primary, *mirror):
        assert (draft.symbol, draft.side, draft.intent_type) == (SYMBOL, "buy", "cancel")
        assert draft.metadata["resting_entry_cancel"] == "true"
        assert draft.metadata["reason"] == "watchlist-removed"
    assert primary[0].metadata["rpg_resting_generation"] == "controlled-existing-schwab-generation"
    assert mirror[0].metadata["webull_mirror_generation_id"] == "controlled-existing-webull-generation"
    await bot._drain_direct_strategy_intents()
    assert bot.intent_emitter.drafts == list(primary)
    assert bot.webull_intent_emitter.drafts == list(mirror)
    # A repeated full snapshot must not mint another cancel or any open order.
    bot._apply_strategy_state_event(snapshot([]), max_watchlist=20)
    await bot._drain_direct_strategy_intents()
    assert bot.intent_emitter.drafts == list(primary)
    assert bot.webull_intent_emitter.drafts == list(mirror)


@pytest.mark.asyncio
async def test_on_list_neighbor_keeps_existing_rest_without_cancel(monkeypatch):
    bot, state = fixture_bot(monkeypatch, population="flat")
    bot._apply_strategy_state_event(snapshot([SYMBOL]), max_watchlist=20)
    await bot._drain_direct_strategy_intents()
    assert state.cw_armed and state.resting_active and state.webull_resting_active
    assert bot._keepwatch.allows_entry(SYMBOL)
    assert not bot.intent_emitter.drafts and not bot.webull_intent_emitter.drafts


@pytest.mark.asyncio
async def test_schwab_only_rest_does_not_invent_a_webull_cancel(monkeypatch):
    bot, state = fixture_bot(monkeypatch, population="held", mirror=False)
    bot._apply_strategy_state_event(snapshot([]), max_watchlist=20)
    await bot._drain_direct_strategy_intents()
    assert not state.resting_active and not state.webull_resting_active
    assert len(bot.intent_emitter.drafts) == 1
    assert bot.intent_emitter.drafts[0].intent_type == "cancel"
    assert not bot.strategy._pending_webull_direct_intents
