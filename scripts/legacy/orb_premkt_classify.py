"""PRE-MARKET (7 AM -> 9:30) name classifier — the operator's method: look back at the whole pre-open
trajectory, not one bar. RUNNER = big run that HOLDS momentum into the open (flips but recovers, still
70-80%). GRINDER = ran then faded all the way down but keeps minimal momentum. CHOPPY = never enough
magnitude (20-30%). Pulls pre-market 1-min bars from POLYGON (arbitrary tickers; our capture stream
doesn't have full pre-market). REPORT ONLY — categorize for review before building the separation.

Features per name-day (Polygon pre-market 07:00-09:30 ET + prev-day close):
  peak%     max (high-prev_close)/prev_close        magnitude of the run
  open%     (9:30 price - prev_close)/prev_close     what's retained at the open
  hold      open% / peak%                            persistence (1=holds highs, low=faded)
  drawdn    deepest pullback from the pre-mkt peak   how hard it faded
  swings    # of >5% direction reversals             choppiness of the path
  pm_er     |last-first|/path over pre-mkt closes    directional efficiency
  pm_vol/tr pre-market volume & trade count          participation
pnl column = fixed-2% PR outcome, CONTEXT ONLY (the winner/loser separation comes after you approve).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.orb_sim import simulate_orb_tick_entry
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")
GATE, UNGATE_MIN = 4.3, 4
CHOP_MAG = 0.30      # peak% below this = CHOPPY (insufficient magnitude)
HOLD_RUN = 0.60      # hold-ratio at/above this (with magnitude) = RUNNER


def _et(y, mo, d, h, m):
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def _etstr(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(_ET).strftime("%H:%M")


def _prev_close(c, sym, date):
    y, mo, d = (int(x) for x in date.split("-"))
    frm = (datetime(y, mo, d) - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        days = list(c.list_aggs(sym, 1, "day", frm, date, limit=20))
    except Exception:
        return None
    prev = [b for b in days if datetime.fromtimestamp(b.timestamp / 1000, timezone.utc).astimezone(_ET).date()
            < datetime(y, mo, d).date()]
    return prev[-1].close if prev else None


def _premkt(c, sym, date):
    try:
        mins = list(c.list_aggs(sym, 1, "minute", date, date, limit=50000))
    except Exception:
        return []
    out = []
    for m in mins:
        t = datetime.fromtimestamp(m.timestamp / 1000, timezone.utc).astimezone(_ET).time()
        if t >= datetime(2000, 1, 1, 7, 0).time() and t < datetime(2000, 1, 1, 9, 30).time():
            out.append(m)
    return out


def classify(peak_pct, hold):
    if peak_pct is None or peak_pct < CHOP_MAG:
        return "CHOPPY"
    return "RUNNER" if hold >= HOLD_RUN else "GRINDER"


def main():
    src = DbMarketDataSource(build_session_factory(get_settings()))
    from massive import RESTClient
    c = RESTClient(api_key=get_settings().massive_api_key, retries=0, read_timeout=20)
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
            pc = _prev_close(c, sym, date)
            pm = _premkt(c, sym, date)
            if not pc or len(pm) < 10:
                rows.append({"date": date, "sym": sym, "skip": "no premkt/prev-close"})
                continue
            highs = [(b.high - pc) / pc for b in pm]
            peak_pct = max(highs)
            peak_i = highs.index(peak_pct)
            open_pct = (pm[-1].close - pc) / pc
            hold = open_pct / peak_pct if peak_pct > 0 else 0
            # deepest pullback from the pre-market peak
            peak_px = max(b.high for b in pm)
            trough_after = min((b.low for b in pm[peak_i:]), default=peak_px)
            drawdn = (peak_px - trough_after) / peak_px if peak_px > 0 else 0
            closes = [b.close for b in pm]
            rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1] > 0]
            swings = sum(1 for i in range(1, len(rets)) if rets[i] * rets[i - 1] < 0 and abs(rets[i]) > 0.05)
            net = abs(closes[-1] - closes[0])
            path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
            pm_er = net / path if path > 0 else 0
            vol = sum(b.volume for b in pm)
            trc = sum(getattr(b, "transactions", 0) or 0 for b in pm)
            pnl = sum(t.pnl for t in simulate_orb_tick_entry(
                tr, q, gap_cap_pct=1.5, trail_pct=2.0, qty=5, observe_open=obs, session_open=so,
                cutoff=cut, capped=False, latency_s=3.0, entry_windows=ewin,
                atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60, bars=build_bars(tr, so)))
            rows.append({"date": date, "sym": sym, "peak": peak_pct, "open": open_pct, "hold": hold,
                         "dd": drawdn, "sw": swings, "er": pm_er, "vol": vol, "tr": trc,
                         "ptime": _etstr(pm[peak_i].timestamp), "cat": classify(peak_pct, hold), "pnl": pnl})

    print(f"PRE-MARKET CLASSIFICATION (Polygon 07:00-09:30 ET)  |  CHOP_MAG={CHOP_MAG:.0%}  HOLD_RUN={HOLD_RUN:.0%}\n")
    hdr = f"{'date':<11}{'sym':<7}{'peak%':>7}{'open%':>7}{'hold':>6}{'draw%':>7}{'sw':>3}{'pm_er':>6}{'pmVol':>8}{'pmTr':>7}{'peak@':>7}  {'CATEGORY':<9}{'pnl':>7}"
    print(hdr)
    print("-" * len(hdr))
    for cat in ["RUNNER", "GRINDER", "CHOPPY"]:
        grp = [r for r in rows if r.get("cat") == cat]
        for r in sorted(grp, key=lambda r: r["peak"], reverse=True):
            print(f"{r['date']:<11}{r['sym']:<7}{r['peak']*100:>6.0f}%{r['open']*100:>6.0f}%{r['hold']:>6.2f}"
                  f"{r['dd']*100:>6.0f}%{r['sw']:>3}{r['er']:>6.2f}{r['vol']/1e6:>6.1f}M{r['tr']/1000:>5.0f}K"
                  f"{r['ptime']:>7}  {cat:<9}{r['pnl']:>+7.2f}")
        if grp:
            p = [r["pnl"] for r in grp]
            w = sum(1 for x in p if x > 0.005)
            print(f"  -> {cat}: n={len(grp)}  total_pnl={sum(p):>+7.2f}  mean={sum(p)/len(p):>+6.2f}  winners={w}/{len(grp)}\n")
    skipped = [r for r in rows if "skip" in r]
    if skipped:
        print("skipped (no premkt/prev-close): " + ", ".join(f"{r['sym']}/{r['date'][5:]}" for r in skipped))


if __name__ == "__main__":
    main()
