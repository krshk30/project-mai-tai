"""Real-emit test for the ORB tick-driven entry V1 (orb_app.py), per the design's discipline.

Verifies the live path end-to-end at the emit boundary: a break TICK on a high-ATR name inside the
window emits ONE quote-priced open intent at the broken level with a 2% trail + up-sized qty; a SLOW
(low-ATR) name is gated out; an out-of-universe name is skipped. Flag OFF is covered by the existing
byte-identical suite (86 orb tests) — here the flag is ON.
"""
from __future__ import annotations

import types
from datetime import timedelta

from project_mai_tai.services.orb_app import OrbService
from project_mai_tai.strategy_core.orb_intrabar import OrbBar

OBS = OrbService._observe_open_utc()   # 09:25 ET (UTC) — the tick-entry running-high anchor


def _svc(universe, gate=4.3):
    svc = OrbService.__new__(OrbService)
    svc.settings = types.SimpleNamespace(
        orb_tick_entry_trail_pct=2.0, orb_tick_entry_quantity=10, orb_broker_account_name="paper:orb")
    svc._states = {}
    svc._universe = {s.upper() for s in universe}
    svc._pending_intents = []
    svc._tick_entry_mode = True
    svc._tick_engines = {}
    svc._tick_gap_cap_pct = 1.5
    svc._tick_window_min = 30
    svc._tick_atr_gate_pct = gate
    return svc


def _bar(m, close, half_range):
    return OrbBar(OBS + timedelta(minutes=m), close, close + half_range, close - half_range, close, 100.0)


def _run(svc, sym, half_range):
    """Build the running-high to 5.00, stay flat while enough bars form for the causal period-5 ATR
    (needs 9+ bars -> ~09:34), then a break tick at 5.05 (within gap-cap) at 09:35. `half_range` sets
    each bar's range -> the ATR gate. (Entries before the ATR is computable are fail-closed = no trade.)"""
    for m, px in enumerate([4.70, 4.80, 4.90, 5.00]):    # rising -> running_high = 5.00 by 09:28
        svc._check_tick_entry(sym, px, OBS + timedelta(minutes=m), _bar(m, px, half_range))
    for m in range(4, 10):                                # 09:29-09:34: flat below 5.00, bars accrue
        svc._check_tick_entry(sym, 4.99, OBS + timedelta(minutes=m), _bar(m, 4.99, half_range))
    svc._check_tick_entry(sym, 5.05, OBS + timedelta(minutes=10), None)   # break (09:35), +1% <= gap-cap


def test_high_atr_break_emits_tick_entry_intent():
    svc = _svc(["MOVR"])
    _run(svc, "MOVR", half_range=0.30)            # range 0.60 ~12% -> ATR5% >> 4.3% gate
    assert svc._pending_intents == [("MOVR", 5.00)], "high-ATR break must emit at the broken level"
    intent = svc._build_open_intent("MOVR", 5.00)
    md = intent.payload.metadata
    assert md["execution_mode"] == "tick_entry_breakout"
    assert md["trail_pct"] == "2.0" and md["stop_loss_pct"] == "2.0"      # 2% OMS trail
    assert md["orb_intended_break_level"] == "5.0000" and md["price_source"] == "ask"
    assert "limit_price" not in md and "reference_price" not in md         # quote-priced fail-closed
    assert int(intent.payload.quantity) == 10                              # high-ATR up-sized


def test_slow_name_is_gated_out():
    svc = _svc(["SLOW"])
    _run(svc, "SLOW", half_range=0.025)           # range 0.05 ~1% -> ATR5% << 4.3% gate
    assert svc._pending_intents == [], "a slow (low-ATR) name must be gated out of the 2% tick config"


def test_out_of_universe_is_skipped():
    svc = _svc([])                                # empty pre-09:25 universe
    _run(svc, "MOVR", half_range=0.30)
    assert svc._pending_intents == []


def test_ungate_first_minutes_admits_slow_early():
    """With the ungate window covering the break, a SLOW name that the ATR gate would reject is
    admitted early (recover the flood-day prize; slow names rarely break that early anyway)."""
    svc = _svc(["SLOW"])
    svc._tick_gate_after_secs = 6 * 60.0          # ungate 09:30-09:36 -> the 09:35 break is admitted
    _run(svc, "SLOW", half_range=0.025)           # slow (gated out without the ungate window)
    assert svc._pending_intents == [("SLOW", 5.00)], "the ungate window admits an early break on a slow name"
