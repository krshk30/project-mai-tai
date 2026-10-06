"""New-PID proof and bounded log ranges; UNKNOWN is never a successful install."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from daily import system_time
from release_policy import CHANGED, ET, NEW_ENV, NUMERIC_ARTIFACT, PM, RETRY_ENABLED, RETRY_MAX, Unknown, digest, moment, need

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


def process_flags(effects):
    from attended import REPO
    current = effects.fleet()
    values = {}
    for name in CHANGED:
        need(current[name] == effects.started[name], "PID moved before process flag proof")
        path = Path(f"/proc/{current[name]['MainPID']}/environ")
        need(path.stat().st_size <= MAX, "process environment bound")
        with path.open("rb") as stream:
            raw = stream.read(MAX + 1)
        need(len(raw) <= MAX, "process environment overflow")
        pairs = [piece.split(b"=", 1) for piece in raw.split(b"\0") if b"=" in piece]
        checks = {key: "true" for key in PM} if name in {"oms", "schwab-1m-v2"} else {}
        if name in {"oms", "orb-schwab"}:
            checks.update({NEW_ENV[2]: "true", "MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED": "true"})
        if name == "orb-schwab":
            checks["MAI_TAI_ORB_SCHWAB_OBSERVE_ENABLED"] = "false"
        values[name] = {}
        if name in {"oms", "schwab-1m-v2"}:
            from retry_zero_readonly import values as retry_values
            values[name].update(retry_values(raw))
        for key, expected in checks.items():
            found = [value.decode() for candidate, value in pairs if candidate.decode() == key]
            need(found == [expected], "explicit new-PID environment missing/drift: " + name + ":" + key)
            values[name][key] = found[0]
    need(effects.fleet() == current, "fleet moved during proc proof")
    # Validate actual catalog denominator before running the isolated checker.
    flags = json.loads((REPO / "ops/health/expected_flags.json").read_bytes())["flags"]
    numeric = json.loads((REPO / "ops/health/expected_numeric.json").read_bytes())["settings"]
    def count(rows):
        return sum(1 + len(row.get("also_check_services", [])) for row in rows)
    need(len(flags) == 130 and count(flags) == 143 and count(numeric) == 8, "actual catalog not151")
    from retry_zero_readonly import catalog
    catalog((effects.job / NUMERIC_ARTIFACT).read_bytes(), (REPO / "ops/health/expected_numeric.json").read_bytes())
    return values


def grade_logs(found, started):
    receipts = {}
    for name in CHANGED:
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
        need(not any("Traceback (most recent call last):" in line for line in records), "new-PID traceback")
        receipts[name] = dict(lines=records, ranges=found[name]["ranges"], start_utc=lower.isoformat())
    v2 = receipts["schwab-1m-v2"]["lines"]
    if not any("[V2-BOOT-HOLD]" in line for line in v2):
        raise Unknown("new-PID BOOT-HOLD unobserved")
    if not any("[V2-LINE-RESTORE]" in line and "entry_allowed=" in line for line in v2):
        raise Unknown("Restoration per-symbol admission evidence missing")
    sync = [re.search(r"live:orb: ok=(\d+) failed=(\d+) consecutive_now=(\d+)", line)
            for line in receipts["oms"]["lines"] if "[BROKER-SYNC-CENSUS]" in line]
    if not sync:
        raise Unknown("new OMS Webull sync unmeasured")
    need(all(match and int(match[2]) == 0 for match in sync), "new OMS Webull sync failed/malformed")
    if not any(int(match[1]) > 0 for match in sync):
        raise Unknown("new OMS Webull sync ok0")
    return receipts


def control_logs(found, state):
    lines = found["text"].splitlines()
    starts = [(i, match[1]) for i, line in enumerate(lines)
              if (match := re.fullmatch(r"INFO:\s+Started server process \[(\d+)\]", line))]
    need(len(starts) == 1 and starts[0][1] == str(state["MainPID"]), "control log lacks unique exact new-PID marker")
    new = lines[starts[0][0]:]
    need(not any("Traceback" in line or "ERROR" in line or "SCHWAB-TOKEN-DEAD" in line
                 or "DEGRADED" in line or "SCHWAB-TOKEN-REFRESHER-IDLE" in line for line in new), "new control/token failure log")
    need(sum("Application startup complete." in line for line in new) == 1
         and sum("[SCHWAB-TOKEN-REFRESHER] starting " in line for line in new) == 1
         and any(re.fullmatch(r"INFO:\s+Uvicorn running on http://(?:127\.0\.0\.1|0\.0\.0\.0):8100 \(Press CTRL\+C to quit\)", line)
                 for line in new), "control startup/token-owner/8100 binding missing")
    token_line = next(line for line in new if "[SCHWAB-TOKEN-REFRESHER] starting " in line)
    match = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})", token_line)
    need(match is not None, "token-owner startup timestamp absent")
    stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
    need(stamp >= system_time(state["ExecMainStartTimestamp"]), "control startup predates new identity")
    return dict(lines=new, ranges=found["ranges"], pid=state["MainPID"], token_owner_started_at_utc=stamp.isoformat())


def collect(effects, baseline, started):
    flags = process_flags(effects)
    deadline = time.monotonic() + 180
    while True:
        effects.redis()
        found = logs(baseline)
        try:
            graded = grade_logs(found, started)
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
    record = install_record(effects)
    output = effects.attempt / "restart-evidence-current.md"
    args = [PY, REPO / "ops/health/v2_restart_evidence.py", "report", "--snapshot", effects.attempt / "before-restart.json",
            "--install-record", record, "--expected-alembic-head", "20261005_0022",
            "--schema-column", "oms_managed_positions.entry_order_id", "--schema-column", "oms_managed_positions.entry_client_order_id",
            "--output", output]
    for name in CHANGED:
        args += ["--restarted", name]
    for name in ("oms", "schwab-1m-v2"):
        for key in PM:
            args += ["--expect-flag", name + ":" + key + "=true"]
    for name in ("oms", "orb-schwab"):
        args += ["--expect-flag", name + ":" + NEW_ENV[2] + "=true"]
    for name in ("oms", "schwab-1m-v2"):
        args += ["--expect-flag", name + ":" + RETRY_ENABLED + "=true",
                 "--expect-flag", name + ":" + RETRY_MAX + "=0"]
    result = effects.command(args, check=False, timeout=240)
    need(result.returncode == 0, "official restart/bar evidence FAIL or UNKNOWN; no N/A guessed")
    raw = output.read_bytes()
    need(b"Final call: PASS;" in raw or b"Final call: EXPECTED BY DESIGN;" in raw, "official report final unproven")
    return dict(process_flags=flags, logs=graded, official_report=str(output), official_report_sha256=digest(raw),
                date_et=effects.now().astimezone(ET).date().isoformat(), future_live_delivery="UNMEASURED")
