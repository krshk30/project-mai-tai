"""RESEARCH: sweep the ENTRY ATR params (period × factor — the trail that defines the flip) and
tag each name by price bucket + behavior type, to see how the entry ATR should change per name type.

The swept param is the ENTRY signal only (compute_atr_trail's period/factor). Entry = enter
IMMEDIATELY at each variant-B ATR touch (the flip), confirmed-window restricted. EXIT is held
CONSTANT at the live ladder + fixed −1.5% hard stop, so P&L differences are attributable to the
entry params, not the exit. Baseline = live 5/3.5.

Classification metrics are computed ONCE per name over the full day's bars, INDEPENDENT of the swept
period (so the tag is a stable property of the name, not an artifact of the sweep):
  - volatility magnitude = median period-14 Wilders ATR% of price
  - directionality      = Kaufman efficiency ratio ER = |net move| / total path (high=trend, low=chop)
  - price               = median close
Raw metrics are dumped; the price/behavior BUCKETS are assigned in aggregation (data-driven, visible).

Feed = massive (dense, all names; structural fidelity) primary; Schwab via --feed=schwab cross-check.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.backtest.data import DbMarketDataSource
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.backtest.v2_sim import BAR_MS, VOL_FLOOR, _Book, _px, _run_exit, _utc, _v2_cfg
from project_mai_tai.backtest.v2_wait3break import bars_from_trades
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.exit_logic.engine import ExitEngine
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")
PERIODS = [5, 10, 14]
FACTORS = [1.5, 2.0, 2.5, 3.0, 3.5]
COMBOS = [(p, f) for p in PERIODS for f in FACTORS]   # 15; baseline (5, 3.5)
EXIT_STOP_PCT = 1.5                                    # live hard stop, held constant


def _touches(bars, period, factor):
    """variant-B ATR touches for a given period/factor: (bar_idx, ts, touch_price)."""
    rows = compute_atr_trail(bars, period=period, factor=factor)
    fired, out = False, []
    for i in range(1, len(bars)):
        prev = rows[i - 1]
        if prev["state"] == "short" and prev["trail"] is not None and bars[i].high >= prev["trail"] and not fired:
            out.append((i, bars[i].ts, prev["trail"]))
            fired = True
        if rows[i]["flip"] == "SELL":
            fired = False
    return out


def simulate_flip(bars, entry_q, massive_q, *, period, factor, qty, windows):
    """Immediate-flip entry for one (period,factor). Returns (pnl_sum, n_trades, n_flips, n_in_window)."""
    cfg = _v2_cfg()
    cfg.stop_loss_pct = EXIT_STOP_PCT
    engine = ExitEngine(cfg)
    ebook = _Book(entry_q)
    mbook = _Book(massive_q)

    def inwin(ts):
        return windows is None or any(a <= ts <= b for a, b in windows)

    pnl_sum = n_tr = n_in = 0
    flips = _touches(bars, period, factor)
    flat_after = None
    for bar_idx, _tms, touch_price in flips:
        if bars[bar_idx].volume <= VOL_FLOOR:
            continue
        bwin = ebook.slice(_utc(bars[bar_idx].ts), _utc(bars[bar_idx].ts + BAR_MS))
        tq = next((q for q in bwin if _px(q) >= touch_price), None)
        entry_ts = tq.ts if tq is not None else _utc(bars[bar_idx].ts + BAR_MS)
        if not inwin(entry_ts):
            continue
        n_in += 1
        if flat_after is not None and entry_ts < flat_after:
            continue
        fq = ebook.at(entry_ts)
        if fq is None or fq.ask <= 0:
            continue
        start = mbook.index_at_or_after(entry_ts)
        _ets, _w, pnl, _r, _n = _run_exit(massive_q, start, fq.ask, qty, cfg, engine)
        pnl_sum += pnl
        n_tr += 1
        flat_after = _ets
    return pnl_sum, n_tr, len(flips), n_in


def classify_metrics(bars):
    """Sweep-independent name metrics: median period-14 ATR%, Kaufman ER, median price."""
    if len(bars) < 20:
        return None
    closes = [b.close for b in bars if b.close > 0]
    if len(closes) < 20:
        return None
    rows = compute_atr_trail(bars, period=14, factor=1.0)   # factor 1 -> loss == raw ATR14
    atrp = [rows[i]["loss"] / bars[i].close * 100 for i in range(len(bars))
            if rows[i]["loss"] is not None and bars[i].close > 0]
    if not atrp:
        return None
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    er = net / path if path > 0 else 0.0
    return {"atr_pct14": statistics.median(atrp), "er": er, "price": statistics.median(closes)}


def _window(y, m, d):
    lo = datetime(y, m, d, 4, 0, tzinfo=_ET).astimezone(timezone.utc)
    hi = datetime(y, m, d, 20, 0, tzinfo=_ET).astimezone(timezone.utc)
    return lo, hi


def main():
    argv = sys.argv[1:]
    qty, feed, wdir, jsonp = 10, "massive", None, None
    for a in argv:
        if a.startswith("--qty="):
            qty = int(a.split("=", 1)[1])
        elif a.startswith("--feed="):
            feed = a.split("=", 1)[1]
        elif a.startswith("--windows-dir="):
            wdir = a.split("=", 1)[1]
        elif a.startswith("--json="):
            jsonp = a.split("=", 1)[1]
    dates = [a for a in argv if a.count("-") == 2 and a[:1].isdigit()]
    src = DbMarketDataSource(build_session_factory(get_settings()))
    out_rows = []
    for date in dates:
        y, m, d = (int(x) for x in date.split("-"))
        lo, hi = _window(y, m, d)
        wins_by_sym = load_windows(f"{wdir}/windows_{date}.json") if wdir else {}
        syms = src.v2_qualified_symbols(lo, hi)
        print(f"DAY {date}: {len(syms)} qualified", flush=True)
        for sym in syms:
            mq = src.quotes(sym, lo, hi)
            if feed == "massive":
                bars = bars_from_trades(src.trades(sym, lo, hi))
                entry_q = mq
            else:
                bars = src.schwab_bars(sym, lo, hi)
                entry_q = src.schwab_quotes(sym, lo, hi)
            if len(bars) < 20 or len(entry_q) == 0 or len(mq) == 0:
                continue
            met = classify_metrics(bars)
            wins = wins_by_sym.get(sym, []) if wdir else None
            combos = {}
            any_entry = False
            for p, f in COMBOS:
                pnl, n_tr, n_flips, n_in = simulate_flip(bars, entry_q, mq, period=p, factor=f, qty=qty, windows=wins)
                combos[f"{p}x{f}"] = {"pnl": round(pnl, 3), "n": n_tr, "flips": n_flips, "in": n_in}
                any_entry = any_entry or n_tr > 0
            if not any_entry:
                continue
            out_rows.append({"date": date, "sym": sym, "metrics": met, "combos": combos})
            mtxt = (f"price={met['price']:.2f} atr14%={met['atr_pct14']:.2f} er={met['er']:.2f}"
                    if met else "metrics=NA")
            print(f"  {sym:<6} {mtxt} | base(5x3.5) pnl={combos['5x3.5']['pnl']:+.2f} n={combos['5x3.5']['n']}", flush=True)
    payload = {"feed": feed, "qty": qty, "dates": dates, "combos": [f"{p}x{f}" for p, f in COMBOS],
               "baseline": "5x3.5", "exit_stop_pct": EXIT_STOP_PCT, "rows": out_rows}
    if jsonp:
        with open(jsonp, "w") as fh:
            json.dump(payload, fh)
        print(f"[json -> {jsonp}]  ({len(out_rows)} traded name-days)", flush=True)


if __name__ == "__main__":
    main()
