"""CLRO 2026-07-06 per-tick wick trace from POLYGON REST (full tape; market_capture is blank because
CLRO ran from the RTH open, not a pre-market gap the scanner streamed). Question: does the tick-driven
2% trail fire on the intrabar wick-DOWN (~6.4) at 09:35 before the same-candle recovery (~6.76) and the
run to 7.15+? Shows the 09:35 candle OHLC, the entry, the per-tick 2% trail (bid/HWM/stop) around 09:35,
and the price the run reached after any exit.
"""
from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import Quote, Trade, build_bars
from project_mai_tai.backtest.fill import QuoteBook, entry_fill
from project_mai_tai.backtest.orb_sim import simulate_orb_tick_entry
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")
SYM, DATE = "CLRO", "2026-07-06"
GATE, UNGATE_MIN, GAP, TRAIL = 4.3, 4, 1.5, 2.0


def _et(h, m):
    y, mo, d = (int(x) for x in DATE.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def _ns(dt):
    return int(dt.timestamp() * 1e9)


def hh(t):
    return t.astimezone(_ET).strftime("%H:%M:%S")


def _ts_ns(o):
    for a in ("sip_timestamp", "participant_timestamp", "timestamp"):
        v = getattr(o, a, None)
        if v:
            return v
    return None


def main():
    from massive import RESTClient
    c = RESTClient(api_key=get_settings().massive_api_key, retries=0, read_timeout=25)
    obs, so, cut, end = _et(9, 25), _et(9, 30), _et(10, 0), _et(10, 10)
    lo, hi = _ns(obs), _ns(end)
    trades = []
    for t in c.list_trades(SYM, timestamp_gte=lo, timestamp_lte=hi, limit=50000):
        ts = _ts_ns(t)
        if ts and getattr(t, "price", None):
            trades.append(Trade(datetime.fromtimestamp(ts / 1e9, timezone.utc), t.price, getattr(t, "size", 0) or 0))
    quotes = []
    for q in c.list_quotes(SYM, timestamp_gte=lo, timestamp_lte=hi, limit=50000):
        ts = _ts_ns(q)
        b, a = getattr(q, "bid_price", 0) or 0, getattr(q, "ask_price", 0) or 0
        if ts and b > 0 and a > 0:
            quotes.append(Quote(datetime.fromtimestamp(ts / 1e9, timezone.utc), b, a))
    trades.sort(key=lambda t: t.ts)
    quotes.sort(key=lambda q: q.ts)
    print(f"{SYM} {DATE}: Polygon {len(trades)} trades, {len(quotes)} quotes (09:25-10:10 ET)")
    bars = build_bars(trades, so)

    print("\n1-min OHLC 09:32-09:40 (the dip-then-rip candle):")
    for b in bars:
        et = b.timestamp.astimezone(_ET)
        if _et(9, 32) <= b.timestamp <= _et(9, 40):
            print(f"  {et.strftime('%H:%M')}  O{b.open:.3f} H{b.high:.3f} L{b.low:.3f} C{b.close:.3f}  range {(b.high-b.low)/b.low*100:.1f}%")

    tk = simulate_orb_tick_entry(trades, quotes, gap_cap_pct=GAP, trail_pct=TRAIL, qty=5, observe_open=obs,
                                 session_open=so, cutoff=cut, capped=False, latency_s=3.0, entry_windows=None,
                                 atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60, bars=bars)
    print(f"\ntick-entry (PR config) trades: {len(tk)}")
    for t in tk:
        print(f"  {hh(t.entry_ts)} {t.entry_price:.3f} -> {hh(t.exit_ts)} {t.exit_price:.3f} "
              f"({(t.exit_price/t.entry_price-1)*100:+.1f}%) pnl{t.pnl:+.2f} {t.exit_reason}")

    # trace the 2% trail for the entry live across 09:35
    live = [t for t in tk if t.entry_ts <= _et(9, 35, ) + timedelta(seconds=30) and (t.exit_ts is None or t.exit_ts >= _et(9, 34, 30))]
    book = QuoteBook(quotes)
    qts = [q.ts for q in quotes]
    if not live:
        # fall back: reconstruct the FIRST entry manually to trace even if it exited pre-09:35
        live = tk[:1]
    for t in live:
        fill, fill_ts = t.entry_price, t.entry_ts + timedelta(seconds=3)
        start = bisect_left(qts, fill_ts)
        hwm, stop = fill, fill * (1 - TRAIL / 100)
        print(f"\nPER-TICK 2% trail from entry {hh(t.entry_ts)} fill {fill:.3f} (HWM*0.98 = the stop):")
        exited = False
        lo_w, hi_w = _et(9, 34, 30), _et(9, 36, 30)
        for j in range(start, len(quotes)):
            q = quotes[j]
            if q.bid <= 0:
                continue
            hwm = max(hwm, q.bid)
            stop = max(stop, hwm * (1 - TRAIL / 100))
            hit = q.bid <= stop
            if lo_w <= q.ts <= hi_w or hit:
                mark = "  <-- 2% TRAIL FIRES (exit)" if hit else ""
                print(f"   {hh(q.ts)} bid {q.bid:.3f}  HWM {hwm:.3f}  stop {stop:.3f}{mark}")
            if hit:
                exited = True
                # what did the price do AFTER this exit?
                after = [x.price for x in trades if x.ts > q.ts and x.ts <= _et(9, 45)]
                if after:
                    peak = max(after)
                    print(f"   ...after exit at {hh(q.ts)} {q.bid:.3f}: price ran to {peak:.3f} "
                          f"({(peak/q.bid-1)*100:+.1f}%) by 09:45 — MISSED" )
                break
        if not exited:
            print("   (no 2% stop in the window — rode through)")


if __name__ == "__main__":
    main()
