"""Part-1 diagnosis: trace BJDX 2026-07-07 ORB bar-close entry #4 exit mechanism.
Prints all BC trades, then for the ~1.78 entry re-walks the trail: bid path, HWM, stop,
the TRIGGER quote and the FILL quote (trigger+latency) — to separate slippage vs gap vs lag.
Run: PYTHONPATH=.../src python scripts/legacy/trace_bjdx4.py
"""
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.fill import QuoteBook, exit_fill
from project_mai_tai.backtest.orb_sim import _ratcheted_trailing_stop, simulate_bar_close
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

ET = ZoneInfo("America/New_York")
LAT = 3.0
TRAIL = 3.0


def et(h, m):
    return datetime(2026, 7, 7, h, m, tzinfo=ET).astimezone(timezone.utc)


def hhmmss(t):
    return t.astimezone(ET).strftime("%H:%M:%S")


src = DbMarketDataSource(build_session_factory(get_settings()))
obs, so, cut, end = et(9, 25), et(9, 30), et(10, 0), et(10, 10)
trades = src.trades("BJDX", obs, end)
quotes = src.quotes("BJDX", obs, end)
bars = build_bars(trades, so)
tr = simulate_bar_close(bars, quotes, gap_cap_pct=1.5, trail_pct=TRAIL, qty=5,
                        observe_open=obs, session_open=so, cutoff=cut, capped=False, latency_s=LAT)
print(f"BJDX 07-07 ORB bar-close trades (trail={TRAIL}% lat={LAT}s, capped=False):  quotes={len(quotes)} trades={len(trades)} bars={len(bars)}")
for i, t in enumerate(tr, 1):
    print(f"  #{i} {hhmmss(t.entry_ts)} entry={t.entry_price:.4f} -> exit={t.exit_price:.4f} "
          f"({hhmmss(t.exit_ts)}) pnl={t.pnl:+.2f} {t.exit_reason} ret={((t.exit_price/t.entry_price)-1)*100:+.1f}% level={t.level}")

# pick the ~1.78 entry (or #4)
target = None
for i, t in enumerate(tr, 1):
    if abs(t.entry_price - 1.78) < 0.03:
        target = (i, t); break
if target is None and len(tr) >= 4:
    target = (4, tr[3])
if target is None:
    print("no target trade"); raise SystemExit
idx, t = target
print(f"\n===== TRACE entry #{idx}: fill={t.entry_price:.4f} @ {hhmmss(t.entry_ts)}  reported exit={t.exit_price:.4f} @ {hhmmss(t.exit_ts)} =====")
book = QuoteBook(quotes)
fill = t.entry_price
fill_ts = t.entry_ts
hwm = fill
stop = fill * (1 - TRAIL / 100)
start = bisect_left(book._ts, fill_ts)
print(f"seed: hwm={hwm:.4f} stop={stop:.4f} (= fill*(1-{TRAIL}%))")
peak = fill
prev_bid = None
i = start
n = len(quotes)
printed = 0
while i < n:
    q = quotes[i]
    new_stop, new_hwm = _ratcheted_trailing_stop(stop, hwm, q.bid, TRAIL)
    ratcheted = new_stop > stop + 1e-9
    peak = max(peak, q.bid)
    stop, hwm = new_stop, new_hwm
    trig = q.bid <= stop
    # print: every ratchet, the trigger, and the last few before trigger
    show = ratcheted or trig or (prev_bid is not None and abs(q.bid - prev_bid) >= 0.01)
    if show or printed < 3:
        flag = "  <-- RATCHET" if ratcheted else ""
        if trig:
            flag = "  <== TRIGGER (bid<=stop)"
        print(f"  {hhmmss(q.ts)} bid={q.bid:.4f} ask={q.ask:.4f}  hwm={hwm:.4f} stop={stop:.4f}{flag}")
        printed += 1
    if trig:
        # the fill happens LAT seconds later at the then-bid
        fq = book.at(q.ts + timedelta(seconds=LAT))
        print(f"\n  TRIGGER at bid={q.bid:.4f} ts={hhmmss(q.ts)}  (stop was {stop:.4f}, hwm peak {hwm:.4f})")
        print(f"  FILL at trigger+{LAT}s -> ts={hhmmss(q.ts+timedelta(seconds=LAT))} bid={fq.bid:.4f}" if fq else "  FILL: no quote")
        # show the bid path across the latency window
        print("  bid path trigger .. trigger+{}s:".format(LAT))
        j = i
        while j < n and quotes[j].ts <= q.ts + timedelta(seconds=LAT + 2):
            print(f"    {hhmmss(quotes[j].ts)} bid={quotes[j].bid:.4f}")
            j += 1
        # gap check: was there any quote with bid in (fill_exit, stop_seed_peak]?
        print(f"\n  peak bid during hold={peak:.4f}  3%-trail-from-peak={peak*(1-TRAIL/100):.4f}")
        break
    prev_bid = q.bid
    i += 1
