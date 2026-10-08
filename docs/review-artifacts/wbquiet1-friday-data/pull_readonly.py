"""Bounded SSH-stdin extraction. No API, DB write, or remote file creation.

Only structured application evidence is retained; SDK exception context is used
to recover the failed request identity and is never serialized.
"""
from collections import deque
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from zoneinfo import ZoneInfo

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
LOW = datetime(2026, 10, 2, tzinfo=ET).astimezone(UTC)
STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[,.](\d+))?")
TAGS = ("WEBULL-ENDPOINT-CALLS", "BROKER-SYNC-CENSUS", "BROKER-SYNC-UNREADABLE",
        "OMS-BROKER-SYNC-PASS", "POSITION-BOOK", "VIRTUAL-CLEAR", "VIRTUAL-RESTORE",
        "OMS-CANCEL", "OMS-RESERVE", "OMS-OCO-EXIT-MISS", "OMS-ENTRY-OWNERSHIP",
        "OMS-FLAT", "SETTLE-LAG", "WBQUIET-SHADOW", "WBQUIET-READER",
        "BARE-FILL", "PROTECT-ATTACHED")
MAX_FILE = 64_000_000
MAX_TOTAL = 256_000_000
MAX_EVENTS = 150000
MAX_OUTPUT = 80_000_000


def sql(query):
    result = subprocess.run(["sudo", "-n", "-u", "postgres", "psql", "-X", "-At", "-d", "project_mai_tai", "-c",
        "BEGIN READ ONLY; SET LOCAL statement_timeout='10s'; SET LOCAL lock_timeout='500ms'; " + query + "; COMMIT"],
        capture_output=True, text=True, timeout=15, check=True, cwd="/tmp")
    if len(result.stdout.encode()) > 2_000_000:
        raise RuntimeError("SQL reply bound exhausted")
    return json.loads("\n".join(line for line in result.stdout.splitlines() if line not in {"BEGIN", "SET", "COMMIT"}))


def pull():
    started = time.monotonic()
    high = datetime.now(UTC)
    sources, events, failures, total = [], [], [], 0
    paths = sorted(Path("/var/log/project-mai-tai").glob("oms.log*"))
    selected = []
    for path in paths:
        suffix = path.name.removesuffix(".gz").rsplit("-", 1)[-1]
        if path.name == "oms.log" or (re.fullmatch(r"\d{8}", suffix)
                and LOW.date() <= datetime.strptime(suffix, "%Y%m%d").date() <= high.date() + timedelta(days=1)):
            selected.append(path)
    if len(selected) > 16:
        raise RuntimeError("log source count bound exhausted")
    expected_rotations = []
    rotation_day = LOW.date() + timedelta(days=1)
    while rotation_day <= high.date():
        expected_rotations.append("oms.log-" + rotation_day.strftime("%Y%m%d"))
        rotation_day += timedelta(days=1)
    present = {path.name.removesuffix(".gz") for path in selected}
    missing_rotations = [name for name in expected_rotations if name not in present]
    content_hashes = set()
    for path in selected:
        digest = hashlib.sha256()
        file_bytes, file_events, file_failures = 0, [], []
        context, current = deque(maxlen=90), None
        first_stamp, last_stamp = None, None
        stat_before = path.stat()
        line_number = 0
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rb") as stream:
            while raw := stream.readline(262145):
                if len(raw) > 262144:
                    raise RuntimeError("log line bound exhausted")
                file_bytes += len(raw)
                total += len(raw)
                if file_bytes > MAX_FILE or total > MAX_TOTAL:
                    raise RuntimeError("uncompressed log byte bound exhausted")
                digest.update(raw)
                text = raw.decode("utf-8", errors="strict").rstrip()
                line_number += 1
                match = STAMP.match(text)
                if match:
                    current = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                    if match[2]:
                        current += timedelta(microseconds=int(match[2].ljust(6, "0")[:6]))
                    first_stamp = first_stamp or current
                    last_stamp = current
                context.append(text)
                minute = re.search(r"\[WEBULL-ENDPOINT-CALLS\] minute=(\S+)", text)
                counter_at = datetime.fromisoformat(minute[1].replace("Z", "+00:00")) if minute else None
                if current is None or not (LOW <= current <= high or counter_at and LOW <= counter_at <= high):
                    continue
                if any("[" + tag in text for tag in TAGS) or "0e40052d917c" in text and match:
                    if match:  # Only timestamped application lines, never headers/exception bodies.
                        file_events.append({"at": current.isoformat(), "path": str(path), "line_number": line_number, "line": text})
                if "ServerException:HTTP Status: 417, Code: ORDER_CAN_NOT_BE_CANCEL" in text:
                    joined = "\n".join(context).replace('\\"', '"')
                    coids = re.findall(r'"client_order_id"\s*:\s*"([^"\n]+)"', joined)
                    file_failures.append({"at": current.isoformat(), "path": str(path), "line_number": line_number,
                        "client_order_id": coids[-1] if coids else None,
                        "request_id": text.rsplit("RequestID:", 1)[-1].strip()})
        checksum = digest.hexdigest()
        duplicate = checksum in content_hashes
        content_hashes.add(checksum)
        sources.append({"path": str(path), "uncompressed_sha256": checksum, "uncompressed_bytes": file_bytes,
            "device": stat_before.st_dev, "inode": stat_before.st_ino, "captured_file_size": stat_before.st_size,
            "retained_lines": len(file_events), "duplicate_full_content_omitted": duplicate,
            "first_stamp_utc": first_stamp.isoformat() if first_stamp else None,
            "last_stamp_utc": last_stamp.isoformat() if last_stamp else None,
            "live_file_right_censored": path.name == "oms.log"})
        if not duplicate:
            events.extend(file_events)
            failures.extend(file_failures)
        if len(events) > MAX_EVENTS:
            raise RuntimeError("retained event count bound exhausted")
    end = high.isoformat()
    queries = {
        "fills": "SELECT coalesce(json_agg(t),'[]'::json) FROM (SELECT f.symbol,f.side,f.quantity,f.filled_at,a.name AS account "
            "FROM fills f JOIN broker_accounts a ON a.id=f.broker_account_id WHERE a.provider='webull' "
            f"AND f.filled_at>='{(LOW-timedelta(minutes=10)).isoformat()}' AND f.filled_at<='{end}' ORDER BY f.filled_at LIMIT 10001) t",
        "terminal_proofs": "SELECT coalesce(json_agg(t),'[]'::json) FROM (SELECT id,payload->>'client_order_id' AS client_order_id, "
            "payload->>'acquired_at' AS acquired_at,payload->>'source' AS source,payload->'execution' AS execution "
            "FROM dashboard_snapshots WHERE snapshot_type='webull_terminal_read_proof' "
            f"AND payload->>'acquired_at'>='{LOW.isoformat()}' AND payload->>'acquired_at'<='{end}' "
            "ORDER BY payload->>'acquired_at' LIMIT 2001) t",
        "accounts": "SELECT coalesce(json_agg(t),'[]'::json) FROM (SELECT name,provider,is_active FROM broker_accounts "
            "WHERE provider='webull' ORDER BY name LIMIT 33) t",
    }
    database, errors = {}, {}
    for name, query in queries.items():
        try:
            rows = sql(query)
            bound = {"fills": 10000, "terminal_proofs": 2000, "accounts": 32}[name]
            if len(rows) > bound:
                raise RuntimeError("SQL row bound exhausted")
            database[name] = rows
        except Exception as exc:
            errors[name] = type(exc).__name__
    result = {"schema_version": 1, "as_of_utc": high.isoformat(), "window_start_utc": LOW.isoformat(),
        "source_files": sources, "log_events": events, "cancel_417": failures, **database, "queries": queries,
        "collection_errors": errors, "elapsed_seconds": time.monotonic() - started,
        "collection_finished_at_utc": datetime.now(UTC).isoformat(),
        "missing_rotated_file_intervals": missing_rotations,
        "expected_rotated_file_suffixes": expected_rotations,
        "day_list_observations": [], "day_list_coverage": "UNMEASURED: response bodies at decision time not retained",
        "scope": "bounded read-only historical evidence; no broker API called"}
    raw = json.dumps(result, separators=(",", ":"), sort_keys=True)
    if len(raw.encode()) > MAX_OUTPUT:
        raise RuntimeError("serialized output bound exhausted")
    return raw


if __name__ == "__main__":
    print(pull())
