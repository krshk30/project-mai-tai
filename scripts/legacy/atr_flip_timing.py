"""NVVE 2026-07-08 ATR-flip TIMING reconciliation. Runs OUR exact ATR math (compute_atr_trail,
period=5, factor=3.5 — the same as the live bot + the TOS ATRTrailingStop(5,3.5,WILDERS)) on the FULL
POLYGON 1-min tape (every minute, unlike the sparse Schwab feed the v2 backtest used). Prints every
flip (BUY = short->long, SELL = long->short) with ET times, so we can compare:
  (A) our backtest engine (Schwab feed, sparse): flips 12:13 / 13:19 / 16:18 ET
  (B) the live bot: one ATR emit 13:19 ET (rejected)
  (C) THIS — our ATR math on FULL data: should match the TOS chart if the logic is right
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import Bar as OBar
from project_mai_tai.backtest.atr_oracle import compute_atr_trail
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")
SYM, DATE = "NVVE", "2026-07-08"


def et(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(_ET).strftime("%H:%M")


def main():
    from massive import RESTClient
    c = RESTClient(api_key=get_settings().massive_api_key, retries=0, read_timeout=25)
    mins = list(c.list_aggs(SYM, 1, "minute", DATE, DATE, limit=50000))
    mins.sort(key=lambda m: m.timestamp)
    # RTH-visible window ~09:30-16:00 ET plus a little, matching the chart
    bars = [OBar(m.timestamp, m.open, m.high, m.low, m.close, int(m.volume or 0)) for m in mins]
    print(f"{SYM} {DATE}: {len(bars)} Polygon 1-min bars (full tape). ATR(5, 3.5, Wilders) flips:\n")
    rows = compute_atr_trail(bars, period=5, factor=3.5)
    print(f"  {'ET':<6}{'flip':<6}{'close':>8}{'trail-line':>12}  (BUY = short->long = long entry)")
    last_state = None
    for i, r in enumerate(rows):
        t = et(bars[i].ts)
        # only show the RTH-ish window the chart covers (09:30-16:00 ET)
        hm = datetime.fromtimestamp(bars[i].ts / 1000, timezone.utc).astimezone(_ET)
        if not (hm.hour * 60 + hm.minute >= 9 * 60 + 30 and hm.hour < 16):
            if r["flip"]:
                pass  # still show flips outside RTH, but mark
        if r["flip"]:
            mark = "  <== BUY flip (long entry)" if r["flip"] == "BUY" else ""
            print(f"  {t:<6}{r['flip']:<6}{r['close']:>8.3f}{(r['trail'] or 0):>12.3f}{mark}")
        last_state = r["state"]


if __name__ == "__main__":
    main()
