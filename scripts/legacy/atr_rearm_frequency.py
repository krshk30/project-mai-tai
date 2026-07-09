"""FREQUENCY for the design: how often does the ATR touch fire on a GRAZE before the real BUY flip
(the pattern that, under the current one-touch-per-segment guard, consumes the entry so a rejected
graze misses the subsequent real flip)? Runs OUR ATR math (compute_atr_trail 5/3.5) on the full Polygon
tape across the ~10-day confirmed-name sample. Per short segment ending in a BUY flip, counts whether
an earlier bar grazed the trail (high>=trail, but NOT the flip bar). That earlier graze is what fires
variant-B early; if its hold-confirm rejects, the current code can't re-arm -> the BUY flip is missed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import Bar as OBar
from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")
DATES = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30",
         "2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08"]


def main():
    from massive import RESTClient
    c = RESTClient(api_key=get_settings().massive_api_key, retries=0, read_timeout=25)
    tot_flips = tot_atrisk = tot_names = 0
    per_day = {}
    for date in DATES:
        wins = load_windows(f"/home/trader/wt-atr-study/windows/windows_{date}.json")
        d_flips = d_atrisk = d_names = 0
        for sym in sorted(wins.keys()):
            try:
                mins = list(c.list_aggs(sym, 1, "minute", date, date, limit=50000))
            except Exception:
                continue
            if len(mins) < 30:
                continue
            mins.sort(key=lambda m: m.timestamp)
            bars = [OBar(m.timestamp, m.open, m.high, m.low, m.close, int(m.volume or 0)) for m in mins]
            rows = compute_atr_trail(bars, period=5, factor=3.5)
            d_names += 1
            seg_graze = False
            for i in range(1, len(bars)):
                prev = rows[i - 1]
                if (prev["state"] == "short" and prev["trail"] is not None
                        and bars[i].high >= prev["trail"] and rows[i]["flip"] != "BUY"):
                    seg_graze = True                        # a graze that is NOT the flip bar
                if rows[i]["flip"] == "BUY":
                    d_flips += 1
                    if seg_graze:
                        d_atrisk += 1                       # this real flip had an earlier graze -> at-risk
                    seg_graze = False
                if rows[i]["flip"] == "SELL":
                    seg_graze = False
        per_day[date] = (d_names, d_flips, d_atrisk)
        tot_names += d_names
        tot_flips += d_flips
        tot_atrisk += d_atrisk

    print("ATR-flip re-arm frequency (Polygon 5/3.5), ~10-day confirmed-name sample\n")
    print(f"  {'date':<12}{'names':>6}{'BUYflips':>10}{'graze-first':>13}{'%at-risk':>10}")
    for date in DATES:
        n, f, a = per_day[date]
        print(f"  {date:<12}{n:>6}{f:>10}{a:>13}{(a/f*100 if f else 0):>9.0f}%")
    print(f"  {'TOTAL':<12}{tot_names:>6}{tot_flips:>10}{tot_atrisk:>13}{(tot_atrisk/tot_flips*100 if tot_flips else 0):>9.0f}%")
    print(f"\n  {tot_atrisk}/{tot_flips} real BUY flips ({tot_atrisk/tot_flips*100:.0f}%) had an EARLIER graze that fires")
    print("  variant-B first -> under the current guard, if that graze's hold-confirm rejects, the")
    print("  real flip is UN-ENTERABLE. (Upper bound: the subset whose graze actually rejects = the miss.)")


if __name__ == "__main__":
    main()
