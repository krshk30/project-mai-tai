"""KIDZ 2026-07-06 per-tick ENTRY trace, 09:30-09:53 — reproduce the PR engine's decision at every
new-session-high break: running-high level, did price exceed it, causal ATR%-so-far vs the 4.3% gate,
which gate (ungate-early / ATR) admitted or blocked it, and the fire/hold/exit outcome.
"""
from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.fill import QuoteBook, entry_fill
from project_mai_tai.backtest.orb_sim import _run_trail_exit
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.orb_tick_entry import atr_pct5

_ET = ZoneInfo("America/New_York")
GATE, TRAIL, UNGATE_MIN, GAP = 4.3, 2.0, 4, 1.5


def _et(h, m):
    return datetime(2026, 7, 6, h, m, tzinfo=_ET).astimezone(timezone.utc)


def hh(t):
    return t.astimezone(_ET).strftime("%H:%M:%S")


src = DbMarketDataSource(build_session_factory(get_settings()))
obs, so, cut, end = _et(9, 25), _et(9, 30), _et(10, 0), _et(10, 10)
early_end = so + timedelta(minutes=UNGATE_MIN)
trace_end = _et(9, 53) + timedelta(seconds=59)
trades = src.trades("KIDZ", obs, end)
quotes = src.quotes("KIDZ", obs, end)
bars = build_bars(trades, so)
book = QuoteBook(quotes)

# per-minute causal ATR% timeline (when does the gate open?)
print("KIDZ 2026-07-06 — causal ATR%-so-far per minute (gate opens at >= 4.3%):")
for mm in range(30, 54):
    tt = _et(9, mm)
    sofar = [b for b in bars if b.timestamp + timedelta(seconds=60) <= tt]
    a = atr_pct5(sofar)
    status = "warming(None)" if a is None else (f"{a:.2f}%  {'OPEN' if a >= GATE else 'closed(<4.3)'}")
    print(f"   {hh(tt)}  bars={len(sofar):<2} ATR={status}")

print("\nper new-session-high BREAK (flat only) — level / exceeded? / causal ATR / gate / outcome:")
running_high = None
bar_iter = iter(bars)
next_bar = next(bar_iter, None)
attempts = 0
i, n = 0, len(trades)
while i < n and trades[i].ts <= trace_end:
    t = trades[i]
    while next_bar is not None and next_bar.timestamp + timedelta(seconds=60) <= t.ts:
        next_bar = next(bar_iter, None)
    if t.ts < obs:
        i += 1
        continue
    if running_high is None:
        running_high = t.price
        print(f"   {hh(t.ts)}  seed running-high = {t.price:.4f}")
        i += 1
        continue
    if so <= t.ts <= cut and t.price > running_high:            # NEW SESSION HIGH while flat
        level = running_high
        sofar = [b for b in bars if b.timestamp + timedelta(seconds=60) <= t.ts]
        a = atr_pct5(sofar)
        early = t.ts < early_end
        if early:
            gate_ok, why = True, "ungate-early (ATR " + ("warming" if a is None else f"{a:.2f}") + "; fail-closed would BLOCK)"
        elif a is None:
            gate_ok, why = False, "BLOCKED: ATR still warming (no gate value)"
        elif a >= GATE:
            gate_ok, why = True, f"ATR-gate PASS ({a:.2f} >= 4.3)"
        else:
            gate_ok, why = False, f"BLOCKED: ATR-gate FAIL ({a:.2f} < 4.3)"
        gap_ok = t.price <= level * (1 + GAP / 100)
        if gate_ok and gap_ok:
            fill = entry_fill(book, t.ts, level, GAP)
            if fill is not None:
                start = bisect_left(book._ts, t.ts + timedelta(seconds=3))
                xts, xpx, xr, _ = _run_trail_exit(quotes, start, fill, TRAIL, book, 3.0)
                print(f"   {hh(t.ts)}  break {t.price:.4f} > rh {level:.4f}  | {why} | FIRE fill {fill:.4f} "
                      f"-> {hh(xts)} exit {xpx:.4f} ({(xpx/fill-1)*100:+.1f}%) {xr}")
                while i < n and trades[i].ts <= xts:
                    running_high = max(running_high, trades[i].price)
                    i += 1
                continue
            why += " | but gap-cap ABANDON"
            gate_ok = False
        if not gate_ok or not gap_ok:
            extra = why if not gate_ok else f"gap-cap BLOCK ({t.price:.4f} > {level*(1+GAP/100):.4f})"
            print(f"   {hh(t.ts)}  break {t.price:.4f} > rh {level:.4f}  | {extra}")
    if running_high is not None:
        running_high = max(running_high, t.price)
    i += 1
