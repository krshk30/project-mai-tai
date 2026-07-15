"""Floor-mechanic sweep. Live fixed-floor vs the operator's ratchet, on real confirmed windows.

A) LIVE      floor pinned at +2%                      (what is deployed today)
B) OPERATOR  floor = max(+2%, (whole% reached) - 1%)  -- guarantees +2%, ratchets up. "free money"
C) ROOM      floor = max(+1%, (whole% reached) - 1%)  -- accepts +1% on faders to buy room to ride

Entry gate mirrors live sim(): rule7, ORB skip, confirmed[confirm->drop] window, 7:00-16:30 ET
entry window, reclaim OFF, exit bounded by the next ATR SELL flip.

usage: floor_sweep.py --dates 2026-07-14 [--syms NXTC SHPH] [--detail]
"""
import argparse
import bisect
from datetime import datetime, timezone

from atr_cw_v2 import confirmed_windows, in_win, _ET
from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.strategy_core.time_utils import is_fillable_et_session
from atr_wait3_oos import fetch_quotes
from scripts.atr_intrabar_run import Bar, c, tns, win

QTY, TGT, STOP, STEP = 2, 2.0, 5.0, 1.0
MODES = [("A", "LIVE      floor pinned +2% (deployed)", 2.0, None),
         ("B", "OPERATOR  floor=max(2%, int(peak))  <- fires at +3%", 2.0, "step"),
         ("E", "TRAIL 0.50% floor=max(2%, peak-0.50%)", 2.0, 0.50),
         ("F", "TRAIL 0.25% floor=max(2%, peak-0.25%)", 2.0, 0.25),
         ("G", "TRAIL 0.10% floor=max(2%, peak-0.10%)", 2.0, 0.10)]


def ets(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(_ET).strftime("%H:%M:%S")


def prep(sym, date, wins):
    warm_ms, trade_ms, hi_ms = win(date)
    aggs = sorted(c.list_aggs(sym, 1, "minute", date, date, limit=50000), key=lambda a: a.timestamp)
    bars = [Bar(a.timestamp, a.open, a.high, a.low, a.close, int(a.volume or 0))
            for a in aggs if warm_ms <= a.timestamp <= hi_ms]
    if len(bars) < 6:
        return None
    _bt = [b.ts for b in bars]
    _raw = sorted((tns(t) // 1_000_000, float(t.price))
                  for t in c.list_trades(sym, timestamp_gte=warm_ms * 1_000_000,
                                         timestamp_lte=hi_ms * 1_000_000, limit=50000))
    trades = []
    for _ms, _px in _raw:
        _bi = bisect.bisect_right(_bt, _ms) - 1
        if 0 <= _bi < len(bars) and bars[_bi].low <= _px <= bars[_bi].high:
            trades.append((_ms, _px))
    qms, qbid, qask, _cx, _np = fetch_quotes(sym, trade_ms, hi_ms)
    if not trades or not qms:
        return None
    rows = compute_atr_trail(bars, period=5, factor=3.5)
    return dict(sym=sym, bars=bars, trail=[r["trail"] for r in rows],
                buy_bars=[i for i, r in enumerate(rows) if r["flip"] == "BUY"],
                sell_bars=[i for i, r in enumerate(rows) if r["flip"] == "SELL"],
                pms=[p[0] for p in trades], ppx=[p[1] for p in trades],
                bt=_bt, qms=qms, qbid=qbid, qask=qask, trade_ms=trade_ms, wins=wins)


def entries_for(P):
    bars, bt, pms, ppx = P["bars"], P["bt"], P["pms"], P["ppx"]

    def low_so_far(bidx, upto):
        lo = bars[bidx].low
        for k in range(bisect.bisect_left(pms, bars[bidx].ts), bisect.bisect_right(pms, upto)):
            lo = min(lo, ppx[k])
        return lo

    def in_orb(ms):
        e = datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(_ET)
        return 9 * 60 + 30 <= e.hour * 60 + e.minute < 10 * 60

    def in_hours(ms):
        return is_fillable_et_session(datetime.fromtimestamp(ms / 1000, timezone.utc),
                                      7, 16, start_minute=0, end_minute=30)

    out = []
    for fbar in P["buy_bars"]:
        if fbar + 2 >= len(bars):
            continue
        trig = max(bars[fbar].high, bars[fbar + 1].high, bars[fbar + 2].high)
        fl = P["trail"][fbar - 1] if fbar > 0 and P["trail"][fbar - 1] is not None else P["trail"][fbar]
        seg_end_bar = next((s for s in P["sell_bars"] if s > fbar), len(bars) - 1)
        seg_end_ms = bars[seg_end_bar].ts + 60_000
        cursor = bars[fbar + 3].ts if fbar + 3 < len(bars) else seg_end_ms
        ems = None
        j = bisect.bisect_left(pms, cursor)
        while j < len(pms) and pms[j] < seg_end_ms:
            if (ppx[j] > trig and pms[j] >= P["trade_ms"] and in_win(pms[j], P["wins"])
                    and not in_orb(pms[j]) and in_hours(pms[j]) and fl is not None
                    and ppx[j] > fl
                    and low_so_far(bisect.bisect_right(bt, pms[j]) - 1, pms[j]) > fl):
                ems = pms[j]
                break
            j += 1
        if ems is None:
            continue
        ai = bisect.bisect_right(P["qms"], ems) - 1
        if ai < 0 or P["qask"][ai] <= 0:
            continue
        fbi = next((s for s in P["sell_bars"] if bars[s].ts + 60_000 > ems), None)
        out.append(dict(ems=ems, entry=P["qask"][ai],
                        flip_ms=(bars[fbi].ts + 60_000) if fbi is not None else seg_end_ms))
    return out


def potential(P, e):
    hi, hi_ms = -99.0, e["ems"]
    for qi in range(bisect.bisect_right(P["qms"], e["ems"]), len(P["qms"])):
        if P["qms"][qi] > e["flip_ms"]:
            break
        if P["qbid"][qi] > 0:
            pct = 100.0 * (P["qbid"][qi] - e["entry"]) / e["entry"]
            if pct > hi:
                hi, hi_ms = pct, P["qms"][qi]
    return hi, hi_ms


def run(P, e, base, ratchet):
    """base=None -> NO target/floor at all: ride to the ATR flip, only the -5% stop protects."""
    entry, qms, qbid = e["entry"], P["qms"], P["qbid"]
    hard = entry * (1 - STOP / 100)
    if base is None:
        peak = -99.0
        for qi in range(bisect.bisect_right(qms, e["ems"]), len(qms)):
            if qms[qi] > e["flip_ms"]:
                break
            b = qbid[qi]
            if b <= 0:
                continue
            peak = max(peak, 100.0 * (b - entry) / entry)
            if b <= hard:
                return dict(px=b, why="HARDSTOP", xms=qms[qi], peak=peak, log=[])
        bi = bisect.bisect_right(qms, e["flip_ms"]) - 1
        return dict(px=qbid[bi] if bi >= 0 and qbid[bi] > 0 else entry, why="ATRFLIP",
                    xms=e["flip_ms"], peak=peak, log=[])
    armed, floor_pct, peak, log = False, base, -99.0, []
    for qi in range(bisect.bisect_right(qms, e["ems"]), len(qms)):
        if qms[qi] > e["flip_ms"]:
            break
        b = qbid[qi]
        if b <= 0:
            continue
        pct = 100.0 * (b - entry) / entry
        peak = max(peak, pct)
        if not armed:
            if pct >= TGT:
                armed = True
                log.append("    %s bid %.4f (%+.2f%%) ARM floor +%.1f%%" % (ets(qms[qi]), b, pct, base))
            elif b <= hard:
                return dict(px=b, why="HARDSTOP", xms=qms[qi], peak=peak, log=log)
            else:
                continue
        if ratchet == "step":
            cand = max(base, float(int(pct)))     # operator: floor = highest whole % REACHED
            if cand > floor_pct:
                floor_pct = cand
                log.append("    %s bid %.4f (%+.2f%%) -> floor UP to +%.2f%%"
                           % (ets(qms[qi]), b, pct, floor_pct))
        elif isinstance(ratchet, float):
            cand = max(base, peak - ratchet)      # continuous trail below the running peak
            if cand > floor_pct:
                floor_pct = cand
                log.append("    %s bid %.4f (%+.2f%%) -> floor UP to +%.2f%%"
                           % (ets(qms[qi]), b, pct, floor_pct))
        lvl = entry * (1 + floor_pct / 100)
        if b <= lvl:
            return dict(px=lvl, why="FLOOR+%.2f%%" % floor_pct, xms=qms[qi], peak=peak, log=log)
    bi = bisect.bisect_right(qms, e["flip_ms"]) - 1
    return dict(px=qbid[bi] if bi >= 0 and qbid[bi] > 0 else entry, why="ATRFLIP",
                xms=e["flip_ms"], peak=peak, log=log)


ap = argparse.ArgumentParser()
ap.add_argument("--dates", nargs="+", required=True)
ap.add_argument("--syms", nargs="*")
ap.add_argument("--detail", action="store_true")
a = ap.parse_args()

tot = {m[0]: 0.0 for m in MODES}
nt = 0
rows_out = []
for date in a.dates:
    allw = confirmed_windows(date)
    syms = a.syms if a.syms else sorted(allw.keys())
    for sym in syms:
        w = allw.get(sym, [])
        if not w:
            continue
        try:
            P = prep(sym, date, w)
        except Exception as ex:
            print("  %s %s prep failed: %s" % (date, sym, ex))
            continue
        if not P:
            continue
        for e in entries_for(P):
            nt += 1
            hi, hi_ms = potential(P, e)
            res = {}
            for key, label, base, ratchet in MODES:
                r = run(P, e, base, ratchet)
                pnl = QTY * (r["px"] - e["entry"])
                tot[key] += pnl
                res[key] = (100 * (r["px"] - e["entry"]) / e["entry"], pnl, r)
            pk = res["A"][2]["peak"]
            rows_out.append((date, sym, e, hi, hi_ms, res, pk))
            if a.detail:
                print("\n%s %s  ENTER %s @ %.4f   (max bid available before flip: %+.2f%% at %s)"
                      % (date, sym, ets(e["ems"]), e["entry"], hi, ets(hi_ms)))
                for key, label, base, ratchet in MODES:
                    ret, pnl, r = res[key]
                    print("  %s %s" % (key, label))
                    for ln in (r["log"] or ["    (never armed)"]):
                        print(ln)
                    print("    %s EXIT %.4f [%s] = %+.2f%%  $%+.2f  (peak %+.2f%%)"
                          % (ets(r["xms"]), r["px"], r["why"], ret, pnl, r["peak"]))

print("\n" + "=" * 92)
print("WINNERS ONLY.  PEAK% = highest bid reached BEFORE the pullback that exits us.")
print("%-10s %-6s %-9s %8s %7s | %s" % ("DATE", "SYM", "ENTRY ET", "AVAIL%", "PEAK%",
                                    "  ".join("%s:%7s" % (m[0], "ret%") for m in MODES)))
print("-" * 92)
for date, sym, e, hi, hi_ms, res, pk in rows_out:
    if res["A"][0] < 1.0:
        continue                                   # winners only: the ratchet only exists post-arm
    print("%-10s %-6s %-9s %+7.2f%% %+7.2f%% | %s" % (
        date, sym, ets(e["ems"]), hi, pk,
        "  ".join("%s:%+6.2f%%" % (k, res[k][0]) for k, _l, _b, _r in MODES)))
print("=" * 92)
print("TRADES: %d   @qty%d" % (nt, QTY))
for key, label, _b, _r in MODES:
    print("  %s  %-38s  $%+7.2f" % (key, label, tot[key]))
print("\n  vs LIVE (A) -- every mode is max(2%, ...) so none can ever book below +2%:")
for key, label, _b, _r in MODES[1:]:
    print("    %s  %-44s  $%+.2f" % (key, label, tot[key] - tot["A"]))
