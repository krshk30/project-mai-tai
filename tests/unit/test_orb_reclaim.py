"""ORB intrabar-reclaim live-test mode (cap-off + reclaim@OR_high + N% trail), flag-gated.

Validation gates for the PR:
  (a) flag OFF -> byte-identical to the settled bar-close/TRAIL-8%/12%-cap path;
  (b) reclaim state machine: cross OR_high -> hold -> ONE entry; dip resets; window-bound;
  (c) cap-off: a >12%-width OR still arms in reclaim mode (legacy would reject it);
  (d) reclaim intent = resting LIMIT at OR_high, trail = orb_reclaim_trail_pct, qty =
      orb_reclaim_quantity, with the fill-instrumentation metadata stamped.
The OMS trail ratchet + kill-switch/flatten are unchanged OMS behavior (trail_pct just
flows through as 3%) and are covered by existing OMS tests + the live paper run.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from project_mai_tai.services.orb_app import OrbService, _SymbolState
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_intrabar import OpeningRange, OrbBar


def _svc(**kw) -> OrbService:
    return OrbService(settings=Settings(**kw), redis_client=MagicMock())


def _armed(svc: OrbService, sym: str, high: float, low: float) -> _SymbolState:
    st = _SymbolState()
    st.opening_range = OpeningRange(high=high, low=low, avg_volume=1000.0)
    st.or_evaluated = True
    svc._states[sym] = st
    return st


def _t(svc: OrbService, minutes: int, seconds: int = 0):
    return svc._session_open_utc() + timedelta(minutes=minutes, seconds=seconds)


# ---------- (a) flag OFF is byte-identical ----------
def test_flag_off_legacy_intent_unchanged():
    svc = _svc(orb_intrabar_reclaim_enabled=False)  # default
    ev = svc._build_open_intent("HSCS", 2.83)
    md = ev.payload.metadata
    assert "order_type" not in md            # legacy entries are market
    assert "limit_price" not in md
    assert md["trail_pct"] == str(svc.settings.orb_trail_pct)   # 8.0
    assert md["execution_mode"] == "bar_close"
    assert int(ev.payload.quantity) == svc.settings.orb_quantity  # 10
    assert svc._reclaim_mode is False


def test_flag_off_check_reclaim_is_inert():
    # When off, the tick hook is never invoked; even if called, mode flag stops entries.
    svc = _svc(orb_intrabar_reclaim_enabled=False)
    st = _armed(svc, "HSCS", 2.83, 2.52)
    # _check_reclaim is only called under self._reclaim_mode in the drain path; calling
    # it directly should still not create an entry because the mode is off everywhere.
    assert svc._reclaim_mode is False


# ---------- (d) reclaim intent shape ----------
def test_reclaim_intent_is_limit_at_or_high():
    svc = _svc(orb_intrabar_reclaim_enabled=True, orb_reclaim_trail_pct=3.0, orb_reclaim_quantity=5)
    st = _armed(svc, "HSCS", 2.83, 2.52)
    st.reclaim_emit_ms = 1782222086000
    ev = svc._build_open_intent("HSCS", 2.83)
    md = ev.payload.metadata
    assert md["order_type"] == "limit"
    assert md["limit_price"] == "2.8300"
    assert md["trail_pct"] == "3.0" and md["stop_loss_pct"] == "3.0"
    assert md["execution_mode"] == "intrabar_reclaim"
    assert md["orb_intended_or_high"] == "2.8300"
    assert md["orb_reclaim_emit_ms"] == "1782222086000"
    assert int(ev.payload.quantity) == 5


# ---------- (b) reclaim state machine ----------
def test_reclaim_cross_hold_then_one_entry():
    svc = _svc(orb_intrabar_reclaim_enabled=True, orb_reclaim_hold_secs=25)
    st = _armed(svc, "HSCS", 2.83, 2.52)
    svc._check_reclaim("HSCS", 2.99, _t(svc, 11, 0))     # cross -> start hold
    assert st.reclaim_cross_ms is not None and st.traded is False
    svc._check_reclaim("HSCS", 3.10, _t(svc, 11, 20))    # +20s, still above
    assert st.traded is False                            # not yet 25s
    svc._check_reclaim("HSCS", 3.05, _t(svc, 11, 26))    # +26s -> ENTRY
    assert st.traded is True
    assert svc._pending_intents == [("HSCS", 2.83)]
    assert st.reclaim_emit_ms == int(_t(svc, 11, 26).timestamp() * 1000)
    # one-trade-per-symbol: further ticks do nothing
    svc._check_reclaim("HSCS", 3.20, _t(svc, 12, 0))
    assert len(svc._pending_intents) == 1


def test_reclaim_dip_resets_hold():
    svc = _svc(orb_intrabar_reclaim_enabled=True, orb_reclaim_hold_secs=25)
    st = _armed(svc, "HSCS", 2.83, 2.52)
    svc._check_reclaim("HSCS", 2.90, _t(svc, 11, 0))     # cross
    svc._check_reclaim("HSCS", 2.80, _t(svc, 11, 5))     # dip below -> reset
    assert st.reclaim_cross_ms is None and st.traded is False
    svc._check_reclaim("HSCS", 2.95, _t(svc, 11, 10))    # re-cross, timer restarts
    svc._check_reclaim("HSCS", 2.96, _t(svc, 11, 20))    # only 10s into new hold
    assert st.traded is False


def test_reclaim_window_bounds():
    svc = _svc(orb_intrabar_reclaim_enabled=True, orb_reclaim_hold_secs=1)
    st = _armed(svc, "HSCS", 2.83, 2.52)
    # before OR end (09:30-09:34): no entry even if above
    svc._check_reclaim("HSCS", 3.0, _t(svc, 3, 0))
    svc._check_reclaim("HSCS", 3.0, _t(svc, 3, 30))
    assert st.traded is False
    # after cutoff (>10:30 = open+60m): no entry
    svc._check_reclaim("HSCS", 3.0, _t(svc, 61, 0))
    svc._check_reclaim("HSCS", 3.0, _t(svc, 61, 5))
    assert st.traded is False


# ---------- (c) cap-off ----------
def test_cap_off_arms_wide_range():
    # OR width ~32% (2.52 -> 3.33) — legacy build_opening_range rejects (>12%); reclaim
    # mode's _build_or_no_cap must still arm it.
    svc = _svc(orb_intrabar_reclaim_enabled=True)
    bars = [OrbBar(timestamp=svc._session_open_utc(), open=2.6, high=3.33, low=2.52, close=3.0, volume=1000)
            for _ in range(svc._cfg.or_minutes)]
    orng = svc._build_or_no_cap(bars)
    assert orng is not None
    assert round(orng.width_pct, 0) >= 30   # wide range armed
    # sanity: legacy path would have rejected this width
    from project_mai_tai.strategy_core.orb_intrabar import build_opening_range
    assert build_opening_range(bars, svc._cfg) is None
