"""[codex] Exact restart-window persisted minutes; no full-session history scan."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from sqlalchemy import event, text
from project_mai_tai.db.session import build_engine
from project_mai_tai.settings import Settings
import release_policy as p


def grade(rows, symbols, stopped, started, now):
    p.need(0 <= (started - stopped).total_seconds() <= 240, "restart interval unreadable/unbounded")
    p.need(0 <= (now - started).total_seconds() <= 600, "continuity measurement too late/future")
    p.need(len(rows) <= 1024 and len(p.canonical(rows)) <= 1_000_000, "restart minute reply exceeds bound")
    result, pending, outside, seen = [], [], [], set()
    floor = stopped.replace(second=0, microsecond=0)
    ceiling = started.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for row in rows:
        key = row["symbol"], p.moment(row["bar_time"])
        p.need(row["symbol"] in symbols and key not in seen and key[1].second == key[1].microsecond == 0,
               "foreign/duplicate/nonminute continuity row")
        seen.add(key)
    for symbol in symbols:
        candles = sorted((row for row in rows if row["symbol"] == symbol), key=lambda row: p.moment(row["bar_time"]))
        prior = max((p.moment(row["bar_time"]) for row in candles
                     if p.moment(row["bar_time"]) < stopped), default=None)
        live_at_stop = prior is not None and 0 <= (stopped - prior).total_seconds() <= 90
        following = next((p.moment(row["bar_time"]) for row in candles
            if p.moment(row["bar_time"]) >= started.replace(second=0, microsecond=0)
            and p.moment(row["bar_time"]) + timedelta(minutes=1) <= now
            and max(p.moment(row["created_at"]), p.moment(row["updated_at"])) >= started), None)
        missing = []
        if live_at_stop and following is not None:
            present = {p.moment(row["bar_time"]) for row in candles}
            minute = prior + timedelta(minutes=1)
            while minute < following:
                if minute not in present:
                    missing.append(minute)
                minute += timedelta(minutes=1)
            outside.extend(dict(symbol=symbol, minute_utc=value.isoformat()) for value in missing
                           if not floor <= value < ceiling)
        if live_at_stop and following is None:
            pending.append(symbol)
        result.append(dict(symbol=symbol, live_at_stop=live_at_stop,
            prior_utc=prior.isoformat() if prior else None, first_closed_post_utc=following.isoformat() if following else None,
            missing_persisted_minutes_utc=[value.isoformat() for value in missing],
            independent_print_status="UNMEASURED", no_live_at_stop="no restart-spanning claim" if not live_at_stop else None))
    p.need(not outside, "missing persisted minute outside actual restart interval: " + json.dumps(outside))
    return dict(verdict="PENDING_FIRST_CLOSED_BAR" if pending else "MEASURED_RESTART_WINDOW",
                pending_symbols=pending, per_stock=result, stop_started_utc=stopped.isoformat(),
                start_returned_utc=started.isoformat(), independent_print_status="UNMEASURED",
                no_zero_holes_claim=True, measured_at_utc=now.isoformat())


def capture(connection, symbols, stopped, started, now):
    p.need(isinstance(symbols, list) and len(symbols) <= 128 and len(symbols) == len(set(symbols))
           and all(__import__("re").fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", symbol) for symbol in symbols),
           "complete bounded watched population required")
    if not symbols:
        return grade([], symbols, stopped, started, now)
    query = text("SELECT symbol,bar_time,created_at,updated_at FROM strategy_bar_history "
                 "WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 AND symbol=ANY(:symbols) "
                 "AND bar_time>=:lo AND bar_time<=:hi ORDER BY symbol,bar_time LIMIT 1025")
    rows = [dict(row) for row in connection.execute(query, dict(symbols=symbols,
            lo=stopped - timedelta(seconds=90), hi=now)).mappings()]
    return grade(rows, symbols, stopped, started, now)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--interval", required=True, type=Path)
    args = parser.parse_args()
    snapshot, interval = json.loads(args.snapshot.read_bytes()), json.loads(args.interval.read_bytes())
    engine = build_engine(Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url,
                          connect_timeout_s=5, statement_timeout_ms=5000)

    @event.listens_for(engine, "begin")
    def readonly(connection):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            result = capture(connection, snapshot["v2_watchlist"]["symbols"], p.moment(interval["stop_started_utc"]),
                             p.moment(interval["start_returned_utc"]), datetime.now(timezone.utc))
        print(json.dumps(result, sort_keys=True))
        return 2 if result["pending_symbols"] else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except p.Stop as exc:
        print("BLOCK continuity: " + str(exc), file=__import__("sys").stderr)
        raise SystemExit(1)
    except Exception as exc:
        print("UNKNOWN continuity: " + type(exc).__name__, file=__import__("sys").stderr)
        raise SystemExit(2)
