"""(1) FIRST-BAR LIQUIDITY GATE on the PR config: keep a name only if first-bar (09:30-09:31) volume
>= floor AND median spread <= ceiling. Volume for edge, spread for tail-risk (SDOT shows volume alone
isn't enough). Sweep floor x ceiling (don't fit to CCXIW alone); report P&L lift, tail-cut (how many of
the big losers excluded), winners kept, and drop-top-name robustness. Pure-DB (no Polygon).

HONEST FRAMING: N is small; this is a strong LEAD, not proof — needs the forward-accrual to confirm,
same as PR #403.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from statistics import median, pstdev
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.orb_sim import simulate_orb_tick_entry
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")
GATE, UNGATE_MIN = 4.3, 4
FLOORS = [100, 200, 300, 500, 750]      # K shares in the first minute
CEILS = [0.5, 0.75, 1.0, 1.5, 2.0]      # % median spread


def _et(y, mo, d, h, m):
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def _firstbar(trades, quotes, so):
    t1 = [t for t in trades if so <= t.ts < so + timedelta(seconds=60)]
    q1 = [q for q in quotes if so <= q.ts < so + timedelta(seconds=60)]
    if len(t1) < 5:
        return {"vol": 0.0, "spr": 99.0}      # too thin to trade -> fails any gate
    vol = sum(t.size for t in t1)
    spr = [(q.ask - q.bid) / ((q.ask + q.bid) / 2) * 100 for q in q1 if q.ask > 0 and q.bid > 0 and q.ask >= q.bid]
    return {"vol": vol, "spr": median(spr) if spr else 99.0}


def main():
    src = DbMarketDataSource(build_session_factory(get_settings()))
    wdir = "/home/trader/wt-atr-study/windows"
    dates = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30",
             "2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08"]
    rows = []
    for date in dates:
        y, mo, dd = (int(x) for x in date.split("-"))
        obs, so, cut, end = _et(y, mo, dd, 9, 25), _et(y, mo, dd, 9, 30), _et(y, mo, dd, 10, 0), _et(y, mo, dd, 10, 10)
        wins = load_windows(f"{wdir}/windows_{date}.json")
        for sym in src.orb_qualified_symbols(obs, end, min_trades=500):
            ewin = wins.get(sym, [])
            if not ewin:
                continue
            tr = src.trades(sym, obs, end)
            q = src.quotes(sym, obs, end)
            if len(tr) < 500 or len(q) < 50:
                continue
            fb = _firstbar(tr, q, so)
            pnl = sum(t.pnl for t in simulate_orb_tick_entry(
                tr, q, gap_cap_pct=1.5, trail_pct=2.0, qty=5, observe_open=obs, session_open=so,
                cutoff=cut, capped=False, latency_s=3.0, entry_windows=ewin,
                atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60, bars=build_bars(tr, so)))
            rows.append({"date": date, "sym": sym, "vol": fb["vol"], "spr": fb["spr"], "pnl": pnl})

    allp = [r["pnl"] for r in rows]
    winners = [r for r in rows if r["pnl"] > 0.005]
    losers = sorted([r for r in rows if r["pnl"] < -0.005], key=lambda r: r["pnl"])
    big5 = losers[:5]
    print(f"universe = {len(rows)} name-days | baseline PR config:")
    print(f"  total={sum(allp):+.1f}  mean={sum(allp)/len(allp):+.2f}  median={statistics.median(allp):+.2f}"
          f"  win={sum(1 for p in allp if p>0.005)/len(allp)*100:.0f}%  worst-tail={sum(r['pnl'] for r in big5):+.1f}")
    print(f"  winners={len(winners)}  losers={len(losers)}  |  5 biggest losers: "
          + ", ".join(f"{r['sym']}/{r['date'][5:]} {r['pnl']:+.1f}(v{r['vol']/1000:.0f}K,s{r['spr']:.1f}%)" for r in big5))

    def report(passed, tag):
        p = [r["pnl"] for r in passed]
        if not p:
            print(f"  {tag}: (empty)")
            return
        dt = sum(p) - max(p, key=abs)
        w_kept = sum(1 for r in winners if r in passed)
        big_cut = sum(1 for r in big5 if r not in passed)
        tail = sum(r["pnl"] for r in passed if r in losers)
        print(f"  {tag:<22} n={len(passed):<3} total={sum(p):>+6.1f} med={statistics.median(p):>+5.2f} "
              f"win={sum(1 for x in p if x>0.005)/len(p)*100:>3.0f}% drop1={dt:>+6.1f}  "
              f"big5-cut={big_cut}/5  winners-kept={w_kept}/{len(winners)}")

    print("\nGATE SWEEP (keep if first-bar vol >= floor AND spread <= ceil):")
    for fl in FLOORS:
        print(f" floor={fl}K:")
        for ce in CEILS:
            passed = [r for r in rows if r["vol"] >= fl * 1000 and r["spr"] <= ce]
            report(passed, f"vol>={fl}K spr<={ce}%")

    print("\nVOLUME-ONLY (no spread gate) and SPREAD-ONLY (no volume gate) — to isolate each lever:")
    for fl in FLOORS:
        report([r for r in rows if r["vol"] >= fl * 1000], f"vol>={fl}K only")
    for ce in CEILS:
        report([r for r in rows if r["spr"] <= ce], f"spr<={ce}% only")


if __name__ == "__main__":
    main()
