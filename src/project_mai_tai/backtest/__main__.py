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
from project_mai_tai.backtest.v2_sim import simulate_v2
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")


def _run_orb(src, a, y, m, d):
    def et(hh, mm):
        return datetime(y, m, d, hh, mm, tzinfo=_ET).astimezone(timezone.utc)

    obs, so, cut, end = et(9, 25), et(9, 30), et(10, 0), et(10, 10)
    trades = src.trades(a.symbol, obs, end)
    quotes = src.quotes(a.symbol, obs, end)
    bars = build_bars(trades, so)
    win = dict(observe_open=obs, session_open=so, cutoff=cut)
    base = dict(gap_cap_pct=a.gap_cap, trail_pct=a.trail, qty=a.qty, capped=a.capped)
    print(f"ORB {a.symbol} {a.date}  mode={a.mode}  {'LIVE(2-cap)' if a.capped else 'THESIS(all-breaks)'}")
    print("Webull latency band (measured): P&L is a RANGE, not a point.")
    for lat in WEBULL_LATENCY_BAND_S:
        fn = simulate_bar_close if a.mode == "bar_close" else simulate_intrabar
        src_arg = bars if a.mode == "bar_close" else trades
        ts = fn(src_arg, quotes, latency_s=lat, **base, **win)
        print(f"  lat={lat:>4.0f}s: {len(ts):>3} trades  net=${sum(t.pnl for t in ts):+.2f}")


def _run_v2(src, a, y, m, d):
    obs = datetime(y, m, d, 4, 0, tzinfo=_ET).astimezone(timezone.utc)      # session 04:00 ET
    end = datetime(y, m, d, 20, 0, tzinfo=_ET).astimezone(timezone.utc)     # -> 20:00 ET
    sb = src.schwab_bars(a.symbol, obs, end)
    sq = src.schwab_quotes(a.symbol, obs, end)
    mq = src.quotes(a.symbol, obs, end)   # massive exit feed
    print(f"v2/ATR {a.symbol} {a.date}  mode={a.mode}  (Schwab ~0s latency; feed-limited)")
    print(f"  schwab_bars={len(sb)} schwab_quotes={len(sq)} massive_quotes={len(mq)}")
    if len(sb) < 10 or len(sq) == 0:
        print("  INSUFFICIENT SCHWAB FEED (coverage gap) — cannot backtest faithfully.")
        return
    ts = simulate_v2(sb, sq, mq, qty=a.qty if a.qty != 5 else 10, mode=a.mode)
    print(f"  {len(ts)} trades  net=${sum(t.pnl for t in ts):+.2f}")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m project_mai_tai.backtest")
    p.add_argument("symbol")
    p.add_argument("date", help="ET session date, YYYY-MM-DD")
    p.add_argument("--strategy", choices=["orb", "v2"], default="orb")
    p.add_argument("--mode", choices=["bar_close", "intrabar"], default="intrabar")
    p.add_argument("--capped", action="store_true", help="ORB live-achievable 2-cap (else thesis)")
    p.add_argument("--qty", type=int, default=5)
    p.add_argument("--trail", type=float, default=3.0)
    p.add_argument("--gap-cap", type=float, default=1.5)
    a = p.parse_args()
    y, m, d = (int(x) for x in a.date.split("-"))
    src = DbMarketDataSource(build_session_factory(get_settings()))
    (_run_v2 if a.strategy == "v2" else _run_orb)(src, a, y, m, d)


if __name__ == "__main__":
    main()
