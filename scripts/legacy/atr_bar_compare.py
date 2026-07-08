"""PIN the 10-min-early ATR flip. Same ATR math (compute_atr_trail 5/3.5) on TWO feeds, side by side,
minute-by-minute around the #2 flip (bot=13:19 vs chart=13:29):
  SCHWAB = strategy_bar_history (what the live bot + v2 backtest use)
  POLYGON = full 1-min tape (what the TOS chart uses)
Shows each minute's close, the ATR trail-line, the state (long/short), and the flip — so the exact
bar where Schwab crosses early (missing bar? different price?) is visible.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.backtest.data import DbMarketDataSource
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")


def et(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).astimezone(_ET)


def main():
    from massive import RESTClient
    src = DbMarketDataSource(build_session_factory(get_settings()))
    c = RESTClient(api_key=get_settings().massive_api_key, retries=0, read_timeout=25)
    start = dt.datetime(2026, 7, 8, 4, 0, tzinfo=_ET).astimezone(dt.timezone.utc)
    end = dt.datetime(2026, 7, 8, 20, 0, tzinfo=_ET).astimezone(dt.timezone.utc)

    schwab = src.schwab_bars("NVVE", start, end)
    poly = list(c.list_aggs("NVVE", 1, "minute", "2026-07-08", "2026-07-08", limit=50000))
    poly.sort(key=lambda m: m.timestamp)
    print(f"NVVE 07-08: SCHWAB feed = {len(schwab)} bars | POLYGON feed = {len(poly)} bars")

    srows = compute_atr_trail([Bar(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in schwab], period=5, factor=3.5)
    prows = compute_atr_trail([Bar(m.timestamp, m.open, m.high, m.low, m.close, int(m.volume or 0)) for m in poly], period=5, factor=3.5)
    sd = {et(schwab[i].ts).strftime("%H:%M"): (schwab[i], srows[i]) for i in range(len(schwab))}
    pd = {et(int(poly[i].timestamp)).strftime("%H:%M"): (poly[i], prows[i]) for i in range(len(poly))}

    print("\nminute-by-minute 13:10-13:32 ET  (state s=short/l=long; * = FLIP)")
    print(f"  {'ET':<6}| {'SCHWAB close':>12} {'trail':>8} {'st':>3} | {'POLY close':>11} {'trail':>8} {'st':>3}")
    print("  " + "-" * 62)
    for m in range(13 * 60 + 10, 13 * 60 + 33):
        hm = f"{m // 60:02d}:{m % 60:02d}"
        s = sd.get(hm)
        p = pd.get(hm)
        if s:
            sb, sr = s
            sc = f"{sb.close:.3f}"
            st = f"{(sr['trail'] or 0):.3f}"
            ss = ("*" + sr["state"][0]) if sr["flip"] else sr["state"][0]
        else:
            sc, st, ss = "-- MISSING", "--", "--"
        if p:
            pb, pr = p
            pc = f"{pb.close:.3f}"
            pt = f"{(pr['trail'] or 0):.3f}"
            ps = ("*" + pr["state"][0]) if pr["flip"] else pr["state"][0]
        else:
            pc, pt, ps = "-- MISSING", "--", "--"
        print(f"  {hm:<6}| {sc:>12} {st:>8} {ss:>3} | {pc:>11} {pt:>8} {ps:>3}")


if __name__ == "__main__":
    main()
