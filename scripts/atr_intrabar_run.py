"""ATR INTRABAR naked test (RESEARCH; off the main engine, nothing shipped/live).

Intrabar stop-and-reverse on Polygon TICKS: bar-based ATR trail (period 5, factor 3.5, Wilders); the
FLIP fires INTRABAR when a trade PRINT crosses the resting trail (a live stop order). Enter LONG at the
ASK at the trigger; exit at the BID on the intrabar flip-short, or a profit target (+2% / +3% / none).
Window 09:00-16:00 ET. Universe = existing v2 pull, gated to names >=30% day-change AT 9 AM (else
excluded, logged). Feed = Polygon full tape (bars+trades+quotes). Off-hours, niced.

    python -m scripts.atr_intrabar_run --range 2026-06-25 2026-07-09 --json=OUT.json
"""
from __future__ import annotations

import bisect
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from massive import RESTClient
from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.backtest.data import DbMarketDataSource
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

ET = ZoneInfo("America/New_York")
QTY = 10
CONFIGS = [("+2%", 2.0), ("+3%", 3.0), ("flip_only", None)]
MIN_CHG_9AM = 30.0
c = RESTClient(api_key=os.environ["MAI_TAI_MASSIVE_API_KEY"], retries=1, read_timeout=40)


def tns(x):
    return int(getattr(x, "sip_timestamp", None) or getattr(x, "participant_timestamp", 0))


def win(date):
    y, m, d = (int(x) for x in date.split("-"))
    warm = datetime(y, m, d, 7, 0, tzinfo=ET).astimezone(timezone.utc)    # trail warm-up (pre-market)
    trade = datetime(y, m, d, 9, 0, tzinfo=ET).astimezone(timezone.utc)   # trading starts 9AM
    hi = datetime(y, m, d, 16, 0, tzinfo=ET).astimezone(timezone.utc)
    return int(warm.timestamp() * 1000), int(trade.timestamp() * 1000), int(hi.timestamp() * 1000)


def prior_close(sym, date):
    y, m, d = (int(x) for x in date.split("-"))
    start = (datetime(y, m, d) - timedelta(days=7)).strftime("%Y-%m-%d")
    day0 = int(datetime(y, m, d, tzinfo=ET).astimezone(timezone.utc).timestamp() * 1000)
    days = sorted(c.list_aggs(sym, 1, "day", start, date, limit=60), key=lambda a: a.timestamp)
    prev = [a for a in days if a.timestamp < day0]
    return prev[-1].close if prev else None


def intrabar_flips(bars, prints):
    rows = compute_atr_trail(bars, period=5, factor=3.5)
    loss = [r["loss"] for r in rows]
    bt = [b.ts for b in bars]
    pbybar = {}
    for ms, px in prints:
        j = bisect.bisect_right(bt, ms) - 1
        if 0 <= j < len(bars):
            pbybar.setdefault(j, []).append((ms, px))
    state = trail = None
    flips = []
    for i in range(len(bars)):
        if loss[i] is None:
            continue
        if state is None:
            state, trail = "long", bars[i].close - loss[i]
            continue
        # RESTING trail is FIXED for the whole bar (a live stop rests until the next bar). ONE flip max.
        flipped = False
        for ms, px in sorted(pbybar.get(i, [])):
            if state == "short" and px > trail:
                flips.append((ms, "BUY", i))
                state = "long"
                flipped = True
                break
            if state == "long" and px < trail:
                flips.append((ms, "SELL", i))
                state = "short"
                flipped = True
                break
        # new trail set at bar close (fresh on a flip, else ratchet in the trend direction)
        if state == "long":
            trail = (bars[i].close - loss[i]) if flipped else max(trail, bars[i].close - loss[i])
        else:
            trail = (bars[i].close + loss[i]) if flipped else min(trail, bars[i].close + loss[i])
    return flips


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def simulate(bars, prints, qms, qbid, qask, trade_ms, hi_ms):
    """One trade per intrabar BUY flip (from 9AM) -> next SELL flip (bid) or target. ask entry / bid exit."""
    flips = intrabar_flips(bars, prints)
    pms = [p[0] for p in prints]
    ppx = [p[1] for p in prints]
    out = []
    for k, (bms, _side, _bar) in [(k, f) for k, f in enumerate(flips)
                                  if f[1] == "BUY" and f[0] >= trade_ms]:
        nxt = next((f for f in flips[k + 1:] if f[1] == "SELL"), None)
        sell_ms = nxt[0] if nxt else hi_ms
        sell_bar = nxt[2] if nxt else len(bars) - 1
        ai = bisect.bisect_right(qms, bms) - 1
        if ai < 0 or qask[ai] <= 0:
            continue
        entry = qask[ai]
        spread = round(qask[ai] - qbid[ai], 4) if qbid[ai] > 0 else None
        seg = [ppx[j] for j in range(bisect.bisect_left(pms, bms), bisect.bisect_right(pms, sell_ms))]
        hi = max(seg, default=entry)
        lo = min(seg, default=entry)
        nb = next((f[2] for f in flips[k + 1:] if f[1] == "BUY"), None)
        seg_bars = (nb - sell_bar) if (nxt and nb) else (None if not nxt else len(bars) - sell_bar)
        exits = {}
        for lab, tgt in CONFIGS:
            hit = None
            if tgt:
                tp = entry * (1 + tgt / 100)
                for qi in range(bisect.bisect_right(qms, bms), len(qms)):
                    if qms[qi] > sell_ms:
                        break
                    if qbid[qi] >= tp:
                        hit = (qms[qi], round(tp, 4))
                        break
            if hit:
                exits[lab] = {"ts": _iso(hit[0]), "px": hit[1], "reason": "TARGET",
                              "secs_to_tgt": round((hit[0] - bms) / 1000, 1)}
            else:
                si = bisect.bisect_right(qms, sell_ms) - 1
                bpx = qbid[si] if (si >= 0 and qbid[si] > 0) else entry
                exits[lab] = {"ts": _iso(sell_ms), "px": round(bpx, 4),
                              "reason": "ATR_FLIP" if nxt else "EOD"}
        bid_at = {}
        for tsec in (180, 300, 600):                        # bid at entry+3/5/10 min (for time-stop tests)
            tms = bms + tsec * 1000
            if tms <= sell_ms:
                qi = bisect.bisect_right(qms, tms) - 1
                bid_at[str(tsec)] = round(qbid[qi], 4) if (qi >= 0 and qbid[qi] > 0) else None
            else:
                bid_at[str(tsec)] = None                    # flip came before this mark
        out.append({"entry_ts": _iso(bms), "entry_px": round(entry, 4), "spread": spread,
                    "mfe_pct": round(100 * (hi - entry) / entry, 3),
                    "mae_pct": round(100 * (lo - entry) / entry, 3),
                    "short_seg_bars": seg_bars, "sell_ts": _iso(sell_ms), "bid_at": bid_at,
                    "exits": exits})
    return out


def _dates(argv):
    ds = [a for a in argv if a.count("-") == 2 and a[:1].isdigit()]
    if len(ds) == 2 and "--range" in argv:
        s = datetime.strptime(ds[0], "%Y-%m-%d").date()
        e = datetime.strptime(ds[1], "%Y-%m-%d").date()
        out, cur = [], s
        while cur <= e:
            if cur.weekday() < 5:
                out.append(cur.isoformat())
            cur += timedelta(days=1)
        return out
    return ds


def _fetch_quotes(sym, lo_ms, hi_ms):
    qms, qbid, qask = [], [], []
    for q in c.list_quotes(sym, timestamp_gte=lo_ms * 1_000_000, timestamp_lte=hi_ms * 1_000_000,
                           limit=50000):
        b = float(getattr(q, "bid_price", 0) or 0)
        a = float(getattr(q, "ask_price", 0) or 0)
        if b > 0 and a > 0:
            qms.append(tns(q) // 1_000_000)
            qbid.append(b)
            qask.append(a)
    order = sorted(range(len(qms)), key=lambda i: qms[i])
    return [qms[i] for i in order], [qbid[i] for i in order], [qask[i] for i in order]


def main():
    argv = sys.argv[1:]
    jsonp = next((a.split("=", 1)[1] for a in argv if a.startswith("--json=")), "atr_intrabar.json")
    src = DbMarketDataSource(build_session_factory(get_settings()))
    out = {"qty": QTY, "window": "09:00-16:00 ET", "gate": ">=30% day-change at 9AM",
           "name_days": [], "universe_log": [], "excluded": []}
    for date in _dates(argv):
        warm_ms, trade_ms, hi_ms = win(date)
        lo = datetime.fromtimestamp(trade_ms / 1000, timezone.utc)
        hi = datetime.fromtimestamp(hi_ms / 1000, timezone.utc)
        try:
            cand = src.v2_qualified_symbols(lo, hi)
        except Exception as e:                                       # noqa: BLE001
            print(f"{date}: universe err {e}", flush=True)
            continue
        kept = []
        for sym in cand:
            try:
                pc = prior_close(sym, date)
                aggs = sorted(c.list_aggs(sym, 1, "minute", date, date, limit=50000),
                              key=lambda a: a.timestamp)
                bars = [Bar(a.timestamp, a.open, a.high, a.low, a.close, int(a.volume or 0))
                        for a in aggs if warm_ms <= a.timestamp <= hi_ms]   # trail warmed from 07:00
                bar9 = next((b for b in bars if b.ts >= trade_ms), None)     # the 9AM bar
                if not pc or not bars or bar9 is None:
                    out["excluded"].append({"date": date, "sym": sym, "why": "no prior-close/bars"})
                    continue
                chg = 100 * (bar9.open - pc) / pc
                if chg < MIN_CHG_9AM:
                    out["excluded"].append({"date": date, "sym": sym, "chg_9am": round(chg, 1),
                                            "why": "<30% at 9AM"})
                    continue
                trades = sorted((tns(t) // 1_000_000, float(t.price))
                                for t in c.list_trades(sym, timestamp_gte=warm_ms * 1_000_000,
                                                       timestamp_lte=hi_ms * 1_000_000, limit=50000))
                qms, qbid, qask = _fetch_quotes(sym, trade_ms, hi_ms)
                if not trades or not qms:
                    out["excluded"].append({"date": date, "sym": sym, "why": "no ticks"})
                    continue
                trs = simulate(bars, trades, qms, qbid, qask, trade_ms, hi_ms)
                kept.append({"sym": sym, "chg_9am": round(chg, 1)})
                if trs:
                    out["name_days"].append({"date": date, "sym": sym, "chg_9am": round(chg, 1),
                                             "trades": trs})
                print(f"  {date} {sym}: +{chg:.0f}% @9AM, {len(trs)} intrabar trades", flush=True)
            except Exception as e:                                   # noqa: BLE001
                print(f"  {date} {sym}: err {e}", flush=True)
        out["universe_log"].append({"date": date, "kept": kept})
    with open(jsonp, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"\n[dumped {sum(len(nd['trades']) for nd in out['name_days'])} trades over "
          f"{len(out['name_days'])} name-days; {len(out['excluded'])} excluded -> {jsonp}]", flush=True)


if __name__ == "__main__":
    main()
