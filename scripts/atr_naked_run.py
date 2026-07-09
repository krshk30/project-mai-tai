"""ATR NAKED signal test — is the raw ATR flip worth anything? RESEARCH ONLY (off the main engine;
nothing shipped, nothing live, no production flags).

ONLY new thing = the exit driver. Everything else is the existing engine:
  - existing trail:          compute_atr_trail(period 5, factor 3.5, Wilders)
  - existing universe pull:  DbMarketDataSource.v2_qualified_symbols (tracked UNION traded)
  - existing ask/bid fills:  entry = Schwab ASK; exit = massive BID
  - existing MFE/MAE/drop-one reporting (atr_naked_report.py)

Entry: ATR flips LONG (bar close crosses up) -> enter at the flip bar's CLOSE, fill at the ASK.
Exit (NEW driver, BYPASSES the OMS ladder — no hard stop, no scales, no floor): close the FULL
position on ATR flip SHORT (bid) OR a profit target, whichever first. Three configs: +2%, +3%, and
NO target (ride until the flip). Window 07:00-18:00 ET. Off-hours, niced.

    python -m scripts.atr_naked_run --range 2026-06-25 2026-07-09 --json=OUT.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import ATR_FACTOR, ATR_PERIOD, compute_atr_trail
from project_mai_tai.backtest.data import DbMarketDataSource
from project_mai_tai.backtest.v2_sim import BAR_MS, _Book, _utc
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")
QTY = 10
CONFIGS = [("+2%", 2.0), ("+3%", 3.0), ("flip_only", None)]
FORCE_INCLUDE = {("2026-07-09", "VRAX")}   # explicit per operator


def _window(date):
    y, m, d = (int(x) for x in date.split("-"))
    lo = datetime(y, m, d, 7, 0, tzinfo=_ET).astimezone(timezone.utc)   # 07:00 ET
    hi = datetime(y, m, d, 18, 0, tzinfo=_ET).astimezone(timezone.utc)  # 18:00 ET
    return lo, hi


def _first_bid_ge(mq, start_idx, t1, tp):
    """First massive quote after start_idx with ts <= t1 and bid >= tp -> (ts, bid) | None."""
    for q in mq[start_idx:]:
        if q.ts > t1:
            return None
        if q.bid >= tp:
            return q.ts, q.bid
    return None


def simulate_atr_naked(bars, sq, mq):
    """One trade per BUY flip. Entry = ask at flip-bar close. Each config exits on flip-short (bid) or
    target (bid), whichever first — NO ladder. Returns per-trade dicts with an exit per config."""
    rows = compute_atr_trail(bars, period=ATR_PERIOD, factor=ATR_FACTOR)
    sbook, mbook = _Book(sq), _Book(mq)
    sells = [i for i, r in enumerate(rows) if r["flip"] == "SELL"]
    buys = [i for i, r in enumerate(rows) if r["flip"] == "BUY"]
    trades = []
    for i in buys:
        entry_ts = _utc(bars[i].ts + BAR_MS)                 # flip bar CLOSE
        fq = sbook.at(entry_ts)
        if fq is None or fq.ask <= 0:
            continue
        entry_px = fq.ask                                    # ASK fill
        spread = round(fq.ask - fq.bid, 4) if fq.bid > 0 else None
        s = next((j for j in sells if j > i), None)
        end_bar = s if s is not None else len(bars) - 1
        flip_ts = _utc(bars[end_bar].ts + BAR_MS)
        flip_reason = "ATR_FLIP" if s is not None else "EOD"
        # bars in the SHORT segment the exit-flip opens (small = flip fired on noise; large = real reversal)
        if s is not None:
            nb = next((b for b in buys if b > s), None)
            short_seg_bars = (nb - s) if nb is not None else (len(bars) - s)
        else:
            short_seg_bars = None
        seg = bars[i + 1:end_bar + 1]
        hi = max((b.high for b in seg), default=entry_px)
        lo = min((b.low for b in seg), default=entry_px)
        start_idx = mbook.index_at_or_after(entry_ts)
        exits = {}
        for label, tgt in CONFIGS:
            tp = entry_px * (1 + tgt / 100) if tgt else None
            hit = _first_bid_ge(mq, start_idx, flip_ts, tp) if tgt else None
            if hit is not None:
                # close AT the target (a +2%/+3% limit fills at the target), NOT the overshooting bid.
                # `secs_to_tgt` surfaces feed-gap artifacts (target "hit" instantly = ask/bid feed mismatch).
                secs = (hit[0] - entry_ts).total_seconds()
                exits[label] = {"ts": hit[0].isoformat(), "px": round(tp, 4), "reason": "TARGET",
                                "secs_to_tgt": round(secs, 1)}
            else:
                bq = mbook.at(flip_ts)
                px = bq.bid if (bq and bq.bid > 0) else entry_px
                exits[label] = {"ts": flip_ts.isoformat(), "px": round(px, 4), "reason": flip_reason}
        trades.append({
            "entry_ts": entry_ts.isoformat(), "entry_px": round(entry_px, 4), "spread": spread,
            "mfe_pct": round(100 * (hi - entry_px) / entry_px, 3),
            "mae_pct": round(100 * (lo - entry_px) / entry_px, 3),
            "short_seg_bars": short_seg_bars,
            "flip_ts": flip_ts.isoformat(), "exits": exits})
    return trades


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


def main():
    argv = sys.argv[1:]
    jsonp = next((a.split("=", 1)[1] for a in argv if a.startswith("--json=")), "atr_naked.json")
    src = DbMarketDataSource(build_session_factory(get_settings()))
    out = {"qty": QTY, "window": "07:00-18:00 ET", "name_days": []}
    universe_log = []
    for date in _dates(argv):
        lo, hi = _window(date)
        try:
            syms = src.v2_qualified_symbols(lo, hi)
        except Exception as e:                                # noqa: BLE001
            print(f"{date}: universe err {e}", flush=True)
            continue
        forced = [s for (d, s) in FORCE_INCLUDE if d == date and s not in syms]
        if forced:
            syms = sorted(set(syms) | set(forced))
            print(f"{date}: FORCED IN {forced}", flush=True)
        universe_log.append({"date": date, "n": len(syms), "symbols": syms, "forced": forced})
        print(f"{date}: {len(syms)} qualified (existing v2 pull) -> {syms}", flush=True)
        for sym in syms:
            try:
                bars = src.schwab_bars(sym, lo, hi)
                sq = src.schwab_quotes(sym, lo, hi)
                mq = src.quotes(sym, lo, hi)
            except Exception as e:                            # noqa: BLE001
                print(f"  {sym}: feed err {e}", flush=True)
                continue
            if len(bars) < 10 or not sq or not mq:
                continue
            trs = simulate_atr_naked(bars, sq, mq)
            if trs:
                out["name_days"].append({"date": date, "sym": sym, "trades": trs})
                print(f"  {sym}: {len(trs)} BUY-flip trades", flush=True)
    out["universe_log"] = universe_log
    with open(jsonp, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"\n[dumped {sum(len(nd['trades']) for nd in out['name_days'])} trades over "
          f"{len(out['name_days'])} name-days -> {jsonp}]", flush=True)


if __name__ == "__main__":
    main()
