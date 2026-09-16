"""`atr_short_flip_bar_ts` — the SELL-flip bar that opened the current short state.

⭐ WHY (2026-09-16, FTFT, live). The reconstructed-segment cap could only see a segment the replay
ARMED (a BUY flip). A SHORT state rebuilt by the same-session db-seed from a SELL that happened
34 minutes before the symbol re-joined the watchlist carried no timestamp the cap could compare, so
the resting path rested on it and the Webull mirror filled. This stamp is what the cap compares.
"""
from __future__ import annotations

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

MIN = 60_000
# 2026-09-16 10:00 ET = 14:00Z; a mid-session base so the 04:00 ET session anchor is not crossed.
BASE_MIN = 1789568400000 // MIN


def _strat() -> SchwabV2Strategy:
    return SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_atr_flip_enabled=True,
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
    ))


def _bar(ts_min: int, hi: float, lo: float, close: float) -> OHLCVBar:
    return OHLCVBar(
        timestamp_ms=ts_min * MIN, open=(hi + lo) / 2, high=hi, low=lo,
        close=close, volume=50_000,
    )


def _feed(strat: SchwabV2Strategy, sym: str, bars: list[OHLCVBar]):
    st = strat.watchlist_state(sym)
    for b in bars:
        st.bars.append(b)
        strat._update_atr_state(st, b)
    return st


def _calm(n: int, start_min: int) -> list[OHLCVBar]:
    return [_bar(start_min + i, 4.25, 4.15, 4.20) for i in range(n)]


def test_sell_flip_stamps_its_bar_and_buy_flip_clears_it() -> None:
    strat = _strat()
    st = _feed(strat, "FTFT", _calm(12, BASE_MIN))
    assert st.atr_state == "long"
    assert st.atr_short_flip_bar_ts == 0

    # A crash bar closes far below the trail -> SELL flip on THIS bar.
    crash_min = BASE_MIN + 12
    st = _feed(strat, "FTFT", [_bar(crash_min, 4.20, 3.00, 3.05)])
    assert st.atr_state == "short"
    assert st.atr_short_flip_bar_ts == crash_min * MIN

    # Staying short ratchets the trail but keeps the ORIGINAL SELL bar.
    st = _feed(strat, "FTFT", [_bar(crash_min + 1, 3.10, 2.95, 3.00)])
    assert st.atr_state == "short"
    assert st.atr_short_flip_bar_ts == crash_min * MIN

    # A rip closes above the short trail -> BUY flip clears the stamp.
    st = _feed(strat, "FTFT", [_bar(crash_min + 2, 6.00, 3.00, 5.90)])
    assert st.atr_state == "long"
    assert st.atr_short_flip_bar_ts == 0


def test_session_reset_clears_the_stamp() -> None:
    strat = _strat()
    st = _feed(strat, "FTFT", _calm(12, BASE_MIN))
    _feed(strat, "FTFT", [_bar(BASE_MIN + 12, 4.20, 3.00, 3.05)])
    assert st.atr_state == "short" and st.atr_short_flip_bar_ts > 0
    strat._apply_session_anchor_reset(st, st.atr_session_anchor_ms + 24 * 60 * MIN)
    assert st.atr_state is None
    assert st.atr_short_flip_bar_ts == 0
