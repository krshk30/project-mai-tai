"""Validate the PRODUCTION liquidity gate reproduces the sweep. The sweep post-filtered whole names;
the production engine gates from first-bar close (09:31), so early entries on a failing name survive.
Confirm the balanced 100K/1.0% gate still lifts the aggregate ~= the sweep's +11.2 through the real
simulate_orb_tick_entry path (single source of truth), and cross-reference per-name."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.orb_sim import simulate_orb_tick_entry
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")
GATE, UNGATE_MIN = 4.3, 4


def _et(y, mo, d, h, m):
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def main():
    src = DbMarketDataSource(build_session_factory(get_settings()))
    wdir = "/home/trader/wt-atr-study/windows"
    dates = ["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30",
             "2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07", "2026-07-08"]
    base_tot = gate_tot = 0.0
    n = changed = 0
    diffs = []
    for date in dates:
        y, mo, dd = (int(x) for x in date.split("-"))
        obs, so, cut, end = _et(y, mo, dd, 9, 25), _et(y, mo, dd, 9, 30), _et(y, mo, dd, 10, 0), _et(y, mo, dd, 10, 10)
        wins = load_windows(f"{wdir}/windows_{date}.json")
        for sym in src.orb_qualified_symbols(obs, end, min_trades=500):
            ewin = wins.get(sym, [])
            if not ewin:
                continue
            tr, q = src.trades(sym, obs, end), src.quotes(sym, obs, end)
            if len(tr) < 500 or len(q) < 50:
                continue
            bars = build_bars(tr, so)
            common = dict(gap_cap_pct=1.5, trail_pct=2.0, qty=5, observe_open=obs, session_open=so,
                          cutoff=cut, capped=False, latency_s=3.0, entry_windows=ewin,
                          atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60, bars=bars)
            base = sum(t.pnl for t in simulate_orb_tick_entry(tr, q, **common))
            gated = sum(t.pnl for t in simulate_orb_tick_entry(
                tr, q, liq_min_volume=100000.0, liq_max_spread_pct=1.0, **common))
            base_tot += base
            gate_tot += gated
            n += 1
            if abs(gated - base) > 0.001:
                changed += 1
                diffs.append((date, sym, base, gated))
    print(f"n={n} name-days | PRODUCTION liquidity gate (100K/1.0%, applied from 09:31 in the real engine)")
    print(f"  baseline total = {base_tot:+.1f}")
    print(f"  gated    total = {gate_tot:+.1f}   (sweep whole-name filter was +11.2)")
    print(f"  names changed by the gate = {changed}\n")
    print("per-name changes (baseline -> gated):")
    for date, sym, b, g in sorted(diffs, key=lambda x: x[3] - x[2]):
        print(f"  {date} {sym:<6} {b:+6.2f} -> {g:+6.2f}  ({g-b:+.2f})")


if __name__ == "__main__":
    main()
