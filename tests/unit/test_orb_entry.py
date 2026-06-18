from __future__ import annotations

import types
from datetime import timedelta

from project_mai_tai.services.orb_app import OrbService
from project_mai_tai.strategy_core.orb_intrabar import ExecutionMode, OrbBar, OrbConfig

OPEN = OrbService._session_open_utc()  # today's 09:30 ET (UTC) — same clock the service uses


def _svc(universe, mode="bar_close"):
    svc = OrbService.__new__(OrbService)
    svc.settings = types.SimpleNamespace(
        orb_trail_pct=8.0, orb_broker_account_name="paper:orb", orb_quantity=10
    )
    svc._states = {}
    svc._universe = {s.upper() for s in universe}
    svc._pending_intents = []
    svc._cfg = OrbConfig()
    svc._mode = ExecutionMode(mode)
    return svc


def _bar(minute, o, h, low, c, v, vwap=None, ema9=None):
    return OrbBar(OPEN + timedelta(minutes=minute), o, h, low, c, v, vwap, ema9)


def _feed_or(svc, symbol):
    # 5 OR bars, range 4.95-5.09 (~2.8% width, in band), avg vol 100
    for m in range(5):
        svc._on_bar(symbol, _bar(m, 5.0, 5.09, 4.95, 5.0, 100))


def test_breakout_emits_open_intent_for_universe_name():
    svc = _svc(["CRVO"])
    _feed_or(svc, "CRVO")
    assert svc._pending_intents == []  # no breakout during the OR window
    # breakout: close>OR_high(5.09), vol 300>=1.5*100, >vwap, >ema9 -> entry at close (bar_close mode)
    svc._on_bar("CRVO", _bar(5, 5.1, 5.4, 5.05, 5.33, 300, vwap=5.1, ema9=5.05))
    assert svc._pending_intents == [("CRVO", 5.33)]
    # one trade per symbol — a second breakout bar does not re-enter
    svc._on_bar("CRVO", _bar(6, 5.4, 5.6, 5.3, 5.55, 400, vwap=5.2, ema9=5.1))
    assert svc._pending_intents == [("CRVO", 5.33)]


def test_name_not_in_universe_is_skipped():
    svc = _svc([])  # empty pre-09:25 universe
    _feed_or(svc, "CRVO")
    svc._on_bar("CRVO", _bar(5, 5.1, 5.4, 5.05, 5.33, 300, vwap=5.1, ema9=5.05))
    assert svc._pending_intents == []  # not armed — out of scope


def test_wide_open_is_width_capped():
    svc = _svc(["WIDE"])
    for m in range(5):
        svc._on_bar("WIDE", _bar(m, 10, 12, 10, 11, 100))  # ~20% wide
    svc._on_bar("WIDE", _bar(5, 12.1, 13.0, 12.0, 12.5, 300, vwap=12.0, ema9=11.5))
    assert svc._pending_intents == []


def test_weak_volume_breakout_does_not_fire():
    svc = _svc(["X"])
    _feed_or(svc, "X")
    svc._on_bar("X", _bar(5, 5.1, 5.4, 5.05, 5.33, 120, vwap=5.1, ema9=5.05))  # vol < 1.5x
    assert svc._pending_intents == []


def test_intrabar_mode_fills_at_or_high():
    svc = _svc(["X"], mode="intrabar")
    _feed_or(svc, "X")
    svc._on_bar("X", _bar(5, 5.1, 5.4, 5.05, 5.33, 300, vwap=5.1, ema9=5.05))
    assert svc._pending_intents == [("X", 5.09)]  # OR_high, not the close


def test_open_intent_metadata_drives_trail8():
    svc = _svc(["X"])
    event = svc._build_open_intent("X", 5.33)
    p = event.payload
    assert p.strategy_code == "orb" and p.side == "buy" and p.intent_type == "open"
    assert p.broker_account_name == "paper:orb" and int(p.quantity) == 10
    md = p.metadata
    assert md["stop_guard_enabled"] == "true"
    assert md["stop_loss_pct"] == "8.0"   # initial stop 8% below entry
    assert md["trail_pct"] == "8.0"       # ratchet -> OMS TRAIL-8% (#340)
    assert md["orb_entry"] == "true"
