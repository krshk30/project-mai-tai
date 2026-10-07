"""New-PID proof and bounded log ranges; UNKNOWN is never a successful install."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from daily import system_time
from release_policy import ATR, CHANGED, ET, NEW_ENV, NUMERIC_ARTIFACT, PM, RETRY_ENABLED, RETRY_MAX, Unknown, digest, moment, need

MAX = 3_000_000


def read_range(path, offset, end):
    need(0 <= offset <= end and end - offset <= MAX, "log range unbounded/truncated")
    with path.open("rb") as stream:
        stream.seek(offset)
        raw = stream.read(end - offset)
    need(len(raw) == end - offset, "log range changed during proof")
    return raw, dict(path=str(path), offset=offset, end=end, sha256=digest(raw))


def logs(baseline):
    result = {}
    for name, old in baseline.items():
        path = Path(old["path"])
        stat = path.stat()
        same = (stat.st_dev, stat.st_ino) == (old["device"], old["inode"])
        ranges = []
        if same and stat.st_size >= old["offset"]:
            raw, receipt = read_range(path, old["offset"], stat.st_size)
            ranges.append(receipt)
        else:
            # Rename rotation follows original inode; copytruncate needs the complete copy.
            candidates = []
            for copy in path.parent.glob(path.name + "*"):
                if copy == path or not copy.is_file() or copy.suffix == ".gz":
                    continue
                info = copy.stat()
                if info.st_size >= old["offset"] and ((info.st_dev, info.st_ino) == (old["device"], old["inode"])
                                                     or same):
                    candidates.append(copy)
            need(len(candidates) == 1, "old log source lost/ambiguous; no silent rotation waiver")
            copy = candidates[0]
            raw, receipt = read_range(copy, old["offset"], copy.stat().st_size)
            ranges.append(receipt)
            current, receipt = read_range(path, 0, stat.st_size)
            raw += current
            ranges.append(receipt)
        need(len(raw) <= MAX, "combined log evidence exceeds bound")
        result[name] = dict(ranges=ranges, text=raw.decode("utf-8"))
    return result


def process_flags(effects, owners=CHANGED):
    from attended import REPO
    current = effects.fleet()
    values = {}
    for name in owners:
        need(current[name] == effects.started[name], "PID moved before process flag proof")
        path = Path(f"/proc/{current[name]['MainPID']}/environ")
        need(path.stat().st_size <= MAX, "process environment bound")
        with path.open("rb") as stream:
            raw = stream.read(MAX + 1)
        need(len(raw) <= MAX, "process environment overflow")
        pairs = [piece.split(b"=", 1) for piece in raw.split(b"\0") if b"=" in piece]
        checks = {key: "true" for key in PM} if name in {"oms", "schwab-1m-v2"} else {}
        if name in {"oms", "orb-schwab"}:
            checks.update({ATR: "true", "MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED": "true"})
        if name == "orb-schwab":
            checks["MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED"] = "false"
        if name in {"oms", "schwab-1m-v2"}:
            checks.update({key: "true" for key in NEW_ENV})
        values[name] = {}
        if name in {"oms", "schwab-1m-v2"}:
            from retry_zero_readonly import values as retry_values
            values[name].update(retry_values(raw))
        for key, expected in checks.items():
            found = [(candidate.decode(), value.decode()) for candidate, value in pairs
                     if candidate.decode().upper() == key]
            need(found == [(key, expected)], "explicit new-PID environment missing/alias/drift: " + name + ":" + key)
            values[name][key] = found[0][1]
    need(effects.fleet() == current, "fleet moved during proc proof")
    # Validate actual catalog denominator before running the isolated checker.
    from catalog_policy import validate_catalogs
    validate_catalogs((REPO / "ops/health/expected_flags.json").read_bytes(),
                      (effects.job / NUMERIC_ARTIFACT).read_bytes())
    from retry_zero_readonly import catalog
    catalog((effects.job / NUMERIC_ARTIFACT).read_bytes(), (REPO / "ops/health/expected_numeric.json").read_bytes())
    return values


def grade_logs(found, started, owners=CHANGED):
    receipts = {}
    for name in owners:
        if name == "control":
            receipts[name] = control_logs(found[name], started[name])
            continue
        lower = system_time(started[name]["ExecMainStartTimestamp"])
        records, stamp = [], None
        for line in found[name]["text"].splitlines():
            match = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})", line)
            if match:
                stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
            need(stamp is not None or not line.strip(), "untimestamped new-log prefix")
            if stamp is not None and stamp >= lower:
                records.append(line)
        if not records:
            raise Unknown("new-PID log population missing: " + name)
        from linesrc_disposition import grade
        disposition = grade(name, records)
        receipts[name] = dict(lines=records, ranges=found[name]["ranges"], start_utc=lower.isoformat(),
                              errors=disposition)
    v2 = receipts["schwab-1m-v2"]["lines"]
    if not any("[V2-BOOT-HOLD]" in line for line in v2):
        raise Unknown("new-PID BOOT-HOLD unobserved")
    if not any("[V2-LINE-RESTORE]" in line and "entry_allowed=" in line for line in v2):
        receipts['schwab-1m-v2']['restoration'] = overnight_hold(v2, started['schwab-1m-v2'], datetime.now(timezone.utc))
    sync = [re.search(r"live:orb: ok=(\d+) failed=(\d+) consecutive_now=(\d+)", line)
            for line in receipts["oms"]["lines"] if "[BROKER-SYNC-CENSUS]" in line]
    if not sync:
        raise Unknown("new OMS Webull sync unmeasured")
    need(all(match and int(match[2]) == 0 for match in sync), "new OMS Webull sync failed/malformed")
    if not any(int(match[1]) > 0 for match in sync):
        raise Unknown("new OMS Webull sync ok0")
    return receipts


def overnight_hold(lines, state, now):
    """No scheduled bars after20: report a proven hold, never restoration PASS."""
    from release_policy import DAY
    start = system_time(state['ExecMainStartTimestamp']).astimezone(ET)
    local = now.astimezone(ET)
    if start.date().isoformat() != DAY or local.date().isoformat() != DAY or start.hour < 20 or local.hour < 20:
        raise Unknown('Restoration per-symbol admission evidence missing')
    holds = [line for line in lines if '[V2-BOOT-HOLD] HELD' in line]
    restores = [line for line in lines if '[V2-BOOT-RESTORE]' in line]
    if not holds or not restores or any('[V2-BOOT-HOLD] RELEASED' in line for line in lines):
        raise Unknown('overnight hold not proven')
    value = restores[-1]
    stamp = re.match(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})', value)
    if not stamp or not 0 <= (now - datetime.strptime(stamp[1], '%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=timezone.utc)).total_seconds() <= 120:
        raise Unknown('overnight hold reading stale')
    match = re.search(r'restoration_complete=0 evaluated=(\d+) confirmed=(\d+) rest_warmed=0 timeout_released=0 warmup_pending=(\d+) warmup_pending_symbols=([^ ]+) reason=rest_warmup_incomplete;', value)
    if not match or not (int(match[1]) == int(match[2]) == int(match[3]) > 0):
        raise Unknown('overnight pending population incomplete')
    names = match[4].split(',')
    if len(names) != int(match[3]) or len(set(names)) != len(names):
        raise Unknown('overnight pending symbols incomplete')
    return dict(verdict='HELD_EXPECTED_NO_SCHEDULED_BARS', population=int(match[3]), symbols=names,
                restoration='UNMEASURED until next scheduled bars', entry_allowed=False,
                source_line=value, next_read='2026-10-07 07:00 ET boot-hold/restoration and scanner verification')


def control_logs(found, state):
    lines = found["text"].splitlines()
    starts = [(i, match[1]) for i, line in enumerate(lines)
              if (match := re.fullmatch(r"INFO:\s+Started server process \[(\d+)\]", line))]
    need(len(starts) == 1 and starts[0][1] == str(state["MainPID"]), "control log lacks unique exact new-PID marker")
    new = lines[starts[0][0]:]
    need(not any("Traceback" in line or "ERROR" in line or "SCHWAB-TOKEN-DEAD" in line
                 or "DEGRADED" in line or "SCHWAB-TOKEN-REFRESHER-IDLE" in line for line in new), "new control/token failure log")
    need(sum("Application startup complete." in line for line in new) == 1
         and any(re.fullmatch(r"INFO:\s+Uvicorn running on http://(?:127\.0\.0\.1|0\.0\.0\.0):8100 \(Press CTRL\+C to quit\)", line)
                 for line in new), "control startup/8100 binding missing")
    return dict(lines=new, ranges=found["ranges"], pid=state["MainPID"])


def collect(effects, baseline, started, owners=CHANGED):
    flags = process_flags(effects, owners)
    deadline = time.monotonic() + 180
    while True:
        effects.redis()
        found = logs(baseline)
        try:
            graded = grade_logs(found, started, owners)
            fresh = effects.flat()
            stamps = {row["account"]: row["updated_at"] for row in fresh["account_stamps"]}
            need(set(stamps) == {"live:schwab_1m_v2", "live:orb"}, "post-start account stamp census incomplete")
            if any(moment(stamp) < effects.start_returned["oms"] for stamp in stamps.values()):
                raise Unknown("fresh account book after new OMS return unmeasured")
            break
        except Unknown:
            if time.monotonic() >= deadline:
                raise
            time.sleep(5)
    effects.sql(effects.install_started)
    # The official collector grades REST/bar holes from the old snapshot and exact restart set.
    # It is NOT the preopen gate, and it is not invoked with a fabricated future date.
    from attended import PY, REPO
    from closeout import install_record
    record = install_record(effects, owners)
    output = effects.attempt / "restart-evidence-current.md"
    args = [PY, effects.job / "restart_report.py", "report", "--snapshot", effects.attempt / "before-restart.json",
            "--install-record", record, "--no-schema-change", "--expected-alembic-head", "20261005_0022",
            "--output", output]
    for name in owners:
        args += ["--restarted", name]
    for name in ("oms", "schwab-1m-v2"):
        for key in PM:
            args += ["--expect-flag", name + ":" + key + "=true"]
    for name in ("oms", "orb-schwab"):
        if name in owners:
            args += ["--expect-flag", name + ":" + ATR + "=true"]
    for name in ("oms", "schwab-1m-v2"):
        for key in NEW_ENV:
            args += ["--expect-flag", name + ":" + key + "=true"]
        args += ["--expect-flag", name + ":" + RETRY_ENABLED + "=true",
                 "--expect-flag", name + ":" + RETRY_MAX + "=0"]
    result = effects.command(args, check=False, timeout=240)
    need(result.returncode == 0, "official restart/bar evidence FAIL or UNKNOWN; no N/A guessed")
    raw = output.read_bytes()
    need(b"Final call: PASS;" in raw or b"Final call: EXPECTED BY DESIGN;" in raw or b"Final call: ACCEPTED_OPEN_LINESRC1;" in raw, "official report final unproven")
    return dict(process_flags=flags, logs=graded, official_report=str(output), official_report_sha256=digest(raw),
                date_et=effects.now().astimezone(ET).date().isoformat(), future_live_delivery="UNMEASURED")
