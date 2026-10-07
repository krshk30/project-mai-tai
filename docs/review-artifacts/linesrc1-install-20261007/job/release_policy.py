"""[codex] Oct7 v2-only policy. Pure transformations; no operational effects."""
from datetime import datetime, time, timezone
import hashlib
from io import StringIO
import json
from pathlib import Path
import re
import shlex
from zoneinfo import ZoneInfo

from dotenv.parser import parse_stream

APP = "f9c9bd332392e2c905fc39b954421c88970844d7"
TREE = "0fa89d5d40473e29e1039cd6a80200b36945d696"
BOX = "5b8b4f642bbc3c312be436d0e92adbc22d9e9f95"
DAY = "2026-10-07"
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
FLAG = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_LINE_CHART_RESTORATION_ENABLED"
PREFIX = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_"
# Restoration is the only changed flag. These eight already-live keys stay literal true.
LIVE_KEYS = tuple(PREFIX + suffix for suffix in (
    "PM_PRINT_ASK_CONFIRM_ENABLED", "PM_FLIP_WAIT_ENABLED", "PM_REST_REPRICE_ENABLED",
    "ATR_REPRICE_HANDOFF_ENABLED", "GAP_HOLD_ENABLED", "RESTING_BUY_ROUND_UP_ENABLED",
    "KEEP_REST_AFTER_BUY_ENABLED", "REMOVED_WAIT_CLEAR_ENABLED"))
V2 = "schwab-1m-v2"
SERVICES = ("control", "market-capture", "market-data", "oms", "orb", "orb-schwab",
            "reconciler", V2, "strategy", "momentum-paper", "option-a-daily-guard",
            "redis", "postgresql")
SOURCES = tuple("src/project_mai_tai/" + path for path in (
    "market_data/schwab_v2_rest_client.py", "services/schwab_1m_v2_bot.py",
    "strategy_core/session_line_restore.py"))
GATE_SHA = "40e57465f08c9cd9e34dc5739a3c68585d162ee2a5daa3e6b387b5d9fa24972e"
SCOPE = "linesrc1-oct7-one-v2-stop-start"
ACK_STATE = {"MainPID": "765206", "NRestarts": "1", "ActiveState": "active",
             "SubState": "running", "ExecMainStartTimestamp": "Wed 2026-10-07 06:31:14 UTC",
             "InvocationID": "de16f6a3ae8f438a8aae3d294d7db686"}


class Stop(RuntimeError):
    pass


class Pending(Stop):
    pass


def need(condition, reason):
    if not condition:
        raise Stop(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, indent=2, sort_keys=True, default=str) + "\n").encode()


def moment(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    need(result.tzinfo is not None, "timestamp lacks zone")
    return result.astimezone(UTC)


def system_time(value):
    need(re.fullmatch(r"[A-Za-z]{3} \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC", value),
         "non-UTC/unreadable systemd start")
    return datetime.strptime(value[4:], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=UTC)


def first_stop_window(now):
    local = now.astimezone(ET)
    if local.date().isoformat() != DAY or local.time() <= time(16):
        raise Pending("first stop requires Oct7 strictly after16 and before midnight ET")


def env_candidate(raw):
    text = raw.decode()
    bindings = list(parse_stream(StringIO(text)))
    need(not any(row.error for row in bindings), "malformed EnvironmentFile")
    keys = [row.key.upper() for row in bindings if row.key is not None]
    need(len(keys) == len(set(keys)), "duplicate/case-alias EnvironmentFile definition")
    found = [(row.key, row.value) for row in bindings if row.key and row.key.upper() == FLAG]
    need(found == [(FLAG, "false")], "restoration must be explicit single literal false")
    pattern = rf"(?m)^{FLAG}=false$"
    need(len(re.findall(pattern, text)) == 1, "restoration assignment must be literal")
    for key in LIVE_KEYS:
        need([(row.key, row.value) for row in bindings if row.key and row.key.upper() == key]
             == [(key, "true")], "live key drift: " + key)
    return re.sub(pattern, FLAG + "=true", text).encode()


def process_values(raw, restoration):
    need(type(raw) is bytes and 0 < len(raw) <= 262144, "proc environment missing/overflow")
    pairs = [piece.decode().split("=", 1) for piece in raw.split(b"\0") if piece]
    need(all(len(pair) == 2 for pair in pairs), "malformed proc environment")
    expected = {**{key: "true" for key in LIVE_KEYS}, FLAG: restoration}
    for key, value in expected.items():
        need([(name, val) for name, val in pairs if name.upper() == key] == [(key, value)],
             "proc value missing/alias/duplicate: " + key)
    return expected


def states(before, current, phase):
    need(set(before) == set(current) == set(SERVICES), "fleet population incomplete")
    need(phase in {0, 1, 2}, "phase invalid")
    for name, state in current.items():
        old = before[name]
        if name != V2 or phase == 0:
            need(state == old, "untouched identity drift: " + name)
        elif phase == 1:
            need(state["MainPID"] == 0 and state["ActiveState"] == "inactive"
                 and state["SubState"] == "dead" and state["Result"] == "success"
                 and state["ExecMainStatus"] == 0 and state["NRestarts"] == 0,
                 "v2 stop not ordinary clean success")
        else:
            need(state["MainPID"] > 0 and state["MainPID"] != old["MainPID"]
                 and state["ActiveState"] == "active" and state["SubState"] == "running"
                 and state["Result"] == "success" and state["NRestarts"] == 0
                 and state["ExecMainStartTimestampMonotonic"] > old["ExecMainStartTimestampMonotonic"]
                 and re.fullmatch(r"[0-9a-f]{32}", state["InvocationID"])
                 and state["InvocationID"] != old["InvocationID"], "new v2 identity not proven")


def assignment(text, key, value):
    pattern = rf"(?m)^{re.escape(key)}=.*$"
    need(len(re.findall(pattern, text)) == 1, "ambiguous assignment: " + key)
    return re.sub(pattern, lambda _: key + "=" + shlex.quote(str(value)), text)


def gate_candidate(raw, state, snapshot, record):
    need(digest(raw) == GATE_SHA, "current morning gate baseline differs")
    text = raw.decode()
    for marker in ("upgrade_ack.py", "daily.py paper", "${EXPECTED_DATE//-/}"):
        need(marker in text, "morning/date/paper mechanism missing: " + marker)
    text = assignment(text, "EXPECTED_SHA", APP)
    text = assignment(text, "EXPECTED_PID", state["MainPID"])
    text = assignment(text, "EXPECTED_START", state["ExecMainStartTimestamp"])
    text = assignment(text, "SNAPSHOT", snapshot)
    text = assignment(text, "INSTALL_RECORD", record)
    # The new evidence baseline classifies this restart only; old grouped flags
    # are retained separately in the original gate backup, never attributed to OMS.
    text = re.sub(r"(?m)^\s*--restarted [^\n]+\n", "", text)
    text = re.sub(r"(?m)^\s*--expect-flag [^\n]+\n", "", text)
    anchor = "  --expected-alembic-head 20261005_0022"
    need(text.count(anchor) == 1 and "--no-schema-change" in text, "collector schema anchor drift")
    flags = {**{key: "true" for key in LIVE_KEYS}, FLAG: "true"}
    additions = "  --restarted schwab-1m-v2 \\\n"
    additions += "".join("  --expect-flag 'schwab-1m-v2:" + key + "=" + value + "' \\\n"
                         for key, value in flags.items())
    text = text.replace(anchor, additions + anchor)
    return text.encode()


def repin_ack(helper, record):
    old = 'APP = "' + BOX + '"'
    need(helper.decode().count(old) == 1, "ack helper APP binding differs")
    data = json.loads(record)
    need(data.get("application") == BOX and data.get("state") == ACK_STATE
         and data.get("decision") == "ACKNOWLEDGED_REDIS_UPGRADE_RESTART",
         "exact morning acknowledgement differs")
    data["application"] = APP
    return helper.replace(old.encode(), ('APP = "' + APP + '"').encode()), canonical(data)


def catalog_ids(rows):
    result = [(row["name"], owner) for row in rows
              for owner in (row["owning_service"], *row.get("also_check_services", []))]
    need(len(result) == len(set(result)), "catalog identity duplicated")
    return result


def catalog_counts(flags, numeric):
    boolean = json.loads(flags)["flags"]
    numbers = json.loads(numeric)["settings"]
    b, n = catalog_ids(boolean), catalog_ids(numbers)
    need(len(b) == 147 and len(n) == 10 and not set(b) & set(n), "actual157 population differs")
    retry = [row for row in numbers if row["name"] == "strategy_schwab_1m_v2_retry_one_max_retries"]
    need(len(retry) == 1 and type(retry[0]["expected"]) is int and retry[0]["expected"] == 0
         and retry[0].get("require_process_env") is True
         and {owner for name, owner in n if name == retry[0]["name"]} == {"oms", V2},
         "installed explicit retry-zero numeric proof missing")
    return {"boolean": len(b), "numeric": len(n), "total": len(b) + len(n)}


def held_logs(text, state, now, stopped=None):
    start = system_time(state["ExecMainStartTimestamp"])
    anchor = start if stopped is None else stopped
    need(anchor.astimezone(ET).date().isoformat() == DAY and anchor.astimezone(ET).hour >= 16
         and 0 <= (start - anchor).total_seconds() <= 240,
         "held admission is only this after16 restart")
    records, stamp = [], None
    for line in text.splitlines():
        match = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})", line)
        if match:
            stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)
        need(stamp is not None or not line.strip(), "untimestamped new-log prefix")
        if stamp is not None and stamp >= start:
            records.append((stamp, line))
    need(records, "new v2 logs unmeasured")
    need(not any(re.search(r"\bERROR\b|Traceback|SCHWAB-TOKEN-DEAD|restoration_complete=1 reconstructed-uncapped", line)
                 for _, line in records), "startup/error log blocker")
    holds = [(stamp, line) for stamp, line in records
             if "[V2-BOOT-HOLD] HELD" in line and "restoration_complete=0" in line]
    need(holds and 0 <= (now - holds[-1][0]).total_seconds() <= 120, "literal fresh BOOT-HOLD missing")
    need(not any("[V2-BOOT-HOLD] released" in line for _, line in records), "held receipt contradicted by release")
    return {"verdict": "HELD_AFTER16_NOT_RESTORATION_PASS", "entry_allowed": False,
            "literal_hold": holds[-1][1], "next_session": "2026-10-08 07:00 ET UNMEASURED"}


def report_disposition(rc, raw, held, continuity=None):
    # The official verdict remains raw. Only the explicitly authorized mechanical
    # restart gap and pending future callback are disclosed, never made PASS.
    expected = ["no post-restart restoration_complete=1 line",
                "no literal post-restart BOOT-HOLD release with restoration_complete=1"]
    rows = [line for line in raw.splitlines() if line.startswith("| ")]
    if rc == 0:
        need(re.search(r"(?m)^Final call: (PASS|EXPECTED BY DESIGN);", raw), "zero collector verdict differs")
        return raw
    failures = [line[2:] for line in raw.split("\nFailures:\n", 1)[-1].split("\nUnknowns:\n", 1)[0].splitlines()
                if line.startswith("- ")]
    unknowns = [line[2:] for line in raw.split("\nUnknowns:\n", 1)[1].splitlines() if line.startswith("- ")] if "\nUnknowns:\n" in raw else []
    need(rc in {1, 2} and set(expected) <= set(failures), "official held pair absent")
    other = [line for line in failures if line not in expected]
    need(all(re.fullmatch(r"REST backfill coverage not proven for [1-9][0-9]* independently printed minute\(s\)", line)
             for line in other), "official non-restoration/non-restart failure")
    if other:
        need(continuity is not None and continuity["verdict"] == "MEASURED_RESTART_WINDOW"
             and any(row["missing_persisted_minutes_utc"] for row in continuity["per_stock"]),
             "restart gap not independently bounded")
        # Match every official missing-minute detail to our narrow captured rows.
        minutes = {row["symbol"]: {moment(value).astimezone(ET).strftime("%Y-%m-%d %H:%M:%S")
                   for value in row["missing_persisted_minutes_utc"]} for row in continuity["per_stock"]}
        detail = re.findall(r"([A-Z][A-Z0-9.\-]*) status=MISSING_PRINTED_MINUTE [^;]*?missing_printed=([1-9][0-9]*) minutes=([^;]+)", raw)
        need(detail and sum(int(count) for _, count, _ in detail)
             == sum(int(re.search(r"for ([1-9][0-9]*)", line)[1]) for line in other),
             "official missing-minute denominator differs")
        for symbol, count, field in detail:
            stamps = re.findall(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (EDT|EST) \((\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC\)", field)
            dates = []
            for local, zone, absolute in stamps:
                stamp = moment(absolute + "+00:00")
                need(stamp.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S") == local
                     and stamp.astimezone(ET).tzname() == zone, "official minute zones conflict")
                dates.append(local)
            need(len(dates) == int(count) and len(set(dates)) == len(dates)
                 and set(dates) <= minutes.get(symbol, set()),
                 "official missing symbol/minute outside actual restart capture")
    if unknowns:
        need(continuity is not None and continuity["verdict"] == "PENDING_FIRST_CLOSED_BAR"
             and all(line.startswith("REST backfill continuity UNKNOWN:")
                     and "no fresh post-restart warmup bar in persisted strategy history" in line for line in unknowns),
             "official unreadable/unrelated unknown, not a pending future callback")
    fail_names = [line.split("|")[1].strip() for line in rows if line.endswith("| FAIL |")]
    need(set(fail_names) <= {"REST warmup", "BOOT-HOLD released", "Bar continuity"}, "other official failed row")
    need(held["verdict"] == "HELD_AFTER16_NOT_RESTORATION_PASS", "literal hold not proven")
    return raw + "\nMECHANICS: COMPLETE_HELD_AFTER16, not collector PASS. " \
        "Known bounded restart minutes and/or future first closed callback are disclosed separately. " \
        "Next07:00 restoration/release remains UNMEASURED.\n" + held["literal_hold"] + "\n"
