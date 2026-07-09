"""Re-baseline A/B: v2_sim rearm=False vs rearm=True over the standard v2-ATR name-day sample, on the
CORRECTED backtest (post-#404). Dumps rich per-name-day JSON so the 3-cut report is a light post-process
(scripts/v2_rearm_ab_report.py). HEAVY (universe x days x 2) -> run OFF-HOURS, niced.

    python -m scripts.v2_rearm_ab_run 2026-06-24 ... 2026-07-08 --qty=10 --json=OUT.json
    (or pass a start/end and it fills weekdays)

Each name-day: run simulate_v2 (mode=intrabar = the live hold-confirm path, full OMS exit ladder) with
rearm off and on; record every trade's pnl + entry path (A/B/reclaim). Classification (Schwab stance) is
applied in the REPORT step from the DB-observed fill/reject sets, so this run stays pure.

CAVEAT: whole-session (no confirmed-window restriction; simulate_v2 has none) — the OFF-vs-ON delta and
the tradeable/restricted split are robust to this; absolute edge may include entries the live confirmed-
window gate would filter. Feed = Schwab bars/quotes for signal+fill, massive bid for the exit ladder.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource
from project_mai_tai.backtest.v2_sim import simulate_v2
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

_ET = ZoneInfo("America/New_York")


def _window(y, m, d):
    lo = datetime(y, m, d, 4, 0, tzinfo=_ET).astimezone(timezone.utc)
    hi = datetime(y, m, d, 20, 0, tzinfo=_ET).astimezone(timezone.utc)
    return lo, hi


def _trades_json(trades):
    return [{"pnl": round(t.pnl, 4), "path": t.path, "entry": t.entry_ts.isoformat(),
             "entry_px": round(t.entry_price, 4), "exit_reason": t.exit_reason, "qty": t.qty}
            for t in trades]


def _dates(argv):
    ds = [a for a in argv if a.count("-") == 2 and a[:1].isdigit()]
    if len(ds) == 2 and any(a == "--range" for a in argv):
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
    qty, jsonp = 10, "v2_rearm_ab.json"
    for a in argv:
        if a.startswith("--qty="):
            qty = int(a.split("=", 1)[1])
        elif a.startswith("--json="):
            jsonp = a.split("=", 1)[1]
    dates = _dates(argv)
    if not dates:
        print("usage: v2_rearm_ab_run YYYY-MM-DD [...] [--range] [--qty=10] [--json=OUT]")
        return
    src = DbMarketDataSource(build_session_factory(get_settings()))
    out = {"qty": qty, "dates": dates, "name_days": []}
    for date in dates:
        y, m, d = (int(x) for x in date.split("-"))
        lo, hi = _window(y, m, d)
        try:
            syms = src.v2_qualified_symbols(lo, hi)
        except Exception as e:              # noqa: BLE001
            print(f"{date}: universe error {e}", flush=True)
            continue
        print(f"{date}: {len(syms)} qualified", flush=True)
        for sym in syms:
            try:
                bars = src.schwab_bars(sym, lo, hi)
                sq = src.schwab_quotes(sym, lo, hi)
                mq = src.quotes(sym, lo, hi)
            except Exception as e:          # noqa: BLE001
                print(f"  {sym}: feed error {e}", flush=True)
                continue
            if len(bars) < 10 or not sq or not mq:
                continue
            off = simulate_v2(bars, sq, mq, qty=qty, mode="intrabar", rearm=False)
            on = simulate_v2(bars, sq, mq, qty=qty, mode="intrabar", rearm=True)
            if not off and not on:
                continue
            out["name_days"].append({
                "date": date, "sym": sym,
                "off": _trades_json(off), "on": _trades_json(on)})
            print(f"  {sym}: off {len(off)}tr/{sum(t.pnl for t in off):+.2f}  "
                  f"on {len(on)}tr/{sum(t.pnl for t in on):+.2f}", flush=True)
    with open(jsonp, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"\n[dumped {len(out['name_days'])} name-days -> {jsonp}]", flush=True)


if __name__ == "__main__":
    main()
