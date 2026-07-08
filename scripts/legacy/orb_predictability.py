"""MAKE-OR-BREAK premise test for the trend/chop name-selector: does EARLY behaviour (first 10 min,
09:30-09:40) predict the FULL-window outcome (full-window ER, and fixed-2% PR P&L)?

Early signals per name-day:
  early_er    Kaufman efficiency ratio over 09:30-09:40 bars (net/path)  -> trend if high
  pullback    deepest drawdown from a running high in the window (%)     -> chop if high
  hh_frac     fraction of bars making a higher high than the prior       -> trend if high
  vwap_adh    fraction of bars closing above the running VWAP            -> trend-up if high
Outcomes:
  full_er     ER over 09:30-10:00 (the trend truth)
  pnl         simulate_orb_tick_entry (gate 4.3 + ungate 4min + 2% trail) — did the name pay?

Reports Spearman rank-corr (each signal vs each outcome), the decision split (top-half early_er vs
bottom-half: does it pay more?), and robustness (drop the top-2 P&L names — is the signal real or 1-2
trenders?). If early_er predicts late -> selector feasible. If not -> don't build it.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.orb_sim import simulate_orb_tick_entry
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.orb_tick_entry import atr_pct5

_ET = ZoneInfo("America/New_York")
GATE, UNGATE_MIN = 4.3, 4


def _et(y, mo, d, h, m):
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def _er(bars):
    cl = [b.close for b in bars if b.close > 0]
    if len(cl) < 2:
        return None
    net = abs(cl[-1] - cl[0])
    path = sum(abs(cl[i] - cl[i - 1]) for i in range(1, len(cl)))
    return net / path if path > 0 else 0.0


def _signals(early):
    er = _er(early)
    # deepest pullback from a running high
    rh, pull = early[0].high, 0.0
    for b in early:
        rh = max(rh, b.high)
        pull = max(pull, (rh - b.low) / rh if rh > 0 else 0)
    hh = sum(1 for i in range(1, len(early)) if early[i].high > early[i - 1].high) / max(1, len(early) - 1)
    cv = tv = 0.0
    adh = n = 0
    for b in early:
        typ = (b.high + b.low + b.close) / 3
        cv += typ * b.volume
        tv += b.volume
        vwap = cv / tv if tv > 0 else typ
        adh += 1 if b.close > vwap else 0
        n += 1
    return er, pull, hh, (adh / n if n else 0)


def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def _pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    return cov / (sx * sy) if sx > 0 and sy > 0 else 0.0


def _spear(xs, ys):
    return _pearson(_rank(xs), _rank(ys))


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
            bars = build_bars(tr, so)
            early = [b for b in bars if so <= b.timestamp < so + timedelta(minutes=10)]
            full = [b for b in bars if so <= b.timestamp < cut]
            if len(early) < 5 or len(full) < 10:
                continue
            er, pull, hh, adh = _signals(early)
            if er is None:
                continue
            full_er = _er(full)
            pnl = sum(t.pnl for t in simulate_orb_tick_entry(
                tr, q, gap_cap_pct=1.5, trail_pct=2.0, qty=5, observe_open=obs, session_open=so,
                cutoff=cut, capped=False, latency_s=3.0, entry_windows=ewin,
                atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60, bars=bars))
            rows.append({"date": date, "sym": sym, "eer": er, "pull": pull, "hh": hh, "adh": adh,
                         "fer": full_er, "pnl": pnl, "atr5": atr_pct5(bars) or 0})

    n = len(rows)
    print(f"name-days = {n}\n")
    print(f"{'date':<11}{'sym':<7}{'eer':>6}{'pull':>7}{'hh':>6}{'adh':>6}  {'full_er':>8}{'pnl':>7}{'atr5':>6}")
    for r in sorted(rows, key=lambda r: r["pnl"], reverse=True):
        print(f"{r['date']:<11}{r['sym']:<7}{r['eer']:>6.2f}{r['pull']*100:>6.1f}%{r['hh']:>6.2f}{r['adh']:>6.2f}"
              f"  {r['fer']:>8.2f}{r['pnl']:>+7.2f}{r['atr5']:>6.2f}")

    def corrs(rs, label):
        print(f"\n{label} (n={len(rs)}) — Spearman rank-corr:")
        print(f"  {'signal':<10}{'vs full_er':>12}{'vs pnl':>10}")
        for key, nm in [("eer", "early_er"), ("pull", "pullback"), ("hh", "hh_frac"), ("adh", "vwap_adh")]:
            xs = [r[key] for r in rs]
            print(f"  {nm:<10}{_spear(xs, [r['fer'] for r in rs]):>12.2f}{_spear(xs, [r['pnl'] for r in rs]):>10.2f}")

    corrs(rows, "ALL")
    top2 = {id(r) for r in sorted(rows, key=lambda r: r["pnl"], reverse=True)[:2]}
    corrs([r for r in rows if id(r) not in top2], "DROP top-2 P&L names (robustness)")

    print("\nDECISION TEST — split by early_er median: does the high-early_er half actually pay more?")
    med = statistics.median(r["eer"] for r in rows)
    hi = [r for r in rows if r["eer"] >= med]
    lo = [r for r in rows if r["eer"] < med]
    for grp, lbl in [(hi, "high early_er"), (lo, "low early_er")]:
        p = [r["pnl"] for r in grp]
        fe = [r["fer"] for r in grp]
        w = sum(1 for x in p if x > 0.005) / len(p) * 100 if p else 0
        print(f"  {lbl:<15} n={len(grp):<3} total_pnl={sum(p):>+7.1f} mean={sum(p)/len(p):>+6.2f} "
              f"win={w:>3.0f}%  mean_full_er={sum(fe)/len(fe):.2f}")
    hi2 = [r for r in hi if id(r) not in top2]
    p2 = [r["pnl"] for r in hi2]
    print(f"  high early_er, DROP top-2: n={len(hi2)} total_pnl={sum(p2):>+7.1f} mean={sum(p2)/len(p2):>+6.2f} "
          f"(is the high-group edge just the 1-2 trenders?)")

    print("\nRANK OVERLAP — of the top-8 full_er (real trenders), how many are in the top-8 early_er?")
    ns = len(rows)
    k = min(8, ns)
    top_fer = {r["sym"] + r["date"] for r in sorted(rows, key=lambda r: r["fer"], reverse=True)[:k]}
    top_eer = {r["sym"] + r["date"] for r in sorted(rows, key=lambda r: r["eer"], reverse=True)[:k]}
    print(f"  overlap = {len(top_fer & top_eer)}/{k}  (random ~= {k*k/ns:.1f})")


if __name__ == "__main__":
    main()
