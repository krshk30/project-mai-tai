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
from statistics import median, pstdev
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


def _firstbar(trades, quotes, so):
    """First 1-min bar (09:30-09:31) tick signature: price dynamics, participation, pressure, liquidity."""
    t1 = [t for t in trades if so <= t.ts < so + timedelta(seconds=60)]
    q1 = [q for q in quotes if so <= q.ts < so + timedelta(seconds=60)]
    if len(t1) < 5:
        return None
    px = [t.price for t in t1]
    sz = [t.size for t in t1]
    real = sum(abs(px[i] - px[i - 1]) for i in range(1, len(px)))          # intra-bar realized path
    net = px[-1] - px[0]
    iber = abs(net) / real if real > 0 else 0                              # intra-bar efficiency ratio
    realpct = real / px[0] * 100 if px[0] > 0 else 0
    cnt, vol = len(t1), sum(sz)
    h = len(t1) // 2
    v1, v2 = sum(sz[:h]), sum(sz[h:])
    vacc = v2 / v1 if v1 > 0 else 0                                        # volume acceleration (2nd half / 1st)
    upv = dnv = 0
    for i in range(1, len(t1)):
        if px[i] > px[i - 1]:
            upv += sz[i]
        elif px[i] < px[i - 1]:
            dnv += sz[i]
    press = upv / (upv + dnv) if (upv + dnv) > 0 else 0.5                  # up-tick volume share
    cv = tv = above = 0
    for t in t1:
        cv += t.price * t.size
        tv += t.size
        above += 1 if t.price > (cv / tv if tv > 0 else t.price) else 0
    spr = [(q.ask - q.bid) / ((q.ask + q.bid) / 2) * 100 for q in q1 if q.ask > 0 and q.bid > 0 and q.ask >= q.bid]
    return {"iber": iber, "realpct": realpct, "cnt": cnt, "vol": vol, "avgsz": vol / cnt,
            "vrate": vol / 60, "vacc": vacc, "press": press, "pvwap": above / cnt,
            "spr": median(spr) if spr else None, "sprvol": pstdev(spr) if len(spr) > 1 else 0,
            "qrate": len(q1) / 60, "price": px[0]}


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
                         "ptime": _etstr(pm[peak_i].timestamp), "cat": classify(peak_pct, hold), "pnl": pnl,
                         "fb": _firstbar(tr, q, so)})

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

    # ---- TABLE 2: first-bar (09:30-09:31) tick signature ----
    fbrows = [r for r in rows if r.get("fb")]
    print("\n\n" + "=" * 110 + "\nFIRST-BAR TICK SIGNATURE (09:30-09:31)  — price dynamics | participation | pressure | liquidity\n" + "=" * 110)
    h2 = (f"{'date':<11}{'sym':<7}{'ib_ER':>6}{'realVol%':>9}{'trades':>7}{'vol':>8}{'avgSz':>7}"
          f"{'vAccel':>7}{'up%':>6}{'>vwap':>7}{'spr%':>6}{'sprVol':>7}{'qRate':>7}{'price':>7}  {'cat':<8}{'pnl':>7}")
    print(h2)
    print("-" * len(h2))
    for cat in ["RUNNER", "GRINDER", "CHOPPY"]:
        for r in sorted([r for r in fbrows if r["cat"] == cat], key=lambda r: r["pnl"], reverse=True):
            f = r["fb"]
            sprs = f"{f['spr']:.2f}" if f["spr"] is not None else "  -"
            print(f"{r['date']:<11}{r['sym']:<7}{f['iber']:>6.2f}{f['realpct']:>8.1f}%{f['cnt']:>7}{f['vol']/1000:>7.0f}K"
                  f"{f['avgsz']:>7.0f}{f['vacc']:>7.2f}{f['press']*100:>5.0f}%{f['pvwap']*100:>6.0f}%{sprs:>6}"
                  f"{f['sprvol']:>7.2f}{f['qrate']:>7.1f}{f['price']:>7.2f}  {cat:<8}{r['pnl']:>+7.2f}")

    # ---- SEPARATION PREVIEW: winners vs losers vs zero-P&L feature means ----
    print("\n\n" + "=" * 96 + "\nSEPARATION PREVIEW — mean feature by outcome (does volume/liquidity split them?)\n" + "=" * 96)
    def grp(pred):
        return [r for r in fbrows if pred(r["pnl"])]
    buckets = [("WINNERS (pnl>0)", grp(lambda p: p > 0.005)),
               ("LOSERS  (pnl<0)", grp(lambda p: p < -0.005)),
               ("ZERO    (~0)", grp(lambda p: -0.005 <= p <= 0.005))]
    print(f"  {'bucket':<18}{'n':>3}{'peak%':>7}{'hold':>6}{'pmVol(M)':>9}{'pmTr(K)':>8}{'fb_vol(K)':>10}{'fb_spr%':>8}{'fb_ibER':>8}{'up%':>6}")
    for lbl, g in buckets:
        if not g:
            continue
        def mean(key, f=lambda r: r):
            vs = [f(r) for r in g if f(r) is not None]
            return sum(vs) / len(vs) if vs else 0
        pmv = mean(None, lambda r: r["vol"] / 1e6)
        pmt = mean(None, lambda r: r["tr"] / 1000)
        fbv = mean(None, lambda r: r["fb"]["vol"] / 1000)
        fbs = mean(None, lambda r: r["fb"]["spr"])
        fbe = mean(None, lambda r: r["fb"]["iber"])
        up = mean(None, lambda r: r["fb"]["press"] * 100)
        pk = mean(None, lambda r: r["peak"] * 100)
        hd = mean(None, lambda r: r["hold"])
        print(f"  {lbl:<18}{len(g):>3}{pk:>6.0f}%{hd:>6.2f}{pmv:>9.1f}{pmt:>8.0f}{fbv:>10.0f}{fbs:>8.2f}{fbe:>8.2f}{up:>5.0f}%")


if __name__ == "__main__":
    main()
