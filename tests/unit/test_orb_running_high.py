"""ORB running-high breakout mode (operator-validated 2026-06-24), flag-gated.

Gates: (a) flag OFF -> mode inactive, byte-identical; (b) reference seeds from 09:25 and
entry fires when a bar breaks the running high inside 09:30-10:00; (c) gap-cap skips a
spike >1.5% above the broken high; (d) window-bound (no entry <09:30 or >10:00);
(e) single entry per symbol (v1); (f) intent = limit at break level, 3% trail, qty 5;
(g) reclaim takes precedence (mutually exclusive).
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from project_mai_tai.services.orb_app import OrbService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_intrabar import OrbBar


def _svc(**kw) -> OrbService:
    return OrbService(settings=Settings(**kw), redis_client=MagicMock())


def _rh_svc(**kw) -> OrbService:
    svc = _svc(orb_running_high_enabled=True, **kw)
    svc._universe = {"FOO"}
    return svc


def _bar(svc: OrbService, minutes: int, o: float, h: float, low: float | None = None):
    ts = svc._session_open_utc() + timedelta(minutes=minutes)
    return OrbBar(timestamp=ts, open=o, high=h, low=low if low is not None else o, close=h, volume=1000.0)


def _feed_seed(svc):
    # 09:25..09:29 observation bars build running high to 10.50 (no trading pre-09:30)
    svc._on_bar("FOO", _bar(svc, -5, 10.00, 10.00))
    svc._on_bar("FOO", _bar(svc, -4, 10.10, 10.50))   # running_high -> 10.50
    svc._on_bar("FOO", _bar(svc, -1, 10.30, 10.40))


def test_flag_off_mode_inactive():
    svc = _svc()                       # default
    assert svc._running_high_mode is False


def test_entry_on_break_of_running_high():
    svc = _rh_svc()
    _feed_seed(svc)
    assert not svc._states["FOO"].traded
    # 09:30 bar breaks 10.50 (open below the level -> fill AT the breakout level 10.50)
    svc._on_bar("FOO", _bar(svc, 0, 10.40, 11.00))
    st = svc._states["FOO"]
    assert st.pending is True and st.attempts == 1   # emitted; confirmed on the fill event
    assert st.traded is False and st.entry_price is None  # no phantom position until a real fill
    assert svc._pending_intents == [("FOO", 10.50)]  # intent fired at the broken high


def test_gap_cap_skips_a_spike_too_far_above():
    svc = _rh_svc()
    _feed_seed(svc)                                   # running high 10.50
    # bar opens 10.80 (+2.9% above 10.50) -> beyond 1.5% gap cap -> skip
    svc._on_bar("FOO", _bar(svc, 1, 10.80, 11.20))
    assert svc._states["FOO"].traded is False
    assert svc._pending_intents == []


def test_no_entry_before_0930_or_after_1000():
    svc = _rh_svc()
    _feed_seed(svc)
    svc._on_bar("FOO", _bar(svc, -2, 10.40, 12.00))   # pre-09:30: seeds only, no trade
    assert svc._states["FOO"].traded is False
    svc._on_bar("FOO", _bar(svc, 31, 10.40, 99.00))   # 10:01 (>10:00 cutoff): no trade
    assert svc._states["FOO"].traded is False


def test_single_entry_per_symbol():
    svc = _rh_svc()
    _feed_seed(svc)
    svc._on_bar("FOO", _bar(svc, 0, 10.40, 11.00))    # entry
    svc._on_bar("FOO", _bar(svc, 3, 11.50, 12.00))    # would break the new high -> no re-entry (v1)
    assert len(svc._pending_intents) == 1


def test_not_in_universe_no_entry():
    svc = _rh_svc()
    svc._universe = set()                              # not pre-09:25 qualified
    _feed_seed(svc)
    svc._on_bar("FOO", _bar(svc, 0, 10.40, 11.00))
    assert svc._states["FOO"].traded is False


def test_intent_shape_limit_trail3_qty5():
    svc = _rh_svc()
    ev = svc._build_open_intent("FOO", 10.50)
    md = ev.payload.metadata
    assert md["order_type"] == "limit"
    assert md["limit_price"] == "10.5000"
    assert md["execution_mode"] == "running_high_breakout"
    assert md["trail_pct"] == str(svc.settings.orb_reclaim_trail_pct)   # 3.0
    assert int(ev.payload.quantity) == svc.settings.orb_reclaim_quantity  # 5


def test_reclaim_takes_precedence():
    svc = _svc(orb_running_high_enabled=True, orb_intrabar_reclaim_enabled=True)
    assert svc._running_high_mode is False             # mutually exclusive
    assert svc._reclaim_mode is True
