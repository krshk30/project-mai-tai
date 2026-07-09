"""Levers 4 & 5 on the tradeable universe (backtest-only; run OFF-HOURS, niced).

(4) BREAK-CONFIRMATION entry (operator's rule): at each BUY flip, enter intrabar the moment a Schwab quote
    breaks the FLIP BAR's OWN high (no fixed wait; unlike D3's 3-bar high + 3-bar wait). Watch from the bar
    after the flip until the next SELL flip / session end. Exit = the live ladder. Swept across hard stops.
(5) HARD-STOP sweep on the current TOUCH entry (simulate_v2 rearm=False, stop_loss_pct in {1.5,2,3,5,none}).

Dumps per-name-day trades (with entry pnl%) -> JSON; report computes net/win/payoff/expectancy/drop-one.

    python -m scripts.v2_lever45_run --range 2026-06-24 2026-07-08 --json=OUT.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.atr_oracle import ATR_FACTOR, ATR_PERIOD, compute_atr_trail
from project_mai_tai.backtest.data import DbMarketDataSource
from project_mai_tai.backtest.v2_sim import (
    BAR_MS, SCHWAB_LATENCY_S, VOL_FLOOR, V2Trade,
    _Book, _px, _run_exit, _utc, _v2_cfg, simulate_v2,
)
from project_mai_tai.exit_logic.engine import ExitEngine
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from dataclasses import replace

_ET = ZoneInfo("America/New_York")
TRAD = {"CANF", "CELZ", "CLRO", "CWD", "FCUV", "FRTT", "INTZ", "KIDZ", "LGPS", "LHAI", "LUCY",
        "SKYQ", "TVRD", "VTAK"}
STOPS = [("1.5", 1.5), ("2.0", 2.0), ("3.0", 3.0), ("5.0", 5.0), ("none", 100.0)]


def simulate_break_confirm(schwab_bars, schwab_quotes, massive_quotes, *, qty, stop_loss_pct):
    """Enter intrabar on the first Schwab quote that breaks the FLIP BAR's high, after each BUY flip,
    before the next SELL flip / EOD. Exit = the v2 ladder (hard stop swept)."""
    cfg = _v2_cfg()
    if stop_loss_pct is not None:
        cfg = replace(cfg, stop_loss_pct=stop_loss_pct)
    engine = ExitEngine(cfg)
    sbook, mbook = _Book(schwab_quotes), _Book(massive_quotes)
    rows = compute_atr_trail(schwab_bars, period=ATR_PERIOD, factor=ATR_FACTOR)
    n = len(schwab_bars)
    sells = [i for i, r in enumerate(rows) if r["flip"] == "SELL"]
    trades, flat_after = [], None
    for i in range(1, n):
        if rows[i]["flip"] != "BUY":
            continue
        flip_bar = schwab_bars[i]
        if flip_bar.volume <= VOL_FLOOR:
            continue
        threshold = flip_bar.high                      # the flip bar's OWN high
        watch_end = next((j for j in sells if j > i), n)   # until next SELL flip / EOD
        # first schwab quote after the flip bar close that breaks the threshold
        t0 = _utc(flip_bar.ts + BAR_MS)
        t1 = _utc(schwab_bars[min(watch_end, n - 1)].ts + BAR_MS)
        win = sbook.slice(t0, t1)
        bq = next((q for q in win if _px(q) >= threshold), None)
        if bq is None:
            continue                                    # high never broken -> no trade (thesis died)
        decision_ts = bq.ts
        if flat_after is not None and decision_ts < flat_after:
            continue
        fq = sbook.at(decision_ts + timedelta(seconds=SCHWAB_LATENCY_S))
        if fq is None or fq.ask <= 0:
            continue
        entry_ts = decision_ts + timedelta(seconds=SCHWAB_LATENCY_S)
        entry_price = fq.ask
        start = mbook.index_at_or_after(entry_ts)
        exit_ts, wavg, pnl, reason, n_legs = _run_exit(massive_quotes, start, entry_price, qty, cfg, engine)
        trades.append(V2Trade(entry_ts, entry_price, threshold, exit_ts, wavg, qty, pnl, reason, n_legs, "break"))
        flat_after = exit_ts
    return trades


def _window(date):
    y, m, d = (int(x) for x in date.split("-"))
    lo = datetime(y, m, d, 4, 0, tzinfo=_ET).astimezone(timezone.utc)
    hi = datetime(y, m, d, 20, 0, tzinfo=_ET).astimezone(timezone.utc)
    return lo, hi


def _tj(trades):
    return [{"pnl": round(t.pnl, 4), "entry_px": round(t.entry_price, 4), "qty": t.qty,
             "exit_reason": t.exit_reason, "pnl_pct": round(100 * t.pnl / (t.entry_price * t.qty), 4)}
            for t in trades]


def _dates(argv):
    ds = [a for a in argv if a.count("-") == 2 and a[:1].isdigit()]
    if len(ds) == 2 and "--range" in argv:
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
    qty, jsonp = 10, "v2_lever45.json"
    for a in argv:
        if a.startswith("--json="):
            jsonp = a.split("=", 1)[1]
    src = DbMarketDataSource(build_session_factory(get_settings()))
    out = {"qty": qty, "name_days": []}
    for date in _dates(argv):
        lo, hi = _window(date)
        try:
            syms = [s for s in src.v2_qualified_symbols(lo, hi) if s in TRAD]
        except Exception as e:            # noqa: BLE001
            print(f"{date}: universe err {e}", flush=True)
            continue
        for sym in syms:
            try:
                bars, sq, mq = src.schwab_bars(sym, lo, hi), src.schwab_quotes(sym, lo, hi), src.quotes(sym, lo, hi)
            except Exception as e:        # noqa: BLE001
                print(f"  {sym}: feed err {e}", flush=True)
                continue
            if len(bars) < 10 or not sq or not mq:
                continue
            rec = {"date": date, "sym": sym, "touch": {}, "break": {}}
            for label, stop in STOPS:
                rec["touch"][label] = _tj(simulate_v2(bars, sq, mq, qty=qty, rearm=False, stop_loss_pct=stop))
                rec["break"][label] = _tj(simulate_break_confirm(bars, sq, mq, qty=qty, stop_loss_pct=stop))
            out["name_days"].append(rec)
            print(f"  {date} {sym}: touch@1.5 {len(rec['touch']['1.5'])}tr  break@1.5 {len(rec['break']['1.5'])}tr", flush=True)
    with open(jsonp, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"\n[dumped {len(out['name_days'])} name-days -> {jsonp}]", flush=True)


if __name__ == "__main__":
    main()
