"""RETAIN1: allowlisted, bounded age pruning; report-only unless --go is supplied.

--keep-days may be repeated as TABLE=DAYS, or supplied once as a common integer.
scanner_cycle_history is a logical target, NOT permission to prune dashboard state.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Sequence

import psycopg


@dataclass(frozen=True)
class Target:
    table: str
    age_column: str
    minimum_days: int


TARGETS = {
    "market_trade_ticks": Target("market_trade_ticks", "received_at", 7),
    "market_quote_ticks": Target("market_quote_ticks", "received_at", 7),
    "market_capture_trades": Target("market_capture_trades", "received_at", 7),
    "market_capture_quotes": Target("market_capture_quotes", "received_at", 7),
    "market_capture_bars": Target("market_capture_bars", "received_at", 7),
    "reconciliation_findings": Target("reconciliation_findings", "created_at", 7),
    "reconciliation_runs": Target("reconciliation_runs", "started_at", 7),
    "scanner_cycle_history": Target("dashboard_snapshots", "created_at", 7),
    "scanner_confirmed_events": Target("scanner_confirmed_events", "event_at", 7),
    "momentum_paper_events": Target("momentum_paper_events", "observed_at", 1),
    "strategy_bar_history": Target("strategy_bar_history", "bar_time", 45),
}
TABLES = ("market_trade_ticks", "market_quote_ticks")
PAPER_PLUMBING = ("PATH_PRINT", "FEED_GAP")
PRUNE_ORDER = tuple(TARGETS)
LOCK_KEY = 2026100701


def _dsn(arg: str | None) -> str:
    raw = arg or os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise ValueError("no DSN: pass --dsn or set MAI_TAI_DATABASE_URL")
    return raw.replace("postgresql+psycopg://", "postgresql://")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tables", default=",".join(TABLES))
    ap.add_argument("--keep-days", action="append", default=[], metavar="DAYS|TABLE=DAYS")
    ap.add_argument("--event-types", default=None, help="paper plumbing only: PATH_PRINT,FEED_GAP")
    ap.add_argument("--batch", type=int, default=10_000)
    ap.add_argument("--max-rows", type=int, default=1_000_000, help="shared cap for the entire run")
    ap.add_argument("--max-seconds", type=int, default=3600)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--go", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args(argv)
    selected = tuple(part.strip() for part in args.tables.split(","))
    if not selected or len(set(selected)) != len(selected):
        ap.error("tables must be nonempty and unique")
    for name in selected:
        if name not in TARGETS:
            ap.error(f"refusing protected or unknown target: {name!r}")
    args.tables = tuple(name for name in PRUNE_ORDER if name in selected)
    common = None
    overrides = {}
    try:
        for item in args.keep_days:
            if "=" in item:
                name, value = item.split("=", 1)
                if name not in selected or name in overrides:
                    raise ValueError("override must name a selected target exactly once")
                overrides[name] = int(value)
            else:
                if common is not None:
                    raise ValueError("only one common keep-days value is allowed")
                common = int(item)
        args.keep_days = {
            name: overrides.get(name, common if common is not None else TARGETS[name].minimum_days)
            for name in args.tables
        }
        for name, days in args.keep_days.items():
            if days < TARGETS[name].minimum_days:
                raise ValueError(f"{name} must retain at least {TARGETS[name].minimum_days} days")
        if not 1 <= args.batch <= 50_000 or args.max_rows < 1 or args.max_seconds < 1:
            raise ValueError("positive bounds required; batch maximum is 50000")
        event_text = args.event_types if args.event_types is not None else ",".join(PAPER_PLUMBING)
        events = tuple(part.strip() for part in event_text.split(","))
        if not events or len(set(events)) != len(events) or not set(events) <= set(PAPER_PLUMBING):
            raise ValueError("paper filter may contain only PATH_PRINT and FEED_GAP, once each")
        if args.event_types is not None and "momentum_paper_events" not in selected:
            raise ValueError("event-types requires the paper target")
        args.event_types = events
    except ValueError as exc:
        ap.error(str(exc))
    return args


def predicate(name: str, cutoff: datetime, events: tuple[str, ...], alias: str) -> tuple[str, tuple]:
    target = TARGETS[name]
    clauses = [f"{alias}.{target.age_column} < %s"]
    params: tuple = (cutoff,)
    if name == "momentum_paper_events":
        if not events or not set(events) <= set(PAPER_PLUMBING):
            raise ValueError("refusing non-plumbing paper events")
        clauses.append(f"{alias}.event_type IN ({','.join('%s' for _ in events)})")
        params += events
    if name == "scanner_cycle_history":
        clauses.append(f"{alias}.snapshot_type = %s")
        params += ("scanner_cycle_history",)
    if name == "reconciliation_runs":
        clauses.append("NOT EXISTS (SELECT 1 FROM reconciliation_findings f "
                       f"WHERE f.reconciliation_run_id = {alias}.id)")
    return " AND ".join(clauses), params


def count_query(name: str, cutoff: datetime, events: tuple[str, ...]) -> tuple[str, tuple]:
    where, params = predicate(name, cutoff, events, "r")
    return f"SELECT count(*) FROM {TARGETS[name].table} r WHERE {where}", params


def delete_query(name: str, cutoff: datetime, events: tuple[str, ...], limit: int) -> tuple[str, tuple]:
    if not 1 <= limit <= 50_000:
        raise ValueError("invalid delete batch bound")
    target = TARGETS[name]
    inner, params = predicate(name, cutoff, events, "c")
    outer, outer_params = predicate(name, cutoff, events, "r")
    # Recheck the type/age guard on the row being deleted, not only the selected ID.
    sql = (f"DELETE FROM {target.table} AS r WHERE r.id IN (SELECT c.id FROM {target.table} c "
           f"WHERE {inner} ORDER BY c.{target.age_column}, c.id LIMIT %s) AND {outer}")
    return sql, (*params, limit, *outer_params)


def _count(conn, name: str, cutoff: datetime, events: tuple[str, ...]) -> int:
    with conn.cursor() as cur:
        cur.execute(*count_query(name, cutoff, events))
        return int(cur.fetchone()[0])


def _size(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_total_relation_size(%s)", (table,))
        return int(cur.fetchone()[0])


def _report(**fields) -> None:
    print(json.dumps(fields, sort_keys=True), flush=True)


def run(conn, args: argparse.Namespace, *, now: datetime | None = None, monotonic=time.monotonic) -> int:
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        raise ValueError("cutoff clock must be timezone-aware")
    cutoffs = {name: reference - timedelta(days=args.keep_days[name]) for name in args.tables}
    started = monotonic()
    budget = args.max_rows
    incomplete = False
    changed = []
    locked = False
    try:
        if args.go:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
                locked = bool(cur.fetchone()[0])
            if not locked:
                raise RuntimeError("another retention writer holds the lock")
        for name in args.tables:
            target = TARGETS[name]
            cutoff = cutoffs[name]
            before_bytes = _size(conn, target.table)
            before = _count(conn, name, cutoff, args.event_types)
            _report(stage="dry-run-count", target=name, physical_table=target.table,
                    eligible=before, keep_days=args.keep_days[name], cutoff=cutoff.isoformat(),
                    relation_bytes=before_bytes, dry_run=not args.go,
                    lower_bound=name == "reconciliation_runs" and not args.go)
            if not args.go:
                continue
            deleted = 0
            while budget > 0 and monotonic() - started < args.max_seconds:
                limit = min(args.batch, budget)
                batch_started = monotonic()
                with conn.cursor() as cur:
                    cur.execute(*delete_query(name, cutoff, args.event_types, limit))
                    rows = cur.rowcount
                batch_seconds = monotonic() - batch_started
                deleted += rows
                budget -= rows
                _report(stage="batch", target=name, deleted=rows, batch_limit=limit,
                        elapsed_seconds=batch_seconds, remaining_run_budget=budget)
                if rows < limit:
                    break
            if deleted:
                changed.append(target.table)
            remaining = _count(conn, name, cutoff, args.event_types)
            incomplete |= remaining > 0
            _report(stage="deleted", target=name, deleted=deleted, eligible_remaining=remaining,
                    relation_bytes_before=before_bytes, relation_bytes_after=_size(conn, target.table))
        for table in dict.fromkeys(changed):
            with conn.cursor() as cur:
                cur.execute(f"VACUUM (ANALYZE) {table}")
            _report(stage="vacuum-analyze", table=table, relation_bytes=_size(conn, table),
                    note="ordinary vacuum makes space reusable; OS file shrink is not promised")
        _report(stage="bounded-incomplete" if incomplete else "complete", dry_run=not args.go,
                rows_deleted=args.max_rows - budget)
        return 3 if incomplete else 0
    finally:
        if locked:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        # Autocommit gives each batch its own transaction and allows ordinary VACUUM.
        options = "-c lock_timeout=1000 -c statement_timeout=60000"
        if not args.go:
            options += " -c default_transaction_read_only=on"
        with psycopg.connect(_dsn(args.dsn), autocommit=True, connect_timeout=10, options=options) as conn:
            return run(conn, args)
    except (ValueError, RuntimeError, psycopg.Error) as exc:
        _report(stage="abort", error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
