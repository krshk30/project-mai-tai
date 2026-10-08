"""Read-only Oct 8 proof collector. Evidence verdicts do not authorize deployment.

The runner supplies the ten-minute wait and may supply --restart-window containing
stop_started_utc. No service action, broker call, Redis write, or SQL write occurs here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import urlopen
from zoneinfo import ZoneInfo

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
REPO = Path("/home/trader/project-mai-tai")
LOG_DIR = Path("/var/log/project-mai-tai")
SERVICES = ("oms", "schwab-1m-v2", "strategy", "control", "market-capture",
            "market-data", "orb", "orb-schwab", "reconciler", "momentum-paper",
            "option-a-daily-guard", "redis", "postgresql")
RESTARTED = {"oms", "schwab-1m-v2", "strategy", "orb-schwab", "control"}
OWNERS = {"strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper"}
OWNER_FIELDS = OWNERS | {"_migration_complete", "_last_applied_id"}
PREFIX = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_"
GAP = PREFIX + "GAP_LINE_CARRY_ENABLED"
LINE = PREFIX + "LINE_CHART_RESTORATION_ENABLED"
HANDOFF = PREFIX + "ATR_REPRICE_HANDOFF_ENABLED"
FLAG_KEYS = {PREFIX + name + "_ENABLED" for name in (
    "PM_PRINT_ASK_CONFIRM", "PM_FLIP_WAIT", "PM_REST_REPRICE", "ATR_REPRICE_HANDOFF",
    "LINE_CHART_RESTORATION", "GAP_LINE_CARRY", "RESTING_BUY_ROUND_UP", "GAP_HOLD",
    "RETRY_ONE", "KEEP_REST_AFTER_BUY", "REMOVED_WAIT_CLEAR", "SLOTCLEAR_FRESH_FLIP", "SLOTCLEAR_FRESH_SELL",
)} | {"MAI_TAI_OMS_V2_WEBULL_MIRROR_RETAINED_HOLD_ENABLED",
     "MAI_TAI_OMS_V2_EH_FRESH_PRICE_ENABLED", "MAI_TAI_MARKET_DATA_SUBSCRIPTION_STARTUP_ENABLED",
     "MAI_TAI_OMS_V2_WEBULL_MIRROR_FRESH_PRICE_ENABLED"}
MAX_REPLY = 4_000_000
MAX_LOG_BYTES = 32_000_000
MAX_LOG_FILES = 24
MAX_LOG_INVENTORY = 256
MAX_CURSOR = 512_000_000
STAMP = re.compile(r"^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[,.]\d+)?(?:Z|\+00:00)?)")
SYNC = re.compile(r"\[OMS-BROKER-SYNC-PASS\] id=(\d+) phase=(start|end)(.*)")


class Unknown(RuntimeError):
    """Messages are public fixed codes, never provider payloads or credentials."""


def moment(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(",", "."))
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)  # Repository configure_logging uses UTC.
    return result.astimezone(UTC)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def command(args, limit=MAX_REPLY):
    result = subprocess.run(args, capture_output=True, timeout=25, check=False)
    if result.returncode or len(result.stdout) > limit:
        raise Unknown("command_failed_or_unbounded")
    return result.stdout.decode("utf-8", errors="strict")


def bounded_file(path, limit):
    with Path(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise Unknown("file_bound_exceeded")
    return raw


def service_identity(role):
    unit = role + ".service" if role in {"redis", "postgresql"} else "project-mai-tai-" + role + ".service"
    fields = ("MainPID", "NRestarts", "ActiveState", "SubState", "Result", "InvocationID",
              "ExecMainStartTimestamp", "ExecMainStartTimestampMonotonic", "InactiveEnterTimestamp",
              "ExecMainStatus", "ExecMainCode")
    raw = command(["systemctl", "show", unit, *["--property=" + field for field in fields]])
    pairs = [line.split("=", 1) for line in raw.splitlines()]
    if any(len(pair) != 2 for pair in pairs) or len(dict(pairs)) != len(pairs):
        raise Unknown("identity_malformed")
    result = dict(pairs)
    if set(result) != set(fields):
        raise Unknown("identity_fields_missing")
    for field in ("MainPID", "NRestarts", "ExecMainStartTimestampMonotonic", "ExecMainStatus", "ExecMainCode"):
        result[field] = int(result[field])
    start = result["ExecMainStartTimestamp"]
    result["start_utc"] = (command(["date", "--date=" + start, "--utc", "--iso-8601=ns"])
                           .strip() if start else None)
    stopped = result["InactiveEnterTimestamp"]
    result["inactive_utc"] = (command(["date", "--date=" + stopped, "--utc", "--iso-8601=ns"])
                              .strip() if stopped else None)
    result["unit"] = unit
    return result


def process_flags(pid):
    pairs = [piece.split(b"=", 1) for piece in bounded_file(f"/proc/{pid}/environ", 262144).split(b"\0") if piece]
    result = {}
    for key in sorted(FLAG_KEYS):
        values = [value.decode("ascii") for name, value in pairs if name == key.encode()]
        if any(value.lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"} for value in values):
            raise Unknown("whitelisted_flag_value_not_boolean")
        result[key] = values
    return {"values": result, "sha256": digest(result)}


def redis_capture(settings, now):
    import redis

    client = redis.Redis.from_url(settings.redis_url, decode_responses=True,
                                  socket_timeout=5, socket_connect_timeout=5)
    try:
        key = settings.redis_stream_prefix + ":market-data-subscription-owners"
        if client.type(key) != "hash" or client.hlen(key) != len(OWNER_FIELDS):
            raise Unknown("owners_population_unproven")
        if sum(client.hstrlen(key, field) for field in OWNER_FIELDS) > 100000:
            raise Unknown("owners_reply_unbounded")
        owners = client.hgetall(key)
        if set(owners) != OWNER_FIELDS or owners["_migration_complete"] != "1":
            raise Unknown("owners_or_marker_unproven")
        sets = {key: json.loads(owners[key]) for key in OWNERS}
        for symbols in sets.values():
            if (not isinstance(symbols, list) or len(symbols) > 1000
                    or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", s) for s in symbols)
                    or len(symbols) != len(set(symbols))):
                raise Unknown("owner_symbols_malformed")
        if len(sets["momentum-paper"]) > 16:
            raise Unknown("paper_cap_exceeded")
        stats = client.info("stats")
        memory = client.info("memory")
        # Bound each event before decoding; do not materialize a stream range.
        stream = settings.redis_stream_prefix + ":strategy-state-isolated"
        cursor, watch = "+", None
        for _ in range(80):
            rows = client.xrevrange(stream, max=cursor, min="-", count=1)
            if not rows:
                break
            entry_id, fields = rows[0]
            cursor = "(" + entry_id
            for raw in fields.values():
                if len(raw.encode()) > 262144:
                    raise Unknown("state_event_unbounded")
                try:
                    event = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict) or event.get("source_service") != "schwab-1m-v2":
                    continue
                symbols = event.get("payload", {}).get("watchlist")
                stamp = moment(event["produced_at"])
                if (not isinstance(symbols, list) or len(symbols) > 128
                        or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", s) for s in symbols)
                        or len(set(symbols)) != len(symbols)
                        or not 0 <= (datetime.now(UTC) - stamp).total_seconds() <= 60):
                    raise Unknown("watchlist_unbounded_or_stale")
                watch = {"symbols": sorted(symbols), "produced_at_utc": stamp.isoformat(), "stream_id": entry_id}
                break
            if watch is not None:
                break
        if watch is None:
            raise Unknown("watchlist_missing")
        return {"owners": owners, "owners_sha256": digest(owners), "sets": sets,
                "active_union": sorted(set().union(*map(set, sets.values()))),
                "evicted_keys": int(stats["evicted_keys"]), "used_memory": int(memory["used_memory"]),
                "v2_watchlist": watch}
    finally:
        client.close()


def log_cursor(path):
    path = Path(path)
    with path.open("rb") as stream:
        stat = __import__("os").fstat(stream.fileno())
        offset = stat.st_size
        stream.seek(max(0, offset - 4096))
        anchor = stream.read(min(4096, offset))
    if offset > MAX_CURSOR:
        raise Unknown("log_cursor_unbounded")
    return {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino, "mtime_ns": stat.st_mtime_ns,
            "offset": offset, "anchor_size": len(anchor), "anchor_sha256": hashlib.sha256(anchor).hexdigest()}


def log_ranges(cursor, *, directory=LOG_DIR):
    """Locate baseline bytes after rename/compression; missing bytes stay UNKNOWN."""
    live = Path(cursor["path"])
    if (not 0 <= cursor["offset"] <= MAX_CURSOR or not 0 <= cursor["anchor_size"] <= min(4096, cursor["offset"])
            or not re.fullmatch(r"[0-9a-f]{64}", cursor["anchor_sha256"])):
        raise Unknown("log_cursor_malformed")
    inventory = list(Path(directory).glob(live.name + "*"))
    if len(inventory) > MAX_LOG_INVENTORY or live not in inventory:
        raise Unknown("log_inventory_missing_or_unbounded")
    paths = [path for path in inventory if path.stat().st_mtime_ns >= cursor["mtime_ns"]
             or (path.stat().st_dev, path.stat().st_ino) == (cursor["device"], cursor["inode"])]
    if len(paths) > MAX_LOG_FILES:
        raise Unknown("recent_log_inventory_unbounded")
    paths.sort(key=lambda path: (path.stat().st_mtime_ns, path.name))
    candidates = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise Unknown("log_not_regular")
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rb") as stream:
            offset, count = cursor["offset"], cursor["anchor_size"]
            stream.seek(offset - count)
            anchor = stream.read(count)
        if len(anchor) == count and hashlib.sha256(anchor).hexdigest() == cursor["anchor_sha256"]:
            stat = path.stat()
            inode_match = (stat.st_dev, stat.st_ino) == (cursor["device"], cursor["inode"])
            candidates.append((path, inode_match))
    exact = [path for path, match in candidates if match]
    if len(exact) == 1:
        original = exact[0]
    elif len(candidates) == 1 and cursor["anchor_size"] > 0:
        original = candidates[0][0]
    else:
        raise Unknown("baseline_log_cursor_not_unique")
    index = paths.index(original)
    selected = paths[index:]
    ranges, total = [], 0
    for path in selected:
        start = cursor["offset"] if path == original else 0
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rb") as stream:
            stream.seek(start)
            raw = stream.read(MAX_LOG_BYTES - total + 1)
        total += len(raw)
        if total > MAX_LOG_BYTES:
            raise Unknown("log_read_bound_exceeded")
        ranges.append({"path": str(path), "inode": path.stat().st_ino,
                       "from_byte": start, "to_byte": start + len(raw),
                       "sha256": hashlib.sha256(raw).hexdigest(),
                       "text": raw.decode("utf-8", errors="strict")})
    if live not in selected:
        raise Unknown("live_log_not_after_cursor")
    return ranges


def log_records(ranges, start, end):
    records, current = [], None
    for item in ranges:
        for number, line in enumerate(item["text"].splitlines(), 1):
            match = STAMP.match(line)
            if match:
                current = moment(match[1])
            if current is None:
                if line.strip():
                    raise Unknown("untimestamped_postcursor_log")
                continue
            if start <= current <= end:
                records.append({"path": item["path"], "range_line": number, "at_utc": current.isoformat(), "line": line})
    return records


def summarize_logs(records):
    errors, starts, ends, shadows, bad_shadow = [], {}, {}, {}, []
    for record in records:
        line = record["line"]
        if re.search(r"\bERROR\b|Traceback \(most recent call last\)", line):
            # Do not serialize raw error payloads: they can contain broker request data.
            errors.append({key: record[key] for key in ("path", "range_line", "at_utc")})
        sync = SYNC.search(line)
        if sync:
            pass_id, phase, tail = sync.groups()
            target = starts if phase == "start" else ends
            target.setdefault(pass_id, []).append(record)
            if phase == "end":
                duration = re.search(r"duration_ms=([0-9.]+)", tail)
                record["duration_ms"] = float(duration[1]) if duration else None
                outcome = re.search(r"outcome=(\w+)", tail)
                record["outcome"] = outcome[1] if outcome else None
        if "[WBQUIET-SHADOW] " in line:
            try:
                payload = json.loads(line.split("[WBQUIET-SHADOW] ", 1)[1])
                if payload.get("policy_applied") is not False:
                    raise ValueError("not shadow")
                pass_id = str(int(payload["pass_id"]))
                shadows.setdefault(pass_id, []).append({"process_pid": int(payload["process_pid"]),
                    "dropped_observations": int(payload["dropped_observations"]),
                    "path": record["path"], "at_utc": record["at_utc"]})
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                bad_shadow.append({key: record[key] for key in ("path", "range_line", "at_utc")})
    complete = sorted(set(starts) & set(ends), key=int)
    missing = sorted(set(complete) - set(shadows), key=int)
    duplicate = sorted({key for group in (starts, ends, shadows) for key, rows in group.items() if len(rows) > 1}, key=int)
    durations = sorted(ends[key][0]["duration_ms"] for key in complete if ends[key][0]["duration_ms"] is not None)
    return {"errors": errors, "traceback_or_error_count": len(errors),
            "sync_start_ids": sorted(starts, key=int), "sync_end_ids": sorted(ends, key=int),
            "complete_sync_ids": complete, "shadow_ids": sorted(shadows, key=int),
            "shadow_missing_for_complete_pass": missing, "duplicate_pass_ids": duplicate,
            "edge_incomplete_ids": sorted(set(starts) ^ set(ends), key=int),
            "malformed_shadow": bad_shadow, "shadows": shadows,
            "sync_outcomes": {key: ends[key][0]["outcome"] for key in complete},
            "sync_duration_ms": {"samples": len(durations), "max": max(durations) if durations else None,
                "p95": durations[max(0, math.ceil(len(durations) * .95) - 1)] if durations else None},
            "dropped_observations_max": max((row["dropped_observations"] for rows in shadows.values() for row in rows), default=None)}


def scanner_receipt(overview, now):
    if overview.get("errors"):
        raise Unknown("overview_errors")
    rows = [row for row in overview.get("services", []) if row.get("service_name") == "strategy-engine"]
    if len(rows) != 1:
        raise Unknown("scanner_heartbeat_missing_or_ambiguous")
    row = rows[0]
    stamp = moment(row["observed_at_raw"])
    details = row.get("details", {})
    age = (now - stamp).total_seconds()
    healthy = (row.get("effective_status", row.get("status")) == "healthy"
               and details.get("main_loop_health") == "healthy"
               and str(details.get("main_loop_exceptions_total")) == "0")
    return {"verdict": "PASS" if healthy and 0 <= age <= 120 else "UNKNOWN" if not 0 <= age <= 120 else "FAIL",
            "observed_at_utc": stamp.isoformat(), "age_seconds": age,
            "effective_status": row.get("effective_status", row.get("status")),
            "main_loop_health": details.get("main_loop_health"),
            "main_loop_exceptions_total": details.get("main_loop_exceptions_total"),
            "scanner_status": overview.get("scanner", {}).get("status"),
            "rule_9b_acceptance": "UNMEASURED", "scope": "heartbeat and main loop only; not a scanner-validation receipt"}


def database_capture(settings, *, symbols=None, low=None, high=None):
    from sqlalchemy import text
    from project_mai_tai.db.session import build_engine

    if symbols is not None and (not isinstance(symbols, list) or len(symbols) > 128
            or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", s) for s in symbols)
            or len(set(symbols)) != len(symbols)):
        raise Unknown("bar_symbol_population_unbounded_or_malformed")
    engine = build_engine(settings.database_url, connect_timeout_s=5, statement_timeout_ms=5000)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET LOCAL lock_timeout='500ms'")
            stats = dict(connection.execute(text(
                "SELECT clock_timestamp() AS at_utc, xact_commit, xact_rollback, stats_reset "
                "FROM pg_stat_database WHERE datname=current_database()" )).mappings().one())
            stats = {key: value.isoformat() if isinstance(value, datetime) else value for key, value in stats.items()}
            rows = []
            query_started = time.perf_counter()
            if symbols:
                query = text("SELECT symbol,bar_time,created_at,updated_at FROM strategy_bar_history "
                    "WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 AND symbol=ANY(:symbols) "
                    "AND bar_time>=:lo AND bar_time<=:hi ORDER BY symbol,bar_time LIMIT 4097")
                rows = [dict(row) for row in connection.execute(query, {"symbols": symbols, "lo": low, "hi": high}).mappings()]
                if len(rows) > 4096:
                    raise Unknown("bar_query_bound_exceeded")
                rows = [{key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()} for row in rows]
            return {"stats": stats, "bars": rows,
                    "bar_query_elapsed_seconds": time.perf_counter() - query_started if symbols else None}
    finally:
        engine.dispose()


def transaction_rate(before, after):
    elapsed = (moment(after["at_utc"]) - moment(before["at_utc"])).total_seconds()
    deltas = {key: int(after[key]) - int(before[key]) for key in ("xact_commit", "xact_rollback")}
    if before["stats_reset"] != after["stats_reset"] or elapsed <= 0 or min(deltas.values()) < 0:
        raise Unknown("database_counters_reset_or_clock_unproven")
    return {"elapsed_seconds": elapsed, **deltas, "transactions_per_second": sum(deltas.values()) / elapsed,
            "scope": "whole database, including collector reads; not OMS-only attribution"}


def bar_continuity(rows, symbols, stopped, started, now):
    if not stopped <= started <= now or (now - stopped).total_seconds() > 7200:
        raise Unknown("restart_window_unproven_or_unbounded")
    lo = stopped.replace(second=0, microsecond=0) - timedelta(minutes=2)
    hi = min(now.replace(second=0, microsecond=0), started.replace(second=0, microsecond=0) + timedelta(minutes=3))
    seen = set()
    for row in rows:
        key = row["symbol"], moment(row["bar_time"])
        if key in seen or key[0] not in symbols or key[1].second or key[1].microsecond:
            raise Unknown("duplicate_foreign_or_nonminute_bar")
        seen.add(key)
    scheduled = []
    minute = stopped.replace(second=0, microsecond=0)
    while minute < started:
        local = minute.astimezone(ET)
        if local.weekday() < 5 and 7 <= local.hour < 20:
            scheduled.append(minute)
        minute += timedelta(minutes=1)
    result = []
    for symbol in symbols:
        present = sorted(stamp for name, stamp in seen if name == symbol and lo <= stamp <= hi)
        missing = [stamp.isoformat() for stamp in scheduled if (symbol, stamp) not in seen]
        prior = [stamp for stamp in present if stamp + timedelta(minutes=1) <= stopped]
        post = [stamp for stamp in present if stamp >= started and stamp + timedelta(minutes=1) <= now]
        live = bool(prior and 0 <= (stopped - prior[-1] - timedelta(minutes=1)).total_seconds() <= 90)
        new_rows = [row for row in rows if row["symbol"] == symbol
                    and moment(row["bar_time"]) >= started
                    and moment(row["bar_time"]) + timedelta(minutes=1) <= now
                    and max(moment(row["created_at"]), moment(row["updated_at"])) >= started]
        result.append({"symbol": symbol, "pre_stop_live_bar": live,
            "prior_utc": prior[-1].isoformat() if prior else None,
            "first_post_utc": post[0].isoformat() if post else None,
            "newly_persisted_post_bar": bool(new_rows), "scheduled_missing_minutes_utc": missing,
            "scope": "persisted minutes; no assertion that missing minutes traded"})
    local_stop, local_start = stopped.astimezone(ET), started.astimezone(ET)
    after_hours_no_schedule = (local_stop.date() == local_start.date() and local_stop.hour >= 20
                              and local_start.hour >= 20 and local_stop.weekday() < 5)
    verdict = "N/A" if after_hours_no_schedule else "MEASURED" if symbols and all(
        row["pre_stop_live_bar"] and row["newly_persisted_post_bar"] for row in result) else "UNKNOWN"
    return {"verdict": verdict, "stop_started_utc": stopped.isoformat(), "new_pid_start_utc": started.isoformat(),
            "scheduled_minutes_utc": [stamp.isoformat() for stamp in scheduled], "per_symbol": result,
            "reason": "no scheduled v2 candles after 20:00 ET" if after_hours_no_schedule else None}


def collect_baseline():
    from project_mai_tai.settings import Settings

    started = time.monotonic()
    now = datetime.now(UTC)
    result = {"schema_version": 1, "captured_at_utc": now.isoformat(), "errors": {}, "services": {}, "process_flags": {}}
    def capture(name, action):
        try:
            result[name] = action()
        except Exception as exc:
            result["errors"][name] = str(exc) if isinstance(exc, Unknown) else type(exc).__name__
    for role in SERVICES:
        try:
            result["services"][role] = service_identity(role)
        except Exception as exc:
            result["errors"]["service:" + role] = type(exc).__name__
    for role in RESTARTED:
        try:
            pid = result["services"][role]["MainPID"]
            if role in {"oms", "schwab-1m-v2"}:
                result["process_flags"][role] = process_flags(pid)
            result["services"][role]["process_cwd"] = str(Path(f"/proc/{pid}/cwd").resolve(strict=True))
        except Exception as exc:
            result["errors"]["proc:" + role] = type(exc).__name__
    capture("checkout", lambda: {"sha": command(["runuser", "-u", "trader", "--", "git", "-C", str(REPO), "rev-parse", "HEAD"]).strip(),
        "clean": not command(["runuser", "-u", "trader", "--", "git", "-C", str(REPO), "status", "--porcelain"]).strip()})
    capture("log_cursors", lambda: {role: log_cursor(LOG_DIR / (role + ".log")) for role in RESTARTED})
    try:
        settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
        capture("redis", lambda: redis_capture(settings, now))
        capture("database", lambda: database_capture(settings))
    except Exception as exc:
        result["errors"]["settings"] = type(exc).__name__
    def scanner():
        with urlopen("http://127.0.0.1:8100/api/overview", timeout=15) as response:
            raw = response.read(MAX_REPLY + 1)
        if len(raw) > MAX_REPLY:
            raise Unknown("overview_unbounded")
        return scanner_receipt(json.loads(raw), datetime.now(UTC))
    capture("scanner", scanner)
    result["capture_elapsed_seconds"] = time.monotonic() - started
    result["capture_started_at_utc"] = result["captured_at_utc"]
    result["captured_at_utc"] = datetime.now(UTC).isoformat()
    return result


def journal_receipt(role, pid, start, now):
    unit = "project-mai-tai-" + role + ".service"
    raw = command(["journalctl", "--unit=" + unit, "--output=json", "--since=" + start.isoformat(),
                   "--until=" + now.isoformat(), "--no-pager"])
    errors, count = [], 0
    for line in raw.splitlines():
        event = json.loads(line)
        if str(event.get("_PID")) != str(pid):
            continue
        count += 1
        if (re.search(r"\bERROR\b|Traceback \(most recent call last\)", str(event.get("MESSAGE", "")))
                or str(event.get("PRIORITY")) in {"0", "1", "2", "3"}):
            errors.append({"cursor": event.get("__CURSOR"), "realtime_us": event.get("__REALTIME_TIMESTAMP")})
    return {"source": "journal:" + unit, "process_pid": pid, "from_utc": start.isoformat(),
            "through_utc": now.isoformat(), "entries_for_new_pid": count, "error_count": len(errors), "errors": errors,
            "scope": "new PID journal only; file logs independently checked even when journal has no entries"}


def paper_close_receipt(before, after, now, root=Path("/home/trader/after-hours")):
    roles = ("momentum-paper", "option-a-daily-guard")
    floor = now.astimezone(ET).replace(hour=9, minute=40, second=0, microsecond=0)
    for role in roles:
        old, new = before["services"][role], after["services"][role]
        if (old["MainPID"] <= 0 or old["ActiveState"] != "active" or old["NRestarts"] != 0
                or new["MainPID"] != 0 or new["ActiveState"] != "inactive" or new["SubState"] != "dead"
                or new["NRestarts"] != 0 or new["Result"] != "success" or new["ExecMainStatus"] != 0
                or new["InvocationID"] != old["InvocationID"]
                or new["ExecMainStartTimestampMonotonic"] != old["ExecMainStartTimestampMonotonic"]
                or moment(old["start_utc"]).astimezone(ET).date() != now.astimezone(ET).date()
                or not moment(old["start_utc"]) <= moment(before["captured_at_utc"]) <= moment(new["inactive_utc"])
                or not floor <= moment(new["inactive_utc"]) <= now):
            raise Unknown("paper_scheduled_close_shape_unproven")
    paths = list((Path(root) / now.astimezone(ET).date().isoformat() / "option-a-daily").glob("run-*/option-a-guard.jsonl"))
    if not paths or len(paths) > 16:
        raise Unknown("guard_audit_inventory_unbounded_or_empty")
    candidates = []
    for path in paths:
        if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
            raise Unknown("guard_audit_owner_mode_unproven")
        with path.open("rb") as stream:
            offset = max(0, path.stat().st_size - 262144)
            stream.seek(offset)
            raw = stream.read(262145)
        if len(raw) > 262144 or not raw.endswith(b"\n"):
            raise Unknown("guard_audit_tail_unbounded_or_partial")
        if offset:
            raw = raw.split(b"\n", 1)[1]
        for line in raw.splitlines():
            row = json.loads(line)
            if (row.get("action") == "stop_paper" and row.get("reason") == "scheduled_session_close"
                    and type(row.get("systemctl_rc")) is int and row["systemctl_rc"] == 0):
                stamp = moment(row["at_utc"])
                paper, guard = (after["services"][role] for role in roles)
                if floor <= moment(paper["inactive_utc"]) <= stamp < moment(guard["inactive_utc"]) + timedelta(seconds=1):
                    candidates.append({"path": str(path), "tail_sha256": hashlib.sha256(raw).hexdigest(),
                        "action": row["action"], "reason": row["reason"], "at_utc": row["at_utc"], "systemctl_rc": 0})
    if len(candidates) != 1:
        raise Unknown("guard_positive_close_receipt_not_unique")
    return candidates[0]


def stopping_time(before, role="schwab-1m-v2"):
    raw = command(["journalctl", "--unit=project-mai-tai-" + role + ".service", "--output=json",
                   "--since=" + before["captured_at_utc"], "--no-pager"])
    stamps = []
    for line in raw.splitlines():
        event = json.loads(line)
        if event.get("_PID") == "1" and str(event.get("MESSAGE", "")).startswith("Stopping "):
            stamps.append(datetime.fromtimestamp(int(event["__REALTIME_TIMESTAMP"]) / 1e6, UTC))
    if len(stamps) != 1:
        raise Unknown("v2_stop_boundary_not_unique")
    return stamps[0]


def evaluate(before, after, approved_sha, line_enabled=True):
    failures, unknown, observations = [], [], []
    unknown.extend("baseline:" + key for key in before.get("errors", {}))
    unknown.extend("after:" + key for key in after.get("errors", {}))
    if "checkout" not in after:
        unknown.append("checkout_proof_unreadable")
    elif after["checkout"].get("sha") != approved_sha or not after["checkout"].get("clean"):
        failures.append("checkout_not_approved_clean_sha")
    now = moment(after["captured_at_utc"])
    for role in SERVICES:
        old, new = before.get("services", {}).get(role), after.get("services", {}).get(role)
        if old is None or new is None:
            unknown.append("identity:" + role)
            continue
        if role in RESTARTED:
            if (new["MainPID"] <= 0 or new["MainPID"] == old["MainPID"] or new["ActiveState"] != "active"
                    or new["SubState"] != "running" or new["NRestarts"] != 0
                    or not new["InvocationID"] or new["InvocationID"] == old["InvocationID"]):
                failures.append("new_identity:" + role)
            if not new.get("start_utc"):
                unknown.append("start:" + role)
            elif not moment(before["captured_at_utc"]) <= moment(new["start_utc"]) <= now:
                failures.append("start_outside_attempt:" + role)
            elif (now - moment(new["start_utc"])).total_seconds() < 600:
                unknown.append("ten_minute_window_not_complete:" + role)
            if new.get("process_cwd") != str(REPO):
                unknown.append("process_source_cwd:" + role)
        elif role == "orb":
            try:
                from retire_orb import validate_receipt
                validate_receipt(after.get("orb_retirement", {}))
                if new["MainPID"] != 0 or new["ActiveState"] != "inactive":
                    failures.append("retirement_identity:orb")
            except Exception:
                failures.append("retirement_proof:orb")
        else:
            keys = ("MainPID", "NRestarts", "ActiveState", "SubState", "InvocationID", "ExecMainStartTimestampMonotonic")
            if any(old[key] != new[key] for key in keys):
                normal_paper_stop = (role in {"momentum-paper", "option-a-daily-guard"}
                    and now.astimezone(ET).time().hour >= 9 and now.astimezone(ET).strftime("%H%M") >= "0940"
                    and new["MainPID"] == 0 and new["ActiveState"] == "inactive"
                    and new["Result"] == "success" and new["NRestarts"] == old["NRestarts"]
                    and after.get("paper_close_receipt", {}).get("reason") == "scheduled_session_close")
                if normal_paper_stop:
                    observations.append("daily_guard_paper_stop_observed_not_identity_unchanged:" + role)
                else:
                    failures.append("untouched_identity_changed:" + role)
    for role in ("oms", "schwab-1m-v2"):
        old = before.get("process_flags", {}).get(role, {}).get("values", {})
        new = after.get("process_flags", {}).get(role, {}).get("values", {})
        if not old or not new:
            unknown.append("proc_flags:" + role)
            continue
        for key, expected in {LINE: str(line_enabled).lower(), HANDOFF: "false", **({GAP: "true"} if role == "schwab-1m-v2" else {})}.items():
            if new.get(key) != [expected]:
                failures.append("required_flag:" + role + ":" + key)
        for key in FLAG_KEYS - {GAP, LINE, HANDOFF}:
            if old.get(key) != new.get(key):
                failures.append("retained_flag_changed:" + role + ":" + key)
    if "redis" in before and "redis" in after:
        if before["redis"]["evicted_keys"] != after["redis"]["evicted_keys"]:
            failures.append("redis_evictions_changed")
        if after["redis"]["used_memory"] > 1_600_000_000:
            failures.append("redis_memory_bound")
    else:
        unknown.append("redis_proof_missing")
    for role, report in after.get("logs", {}).items():
        if report["traceback_or_error_count"]:
            failures.append("post_start_traceback_or_error:" + role)
        if role == "oms":
            if report["duplicate_pass_ids"] or report["malformed_shadow"]:
                failures.append("shadow_duplicate_or_malformed")
            if not report["complete_sync_ids"] or report["shadow_missing_for_complete_pass"]:
                unknown.append("shadow_coverage_incomplete")
            if report["sync_duration_ms"]["samples"] != len(report["complete_sync_ids"]):
                unknown.append("sync_duration_measurement_incomplete")
            if any(value is None for value in report["sync_outcomes"].values()):
                unknown.append("sync_outcome_measurement_incomplete")
            if any(value == "failed" for value in report["sync_outcomes"].values()):
                failures.append("new_oms_sync_pass_failed")
            if report["dropped_observations_max"]:
                unknown.append("shadow_observations_dropped")
            if any(row["process_pid"] != after["services"]["oms"]["MainPID"]
                   for rows in report["shadows"].values() for row in rows):
                failures.append("shadow_pid_not_new_oms")
    if set(after.get("logs", {})) != RESTARTED:
        unknown.append("post_start_log_population_incomplete")
    for role, receipt in after.get("journals", {}).items():
        if receipt["error_count"]:
            failures.append("post_start_journal_error:" + role)
    if set(after.get("journals", {})) != RESTARTED:
        unknown.append("post_start_journal_population_incomplete")
    if after.get("scanner", {}).get("verdict") != "PASS":
        unknown.append("scanner_not_proven_fresh_healthy")
    if after.get("bar_continuity", {}).get("verdict") not in {"N/A", "MEASURED"}:
        unknown.append("bar_continuity_unmeasured")
    if "transaction_rate" not in after:
        unknown.append("database_transaction_rate_unmeasured")
    return {"verdict": "FAIL" if failures else "UNKNOWN" if unknown else "PASS",
            "failures": sorted(set(failures)), "unknown": sorted(set(unknown)), "observations": observations,
            "scope": "post-install evidence only; no trading admission or scanner rule 9b certification"}


def receipt_exit_code(result):
    """Telemetry limits must not become new deployment gates or block re-pinning."""
    assessment = result.get("assessment", {})
    critical_failure = ("checkout_not_approved_clean_sha", "new_identity:", "start_outside_attempt:",
        "untouched_identity_changed:", "required_flag:", "retained_flag_changed:", "redis_evictions_changed",
        "redis_memory_bound", "post_start_traceback_or_error:", "post_start_journal_error:", "retirement_")
    if any(failure.startswith(critical_failure) for failure in assessment.get("failures", [])):
        return 1
    unknown_prefix = ("identity:", "start:", "proc_flags:", "process_source_cwd:", "redis_proof_missing", "checkout_proof_unreadable")
    if any(value.startswith(unknown_prefix) for value in assessment.get("unknown", [])):
        return 2
    for errors in (result.get("errors", {}), result.get("baseline_errors", {})):
        if any(key == "checkout" or key == "redis" or key == "settings" or key.startswith(("service:", "proc:")) for key in errors):
            return 2
    if result.get("error_type"):
        return 2
    return 0


def collect_after(before, approved_sha, restart_window=None, *, line_enabled=True, retirement=None):
    after = collect_baseline()
    after['orb_retirement'] = retirement
    now = moment(after["captured_at_utc"])
    after["logs"] = {}
    after["journals"] = {}
    for role in RESTARTED:
        try:
            ranges = log_ranges(before["log_cursors"][role])
            start = moment(after["services"][role]["start_utc"])
            records = log_records(ranges, start, now)
            report = summarize_logs(records)
            report["ranges"] = [{key: value for key, value in item.items() if key != "text"} for item in ranges]
            report["from_utc"], report["through_utc"] = start.isoformat(), now.isoformat()
            after["logs"][role] = report
        except Exception as exc:
            after["errors"]["logs:" + role] = str(exc) if isinstance(exc, Unknown) else type(exc).__name__
        try:
            after["journals"][role] = journal_receipt(role, after["services"][role]["MainPID"],
                moment(after["services"][role]["start_utc"]), now)
        except Exception as exc:
            after["errors"]["journal:" + role] = str(exc) if isinstance(exc, Unknown) else type(exc).__name__
    if any(before.get("services", {}).get(role, {}).get("MainPID") != after.get("services", {}).get(role, {}).get("MainPID")
           for role in ("momentum-paper", "option-a-daily-guard")):
        try:
            after["paper_close_receipt"] = paper_close_receipt(before, after, now)
        except Exception as exc:
            after["errors"]["paper_close_receipt"] = str(exc) if isinstance(exc, Unknown) else type(exc).__name__
    try:
        after["transaction_rate"] = transaction_rate(before["database"]["stats"], after["database"]["stats"])
    except Exception as exc:
        after["errors"]["transaction_rate"] = type(exc).__name__
    try:
        from project_mai_tai.settings import Settings

        stopped = moment(restart_window["stop_started_utc"]) if restart_window else stopping_time(before)
        started = moment(after["services"]["schwab-1m-v2"]["start_utc"])
        symbols = before["redis"]["v2_watchlist"]["symbols"]
        if not moment(before["captured_at_utc"]) <= stopped <= started or (now - stopped).total_seconds() > 7200:
            raise Unknown("restart_window_outside_attempt")
        db = database_capture(Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env"), symbols=symbols,
            low=stopped - timedelta(minutes=2), high=min(now, started + timedelta(minutes=4)))
        grade_started = time.perf_counter()
        after["bar_continuity"] = bar_continuity(db["bars"], symbols, stopped, started, now)
        after["bar_continuity"]["grading_elapsed_seconds"] = time.perf_counter() - grade_started
        after["bar_continuity"]["query_elapsed_seconds"] = db["bar_query_elapsed_seconds"]
        after["bar_continuity"]["gapline_repair_cost"] = "UNMEASURED; collector does not execute or time strategy repair"
        after["bar_continuity"]["boundary_source"] = "runner_receipt" if restart_window else "systemd_journal"
        after["bar_continuity"]["watchlist_at_baseline"] = before["redis"]["v2_watchlist"]
    except Exception as exc:
        after["errors"]["bar_continuity"] = str(exc) if isinstance(exc, Unknown) else type(exc).__name__
    after["assessment"] = evaluate(before, after, approved_sha, line_enabled)
    after["baseline_errors"] = before.get("errors", {})
    after["source_binding"] = {"approved_checkout_sha": approved_sha,
        "scope": "clean current checkout plus new identities and process cwd; not an in-memory bytecode hash"}
    return after


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    baseline = sub.add_parser("baseline")
    baseline.add_argument("--output", type=Path, required=True)
    after = sub.add_parser("after")
    after.add_argument("--baseline", type=Path, required=True)
    after.add_argument("--approved-sha", required=True)
    after.add_argument("--restart-window", type=Path)
    after.add_argument("--line-enabled", choices=('true', 'false'), required=True)
    after.add_argument("--retirement", type=Path, required=True)
    after.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.mode == "after" and not re.fullmatch(r"[0-9a-f]{40}", args.approved_sha):
        parser.error("--approved-sha requires a full SHA")
    try:
        if args.mode == "baseline":
            result = collect_baseline()
            verdict = "UNKNOWN" if result["errors"] else "PASS"
            result["assessment"] = {"verdict": verdict, "scope": "baseline capture only; not a gate pass"}
        else:
            before = json.loads(bounded_file(args.baseline, 2_000_000))
            window = json.loads(bounded_file(args.restart_window, 10000)) if args.restart_window else None
            result = collect_after(before, args.approved_sha, window, line_enabled=args.line_enabled == 'true',
                                   retirement=json.loads(bounded_file(args.retirement, 300000)))
            verdict = result["assessment"]["verdict"]
    except Exception as exc:
        result = {"schema_version": 1, "assessment": {"verdict": "UNKNOWN"}, "error_type": type(exc).__name__}
        verdict = "UNKNOWN"
    rc = receipt_exit_code(result)
    result["receipt_exit_code"] = rc
    result["exit_policy"] = "telemetry, bar/tape coverage and scanner-validation limits are report-only; never new admission gates"
    raw = json.dumps(result, sort_keys=True, indent=2) + "\n"
    # Exclusive evidence creation prevents an earlier attempt from being overwritten.
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(raw)
    print(json.dumps({"verdict": verdict, "rc": rc, "output": str(args.output), "sha256": hashlib.sha256(raw.encode()).hexdigest()}))
    return rc


if __name__ == "__main__":
    sys.exit(main())
