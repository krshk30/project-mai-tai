"""LPCN recorded bar/timing; quote inputs are price proxies, not decision-cache evidence."""
from dataclasses import replace
from decimal import Decimal

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from tests.unit.test_v2_flip_owned_first_entry import _strategy, _book, _leg, PRIMARY


WATCH = 1791376769032  # LPCN re-add 2026-10-07 12:39:29.032 UTC.
BUY = 1791376803325
BAR = OHLCVBar(1791376740000, 2.83, 2.97, 2.81, 2.96, 609033)


def lpcn(*, enabled=True, eh=True, closes=0):
    strategy, clock, identities, owners = _strategy(dual=True, retry_one=True)
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_flip_enabled = enabled
    strategy.settings.strategy_schwab_1m_v2_retry_one_max_retries = 0
    strategy.settings.strategy_schwab_1m_v2_resting_buy_round_up_enabled = True
    strategy._resting_trigger_offset_pct = 0.5
    strategy._boot_ms = 1791371649241
    strategy._entries_held = False
    strategy._resting_session_is_eh = lambda now=None: eh
    strategy._entry_window_closed_for_session = lambda: False
    clock[0] = WATCH
    state = strategy.watchlist_state("LPCN")
    state.atr_state = "short"
    state.atr_short_flip_bar_ts = 1791374580000
    state.retry_one_segment_id = state.atr_short_flip_bar_ts
    state.retry_one_closes_in_segment = closes
    bot = object.__new__(SchwabV2BotService)
    bot.strategy = strategy
    bot._watch_start_ms = {"LPCN": WATCH}
    strategy._cw_armed_segment_safety_enabled = True
    bot._cap_reconstructed_segment("LPCN", stage="db-seed")
    state.bars.append(BAR)
    clock[0] = BUY
    _book(strategy, clock, "LPCN")
    return strategy, state, clock, identities, owners


def buy(strategy, state, *, phase="live"):
    state.atr_state = "long"
    strategy._cw_v2_track(state, {"flip": "BUY", "flip_level": 2.9535,
                                  "state": "long", "observation_phase": phase})


def crossing(clock, ask=2.97):
    return Quote(symbol="LPCN", last_price=ask, ask_price=ask, bid_price=2.96,
                 quote_time_ms=clock[0])


@pytest.mark.parametrize("eh", [True, False])
def test_recorded_lpcn_bar_close_after_readd_first_reactive_quote_proxy_emits_both_legs_once(eh):
    strategy, state, clock, _, owners = lpcn(eh=eh)
    buy(strategy, state)
    draft = strategy.on_quote("LPCN", crossing(clock))
    assert draft is not None
    mirror = strategy.drain_webull_fanout_intents()
    assert len(mirror) == 1
    assert draft.metadata["cw_entry_slot"] == mirror[0].metadata["cw_entry_slot"] == "first"
    assert draft.metadata["fanout_slot_id"] == mirror[0].metadata["fanout_slot_id"]
    assert Decimal(draft.metadata["limit_price"]) <= Decimal(draft.metadata["entry_price"]) * Decimal("1.005")
    assert draft.quantity == 202 and mirror[0].quantity == 101
    assert owners[-1][0].phase == "awaiting_fill"
    assert strategy.on_quote("LPCN", crossing(clock)) is None
    assert strategy.drain_webull_fanout_intents() == []
    assert state.cw_resting_taken is True


def test_recorded_lpcn_flag_off_retains_seed_cap_and_strict_first_rest_rule():
    strategy, state, clock, _, _ = lpcn(enabled=False)
    buy(strategy, state)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert strategy.on_quote("LPCN", crossing(clock)) is None


@pytest.mark.parametrize("phase", ["replay", "db-seed", "rest-warmup"])
def test_lpcn_historical_delivery_never_turns_seed_flip_into_a_live_first_entry(phase):
    strategy, state, clock, _, _ = lpcn()
    buy(strategy, state, phase=phase)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert strategy.on_quote("LPCN", crossing(clock)) is None


def test_pre_watch_closed_bar_stays_capped_ftft_class_control():
    strategy, state, clock, _, _ = lpcn()
    state.bars[-1] = replace(BAR, timestamp_ms=BAR.timestamp_ms - 60000)
    buy(strategy, state)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert strategy.on_quote("LPCN", crossing(clock)) is None


def test_lpcn_recorded_bar_with_later_watch_boundary_cannot_become_a_fresh_buy():
    strategy, state, clock, _, _ = lpcn()
    # Boundary counterfactual keeps the recorded candle fresh but closes it before the watch.
    state.cw_seed_cap_watch_start_ms = BAR.timestamp_ms + 60001
    buy(strategy, state)
    assert not state.slotclear_fresh_buy_bar_ms
    assert state.cw_resting_taken and state.cw_reclaim_taken


@pytest.mark.parametrize("blocker", ["closed", "held", "working", "unknown", "stale_book", "boot", "gap", "line"])
def test_seed_cap_is_not_permission_to_release_real_or_unknown_ownership(blocker):
    strategy, state, clock, _, _ = lpcn(closes=int(blocker == "closed"))
    if blocker == "held":
        _book(strategy, clock, "LPCN", _leg(PRIMARY, "owned-row", entered_ms=WATCH))
    elif blocker == "working":
        state.resting_active = True
    elif blocker == "unknown":
        state.flip_owner_phase = "unknown"
    elif blocker == "stale_book":
        state.flip_owner_evidence_at_ms = WATCH - 60000
    elif blocker == "boot":
        strategy._entries_held = True
    elif blocker == "gap":
        strategy.gap_hold_active = lambda symbol: True
    elif blocker == "line":
        strategy.line_buy_ready = lambda symbol: False
    buy(strategy, state)
    assert state.slotclear_fresh_buy_bar_ms == 0
    assert state.cw_resting_taken and state.cw_reclaim_taken


def test_lpcn_fresh_exception_never_chases_above_half_percent():
    strategy, state, clock, _, _ = lpcn()
    buy(strategy, state)
    assert strategy.on_quote("LPCN", crossing(clock, ask=3.00)) is None
    assert strategy.drain_webull_fanout_intents() == []


def test_restart_boundary_uses_closed_live_bar_not_replayed_bar_or_open_time():
    strategy, state, clock, _, _ = lpcn()
    strategy._boot_ms = WATCH
    buy(strategy, state)
    assert strategy.on_quote("LPCN", crossing(clock)) is not None


def test_slotclear1_fail_safe_default_off():
    assert Settings().strategy_schwab_1m_v2_slotclear_fresh_flip_enabled is False


@pytest.mark.parametrize("case", [
    # The actual MTEN cap watch precedes the supplied 12:34 add; use our log's boundary.
    ("MTEN", 1791376156698, 1791376080000, 1791376621140,
     OHLCVBar(1791376560000, 1.33, 1.50, 1.33, 1.47, 1009309), 1.4524, 1.46),
    ("JAGX", 1791295704944, 1791293580000, 1791296163048,
     OHLCVBar(1791296100000, 6.77, 7.23, 6.5801, 7.13, 591475), 6.8977, 6.94),
])
def test_recorded_mten_jagx_live_flip_with_in_band_quote_proxy_drafts_both_accounts(case):
    symbol, watch, sell, seen, bar, level, proxy = case
    strategy, clock, _, _ = _strategy(dual=True, retry_one=True)
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_flip_enabled = True
    strategy.settings.strategy_schwab_1m_v2_resting_buy_round_up_enabled = True
    strategy.settings.strategy_schwab_1m_v2_retry_one_max_retries = 0
    strategy._resting_trigger_offset_pct = 0.5
    strategy._boot_ms = watch - 60000
    strategy._entries_held = False
    strategy._entry_window_closed_for_session = lambda: False
    clock[0] = watch
    state = strategy.watchlist_state(symbol)
    state.atr_state, state.atr_short_flip_bar_ts = "short", sell
    state.retry_one_segment_id = sell
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot._watch_start_ms = strategy, {symbol: watch}
    strategy._cw_armed_segment_safety_enabled = True
    bot._cap_reconstructed_segment(symbol, stage="db-seed")
    state.bars.append(bar)
    clock[0] = seen
    _book(strategy, clock, symbol)
    strategy._cw_v2_track(state, {"flip": "BUY", "state": "long", "flip_level": level,
                                  "observation_phase": "live"})
    draft = strategy.on_quote(symbol, Quote(symbol, proxy, proxy, proxy, seen, 0))
    assert draft is not None and draft.metadata["cw_entry_slot"] == "first"
    assert len(strategy.drain_webull_fanout_intents()) == 1
    assert strategy.on_quote(symbol, Quote(symbol, proxy, proxy, proxy, seen, 0)) is None


def test_recorded_ftft_stale_sell_stays_capped_with_slotclear_flag_on():
    from tests.unit.test_v2_flip_owned_first_entry import _seed_cap_short, _signal
    strategy, clock, _, _ = _strategy(dual=True, retry_one=True)
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_flip_enabled = True
    watch = 1789570184969
    clock[0] = watch + 78000
    state = _seed_cap_short(strategy, clock, "FTFT", sell_flip_bar_ts=1789568100000,
                            watch_start_ms=watch)
    _book(strategy, clock, "FTFT")
    strategy._cw_v2_resting_track(state, _signal(state="short"))
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert strategy.drain_pending_intents() == []


@pytest.mark.parametrize("eh", [False, True])
def test_lpcn_boundary_fresh_sell_arms_first_rest_but_not_a_stale_sell(eh):
    strategy, state, clock, _, _ = lpcn(eh=eh, closes=1)
    state.atr_short_flip_bar_ts = BAR.timestamp_ms
    strategy._cw_v2_track(state, {"flip": "SELL", "state": "short",
                                 "observation_phase": "live"})
    assert not state.cw_resting_taken and not state.cw_reclaim_taken
    assert state.retry_one_segment_id == BAR.timestamp_ms
    assert state.retry_one_closes_in_segment == 0
    state.atr_state, state.atr_trail, state.atr_state_age = "short", 2.9535, 3
    strategy._cw_v2_resting_track(state, {"flip": None, "state": "short", "trail": 2.9535})
    assert state.resting_active
    assert state.slotclear_fresh_buy_bar_ms == 0


def test_fresh_buy_permission_is_revoked_on_scanner_removal():
    strategy, state, clock, _, _ = lpcn()
    buy(strategy, state)
    assert state.slotclear_fresh_buy_bar_ms
    strategy._remove_waiting_buy(state, reason="watchlist-removed")
    assert state.slotclear_fresh_buy_bar_ms == 0
    assert strategy.on_quote("LPCN", crossing(clock)) is None


def test_fresh_buy_cannot_emit_when_liquidity_or_window_changes_after_the_flip():
    strategy, state, clock, _, _ = lpcn()
    buy(strategy, state)
    strategy._liquidity_floor_ok = lambda state: False
    assert strategy.on_quote("LPCN", crossing(clock)) is None
    strategy._liquidity_floor_ok = lambda state: True
    strategy._entry_window_closed_for_session = lambda: True
    assert strategy.on_quote("LPCN", crossing(clock)) is None


@pytest.mark.parametrize("webull", [False, True])
@pytest.mark.parametrize("ask", [2.97, 3.00])
def test_recorded_lpcn_existing_oms_reactive_pricer_preserves_half_percent_cap(monkeypatch, webull, ask):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    import project_mai_tai.oms.service as oms_module
    from tests.unit.test_oms_v2_eh_reactive_entry import _oms, _v2_open

    strategy, state, clock, _, _ = lpcn()
    buy(strategy, state)
    draft = strategy.on_quote("LPCN", crossing(clock))
    mirror = strategy.drain_webull_fanout_intents()[0]
    selected = mirror if webull else draft
    now = datetime.fromtimestamp(clock[0] / 1000, UTC)
    monkeypatch.setattr(oms_module, "utcnow", lambda: now)
    monkeypatch.setattr(oms_module, "_is_regular_market_session", lambda *args: False)
    monkeypatch.setattr(oms_module, "_extended_hours_session", lambda *args: "AM")
    service = _oms(oms_v2_eh_entry_enabled=True)
    service._latest_quotes_by_symbol["LPCN"] = {"ask": Decimal(str(ask)), "received_at": now}
    event = _v2_open({**selected.metadata, "order_type": "limit", "session": "AM"}, symbol="LPCN")
    if webull:
        event.payload.broker_account_name = "paper:orb"
    intent = SimpleNamespace(id=None, status="created", payload={})
    with service.session_factory() as session:
        result = service._apply_v2_eh_reactive_entry(session=session, event=event, intent=intent)
    if ask == 3.00:
        assert result is not None
        assert event.payload.metadata["abandon_reason_code"] == "ASK_PAST_CROSS_CAP"
    else:
        assert result is None
        assert Decimal(event.payload.metadata["limit_price"]) <= Decimal(event.payload.metadata["entry_price"]) * Decimal("1.005")
