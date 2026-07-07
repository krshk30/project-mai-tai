"""Single entry point for the validated backtest engine — the ONLY supported way to run an
ORB backtest. No throwaway scripts (see docs/backtest-engine-design.md); the quarantined
scripts/legacy/ backtests are superseded by this engine.

    python -m project_mai_tai.backtest SYMBOL YYYY-MM-DD [--mode bar_close|intrabar]
        [--capped] [--qty N] [--trail PCT] [--gap-cap PCT]

Reads market_capture_* (stream-trades decision source) from the DB and reports P&L across the
MEASURED per-broker latency band (never a single point). Default mode=intrabar, thesis (all
genuine breaks); --capped = live-achievable 2-attempt cap (reclaim = eager upper bound).
Conclusions are trustworthy only when the CI golden gate (tests/backtest) is green.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.orb_sim import (
    WEBULL_LATENCY_BAND_S,
    simulate_bar_close,
    simulate_intrabar,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m project_mai_tai.backtest")
    p.add_argument("symbol")
    p.add_argument("date", help="ET session date, YYYY-MM-DD")
    p.add_argument("--mode", choices=["bar_close", "intrabar"], default="intrabar")
    p.add_argument("--capped", action="store_true", help="live-achievable 2-cap (default: thesis/all-breaks)")
    p.add_argument("--qty", type=int, default=5)
    p.add_argument("--trail", type=float, default=3.0)
    p.add_argument("--gap-cap", type=float, default=1.5)
    a = p.parse_args()
    y, m, d = (int(x) for x in a.date.split("-"))

    def et(hh, mm):
        return datetime(y, m, d, hh, mm, tzinfo=_ET).astimezone(timezone.utc)

    obs, so, cut, end = et(9, 25), et(9, 30), et(10, 0), et(10, 10)
    src = DbMarketDataSource(build_session_factory(get_settings()))
    trades = src.trades(a.symbol, obs, end)
    quotes = src.quotes(a.symbol, obs, end)
    bars = build_bars(trades, so)
    win = dict(observe_open=obs, session_open=so, cutoff=cut)
    base = dict(gap_cap_pct=a.gap_cap, trail_pct=a.trail, qty=a.qty, capped=a.capped)

    print(f"{a.symbol} {a.date}  mode={a.mode}  {'LIVE(2-cap)' if a.capped else 'THESIS(all-breaks)'}  "
          f"qty={a.qty} trail={a.trail}% gap_cap={a.gap_cap}%")
    print("Webull latency band (measured): P&L is a RANGE, not a point.")
    for lat in WEBULL_LATENCY_BAND_S:
        if a.mode == "bar_close":
            ts = simulate_bar_close(bars, quotes, latency_s=lat, **base, **win)
        else:
            ts = simulate_intrabar(trades, quotes, latency_s=lat, **base, **win)
        print(f"  lat={lat:>4.0f}s: {len(ts):>3} trades  net=${sum(t.pnl for t in ts):+.2f}")


if __name__ == "__main__":
    main()
