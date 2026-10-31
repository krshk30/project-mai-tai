"""One bounded RR/RO TODAY collection, not an input-completeness oracle.

Only the future Main-executed job calls collect(). No application/provider or
live-service construction. No count/availability polling before population read.
"""

from collections import Counter, deque
from datetime import datetime, time as day_time, timezone
from decimal import Decimal
import hashlib
import json
import os
import re
import time
from uuid import UUID
from zoneinfo import ZoneInfo

import atrsimlive1_queries as queries
from atrsimlive1_progress import stage


ET = ZoneInfo("America/New_York")
MAX_INPUT_BYTES = 512 * 1024 * 1024


def plain(value):
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("source_clock_requires_timezone")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def encoded(value):
    return json.dumps(plain(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _rows(cursor):
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def collect(*, dsn, processor, capture_log_path, now=None, working_policy_path=None, null_policy=None, incremental=None,
            deadline_partial=None):
    from atrsimlive1_null_policy import require_null_policy

    require_null_policy(null_policy)
    if incremental is not None and working_policy_path is None:
        raise ValueError('incremental_requires_explicit_working_window_policy')
    import psycopg  # Delayed; author source inspection never imports DB drivers.

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("aware_collection_clock_required")
    day = now.astimezone(ET).date()
    start = datetime.combine(day, day_time(4), ET).astimezone(timezone.utc)
    end_of_day = datetime.combine(day, day_time(20), ET).astimezone(timezone.utc)
    if not start <= now:
        raise ValueError("today_04_not_reached")
    stage("COLLECT", "+")
    started = time.monotonic()
    query_number = 0
    working_bytes = 0
    def fetch(cursor, sql, bound, label, *, mappings=True):
        nonlocal query_number, working_bytes
        if working_policy_path is not None:
            if time.monotonic() - started > 180:
                raise TimeoutError("today_collection_budget")
            if query_number >= queries.MAX_CAPTURE_PAGES:
                raise ValueError("today_capture_page_budget")
        query_number += 1
        stage(label, "+")
        code = "Q" + str(query_number)
        stage(code, "+")
        cursor.execute(sql, bound)
        result = _rows(cursor) if mappings else cursor.fetchall()
        if working_policy_path is not None:
            working_bytes += len(encoded(result))
            if working_bytes > MAX_INPUT_BYTES:
                raise ValueError("today_source_byte_cap")
            if time.monotonic() - started > 180:
                raise TimeoutError("today_collection_budget")
        stage(code, "-", len(result))
        return result
    materialized = deque()
    with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as connection:
        try:
            with connection.cursor() as cursor:
                for command in queries.BEGIN:
                    cursor.execute(command)
                identity, = fetch(cursor, queries.IDENTITY, None, "IDENTITY")
                asof = identity["source_asof"]
                if (identity["read_only"] != "on"
                        or identity["isolation"] != "repeatable read"
                        or asof.astimezone(ET).date() != day):
                    raise ValueError("actual_today_readonly_snapshot_required")
                params = dict(day=day, start=start, end=min(asof, end_of_day),
                              asof=asof, required_symbols=["ELPW", "POM", "CHSN"])
                symbols = [row[0] for row in fetch(cursor, queries.ROSTER, params, "ROSTER", mappings=False)]
                if (not 1 <= len(symbols) <= 256 or len(set(symbols)) != len(symbols)
                        or any(not isinstance(s, str) or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", s) is None
                               for s in symbols)):
                    raise ValueError("today_symbol_population_overflow_or_shape")
                params["symbols"] = symbols
                if incremental is not None:
                    incremental.bind_snapshot(identity, params, symbols)
                data, size = {}, 0
                for kind, sql in queries.SOURCES.items():
                    if time.monotonic() - started > 180:
                        raise TimeoutError("today_collection_budget")
                    rows = fetch(cursor, sql, params, "SOURCE:" + kind)
                    if len(rows) > queries.LIMITS[kind]:
                        raise ValueError("today_source_row_cap:" + kind)
                    size += len(encoded(rows))
                    if size > MAX_INPUT_BYTES:
                        raise ValueError("today_source_byte_cap")
                    data[kind] = rows
                universe = None
                if working_policy_path is not None:
                    from atrsimlive1_window_collect import metadata, policy_input

                    policy, selection_source = policy_input(working_policy_path,
                        snapshot=identity['snapshot_id'], asof=asof)
                    universe = metadata(lambda sql, bound, label: fetch(cursor, sql, bound, label), params)
                pages, total_bytes = 0, size
                for symbol in symbols:
                    scoped = {kind: [row for row in rows if row["symbol"] == symbol]
                              for kind, rows in data.items()}
                    if universe is not None:
                        from atrsimlive1_window_collect import collect_symbol

                        selector = collect_symbol if incremental is None else incremental.collect_symbol
                        certificate = selector(symbol=symbol, scoped=scoped, params=params,
                            snapshot=identity['snapshot_id'], universe=universe,
                            fetch=lambda sql, bound, label: fetch(cursor, sql, bound, label),
                            policy=policy, source=selection_source, queries=queries, null_policy=null_policy)
                        if incremental is not None and working_bytes + incremental.materialized_bytes > MAX_INPUT_BYTES:
                            raise ValueError('today_source_byte_cap')
                        materialized.append((symbol, scoped, certificate))
                        continue
                    for kind, sql in queries.CAPTURE_PAGES.items():
                        stage("PAGE:" + kind + ":" + symbol, "+")
                        scoped[kind] = []
                        after, after_id, symbol_count = None, None, 0
                        while True:
                            pages += 1
                            if pages > queries.MAX_CAPTURE_PAGES:
                                raise ValueError("today_capture_page_budget")
                            if time.monotonic() - started > 180:
                                raise TimeoutError("today_collection_budget")
                            bound = dict(params, symbol=symbol, after_ts=after,
                                         after_id=after_id)
                            # The PAGE header maps compact query IDs to symbol/kind.
                            query_number += 1
                            code = "Q" + str(query_number)
                            stage(code, "+")
                            cursor.execute(sql, bound)
                            found = _rows(cursor)
                            stage(code, "-", len(found))
                            page = found[:queries.PAGE_SIZE]
                            symbol_count += len(page)
                            if symbol_count > queries.MAX_SYMBOL_CAPTURE_ROWS:
                                raise ValueError("today_symbol_capture_cap:" + kind)
                            total_bytes += len(encoded(page))
                            if total_bytes > MAX_INPUT_BYTES:
                                raise ValueError("today_source_byte_cap")
                            scoped[kind].extend(page)
                            if len(found) <= queries.PAGE_SIZE:
                                break
                            next_key = page[-1]["event_ts"], page[-1]["id"]
                            if after is not None and next_key <= (after, after_id):
                                raise ValueError("today_capture_cursor_not_advancing")
                            after, after_id = next_key
                    materialized.append((symbol, scoped, None))
        finally:
            # Even source failure never commits; connection closes before child return.
            with connection.cursor() as cursor:
                stage("ROLLBACK", "+")
                cursor.execute("ROLLBACK")
                stage("ROLLBACK", "-")
    # All DB work and connection closure precede file hashing / pure engines.
    # Keep the original one snapshot and every account of one symbol together.
    # The existing 512MiB bound now caps the entire materialized population;
    # overflow refuses rather than publishing a partial set or raising limits.
    stage("DB_CLOSED", "-")
    collection_seconds = time.monotonic() - started
    del data
    from atrsimlive1_data_coverage import capture_log

    log_witness = capture_log(capture_log_path, asof=asof)
    outputs = []
    if deadline_partial is not None:
        # This occurs only AFTER the original RR rollback/connection closure
        # and after the full original roster has been read. Not a fake census.
        deadline_partial.bind_roster(day=day.isoformat(), source_asof=asof,
            snapshot_id=identity['snapshot_id'], roster=symbols)
    while materialized:
        if deadline_partial is not None and deadline_partial.stop_before_next_symbol():
            break
        symbol, scoped, certificate = materialized.popleft()
        stage("HASH:" + symbol, "+")
        hashes = {k: digest(v) for k, v in scoped.items()}
        stage("HASH:" + symbol, "-")
        stage("MODEL:" + symbol, "+")
        packet = dict(trading_day=day.isoformat(), source_asof=asof,
            snapshot_id=identity["snapshot_id"], start=start, end=params["end"],
            symbols=[symbol], rows=scoped, source_hashes=hashes,
            capture_log=log_witness,
            source_counts={k: len(v) for k, v in scoped.items()},
            read_proof=dict(provider="massive", table="market_capture_trades",
                symbol=symbol, start=start, end=params["end"], full_read=True,
                truncated=False, row_count=len(scoped["trades"]),
                rows_sha256=hashes["trades"],
                bar_provider="schwab", bar_table="strategy_bar_history", bars_full_read=True,
                bars_truncated=False, bar_count=len(scoped["bars"]),
                bars_sha256=hashes["bars"]),
            query_sha256=digest({**queries.SOURCES, **queries.CAPTURE_PAGES}),
            collection_seconds=collection_seconds)
        if certificate is not None:
            from atrsimlive1_window_collect import seal

            packet = seal(packet, certificate)
            if incremental is not None:
                packet = incremental.seal_packet(packet)
        outputs.append(processor(packet))
        stage("MODEL:" + symbol, "-")
    stage("COLLECT", "-")
    return outputs


def dsn_from_environment(name):
    if name not in {"DATABASE_URL", "MAI_TAI_DATABASE_URL"}:
        raise ValueError("explicit_existing_database_environment_required")
    value = os.environ.get(name)
    if not value:
        raise ValueError("database_environment_unavailable")
    value = value.replace("postgresql+psycopg://", "postgresql://", 1)
    value = value.replace("postgresql+psycopg2://", "postgresql://", 1)
    if not value.startswith(("postgresql://", "postgres://")):
        raise ValueError("postgres_read_source_required")
    return value
