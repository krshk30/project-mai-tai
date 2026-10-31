"""C1 14:46 per-run supplied-data scope; never native market completeness.

No arbitrary time-gap threshold. Missing/zero-bar coverage is conservative:
only a no-print interior minute immediately between independently covered
positive-volume minutes may bridge; no leading/trailing/whole-day inference.
"""

from bisect import bisect_left
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

from atrsimlive1_collect import digest


ET = ZoneInfo("America/New_York")
MINUTE = 60000000000
MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 65536
STAMP = re.compile(r"^(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:\d\d))\s")
STATS = re.compile(r"\[CAPTURE\] rows_written=(\d+) buffered=(\d+) dropped=(\d+)\s*$")
FAULTS = ("[CAPTURE] starting", "[CAPTURE] cancelled", "[CAPTURE] loop iteration failed",
          "flush failed", "flush_failed")


def capture_log(path, *, asof):
    """Read one bounded stable regular-file snapshot, never log raw content.

Require actual aware timestamp prefixes. Untimestamped CAPTURE records cannot
be assigned to TODAY by mtime or caller assertion. No rotation/retry fallback.
"""
    reference = dict(artifact=str(path), status="UNKNOWN", issues=[], native_market_complete=False)
    try:
        path = Path(path)
        if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("CAPTURE_LOG_SYMLINK_OR_RELATIVE")
        records, total, checksum = [], 0, hashlib.sha256()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_LOG_BYTES:
                raise ValueError("CAPTURE_LOG_CAP_OR_NOT_REGULAR")
            while True:
                raw = stream.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                total += len(raw)
                checksum.update(raw)
                if total > MAX_LOG_BYTES or len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
                    raise ValueError("CAPTURE_LOG_TRUNCATED_OR_LINE_CAP")
                line = raw.decode("utf-8", errors="strict").rstrip("\n")
                if "[CAPTURE]" not in line and not any(fault in line for fault in FAULTS):
                    continue
                stamp = STAMP.match(line)
                if stamp is None:
                    raise ValueError("CAPTURE_LOG_TIMESTAMP_DAY_UNKNOWN")
                clock = datetime.fromisoformat(stamp[1].replace(",", ".").replace("Z", "+00:00"))
                if clock > asof:
                    continue  # Never use a future log line as a snapshot witness.
                if clock.astimezone(ET).date() != asof.astimezone(ET).date():
                    continue
                match = STATS.search(line)
                if match is not None:
                    records.append((clock, tuple(int(value) for value in match.groups())))
                elif any(fault in line for fault in FAULTS):
                    records.append((clock, None))
            after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or total != before.st_size):
            raise ValueError("CAPTURE_LOG_CHANGED_OR_ROTATED")
        reference.update(sha256=checksum.hexdigest(), bytes=total,
                         device=before.st_dev, inode=before.st_ino)
        if any(a[0] > b[0] for a, b in zip(records, records[1:])):
            raise ValueError("CAPTURE_LOG_NONMONOTONE_CLOCK")
        stats = [(clock, values) for clock, values in records if values is not None]
        if len(stats) < 2:
            raise ValueError("CAPTURE_LOG_DAY_STATS_INCOMPLETE")
        if any(values[2] > 0 for _, values in stats):
            raise ValueError("CAPTURE_LOG_DAY_DROPPED_POSITIVE")
        first, last = stats[0][0], stats[-1][0]
        if any(values is None and first <= clock <= last for clock, values in records):
            raise ValueError("CAPTURE_LOG_RESTART_OR_FAILURE_IN_WINDOW")
        if any(left[1][0] > right[1][0] for left, right in zip(stats, stats[1:])):
            raise ValueError("CAPTURE_LOG_COUNTER_RESET")
        reference.update(status="AVAILABLE", first_stats_at=first.isoformat(),
                         last_stats_at=last.isoformat(), stats_count=len(stats),
                         dropped=0, actual_timestamp_source="aware prefix on each retained log line",
                         asof=asof.isoformat(), ruling="C1_20261028_1446_DATA_DERIVED")
    except (OSError, ValueError, UnicodeError) as error:
        # Only our literal internal codes, never raw path/SDK/account text.
        code = str(error) if type(error) is ValueError and str(error).startswith("CAPTURE_LOG_") else "CAPTURE_LOG_UNREADABLE"
        reference["issues"] = [code]
    return reference


def derive(*, symbol, day, bars, trades, read_proof, log, asof_ns, nanos):
    """Return compatible scoped windows plus exact gap/census evidence."""
    report = dict(symbol=symbol, trading_day=day, method="C1_20261028_1446_DATA_DERIVED",
                  native_market_complete=False, capture_log=log, read_proof=read_proof,
                  issues=[], minutes=[], windows=[])
    schwab_reference = read_proof.get("bar_provider") == "schwab"
    log_complete = log.get("status") == "AVAILABLE"
    if log_complete and (log.get("ruling") != "C1_20261028_1446_DATA_DERIVED"
                         or nanos(log["asof"]) != asof_ns or log.get("dropped") != 0):
        log_complete = False
    known_log_fault = any(code in {"CAPTURE_LOG_DAY_DROPPED_POSITIVE",
        "CAPTURE_LOG_RESTART_OR_FAILURE_IN_WINDOW", "CAPTURE_LOG_COUNTER_RESET"}
        for code in log.get("issues", []))
    known_log_fault = known_log_fault or (type(log.get("dropped")) is int and log["dropped"] > 0)
    if not log_complete:
        report["secondary_issues"] = ["SECONDARY_CAPTURE_LOG_UNKNOWN_NONBLOCKING", *log.get("issues", [])]
    if known_log_fault:
        report["issues"].append("UNKNOWN_POSITIVE_CAPTURE_LOG_FAILURE")
    if (read_proof.get("provider") != "massive" or read_proof.get("symbol") != symbol
            or read_proof.get("full_read") is not True or read_proof.get("truncated") is not False
            or read_proof.get("rows_sha256") != digest(trades)
            or read_proof.get("row_count") != len(trades)
            or read_proof.get("bar_table") != ("strategy_bar_history" if schwab_reference else "market_capture_bars")
            or read_proof.get("bars_full_read") is not True
            or read_proof.get("bars_truncated") is not False
            or read_proof.get("bars_sha256") != digest(bars)
            or read_proof.get("bar_count") != len(bars)):
        report["issues"] = ["UNKNOWN_SAME_SNAPSHOT_FULL_READ_CERTIFICATE"]
        return [], report
    left, right = nanos(read_proof["start"]), min(nanos(read_proof["end"]), asof_ns)
    # C1 15:34: unreadable secondary log is UNKNOWN, not a completeness veto.
    # Known drops/resets remain evidence faults; no chmod or fabricated zero.
    reference_clock = "bar_time" if schwab_reference else "event_ts"
    report["reference_bar_source"] = "schwab strategy_bar_history" if schwab_reference else "legacy Massive bar contract"
    by_minute, print_counts = {}, {}
    for row in bars:
        start = nanos(row[reference_clock])
        identity_valid = ((row.get("symbol") == symbol and nanos(row["created_at"]) <= asof_ns)
                          if schwab_reference else
                          (row.get("provider") == "massive" and row.get("symbol") == symbol
                           and row.get("interval_secs") == 60 and nanos(row["received_at"]) <= asof_ns))
        if (not identity_valid or start % MINUTE or start in by_minute
                or type(row.get("volume")) is not int or row["volume"] < 0):
            report["issues"] = ["UNKNOWN_CAPTURE_BAR_IDENTITY_VOLUME_OR_DUPLICATE"]
            return [], report
        by_minute[start] = row
    for row in trades:
        clock = nanos(row["event_ts"])
        if row.get("provider") != "massive" or row.get("symbol") != symbol or nanos(row["received_at"]) > asof_ns:
            report["issues"] = ["UNKNOWN_CAPTURE_TRADE_IDENTITY_OR_ASOF"]
            return [], report
        minute = clock // MINUTE * MINUTE
        print_counts[minute] = print_counts.get(minute, 0) + 1
    data_matches = {minute for minute, row in by_minute.items()
                   if row["volume"] > 0 and print_counts.get(minute, 0) > 0
                   and left <= minute and minute + MINUTE <= right}
    independent = set() if known_log_fault else data_matches
    covered = set(independent)
    for minute in range(left // MINUTE * MINUTE, right // MINUTE * MINUTE, MINUTE):
        bar, prints = by_minute.get(minute), print_counts.get(minute, 0)
        if minute in independent:
            status = "COVERED_POSITIVE_BAR_WITH_PRINT"
        elif minute in data_matches:
            status = "UNKNOWN_DATA_MATCH_KNOWN_LOG_FAILURE"
        elif bar is not None and bar["volume"] > 0 and prints == 0:
            status = "UNKNOWN_POSITIVE_BAR_ZERO_PRINTS"
        elif ((bar is None or bar["volume"] == 0) and prints == 0
              and minute - MINUTE in independent and minute + MINUTE in independent):
            status = "COVERED_INTERIOR_NO_TRADING_BETWEEN_INDEPENDENT_MINUTES"
            covered.add(minute)
        else:
            status = "UNKNOWN_UNPROVEN_MINUTE_OR_LOG_BOUNDARY"
        report["minutes"].append(dict(start_ns=minute, capture_bar_id=None if bar is None else bar["id"],
                                      bar_volume=None if bar is None else bar["volume"],
                                      print_count=prints, status=status))
    ordered = sorted(trades, key=lambda row: (nanos(row["event_ts"]), row["id"]))
    clocks = [nanos(row["event_ts"]) for row in ordered]
    intervals = []
    for minute in sorted(covered):
        if intervals and intervals[-1][1] == minute:
            intervals[-1] = intervals[-1][0], minute + MINUTE
        else:
            intervals.append((minute, minute + MINUTE))
    windows = []
    for start, end in intervals:
        rows = ordered[bisect_left(clocks, start):bisect_left(clocks, end)]
        if not rows:
            continue
        event_clocks = [nanos(row["event_ts"]) for row in rows]
        metrics = dict(start_ns=start, end_ns=end, row_count=len(rows), rows_sha256=digest(rows),
            first_print_id=rows[0]["id"], last_print_id=rows[-1]["id"],
            first_print_ns=nanos(rows[0]["event_ts"]), last_print_ns=nanos(rows[-1]["event_ts"]),
            max_interprint_gap_ns=max((b - a for a, b in zip(event_clocks, event_clocks[1:])), default=0),
            full_read=True, truncated=False)
        begin = datetime.fromtimestamp(start // 1000000000, timezone.utc).astimezone(ET)
        finish = datetime.fromtimestamp(end // 1000000000, timezone.utc).astimezone(ET)
        note = (f"coverage: C1 supplied watched window {begin:%H:%M}–{finish:%H:%M} ET; "
                "outside-window and native/market completeness UNKNOWN")
        windows.append((start, end, rows, note, metrics))
        report["windows"].append(metrics)
    if not windows:
        report["issues"].append("UNKNOWN_NO_DATA_DERIVED_COVERED_INTERVAL")
    return windows, report
