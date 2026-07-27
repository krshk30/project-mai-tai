"""P1.3 + P1.4 armed-segment safety — the arm-bar-ts discriminator, boot-hold gate, and the
before/after cap acceptance test.

Asserts on STATE (cw_armed_segments dicts, _entries_held, draft-or-None), never on log narration.
The discriminator: a RECONSTRUCTED arm (arm_bar_ts < boot) is 'dangerous' while uncapped; a LIVE
post-boot flip (arm_bar_ts >= boot) is never dangerous — so the check runs continuously, race-free.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy
from project_mai_tai.market_data.schwab_v2_rest_client import Quote

_ET = ZoneInfo("America/New_York")
NON_ORB_MS = int(datetime(2026, 7, 10, 11, 0, tzinfo=_ET).timestamp() * 1000)  # 11:00 ET


def _strat(**overrides) -> SchwabV2Strategy:
    kwargs = {
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
    }
    kwargs.update(overrides)
    return SchwabV2Strategy(Settings(**kwargs))


def _safe(**overrides) -> SchwabV2Strategy:
    return _strat(strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=True, **overrides)


def _bar(high: float, *, ts: int, vol: int = 10_000, low: float | None = None) -> OHLCVBar:
    return OHLCVBar(timestamp_ms=ts, open=high - 0.1, high=high,
                    low=high - 0.2 if low is None else low, close=high - 0.05, volume=vol)


def _sig(flip=None, *, flip_level=None) -> dict:
    return {"touch": False, "touch_price": None, "flip": flip, "flip_level": flip_level,
            "trail": 9.5, "loss": 0.5, "state": "long", "state_age": 1}


def _quote(px: float, *, ts: int = NON_ORB_MS) -> Quote:
    return Quote("TEST", px - 0.01, px + 0.01, px, ts, 0)


def _arm(strat: SchwabV2Strategy, state, *, base_ts: int) -> None:
    """Arm a CW-v2 segment (BUY flip + 2 bars -> trigger 12.0, flip_level 9.5), stamping the arm at
    `base_ts` so tests control reconstructed (past ts) vs live (>= boot ts)."""
    steps = [(12.0, _sig(flip="BUY", flip_level=9.5)), (10.0, _sig()), (11.0, _sig())]
    for i, (high, sig) in enumerate(steps):
        state.bars.append(_bar(high, ts=base_ts + i))
        strat._cw_v2_track(state, sig)
    strat._cw_v2_track(state, _sig())  # watch phase
    assert state.cw_armed is True and state.cw_bars_waited == 2 and state.cw_trigger == 12.0


def test_flag_off_is_byte_identical() -> None:
    s = _strat()  # safety flag default off
    assert s._entries_held is False
    st = s.watchlist_state("TEST")
    _arm(s, st, base_ts=1)
    assert st.cw_arm_bar_ts == 0  # arm NOT stamped when the flag is off
    draft = s._cw_v2_quote(st, _quote(12.5))  # break of the 12.0 trigger, above flip 9.5
    assert draft is not None and draft.side == "buy"  # entry flows: no hold


def test_flag_on_boot_holds_all_entries() -> None:
    s = _safe()
    assert s._entries_held is True  # held on boot
    st = s.watchlist_state("TEST")
    _arm(s, st, base_ts=s._boot_ms + 10_000)  # LIVE arm (future ts)
    assert st.cw_arm_bar_ts >= s._boot_ms
    assert s._cw_v2_quote(st, _quote(12.5)) is None  # HELD — a breaking quote does NOT enter
    s._entries_held = False  # release
    assert s._cw_v2_quote(st, _quote(12.5)) is not None  # now it enters


def test_reconstructed_uncapped_is_dangerous() -> None:
    s = _safe()
    st = s.watchlist_state("TEST")
    _arm(s, st, base_ts=1)  # PAST ts (< boot) => reconstructed
    seg = s.cw_armed_segments()
    assert len(seg) == 1
    assert seg[0]["reconstructed"] is True
    assert seg[0]["dangerous"] is True  # entries=0 < max => the P1.3 target / boot-hold blocker
    assert any(x["dangerous"] for x in s.cw_armed_segments())  # release must NOT fire


def test_p13_cap_flips_dangerous_to_safe() -> None:
    """The before/after acceptance test — P1.3's marking makes a reconstructed segment safe."""
    s = _safe()
    st = s.watchlist_state("TEST")
    _arm(s, st, base_ts=1)
    assert s.cw_armed_segments()[0]["dangerous"] is True  # BEFORE the cap
    st.cw_entries_this_flip = s._cw_v2_max_entries_per_flip  # P1.3: mark reconstructed segment used
    seg = s.cw_armed_segments()[0]
    assert seg["capped"] is True and seg["dangerous"] is False  # AFTER the cap
    assert not any(x["dangerous"] for x in s.cw_armed_segments())  # release condition now met


def test_live_post_boot_flip_is_never_dangerous() -> None:
    """The discriminator: a legit live flip (arm_bar_ts >= boot) with entries=0 is NOT dangerous,
    so the continuous verify never false-holds on it."""
    s = _safe()
    st = s.watchlist_state("TEST")
    _arm(s, st, base_ts=s._boot_ms + 60_000)  # FUTURE ts => live
    seg = s.cw_armed_segments()[0]
    assert seg["reconstructed"] is False
    assert seg["entries_this_flip"] == 0 and seg["dangerous"] is False


def test_disarm_clears_arm_bar_ts() -> None:
    s = _safe()
    st = s.watchlist_state("TEST")
    _arm(s, st, base_ts=1)
    assert st.cw_arm_bar_ts != 0
    s._cw_v2_track(st, _sig(flip="SELL"))  # SELL flip disarms
    assert st.cw_armed is False and st.cw_arm_bar_ts == 0
    assert s.cw_armed_segments() == []  # no armed segments after disarm


# --------------------------------------------------------------- fossil-arm guard (the GMEX incident)
# 2026-07-27: GMEX confirmed 06:33 ET with only multi-week-old bars; the warmup replay found a BUY
# flip on a bar 54 DAYS old and armed off it. That arm read as reconstructed+uncapped => "dangerous",
# so the P1.3 boot-hold suppressed CW-v2 entries BOT-WIDE for 53 min until a manual restart. One
# stale symbol took every symbol down. The guard refuses to arm off a fossil bar in the first place.

_DAY_MS = 86_400_000


def _fossil_guarded(**overrides) -> SchwabV2Strategy:
    """Safety flag ON *and* the fossil-arm guard enabled at the recommended 24h. The guard ships
    DEFAULT-OFF (0) so it is byte-identical until the operator sets the env var, so these tests turn
    it on explicitly rather than relying on a default."""
    return _safe(strategy_schwab_1m_v2_cw_v2_arm_max_bar_age_secs=86400.0, **overrides)


def _try_arm(strat: SchwabV2Strategy, state, *, base_ts: int) -> None:
    """Same drive sequence as `_arm` but WITHOUT the did-it-arm assertion — used by the fossil tests,
    where refusing to arm is the expected outcome."""
    steps = [(12.0, _sig(flip="BUY", flip_level=9.5)), (10.0, _sig()), (11.0, _sig())]
    for i, (high, sig) in enumerate(steps):
        state.bars.append(_bar(high, ts=base_ts + i))
        strat._cw_v2_track(state, sig)
    strat._cw_v2_track(state, _sig())


def test_fossil_bar_does_not_arm() -> None:
    """A BUY flip on a bar older than the max age must NOT arm — the GMEX reproduction."""
    s = _fossil_guarded()
    st = s.watchlist_state("TEST")
    _try_arm(s, st, base_ts=s._boot_ms - 54 * _DAY_MS)  # 54 days old, exactly the GMEX case
    assert st.cw_armed is False          # never armed
    assert st.cw_arm_bar_ts == 0         # nothing stamped
    assert s.cw_armed_segments() == []   # => no "dangerous" segment to poison the boot-hold


def test_fossil_arm_does_not_poison_the_boot_hold() -> None:
    """The consequence that actually hurt: with no fossil segment, nothing can hold entries."""
    s = _fossil_guarded()
    st = s.watchlist_state("TEST")
    _try_arm(s, st, base_ts=s._boot_ms - 54 * _DAY_MS)
    assert not any(x["dangerous"] for x in s.cw_armed_segments())  # release CAN fire


def test_same_session_warmup_still_arms() -> None:
    """The guard must be generous: a legitimate same-session warmup replay (hours old, still < boot)
    arms normally and is still correctly flagged reconstructed."""
    s = _fossil_guarded()
    st = s.watchlist_state("TEST")
    _arm(s, st, base_ts=s._boot_ms - 15 * 3_600_000)  # 15h — a 19:00 boot replaying back to 04:00
    assert st.cw_armed is True
    seg = s.cw_armed_segments()
    assert len(seg) == 1 and seg[0]["reconstructed"] is True and seg[0]["dangerous"] is True


def test_live_flip_always_arms() -> None:
    """A live post-boot flip is never a fossil."""
    s = _fossil_guarded()
    st = s.watchlist_state("TEST")
    _arm(s, st, base_ts=s._boot_ms + 10_000)
    assert st.cw_armed is True
    assert s.cw_armed_segments()[0]["reconstructed"] is False


def test_guard_disabled_restores_pre_fix_behaviour() -> None:
    """Setting the max age to 0 disables the guard exactly (the rollback lever)."""
    s = _safe(strategy_schwab_1m_v2_cw_v2_arm_max_bar_age_secs=0)  # explicit OFF == the default
    st = s.watchlist_state("TEST")
    _try_arm(s, st, base_ts=s._boot_ms - 54 * _DAY_MS)
    assert st.cw_armed is True                       # arms again, as before the fix
    assert s.cw_armed_segments()[0]["dangerous"] is True  # and poisons the boot-hold, as before
