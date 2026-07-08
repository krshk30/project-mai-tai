"""Generic ORB tick-entry per-tick TRACE (manual chart-validation).  NO confirmed-window gate —
runs the tick-entry logic on ANY name/day even if it was NOT in the scanner's confirmed universe
(so it never comes back empty for a lack of windows). Shows, per new-session-high break: running-high
level, whether price exceeded it, causal ATR%-so-far vs the 4.3 gate, entry/exit/P&L, and whether the
ungate-first-4-min or the ATR gate admitted/blocked it. Plus the name's behavior tag (ATR5%/ER).

    python -m ... orb_trace  SYMBOL YYYY-MM-DD [SYMBOL YYYY-MM-DD ...]
"""
from __future__ import annotations

import sys
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.fill import QuoteBook, entry_fill
from project_mai_tai.backtest.orb_sim import _run_trail_exit
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.orb_tick_entry import atr_pct5

_ET = ZoneInfo("America/New_York")
GATE, TRAIL, UNGATE_MIN, GAP, ER_HI = 4.3, 2.0, 4, 1.5, 0.10


def hh(t):
    return t.astimezone(_ET).strftime("%H:%M:%S")


def behavior(bars):
    atr5 = atr_pct5(bars)
    closes = [b.close for b in bars if b.close > 0]
    net = abs(closes[-1] - closes[0]) if len(closes) > 1 else 0.0
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    er = net / path if path > 0 else 0.0
    tag = "slow" if (atr5 is None or atr5 < GATE) else ("grinding" if er >= ER_HI else "volatile")
    return atr5, er, tag


def trace(src, sym, date):
    y, mo, d = (int(x) for x in date.split("-"))

    def et(h, m):
        return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)

    obs, so, cut, end = et(9, 25), et(9, 30), et(10, 0), et(10, 10)
    early_end = so + timedelta(minutes=UNGATE_MIN)
    trades = src.trades(sym, obs, end)
    quotes = src.quotes(sym, obs, end)
    print(f"\n{'='*100}\n{sym} {date}  (CONFIRMED-WINDOW GATE OFF — manual chart-validation, not a scanner-universe result)\n{'='*100}")
    if len(trades) < 500 or len(quotes) < 50:
        print(f"  insufficient data: trades={len(trades)} quotes={len(quotes)} (need >=500/>=50) — cannot trace")
        return
    bars = build_bars(trades, so)
    atr5, er, tag = behavior(bars)
    print(f"  behavior: ATR5%={atr5:.2f} (median, full ORB window)  ER={er:.2f}  ->  [{tag.upper()}]"
          f"  {'(gate would ADMIT from 09:34)' if tag != 'slow' else '(gate would EXCLUDE after 09:34; only ungate-early)'}")
    print(f"  causal ATR%-so-far per minute (gate opens at >= {GATE}%):")
    for mm in range(30, 60):
        tt = et(9, mm)
        sofar = [b for b in bars if b.timestamp + timedelta(seconds=60) <= tt]
        a = atr_pct5(sofar)
        st = "warming(None)" if a is None else f"{a:.2f}%  {'OPEN' if a >= GATE else 'closed(<4.3)'}"
        print(f"     {hh(tt)} bars={len(sofar):<2} ATR={st}")

    print("  per new-session-high BREAK (flat only):")
    book = QuoteBook(quotes)
    running_high = None
    n_fire = 0
    pnl_sum = 0.0
    i, n = 0, len(trades)
    while i < n:
        t = trades[i]
        if t.ts < obs:
            i += 1
            continue
        if running_high is None:
            running_high = t.price
            print(f"     {hh(t.ts)} seed running-high = {t.price:.4f}")
            i += 1
            continue
        if so <= t.ts <= cut and t.price > running_high:
            level = running_high
            sofar = [b for b in bars if b.timestamp + timedelta(seconds=60) <= t.ts]
            a = atr_pct5(sofar)
            early = t.ts < early_end
            if early:
                gate_ok, why = True, "ungate-early (ATR " + ("warming" if a is None else f"{a:.2f}") + "; fail-closed would BLOCK)"
            elif a is None:
                gate_ok, why = False, "BLOCKED: ATR warming (no value)"
            elif a >= GATE:
                gate_ok, why = True, f"ATR-gate PASS ({a:.2f}>=4.3)"
            else:
                gate_ok, why = False, f"BLOCKED: ATR-gate FAIL ({a:.2f}<4.3)"
            gap_ok = t.price <= level * (1 + GAP / 100)
            if gate_ok and gap_ok:
                fill = entry_fill(book, t.ts, level, GAP)
                if fill is not None:
                    start = bisect_left(book._ts, t.ts + timedelta(seconds=3))
                    xts, xpx, xr, _ = _run_trail_exit(quotes, start, fill, TRAIL, book, 3.0)
                    pnl = (xpx - fill) * 5
                    pnl_sum += pnl
                    n_fire += 1
                    print(f"     {hh(t.ts)} break {t.price:.4f} > rh {level:.4f} | {why} | FIRE fill {fill:.4f} "
                          f"-> {hh(xts)} {xpx:.4f} ({(xpx/fill-1)*100:+.1f}%) pnl{pnl:+.2f} {xr}")
                    while i < n and trades[i].ts <= xts:
                        running_high = max(running_high, trades[i].price)
                        i += 1
                    continue
                why, gate_ok = why + " | gap-cap ABANDON", False
            print(f"     {hh(t.ts)} break {t.price:.4f} > rh {level:.4f} | "
                  + (why if not gate_ok else f"gap-cap BLOCK ({t.price:.4f} > {level*(1+GAP/100):.4f})"))
        if running_high is not None:
            running_high = max(running_high, t.price)
        i += 1
    print(f"  -> {n_fire} entries, net pnl {pnl_sum:+.2f} (qty5)")


def main():
    args = sys.argv[1:]
    pairs = [(args[i], args[i + 1]) for i in range(0, len(args) - 1, 2)]
    src = DbMarketDataSource(build_session_factory(get_settings()))
    for sym, date in pairs:
        trace(src, sym, date)


if __name__ == "__main__":
    main()
