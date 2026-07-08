"""Production ORB tick-entry engine — CI gates.

1. PARITY: the production `OrbTickEntry` engine, driven by the backtest `simulate_orb_tick_entry`
   with the gate OFF, is TRADE-IDENTICAL to the validated `simulate_intrabar`. This is what makes the
   back-test validate the REAL code path (the same engine the live orb_app.py tick handler uses) — the
   intrabar-2% result reproduces through production code, not a parallel research reimplementation.
2. ATR PIN: the engine's self-contained `atr_pct5` equals the vendored oracle's period-5 ATR% (median
   loss/close), so strategy_core needs no backtest/analysis import yet stays numerically identical.
3. GATE: the causal high-ATR gate excludes/admits as expected.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

import pytest

from project_mai_tai.backtest.atr_oracle import Bar as OracleBar
from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.backtest.data import FixtureMarketDataSource, build_bars
from project_mai_tai.backtest.orb_sim import simulate_intrabar, simulate_orb_tick_entry
from project_mai_tai.strategy_core.orb_tick_entry import atr_pct5

FIX = Path(__file__).parent / "fixtures"
_SRC = FixtureMarketDataSource(FIX)


def _u(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _load(sym, y, m, d):
    obs, so, cut, end = _u(y, m, d, 13, 25), _u(y, m, d, 13, 30), _u(y, m, d, 14, 0), _u(y, m, d, 14, 10)
    trades = _SRC.trades(sym, obs, end)
    quotes = _SRC.quotes(sym, obs, end)
    return {"bars": build_bars(trades, so), "trades": trades, "quotes": quotes,
            "observe_open": obs, "session_open": so, "cutoff": cut}


_BASE = dict(gap_cap_pct=1.5, trail_pct=3.0, qty=5, latency_s=3.0)


def _win(d):
    return dict(observe_open=d["observe_open"], session_open=d["session_open"], cutoff=d["cutoff"])


def _sig(ts):
    return [(t.entry_ts, round(t.entry_price, 4), t.exit_ts,
             round(t.exit_price, 4) if t.exit_price is not None else None, round(t.pnl, 4)) for t in ts]


@pytest.mark.parametrize("sym,y,m,d", [("KIDZ", 2026, 7, 6), ("CELZ", 2026, 6, 30), ("SDOT", 2026, 6, 26)])
def test_production_engine_reproduces_intrabar(sym, y, m, d):
    """simulate_orb_tick_entry (production OrbTickEntry, gate off) == simulate_intrabar exactly."""
    dd = _load(sym, y, m, d)
    for capped in (True, False):
        for trail in (2.0, 3.0):
            kw = dict(gap_cap_pct=1.5, qty=5, latency_s=3.0, trail_pct=trail, capped=capped, **_win(dd))
            ref = simulate_intrabar(dd["trades"], dd["quotes"], **kw)
            prod = simulate_orb_tick_entry(dd["trades"], dd["quotes"], **kw)  # gate off, bars=None
            assert _sig(prod) == _sig(ref), f"{sym} capped={capped} trail={trail}: production != validated intrabar"


def test_atr_pct5_pinned_to_oracle():
    """The engine's self-contained atr_pct5 == vendored oracle period-5 ATR% (median loss/close*100)."""
    for sym, y, m, d in [("KIDZ", 2026, 7, 6), ("CELZ", 2026, 6, 30), ("SDOT", 2026, 6, 26)]:
        bars = _load(sym, y, m, d)["bars"]
        ob = [OracleBar(int(b.timestamp.timestamp() * 1000), b.open, b.high, b.low, b.close, int(b.volume)) for b in bars]
        rows = compute_atr_trail(ob, period=5, factor=1.0)          # factor 1 -> loss == raw ATR5
        oracle = [rows[i]["loss"] / bars[i].close * 100 for i in range(len(bars))
                  if rows[i]["loss"] is not None and bars[i].close > 0]
        expected = median(oracle) if oracle else None
        got = atr_pct5(bars)
        if expected is None:
            assert got is None
        else:
            # tol reflects the oracle rounding `loss` to 4dp in its row output (atr_pct5 is unrounded,
            # i.e. more precise); per-element ATR% rounding error ~loss_round/close*100 ≈ 0.007, so the
            # median can differ by ~0.002. Anything under 0.02 means "same computation modulo display rounding".
            assert got is not None and abs(got - expected) < 0.02, f"{sym}: atr_pct5 {got} != oracle {expected}"


def test_high_atr_gate_admits_and_excludes():
    """A high threshold blocks all entries; None admits (== ungated); a mid threshold admits a mover."""
    dd = _load("CELZ", 2026, 6, 30)                                  # CELZ is high-ATR (grinding)
    kw = dict(**_BASE, capped=False, **_win(dd))
    ungated = simulate_orb_tick_entry(dd["trades"], dd["quotes"], atr_gate_pct=None, bars=dd["bars"], **kw)
    blocked = simulate_orb_tick_entry(dd["trades"], dd["quotes"], atr_gate_pct=99.0, bars=dd["bars"], **kw)
    admitted = simulate_orb_tick_entry(dd["trades"], dd["quotes"], atr_gate_pct=1.0, bars=dd["bars"], **kw)
    assert len(blocked) == 0, "ATR% 99% gate must block every entry"
    assert len(admitted) >= 1, "a 1% gate must admit CELZ (a high-ATR mover)"
    assert len(admitted) <= len(ungated)


def test_ungate_first_minutes_bypasses_gate_early():
    """gate_after_secs ungates the early window: a blocking gate (99%) with gate_after covering the
    whole entry window admits every break (== no gate) — the recover-the-flood-day-prize behavior."""
    dd = _load("CELZ", 2026, 6, 30)
    kw = dict(**_BASE, capped=False, **_win(dd))
    ungated = simulate_orb_tick_entry(dd["trades"], dd["quotes"], atr_gate_pct=None, bars=dd["bars"], **kw)
    blocked = simulate_orb_tick_entry(dd["trades"], dd["quotes"], atr_gate_pct=99.0,
                                      gate_after_secs=0.0, bars=dd["bars"], **kw)
    ungate_all = simulate_orb_tick_entry(dd["trades"], dd["quotes"], atr_gate_pct=99.0,
                                         gate_after_secs=1800.0, bars=dd["bars"], **kw)  # 30min covers cutoff
    assert len(blocked) == 0
    assert _sig(ungate_all) == _sig(ungated), "ungate window covering the session == no gate"


def test_first_bar_liquidity_gate():
    """First-bar liquidity gate: a loose gate == no gate on a liquid name; an impossible volume floor
    blocks every entry AFTER the first bar closes (early entries can't be measured yet)."""
    dd = _load("CELZ", 2026, 6, 30)                                  # CELZ is deeply liquid
    kw = dict(gap_cap_pct=1.5, trail_pct=2.0, qty=5, latency_s=3.0, capped=False, **_win(dd))
    ungated = simulate_orb_tick_entry(dd["trades"], dd["quotes"], liq_min_volume=None, **kw)
    loose = simulate_orb_tick_entry(dd["trades"], dd["quotes"], liq_min_volume=1.0, liq_max_spread_pct=100.0, **kw)
    blocked = simulate_orb_tick_entry(dd["trades"], dd["quotes"], liq_min_volume=1e15, **kw)
    fb_close = dd["session_open"] + timedelta(seconds=60)
    assert _sig(loose) == _sig(ungated), "a loose liquidity gate == no gate on a liquid name"
    assert all(t.entry_ts < fb_close for t in blocked), "impossible floor blocks every post-first-bar entry"
    assert len(blocked) < len(ungated), "CELZ has post-first-bar entries the floor removes"
