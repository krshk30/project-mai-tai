"""[codex] Oct7 paired policy. Pure transformations; no operational effects."""
from datetime import datetime, time, timezone
import hashlib
from io import StringIO
import json
from pathlib import Path
import re
import shlex
from zoneinfo import ZoneInfo

from dotenv.parser import parse_stream

PLAN_BASE = "994f08aee2c35b3809b3c3384e0d8628807dcfb7"
APP = PLAN_BASE
TREE = "7d827a407825d1d93aae8c3946d8b55320b9680f"
BOX = "5b8b4f642bbc3c312be436d0e92adbc22d9e9f95"
DAY = "2026-10-07"
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
FLAG = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_LINE_CHART_RESTORATION_ENABLED"
RETAINED_FLAG = "MAI_TAI_OMS_V2_WEBULL_MIRROR_RETAINED_HOLD_ENABLED"
CATALOG_BASE_SHA = "2f80d2d86601aed63416d0acc902bedf41215796ea16388bd51fde10fd4c6be1"
ROLLBACK_BASELINE_SHA = "fd05165a80683dd1979f428fa67ccd73ab36040a4d88cefff3ea3459f4c3066b"
HANDOFF_FLAG = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_REPRICE_HANDOFF_ENABLED"
PREFIX = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_"
# Seven live keys stay true; handoff stays false. Restoration and retained hold turn on.
LIVE_KEYS = tuple(PREFIX + suffix for suffix in (
    "PM_PRINT_ASK_CONFIRM_ENABLED", "PM_FLIP_WAIT_ENABLED", "PM_REST_REPRICE_ENABLED",
    "GAP_HOLD_ENABLED", "RESTING_BUY_ROUND_UP_ENABLED",
    "KEEP_REST_AFTER_BUY_ENABLED", "REMOVED_WAIT_CLEAR_ENABLED"))
V2 = "schwab-1m-v2"
SERVICES = ("control", "market-capture", "market-data", "oms", "orb", "orb-schwab",
            "reconciler", V2, "strategy", "momentum-paper", "option-a-daily-guard",
            "redis", "postgresql")
SOURCES = tuple("src/project_mai_tai/" + path for path in (
    "market_data/schwab_v2_rest_client.py", "services/schwab_1m_v2_bot.py",
    "strategy_core/session_line_restore.py", "oms/mirror_retained_hold.py", "oms/service.py"))
GATE_SHA = "40e57465f08c9cd9e34dc5739a3c68585d162ee2a5daa3e6b387b5d9fa24972e"
SCOPE = "linesrc1-hotfix1-oct7-unattended-paired-v2-oms"
PHASES = (("stop", V2), ("restart", "oms"), ("start", V2))
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


def rollback_baseline():
    raw = Path(__file__).with_name("paired-baseline.json").read_bytes()
    need(digest(raw) == ROLLBACK_BASELINE_SHA, "exact authorized rollback capture differs")
    return json.loads(raw)


def require_rollback_baseline(value):
    recorded = rollback_baseline()
    need(all(value.get(key) == expected for key, expected in recorded.items()),
         "release did not bind exact authorized rollback baseline")


def rollback_catalog(raw):
    need(digest(raw) == CATALOG_BASE_SHA, "source/installed Boolean catalog baseline differs")
    data = json.loads(raw)
    rows = [row for row in data["flags"] if row["name"] == "oms_v2_webull_mirror_retained_hold_enabled"]
    need(len(rows) == 1 and rows[0]["expected"] is True and rows[0]["owning_service"] == "oms"
         and not rows[0].get("also_check_services"), "retained-hold overlay owner/value drift")
    handoff = [row for row in data["flags"] if row["name"] == "strategy_schwab_1m_v2_atr_reprice_handoff_enabled"]
    need(len(handoff) == 1 and handoff[0]["expected"] is True, "source handoff catalog differs")
    handoff[0]["expected"] = False
    return canonical(data)


def rollback_runtime(raw, flags):
    need(digest(raw) == "d585c601eded25732dcaa05ec7886df44b42f506ed3e9efc933c5037649dc13d",
         "recorded pre-rollback runtime baseline differs")
    data = json.loads(raw)
    path = "/home/trader/restart_evidence/expected_flags.json"
    need(data["evidence_inputs"].get(path) == "acf5a6dc55d1e8c54bb3b1a7c24ed7b093673c9729bd7accef83a2a848d19652", "runtime installed catalog pin differs")
    data["evidence_inputs"][path] = digest(flags)
    return canonical(data)


def retained_off(raw, value="false"):
    need(type(raw) is bytes and 0 < len(raw) <= 262144, "proc environment missing/overflow")
    pairs = [piece.decode().split("=", 1) for piece in raw.split(b"\0") if piece]
    need(all(len(pair) == 2 for pair in pairs), "malformed proc environment")
    need([(key, actual) for key, actual in pairs if key.upper() == RETAINED_FLAG] == [(RETAINED_FLAG, value)],
         "authorized retained-hold OFF missing/alias/duplicate")


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
    need([(row.key, row.value) for row in bindings if row.key and row.key.upper() == RETAINED_FLAG]
         == [(RETAINED_FLAG, "false")] and re.search(rf"(?m)^{RETAINED_FLAG}=false$", text),
         "rollback retained hold must stay explicit single literal false")
    need([(row.key, row.value) for row in bindings if row.key and row.key.upper() == HANDOFF_FLAG]
         == [(HANDOFF_FLAG, "false")] and re.search(rf"(?m)^{HANDOFF_FLAG}=false$", text), "RPG handoff must remain literal OFF")
    for key in LIVE_KEYS:
        need([(row.key, row.value) for row in bindings if row.key and row.key.upper() == key]
             == [(key, "true")], "live key drift: " + key)
    return re.sub(rf"(?m)^{RETAINED_FLAG}=false$", RETAINED_FLAG + "=true", re.sub(pattern, FLAG + "=true", text)).encode()


def process_values(raw, restoration, handoff="false"):
    need(type(raw) is bytes and 0 < len(raw) <= 262144, "proc environment missing/overflow")
    pairs = [piece.decode().split("=", 1) for piece in raw.split(b"\0") if piece]
    need(all(len(pair) == 2 for pair in pairs), "malformed proc environment")
    expected = {**{key: "true" for key in LIVE_KEYS}, FLAG: restoration, HANDOFF_FLAG: handoff}
    if restoration == "true":
        retained_off(raw, "true")
        expected[RETAINED_FLAG] = "true"
    for key, value in expected.items():
        need([(name, val) for name, val in pairs if name.upper() == key] == [(key, value)],
             "proc value missing/alias/duplicate: " + key)
    return expected


def states(before, current, phase):
    need(set(before) == set(current) == set(SERVICES), "fleet population incomplete")
    need(phase in range(4), "phase invalid")
    stopped = {name for action, name in PHASES[:phase] if action in {"stop", "restart"}}
    started = {name for action, name in PHASES[:phase] if action in {"start", "restart"}}
    for name, state in current.items():
        old = before[name]
        if name not in stopped:
            need(state == old, "untouched identity drift: " + name)
        elif name not in started:
            need(state["MainPID"] == 0 and state["ActiveState"] == "inactive"
                 and state["SubState"] == "dead" and state["Result"] == "success"
                 and state["ExecMainStatus"] == 0 and state["NRestarts"] == 0,
                 "v2 stop not ordinary clean success")
            stable = set(old) - {"MainPID", "ActiveState", "SubState", "Result", "ExecMainCode", "ExecMainStatus", "InactiveEnterTimestamp"}
            need(all(state[key] == old[key] for key in stable), "stopped owner identity/config drift: " + name)
        else:
            need(state["MainPID"] > 0 and state["MainPID"] != old["MainPID"]
                 and state["ActiveState"] == "active" and state["SubState"] == "running"
                 and state["Result"] == "success" and state["NRestarts"] == 0
                 and state["ExecMainStatus"] == 0
                 and state["ExecMainStartTimestampMonotonic"] > old["ExecMainStartTimestampMonotonic"]
                 and re.fullmatch(r"[0-9a-f]{32}", state["InvocationID"])
                 and state["InvocationID"] != old["InvocationID"], "new v2 identity not proven")
            need(all(state[key] == old[key] for key in ("FragmentPath", "DropInPaths", "EnvironmentFiles")), "new owner config drift")


def assignment(text, key, value):
    pattern = rf"(?m)^{re.escape(key)}=.*$"
    need(len(re.findall(pattern, text)) == 1, "ambiguous assignment: " + key)
    return re.sub(pattern, lambda _: key + "=" + shlex.quote(str(value)), text)


def gate_candidate(raw, state, snapshot, record, untouched_oms=None):
    need(digest(raw) == GATE_SHA, "current morning gate baseline differs")
    text = raw.decode()
    for marker in ("upgrade_ack.py", "daily.py paper", "${EXPECTED_DATE//-/}"):
        need(marker in text, "morning/date/paper mechanism missing: " + marker)
    text = assignment(text, "EXPECTED_SHA", APP)
    text = assignment(text, "EXPECTED_PID", state["MainPID"])
    text = assignment(text, "EXPECTED_START", state["ExecMainStartTimestamp"])
    text = assignment(text, "SNAPSHOT", snapshot)
    text = assignment(text, "INSTALL_RECORD", record)
    if untouched_oms is not None:
        need(untouched_oms["MainPID"] > 0 and untouched_oms["NRestarts"] == 0, "new OMS identity incomplete")
        text = assignment(text, "EXPECTED_OMS_PID", untouched_oms["MainPID"])
        text = assignment(text, "EXPECTED_OMS_START", untouched_oms["ExecMainStartTimestamp"])
    # Classify only this pair; old grouped flags stay in the historical gate backup.
    text = re.sub(r"(?m)^\s*--restarted [^\n]+\n", "", text)
    text = re.sub(r"(?m)^\s*--expect-flag [^\n]+\n", "", text)
    anchor = "  --expected-alembic-head 20261005_0022"
    need(text.count(anchor) == 1 and "--no-schema-change" in text, "collector schema anchor drift")
    flags = {**{key: "true" for key in LIVE_KEYS}, FLAG: "true", RETAINED_FLAG: "true", HANDOFF_FLAG: "false"}
    additions = "  --restarted schwab-1m-v2 \\\n  --restarted oms \\\n"
    additions += "".join("  --expect-flag '" + owner + ":" + key + "=" + value + "' \\\n"
                         for owner in (V2, "oms") for key, value in flags.items())
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


def held_logs(text, state, now, stopped=None, allow_official_release=False):
    start = system_time(state["ExecMainStartTimestamp"])
    anchor = start if stopped is None else stopped
    need(anchor.astimezone(ET).date().isoformat() == DAY and anchor.astimezone(ET).hour >= 16
         and start >= anchor,
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
    releases = [(stamp, line) for stamp, line in records if "[V2-BOOT-HOLD] released" in line]
    if releases and allow_official_release:
        stamp, line = releases[-1]
        need(stamp <= now and "restoration_complete=1" in line and "reconstructed_uncapped=0" in line,
             "released BOOT marker not exact safe shape")
        need(not any(moment > stamp and "[V2-BOOT-HOLD] HELD" in record for moment, record in records),
             "released state contradicted by later hold")
        return dict(verdict="RELEASED_MARKER_REQUIRES_OFFICIAL_PROOF", literal_hold=line,
                    entry_allowed="source boot hold released; entry window unchanged",
                    next_session="2026-10-08 07:00 ET live line acceptance UNMEASURED")
    need(holds and 0 <= (now - holds[-1][0]).total_seconds() <= 120, "literal fresh BOOT-HOLD missing")
    need(not any("[V2-BOOT-HOLD] released" in line for _, line in records), "held receipt contradicted by release")
    return {"verdict": "HELD_AFTER16_NOT_RESTORATION_PASS", "entry_allowed": False,
            "literal_hold": holds[-1][1], "next_session": "2026-10-08 07:00 ET UNMEASURED"}


def oms_logs(text, state, now):
    start = system_time(state["ExecMainStartTimestamp"])
    stamp, scoped = None, []
    for line in text.splitlines():
        match = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})", line)
        if match:
            stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)
        need(stamp is not None or not line.strip(), "OMS new-range unscoped prefix")
        if stamp is not None and start <= stamp <= now:
            scoped.append(line)
    need(scoped and not any(re.search(r"\bERROR\b|Traceback|SCHWAB-TOKEN-DEAD", line) for line in scoped),
         "new OMS startup/error evidence unproven")
    return dict(scoped_records=len(scoped), errors=0, start_utc=start.isoformat(),
                freshness="separate current process/general-health proof; no aging startup-log exemption")


def report_disposition(rc, raw, held, continuity=None):
    # The official verdict remains raw. Only the explicitly authorized mechanical
    # restart gap and pending future callback are disclosed, never made PASS.
    released = held.get("verdict") == "RELEASED_MARKER_REQUIRES_OFFICIAL_PROOF"
    expected = [] if released else ["no post-restart restoration_complete=1 line",
                "no literal post-restart BOOT-HOLD release with restoration_complete=1"]
    rows = [line for line in raw.splitlines() if line.startswith("| ")]
    if released:
        for name in ("REST warmup", "BOOT-HOLD released"):
            matching = [line for line in rows if line.split("|")[1].strip() == name]
            need(len(matching) == 1 and matching[0].endswith("| PASS |"), "released state lacks official " + name + " PASS")
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
    need(released or held["verdict"] == "HELD_AFTER16_NOT_RESTORATION_PASS", "literal boot state not proven")
    return raw + "\nMECHANICS: COMPLETE_AFTER16, not collector PASS. " \
        "Known bounded restart minutes and/or future first closed callback are disclosed separately. " \
        "Next07:00 restoration/release remains UNMEASURED.\n" + held["literal_hold"] + "\n"
