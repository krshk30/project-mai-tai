"""Replay a stop/target exit from captured Schwab trade ticks.

Resolves which of `+target%` / `-stop%` was hit FIRST inside an ambiguous candle
by walking `market_trade_ticks` in event-time order. If there are zero trade
ticks in the window it returns `UNRESOLVED_NO_TICKS` — never a guessed answer.

Usage (env sourced for MAI_TAI_DATABASE_URL, or pass --dsn):
  python scripts/replay_exit_from_ticks.py \
    --symbol GLXG --entry-ts '2026-06-11T14:30:00+00:00' \
    --entry-price 3.15 --qty 100 --target-pct 2 --stop-pct 1.5 --window-mins 30

Read-only. Exit 0 on a resolved or no-ticks answer; 2 on bad input.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta

import psycopg


def _dsn(arg: str | None) -> str:
    raw = arg or os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise SystemExit("no DSN: pass --dsn or set MAI_TAI_DATABASE_URL")
    return raw.replace("postgresql+psycopg://", "postgresql://")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="schwab_1m_v2")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--entry-ts", required=True, help="ISO8601, tz-aware (e.g. ...+00:00)")
    ap.add_argument("--entry-price", type=float, required=True)
    ap.add_argument("--qty", type=float, default=100.0)
    ap.add_argument("--target-pct", type=float, required=True)
    ap.add_argument("--stop-pct", type=float, required=True)
    ap.add_argument("--window-mins", type=float, default=30.0)
    ap.add_argument("--provider", default="schwab")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    entry_ts = datetime.fromisoformat(args.entry_ts)
    if entry_ts.tzinfo is None:
        raise SystemExit("--entry-ts must be timezone-aware")
    end_ts = entry_ts + timedelta(minutes=args.window_mins)
    target_price = args.entry_price * (1 + args.target_pct / 100.0)
    stop_price = args.entry_price * (1 - args.stop_pct / 100.0)

    with psycopg.connect(_dsn(args.dsn)) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_ts, price, size
            FROM market_trade_ticks
            WHERE provider = %s AND symbol = %s
              AND event_ts >= %s AND event_ts <= %s
            ORDER BY event_ts ASC, id ASC
            """,
            (args.provider, args.symbol.upper(), entry_ts, end_ts),
        )
        rows = cur.fetchall()

    result: dict[str, object] = {
        "symbol": args.symbol.upper(), "strategy": args.strategy,
        "entry_ts": entry_ts.isoformat(), "entry_price": args.entry_price,
        "qty": args.qty, "target_pct": args.target_pct, "stop_pct": args.stop_pct,
        "target_price": round(target_price, 6), "stop_price": round(stop_price, 6),
        "window_mins": args.window_mins, "ticks_in_window": len(rows),
    }

    if not rows:
        result["outcome"] = "UNRESOLVED_NO_TICKS"
        print(json.dumps(result, indent=2, default=str))
        return 0

    for event_ts, price, size in rows:
        p = float(price)
        if p >= target_price:
            result.update(outcome="TARGET", exit_ts=event_ts.isoformat(), exit_price=p,
                          exit_size=size, pnl=round((p - args.entry_price) * args.qty, 4))
            break
        if p <= stop_price:
            result.update(outcome="STOP", exit_ts=event_ts.isoformat(), exit_price=p,
                          exit_size=size, pnl=round((p - args.entry_price) * args.qty, 4))
            break
    else:
        last = rows[-1]
        result.update(outcome="NO_HIT_IN_WINDOW",
                      last_tick_ts=last[0].isoformat(), last_price=float(last[1]),
                      unrealized_pnl=round((float(last[1]) - args.entry_price) * args.qty, 4))

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
