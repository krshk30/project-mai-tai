#!/usr/bin/env python3
"""
backtest_validate_trades.py

Option A trade-validation harness: post-hoc consistency check of trades
that fired vs the bars that drove them.

For a given trading day + strategy, this script:

1. Loads every TradeIntent the strategy emitted that day.
2. For each intent, finds the driving bar in strategy_bar_history
   (the most-recent bar at or just before the intent's created_at).
3. Compares the intent's metadata (reference_price, path, score) against
   the bar's persisted decision metadata (close_price, decision_path,
   decision_score, decision_status).
4. Flags any intent whose driving bar does not look like a clean signal
   (e.g., bar marked `idle` / `blocked` / `''` instead of `signal`).
5. Reports COVERAGE GAPS: bars that recorded `decision_status='signal'`
   but for which no TradeIntent exists in the same window.

Intended use: validate that trades happened as expected, identify any
discrepancies, and have a single audit document per session-day.

This is a read-only consistency check. No side effects on live trading.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg


EASTERN = ZoneInfo("America/New_York")

# How wide a window to look back from intent.created_at to find a driving bar.
# 60s for 30s bars (covers two buckets), 120s for 60s bars.
_LOOKBACK_BY_INTERVAL = {30: 60.0, 60: 120.0}


@dataclass
class IntentRow:
    intent_id: str
    symbol: str
    side: str
    intent_type: str
    quantity: Decimal
    created_at: datetime
    reason: str
    payload: dict


@dataclass
class BarRow:
    bar_time: datetime
    interval_secs: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int
    decision_status: str
    decision_reason: str
    decision_path: str
    decision_score: str


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Validate that trade_intents are consistent with the persisted bars that drove them."
    )
    ap.add_argument("--day", required=True, help="Trading session day in ET, format YYYY-MM-DD")
    ap.add_argument(
        "--strategy-code",
        required=True,
        help="Strategy code (e.g. macd_30s, schwab_1m, polygon_30s)",
    )
    ap.add_argument(
        "--symbol",
        action="append",
        help="Restrict to one or more symbols (repeatable). Default: all symbols.",
    )
    ap.add_argument(
        "--intent-type",
        default="open",
        choices=["open", "close", "scale", "all"],
        help="Filter intents by type. Default: open (the entry signals).",
    )
    ap.add_argument(
        "--price-tolerance",
        type=float,
        default=0.02,
        help="Max abs(intent.reference_price - bar.close) accepted as a clean match (dollars).",
    )
    ap.add_argument(
        "--interval-secs",
        type=int,
        default=None,
        help="Bar interval seconds (30 for macd_30s/polygon_30s, 60 for schwab_1m). "
        "Default: inferred from --strategy-code.",
    )
    ap.add_argument(
        "--dsn",
        default=None,
        help="psycopg DSN. Default: read from MAI_TAI_DATABASE_URL env var.",
    )
    ap.add_argument(
        "--show-clean",
        action="store_true",
        help="Print every clean match (default: only print discrepancies + coverage gaps).",
    )
    return ap.parse_args()


def _infer_interval(strategy_code: str) -> int:
    if strategy_code.endswith("_30s") or "30s" in strategy_code:
        return 30
    if strategy_code.endswith("_1m") or strategy_code == "schwab_1m":
        return 60
    raise SystemExit(
        f"could not infer interval_secs from strategy_code={strategy_code}; pass --interval-secs"
    )


def _resolve_dsn(arg_dsn: Optional[str]) -> str:
    if arg_dsn:
        return arg_dsn
    env_url = os.environ.get("MAI_TAI_DATABASE_URL", "").strip()
    if not env_url:
        raise SystemExit(
            "no DSN: pass --dsn or set MAI_TAI_DATABASE_URL "
            "(e.g. dbname=project_mai_tai user=mai_tai host=localhost)"
        )
    if env_url.startswith("postgresql+psycopg://") or env_url.startswith("postgresql://"):
        scheme, _, rest = env_url.partition("://")
        creds, _, hostpath = rest.partition("@")
        user, _, password = creds.partition(":")
        host_port, _, db = hostpath.partition("/")
        host, _, port = host_port.partition(":")
        parts = [f"dbname={db}", f"user={user}", f"host={host}"]
        if port:
            parts.append(f"port={port}")
        if password and "PGPASSWORD" not in os.environ:
            os.environ["PGPASSWORD"] = password
        return " ".join(parts)
    return env_url


def _session_day_window(day: str) -> tuple[datetime, datetime]:
    parsed = datetime.strptime(day, "%Y-%m-%d").date()
    start_et = datetime.combine(parsed, time(hour=0), tzinfo=EASTERN)
    end_et = start_et + timedelta(days=1)
    return start_et.astimezone(UTC), end_et.astimezone(UTC)


def _load_intents(
    conn: psycopg.Connection,
    *,
    strategy_code: str,
    intent_type: str,
    symbols: list[str] | None,
    start_utc: datetime,
    end_utc: datetime,
) -> list[IntentRow]:
    sql = """
        SELECT ti.id::text, ti.symbol, ti.side, ti.intent_type, ti.quantity,
               ti.created_at, ti.reason, ti.payload
        FROM trade_intents ti
        JOIN strategies s ON ti.strategy_id = s.id
        WHERE s.code = %(code)s
          AND ti.created_at >= %(start)s
          AND ti.created_at < %(end)s
    """
    params: dict[str, object] = {
        "code": strategy_code,
        "start": start_utc,
        "end": end_utc,
    }
    if intent_type != "all":
        sql += " AND ti.intent_type = %(itype)s"
        params["itype"] = intent_type
    if symbols:
        sql += " AND upper(ti.symbol) = ANY(%(symbols)s)"
        params["symbols"] = [s.upper() for s in symbols]
    sql += " ORDER BY ti.created_at ASC"

    rows: list[IntentRow] = []
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for r in cur.fetchall():
            payload = r[7] or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            rows.append(
                IntentRow(
                    intent_id=r[0],
                    symbol=str(r[1]).upper(),
                    side=str(r[2]),
                    intent_type=str(r[3]),
                    quantity=Decimal(r[4]) if r[4] is not None else Decimal(0),
                    created_at=r[5],
                    reason=str(r[6] or ""),
                    payload=dict(payload),
                )
            )
    return rows


def _load_bars(
    conn: psycopg.Connection,
    *,
    strategy_code: str,
    interval_secs: int,
    symbols: list[str] | None,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[str, list[BarRow]]:
    sql = """
        SELECT symbol, bar_time, interval_secs, open_price, high_price, low_price,
               close_price, volume, trade_count, decision_status, decision_reason,
               decision_path, decision_score
        FROM strategy_bar_history
        WHERE strategy_code = %(code)s
          AND interval_secs = %(interval)s
          AND bar_time >= %(start)s
          AND bar_time < %(end)s
    """
    params: dict[str, object] = {
        "code": strategy_code,
        "interval": interval_secs,
        "start": start_utc - timedelta(seconds=interval_secs * 4),
        "end": end_utc,
    }
    if symbols:
        sql += " AND upper(symbol) = ANY(%(symbols)s)"
        params["symbols"] = [s.upper() for s in symbols]
    sql += " ORDER BY symbol, bar_time ASC"

    out: dict[str, list[BarRow]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for r in cur.fetchall():
            out[str(r[0]).upper()].append(
                BarRow(
                    bar_time=r[1],
                    interval_secs=int(r[2]),
                    open=float(r[3]),
                    high=float(r[4]),
                    low=float(r[5]),
                    close=float(r[6]),
                    volume=int(r[7]),
                    trade_count=int(r[8]),
                    decision_status=str(r[9] or ""),
                    decision_reason=str(r[10] or ""),
                    decision_path=str(r[11] or ""),
                    decision_score=str(r[12] or ""),
                )
            )
    return out


def _find_driving_bar(
    intent: IntentRow, bars: list[BarRow], interval_secs: int
) -> tuple[BarRow | None, float | None]:
    """Return the most-recent bar at or before intent.created_at, plus the
    age-in-seconds. Bars beyond the strategy's lookback window are ignored."""
    lookback = _LOOKBACK_BY_INTERVAL.get(interval_secs, float(interval_secs) * 2)
    best: BarRow | None = None
    best_age: float | None = None
    for bar in bars:
        bar_close_at = bar.bar_time + timedelta(seconds=interval_secs)
        if bar_close_at > intent.created_at:
            continue
        age = (intent.created_at - bar_close_at).total_seconds()
        if age > lookback:
            continue
        if best is None or bar.bar_time > best.bar_time:
            best = bar
            best_age = age
    return best, best_age


def _intent_path(intent: IntentRow) -> str:
    md = intent.payload.get("metadata") or {}
    if isinstance(md, dict):
        v = md.get("path")
        if v:
            return str(v)
    v = intent.payload.get("path")
    return str(v) if v else ""


def _intent_score(intent: IntentRow) -> str:
    md = intent.payload.get("metadata") or {}
    if isinstance(md, dict):
        v = md.get("score")
        if v is not None:
            return str(v)
    v = intent.payload.get("score")
    return str(v) if v is not None else ""


def _intent_reference_price(intent: IntentRow) -> float | None:
    md = intent.payload.get("metadata") or {}
    if isinstance(md, dict):
        v = md.get("reference_price")
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    v = intent.payload.get("reference_price")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _validate_open_intent(
    intent: IntentRow, bar: BarRow | None, *, price_tolerance: float
) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if bar is None:
        return False, ["no driving bar found within lookback window"]
    if bar.decision_status not in {"signal"}:
        issues.append(
            f"driving bar decision_status='{bar.decision_status}' "
            f"reason='{bar.decision_reason or '<empty>'}' (expected 'signal')"
        )
    intent_path = _intent_path(intent)
    if intent_path and bar.decision_path and intent_path != bar.decision_path:
        issues.append(
            f"path mismatch: intent.path='{intent_path}' bar.decision_path='{bar.decision_path}'"
        )
    intent_score = _intent_score(intent)
    if intent_score and bar.decision_score and intent_score != bar.decision_score:
        issues.append(
            f"score mismatch: intent.score='{intent_score}' bar.decision_score='{bar.decision_score}'"
        )
    intent_price = _intent_reference_price(intent)
    if intent_price is not None:
        diff = abs(intent_price - bar.close)
        if diff > price_tolerance:
            issues.append(
                f"price drift: intent.reference_price={intent_price:.4f} "
                f"bar.close={bar.close:.4f} diff={diff:.4f} > tol={price_tolerance:.4f}"
            )
    return len(issues) == 0, issues


def _validate_close_intent(
    intent: IntentRow, bar: BarRow | None, *, price_tolerance: float
) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if bar is None:
        issues.append("no bar at or just before close intent (acceptable for emergency closes)")
    intent_price = _intent_reference_price(intent)
    if bar is not None and intent_price is not None:
        diff = abs(intent_price - bar.close)
        if diff > price_tolerance * 5:
            issues.append(
                f"large price drift on close: intent.reference_price={intent_price:.4f} "
                f"bar.close={bar.close:.4f} diff={diff:.4f}"
            )
    return len(issues) == 0, issues


def _coverage_gap_signals(
    intents_by_symbol: dict[str, list[IntentRow]],
    bars: dict[str, list[BarRow]],
    interval_secs: int,
) -> list[tuple[str, BarRow]]:
    """Find bars marked decision_status='signal' that do NOT have a
    corresponding open intent within (bar_close, bar_close + interval]."""
    gaps: list[tuple[str, BarRow]] = []
    for symbol, sym_bars in bars.items():
        symbol_intents = intents_by_symbol.get(symbol, [])
        for bar in sym_bars:
            if bar.decision_status != "signal":
                continue
            bar_close = bar.bar_time + timedelta(seconds=interval_secs)
            window_end = bar_close + timedelta(seconds=interval_secs * 2)
            matched = any(
                ti.intent_type == "open"
                and bar_close <= ti.created_at <= window_end
                for ti in symbol_intents
            )
            if not matched:
                gaps.append((symbol, bar))
    return gaps


def _format_intent_line(intent: IntentRow, bar: BarRow | None, age: float | None) -> str:
    et_time = intent.created_at.astimezone(EASTERN).strftime("%H:%M:%S")
    parts = [
        f"{et_time} ET",
        f"{intent.symbol:<6}",
        f"{intent.intent_type:<6}",
        f"{intent.side:<4}",
        f"qty={intent.quantity}",
    ]
    refp = _intent_reference_price(intent)
    parts.append(f"ref=${refp:.4f}" if refp is not None else "ref=N/A   ")
    parts.append(f"path={_intent_path(intent) or '-':<14}")
    parts.append(f"score={_intent_score(intent) or '-':<3}")
    if bar is not None:
        parts.append(f"bar={bar.bar_time.astimezone(EASTERN).strftime('%H:%M:%S')}")
        parts.append(f"close=${bar.close:.4f}")
        parts.append(f"status={bar.decision_status or '-':<8}")
        parts.append(f"bar.path={bar.decision_path or '-':<14}")
        if age is not None:
            parts.append(f"age={age:5.1f}s")
    else:
        parts.append("bar=NONE")
    return " | ".join(parts)


def main() -> int:
    args = _parse_args()
    interval_secs = args.interval_secs or _infer_interval(args.strategy_code)
    if interval_secs not in (30, 60):
        raise SystemExit(f"unsupported interval_secs={interval_secs}")
    dsn = _resolve_dsn(args.dsn)
    start_utc, end_utc = _session_day_window(args.day)

    with psycopg.connect(dsn) as conn:
        intents = _load_intents(
            conn,
            strategy_code=args.strategy_code,
            intent_type=args.intent_type,
            symbols=args.symbol,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        bars = _load_bars(
            conn,
            strategy_code=args.strategy_code,
            interval_secs=interval_secs,
            symbols=args.symbol,
            start_utc=start_utc,
            end_utc=end_utc,
        )

    intents_by_symbol: dict[str, list[IntentRow]] = defaultdict(list)
    for ti in intents:
        intents_by_symbol[ti.symbol].append(ti)

    print(f"=== Trade validation for {args.strategy_code} on {args.day} ===")
    print(
        f"Interval: {interval_secs}s | Intent filter: {args.intent_type} | "
        f"Loaded {len(intents)} intent(s), bars across {len(bars)} symbol(s)"
    )
    print()

    clean_count = 0
    discrepancy_count = 0
    by_status: dict[str, int] = defaultdict(int)
    discrepancies: list[tuple[IntentRow, BarRow | None, list[str]]] = []

    for ti in intents:
        sym_bars = bars.get(ti.symbol, [])
        bar, age = _find_driving_bar(ti, sym_bars, interval_secs)
        if ti.intent_type in {"open", "scale"}:
            ok, issues = _validate_open_intent(ti, bar, price_tolerance=args.price_tolerance)
        else:
            ok, issues = _validate_close_intent(ti, bar, price_tolerance=args.price_tolerance)
        if ok:
            clean_count += 1
            by_status["clean"] += 1
            if args.show_clean:
                print(f"  OK  {_format_intent_line(ti, bar, age)}")
        else:
            discrepancy_count += 1
            by_status["discrepancy"] += 1
            discrepancies.append((ti, bar, issues))

    if discrepancies:
        print(f"--- Discrepancies ({len(discrepancies)}) ---")
        for ti, bar, issues in discrepancies:
            print(f"  !!  {_format_intent_line(ti, bar, None)}")
            for issue in issues:
                print(f"        - {issue}")
        print()

    if args.intent_type in {"open", "all"}:
        gaps = _coverage_gap_signals(intents_by_symbol, bars, interval_secs)
        if gaps:
            print(f"--- Coverage gaps: bars marked 'signal' with no matching open intent ({len(gaps)}) ---")
            for symbol, bar in gaps[:50]:
                print(
                    f"  ??  {bar.bar_time.astimezone(EASTERN).strftime('%H:%M:%S')} ET | "
                    f"{symbol:<6} close=${bar.close:.4f} path={bar.decision_path or '-'} "
                    f"score={bar.decision_score or '-'} reason={bar.decision_reason or '-'}"
                )
            if len(gaps) > 50:
                print(f"  ... and {len(gaps) - 50} more")
            print()
        else:
            print("No coverage gaps: every signal-bar has at least one matching open intent.")

    print("=== Summary ===")
    print(f"Total intents: {len(intents)}")
    print(f"  clean       : {clean_count}")
    print(f"  discrepancy : {discrepancy_count}")
    if args.intent_type in {"open", "all"}:
        signal_bars = sum(
            1 for sym_bars in bars.values() for b in sym_bars if b.decision_status == "signal"
        )
        print(f"Bars marked 'signal' (open path): {signal_bars}")

    return 0 if discrepancy_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
