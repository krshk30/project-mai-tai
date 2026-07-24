"""Reactive-entry EXTENDED-HOURS live-bar guard (#528 mirror, 2026-07-24 Phase B / P-B1).

The reactive break fires on a live QUOTE, but the ARM (cw_trigger) is built from bar highs in
`_cw_v2_track`, which runs on every bar incl. warmup replays. Pre-market, a replayed prior-session
BUY flip can arm a stale trigger a live quote then breaks -> an entry on an hours-old level (the #528
trap). This guard suppresses an EH reactive entry unless the driving bar is within
`_reactive_max_bar_age_ms` of wall-clock. RTH is byte-identical (guard skipped in regular hours).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

_ET = ZoneInfo("America/New_York")
# Wall-clock "now" for the guard's bar-age math: 08:00 ET premarket = extended hours.
EH_NOW_MS = int(datetime(2026, 7, 10, 8, 0, tzinfo=_ET).timestamp() * 1000)
# A regular-session instant (11:00 ET) for the RTH byte-identical control.
RTH_NOW_MS = int(datetime(2026, 7, 10, 11, 0, tzinfo=_ET).timestamp() * 1000)


def _strat(**overrides):
    kwargs = {
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
    }
    kwargs.update(overrides)
    return SchwabV2Strategy(Settings(**kwargs))


def _bar(high: float, *, vol: int = 10_000, low: float | None = None, ts: int = 0) -> OHLCVBar:
    return OHLCVBar(timestamp_ms=ts, open=high - 0.1, high=high,
                    low=high - 0.2 if low is None else low, close=high - 0.05, volume=vol)


def _sig(flip=None, *, flip_level=None, trail=9.5, loss=0.5, state="long", age=1) -> dict:
    return {"touch": False, "touch_price": None, "flip": flip, "flip_level": flip_level,
            "trail": trail, "loss": loss, "state": state, "state_age": age}


def _quote(px: float, *, ts: int) -> Quote:
    return Quote("TEST", px - 0.01, px + 0.01, px, ts, 0)


def _feed(strat, state, bar, sig):
    state.bars.append(bar)
    strat._cw_v2_track(state, sig)


def _arm_to_watch(strat, state, *, last_bar_ts: int):
    """Arm (BUY flip high 12.0, flip_level 9.5) + 2 trigger bars -> trigger 12.0, then one watch-phase
    bar (high 11.5, below trigger so no bar-path entry) stamped `last_bar_ts` = the driving bar."""
    _feed(strat, state, _bar(12.0, ts=1), _sig(flip="BUY", flip_level=9.5))
    _feed(strat, state, _bar(10.0, ts=2), _sig())
    _feed(strat, state, _bar(11.0, ts=3), _sig())
    _feed(strat, state, _bar(11.5, ts=last_bar_ts), _sig())  # watch phase; resets forming-bar low
    assert state.cw_trigger == 12.0 and state.cw_armed is True


# --------------------------------------------------------------------------- EH guard

def test_eh_stale_bar_suppresses_reactive_entry(monkeypatch):
    strat = _strat()
    state = strat.watchlist_state("TEST")
    monkeypatch.setattr(strat, "_now_ms", lambda: EH_NOW_MS)
    # driving bar is ~1h old (a warmup-replayed prior-session bar) -> stale.
    _arm_to_watch(strat, state, last_bar_ts=EH_NOW_MS - 3_600_000)
    # a live premarket quote breaks the (stale) trigger, but the guard blocks the EH fire.
    assert strat._cw_v2_quote(state, _quote(12.5, ts=EH_NOW_MS)) is None
    assert state.cw_entries_this_flip == 0 and state.cw_v2_emit_claimed is False


def test_eh_live_bar_fires_reactive_entry(monkeypatch):
    strat = _strat()
    state = strat.watchlist_state("TEST")
    monkeypatch.setattr(strat, "_now_ms", lambda: EH_NOW_MS)
    # driving bar is 10s old -> live; the EH reactive entry fires.
    _arm_to_watch(strat, state, last_bar_ts=EH_NOW_MS - 10_000)
    draft = strat._cw_v2_quote(state, _quote(12.5, ts=EH_NOW_MS))
    assert draft is not None
    assert draft.side == "buy" and draft.intent_type == "open"
    assert state.cw_entries_this_flip == 1 and state.cw_v2_emit_claimed is True


def test_rth_stale_bar_still_fires_byte_identical(monkeypatch):
    """RTH control: even a stale driving bar fires in regular hours — the guard is EH-only, so RTH
    behaviour is byte-identical to before P-B1."""
    strat = _strat()
    state = strat.watchlist_state("TEST")
    monkeypatch.setattr(strat, "_now_ms", lambda: RTH_NOW_MS)
    _arm_to_watch(strat, state, last_bar_ts=RTH_NOW_MS - 3_600_000)  # stale, but RTH
    draft = strat._cw_v2_quote(state, _quote(12.5, ts=RTH_NOW_MS))
    assert draft is not None
    assert state.cw_entries_this_flip == 1


def test_eh_guard_threshold_is_180s_default():
    """Pin the default max-bar-age; mutate the default and this turns red."""
    assert Settings().strategy_schwab_1m_v2_cw_v2_reactive_entry_max_bar_age_secs == 180.0
    assert _strat()._reactive_max_bar_age_ms == 180_000
