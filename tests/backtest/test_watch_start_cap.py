"""Pin the #618/#619 per-symbol watch-start cap in the REPLAY engine (built 2026-07-31).

⭐ WHY THESE EXIST. The replay used to force `cw_armed_segment_safety_enabled=False`, on the
reasoning that the flag was only the boot-hold the live bot releases. #618/#619 gave that same flag
a second, permanent meaning -- the per-symbol watch-start cap -- and the force-off silently deleted
it from every backtest. It fired 76 times in the live v2 log on 2026-07-30, so the replay was
modelling a strictly more permissive bot than the one we trade.

These tests pin BOTH the value of the flag in the replay's config and the BEHAVIOUR it buys, so the
same drift cannot recur silently.
"""

from __future__ import annotations

from project_mai_tai.backtest.replay import (
    LIVE_LOCKED,
    REPLAY_FORCED,
    ReplayStrategy,
    build_replay_settings,
)
from project_mai_tai.backtest.watch_start import WatchWindow, build_windows, watch_start_for

_MIN = 60_000
_ARM_BAR_TS = 1_785_400_000_000  # arbitrary fixed epoch ms; no wall-clock reads in tests


def _strategy(watch_start_ms: int | None) -> ReplayStrategy:
    return ReplayStrategy(build_replay_settings(), watch_start_ms=watch_start_ms)


def _arm(strat: ReplayStrategy, symbol: str, arm_bar_ts: int) -> object:
    st = strat.watchlist_state(symbol)
    st.cw_armed = True
    st.cw_arm_bar_ts = arm_bar_ts
    st.cw_entries_this_flip = 0
    return st


# --------------------------------------------------------------------------- config pins

def test_replay_no_longer_forces_the_safety_flag_off() -> None:
    """REPLAY_FORCED must not carry the flag at all -- that override is what caused the drift."""
    assert "strategy_schwab_1m_v2_cw_armed_segment_safety_enabled" not in REPLAY_FORCED


def test_live_locked_pins_the_safety_flag_true() -> None:
    """Off-VPS / CI has no env, so the fallback must encode the LIVE value (env carries true)."""
    assert LIVE_LOCKED["strategy_schwab_1m_v2_cw_armed_segment_safety_enabled"] is True
    assert build_replay_settings().strategy_schwab_1m_v2_cw_armed_segment_safety_enabled is True


def test_env_still_wins_over_the_fallback() -> None:
    """LIVE_LOCKED is a FALLBACK (#592). An explicit value must still beat it, or we have merely
    swapped one silent override for another."""
    s = build_replay_settings(strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=False)
    assert s.strategy_schwab_1m_v2_cw_armed_segment_safety_enabled is False


def test_boot_hold_stays_released() -> None:
    """Turning the flag on must NOT resurrect boot-hold -- that is still a modelling choice."""
    assert _strategy(None)._entries_held is False


# --------------------------------------------------------------------------- behaviour pins

def test_flip_predating_watch_start_is_capped() -> None:
    """The APLX/SNDG shape: flip at 09:16, symbol joined the watchlist 09:38 -> no entry."""
    strat = _strategy(_ARM_BAR_TS + 22 * _MIN)   # joined 22 min AFTER the flip bar
    st = _arm(strat, "APLX", _ARM_BAR_TS)
    assert strat.cap_reconstructed_segment("APLX") is True
    assert st.cw_entries_this_flip == strat._cw_v2_max_entries_per_flip  # segment used up


def test_flip_after_watch_start_is_untouched() -> None:
    """A flip we actually watched happen must still be tradeable -- the cap is not a blanket ban."""
    strat = _strategy(_ARM_BAR_TS - 5 * _MIN)    # joined 5 min BEFORE the flip bar
    st = _arm(strat, "SNDG", _ARM_BAR_TS)
    assert strat.cap_reconstructed_segment("SNDG") is False
    assert st.cw_entries_this_flip == 0


def test_boundary_is_inclusive_fail_closed() -> None:
    """⛔ `<=`, not `<`: a bar ts is the bar's OPEN, so joining exactly at it means we did NOT see
    that bar open. Live comment calls this out explicitly; pin it or a refactor will 'tidy' it."""
    strat = _strategy(_ARM_BAR_TS)
    _arm(strat, "IRE", _ARM_BAR_TS)
    assert strat.cap_reconstructed_segment("IRE") is True


def test_disabling_the_flag_restores_the_old_permissive_behaviour() -> None:
    """THE MUTATION TEST. With the flag off -- the state the replay used to force -- the very
    segment the live bot suppresses sails through. This is what the bug looked like."""
    settings = build_replay_settings(
        strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=False
    )
    strat = ReplayStrategy(settings, watch_start_ms=_ARM_BAR_TS + 22 * _MIN)
    st = _arm(strat, "APLX", _ARM_BAR_TS)
    assert strat.cap_reconstructed_segment("APLX") is False
    assert st.cw_entries_this_flip == 0   # tradeable -- exactly the entry live refuses


def test_replay_boot_ms_is_repointed_to_the_window_start() -> None:
    """⛔ THE LANDMINE GUARD. `_boot_ms` is wall-clock-NOW in the live strategy. Replaying a PAST
    day leaves that reference AFTER the whole session, so `arm_bar_ts <= watch_start` would hold for
    every segment and cap the day to ZERO entries -- a silent, total kill of the engine that no
    existing golden test would notice. `replay_symbol_day` must re-point it at the window start.

    Pinned by reading the source rather than running a full replay so the guard stays hermetic and
    cannot be defeated by a fixture that happens to have no arms.
    """
    import inspect

    from project_mai_tai.backtest import replay as replay_mod

    src = inspect.getsource(replay_mod.replay_symbol_day)
    assert "_boot_ms" in src, (
        "replay_symbol_day no longer re-points _boot_ms; with the #618 cap enabled this silently "
        "caps every segment of a historical replay to zero entries"
    )
    assert "int(start.timestamp() * 1000)" in src


def test_watch_start_none_falls_back_to_boot() -> None:
    """No scanner rows for the symbol => pre-07-30 behaviour (compare against process boot),
    never 'unknown means allow'."""
    strat = _strategy(None)
    strat._boot_ms = _ARM_BAR_TS + _MIN
    _arm(strat, "GMEX", _ARM_BAR_TS)
    assert strat.cap_reconstructed_segment("GMEX") is True


# --------------------------------------------------------------------------- window reconstruction

def test_flickering_confirm_feed_uses_the_current_membership() -> None:
    """SNDG confirmed/faded/re-confirmed three times inside three minutes on 07-30. The bot
    re-stamps watch-start on every re-join, so an arm after the LAST re-confirm must measure
    against THAT join, not the first one of the day."""
    events = [
        ("CONFIRM", 1_000), ("FADE", 2_000),
        ("CONFIRM", 3_000), ("RETENTION_DROP", 4_000),
        ("CONFIRM", 5_000),
    ]
    windows = build_windows(events)
    assert windows == [WatchWindow(1_000, 2_000), WatchWindow(3_000, 4_000), WatchWindow(5_000, None)]
    assert watch_start_for(windows, 5_500) == 5_000      # inside the live window
    assert watch_start_for(windows, 3_500) == 3_000      # inside the middle window


def test_arm_in_a_gap_uses_the_prior_window_not_none() -> None:
    """An arm between a FADE and the re-CONFIRM must NOT read as 'never watched' -- that would
    exempt the segment from the cap instead of tightening it."""
    windows = build_windows([("CONFIRM", 1_000), ("FADE", 2_000), ("CONFIRM", 5_000)])
    assert watch_start_for(windows, 3_000) == 1_000


def test_arm_before_any_confirm_is_none() -> None:
    windows = build_windows([("CONFIRM", 5_000)])
    assert watch_start_for(windows, 1_000) is None


def test_duplicate_confirm_does_not_restart_the_clock() -> None:
    """A CONFIRM while already a member means the symbol never left; restarting would wrongly move
    watch-start forward and over-cap."""
    windows = build_windows([("CONFIRM", 1_000), ("CONFIRM", 2_000), ("FADE", 3_000)])
    assert windows == [WatchWindow(1_000, 3_000)]
