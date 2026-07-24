"""Single entry point for the validated backtest engine — the ONLY supported way to run an
ORB or v2 backtest. No throwaway scripts (see docs/backtest-engine-design.md); the quarantined
scripts/legacy/ backtests are superseded by this engine.

    # ORB (the latency-band harness):
    python -m project_mai_tai.backtest SYMBOL YYYY-MM-DD --strategy orb [--mode bar_close|intrabar|resting]
        [--capped] [--qty N] [--trail PCT] [--gap-cap PCT]

    # v2/ATR (the REPLAY engine — the real live strategy, entry+exit):
    python -m project_mai_tai.backtest SYMBOL YYYY-MM-DD --strategy v2 [--eh]
    python -m project_mai_tai.backtest YYYY-MM-DD --strategy v2 --sheet [--eh]

ORB reads market_capture_* (stream-trades decision source) from the DB and reports P&L across the
MEASURED per-broker latency band (never a single point); default mode=intrabar, thesis (all genuine
breaks); --capped = live-achievable 2-attempt cap. **v2 no longer has --mode variants**: those were
the re-implementations that drifted from live — the replay runs the ACTUAL live strategy
(resting-primary + reactive-fallback) and the shared cw_exit_decision (docs/backtest-replay-engine-design.md
P4). Conclusions are trustworthy only when the CI golden gate (tests/backtest) is green.
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
    simulate_resting,
)
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
        if a.mode == "resting":
            ts = simulate_resting(bars, trades, quotes, latency_s=lat, **base, **win)
        elif a.mode == "bar_close":
            ts = simulate_bar_close(bars, quotes, latency_s=lat, **base, **win)
        else:
            ts = simulate_intrabar(trades, quotes, latency_s=lat, **base, **win)
        print(f"  lat={lat:>4.0f}s: {len(ts):>3} trades  net=${sum(t.pnl for t in ts):+.2f}")


def _run_v2(src, a, y, m, d):
    """v2/ATR backtest = the REPLAY engine (backtest/replay.py): the REAL live SchwabV2Strategy
    entry + the shared cw_exit_decision / static-OCO exit, reading the live-locked Settings. No
    re-implemented mode variants — one replay, one truth (docs/backtest-replay-engine-design.md P4)."""
    from project_mai_tai.backtest.replay import build_replay_settings, replay_symbol_day

    settings = build_replay_settings(base=get_settings(), eh_enabled=a.eh)
    res = replay_symbol_day(src, a.symbol, a.date, settings)
    print(f"v2/REPLAY {a.symbol} {a.date}  (real strategy entry+exit; Schwab bars"
          f"{'; EH entry ON' if a.eh else ''})")
    print(f"  schwab_bars={res.n_bars} schwab_quotes={res.n_quotes}")
    for sk in res.skips:
        print(f"  SKIP  {sk.reason}: {sk.detail}")
    for mi in res.misses:
        print(f"  MISS  {mi.reason}: {mi.detail}")
    if not res.entries:
        print("  (no entry — no confirmed ATR signal / vol-floor / no fill)")
    for e in res.entries:
        print(f"  ENTRY {e.mode}/{e.order_type} @ {e.fill_price:.4f} "
              f"{e.fill_ts.astimezone(_ET):%H:%M:%S} ET (ref {e.entry_ref:.4f})")
    for t in res.trades:
        print(f"  TRADE [{t.geometry}] exit @ {t.exit_px:.4f} "
              f"{t.exit_ts.astimezone(_ET):%H:%M:%S} ET reason={t.exit_reason} ret {t.ret_pct:+.2f}%")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m project_mai_tai.backtest")
    p.add_argument("symbol", nargs="?", help="omit with --sheet")
    p.add_argument("date", help="ET session date, YYYY-MM-DD")
    p.add_argument("--strategy", choices=["orb", "v2"], default="orb")
    p.add_argument("--mode", choices=["bar_close", "intrabar", "resting"], default="intrabar",
                   help="ORB only; v2 replays the ACTUAL live strategy (no mode variants)")
    p.add_argument("--eh", action="store_true",
                   help="v2 only: enable the EXTENDED-HOURS entry paths in the replay (LIVE default OFF)")
    p.add_argument("--sheet", action="store_true",
                   help="render the full daily sheet for ALL qualified names (every name gets a "
                        "reason: SKIP-no-feed / 0t-no-signal — no silent absence)")
    p.add_argument("--capped", action="store_true", help="ORB live-achievable 2-cap (else thesis)")
    p.add_argument("--qty", type=int, default=5)
    p.add_argument("--trail", type=float, default=3.0)
    p.add_argument("--gap-cap", type=float, default=1.5)
    a = p.parse_args()
    y, m, d = (int(x) for x in a.date.split("-"))
    src = DbMarketDataSource(build_session_factory(get_settings()))
    if a.sheet:
        from project_mai_tai.backtest.daily_sheet import render_orb_sheet, render_v2_sheet
        if a.strategy == "v2":
            print(render_v2_sheet(src, y, m, d, eh_enabled=a.eh))
        else:
            print(render_orb_sheet(src, y, m, d))
        return
    if not a.symbol:
        p.error("symbol is required unless --sheet is given")
    (_run_v2 if a.strategy == "v2" else _run_orb)(src, a, y, m, d)


if __name__ == "__main__":
    main()
