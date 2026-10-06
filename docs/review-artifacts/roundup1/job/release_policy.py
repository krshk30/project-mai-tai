"""Pure policy for the attended Oct6 release; no system or trading effects."""
from datetime import datetime, time, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
APP = "4805ddc81184c76b4d5cef5c483c809edb666fe6"
TREE = "4248057079864f93066a69f355e2607840c701b9"
BOX = "7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf"
DAY = "2026-10-06"
BASELINE_GATE = "8cdafcded0f748b6779696953cc8a101409b6bc0ce9a2314938d7f4fa82cf15a"
ADAPTER = "a9f2407612baf48ee4b0577e42ccd0c714be1c1315b8276f5f9638a2999a1aaa"
SCOPE = "six-item-4805-retry-zero-display-one-control-restart-install1"
AUTHORITY = "operator-standing-mechanics-authority"
CHANGED = ("schwab-1m-v2", "orb-schwab", "oms", "control")
SERVICES = ("control", "market-capture", "market-data", "oms", "orb", "orb-schwab",
            "reconciler", "schwab-1m-v2", "strategy", "momentum-paper",
            "option-a-daily-guard", "redis", "postgresql")
PREFIX = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_"
PM = tuple(PREFIX + suffix for suffix in (
    "PM_PRINT_ASK_CONFIRM_ENABLED", "PM_FLIP_WAIT_ENABLED", "PM_REST_REPRICE_ENABLED",
    "ATR_REPRICE_HANDOFF_ENABLED", "GAP_HOLD_ENABLED", "RESTING_BUY_ROUND_UP_ENABLED",
    "LINE_CHART_RESTORATION_ENABLED")) + ("MAI_TAI_OMS_V2_WEBULL_MIRROR_FRESH_PRICE_ENABLED",)
ATR = "MAI_TAI_ORB_SCHWAB_ATR_ENTRY_GATE_ENABLED"
NEW_ENV = (PREFIX + "RESTING_BUY_ROUND_UP_ENABLED", PREFIX + "LINE_CHART_RESTORATION_ENABLED", ATR)
RETRY_ENABLED = PREFIX + "RETRY_ONE_ENABLED"
RETRY_MAX = PREFIX + "RETRY_ONE_MAX_RETRIES"
ENV_UPDATES = {**{key: "true" for key in NEW_ENV}, RETRY_MAX: "0"}
NUMERIC_ARTIFACT = "expected_numeric.retry-zero.json"
NUMERIC_SHA = "bff3fad73fa593b48af08fc3cb8ab6cad5d2d055e1bc787706ff79ad34dbf205"
PHASES = (
    ("stop", "schwab-1m-v2"), ("stop", "orb-schwab"), ("stop", "oms"),
    ("start", "oms"), ("start", "orb-schwab"), ("start", "schwab-1m-v2"), ("restart", "control"))


class Stop(RuntimeError):
    pass


class Unknown(RuntimeError):
    pass


def need(condition, message):
    if not condition:
        raise Stop(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def moment(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    need(result.tzinfo is not None, "timestamp lacks zone")
    return result.astimezone(UTC)


def first_write_window(now):
    local = now.astimezone(ET)
    need(local.date().isoformat() == DAY and local.time() > time(16), "first-write/stop window closed")


def decision_record(release, release_hash):
    need(release["approved_sha"] == APP and release["tree"] == TREE and release["box_sha"] == BOX
         and release["scope"] == SCOPE and release["date_et"] == DAY, "release scope drift")
    need(re.fullmatch(r"[0-9a-f]{40}", release["plan_commit"]) is not None, "plan commit unbound")
    need(re.fullmatch(r"[0-9a-f]{64}", release_hash) is not None, "release hash unbound")
    need(not release.get("blocking_acceptance"), "unresolved immutable acceptance blocker")
    return dict(decision="APPROVED", authority=AUTHORITY, attended=True,
                release_sha256=release_hash, approved_sha=APP, plan_commit=release["plan_commit"], date_et=DAY, scope=SCOPE)


def approval(release, release_hash, decision, now):
    need(decision == decision_record(release, release_hash), "literal standing-authority approval absent/mismatched")
    first_write_window(now)


def states(before, current, completed):
    need(set(before) == set(current) == set(SERVICES), "fleet census incomplete")
    need(0 <= completed <= len(PHASES), "unknown phase")
    stopped, started = set(), set()
    for action, name in PHASES[:completed]:
        if action in {"stop", "restart"}:
            stopped.add(name)
        if action in {"start", "restart"}:
            started.add(name)
    for name in SERVICES:
        old, state = before[name], current[name]
        if name in stopped - started:
            need(state["MainPID"] == 0 and state["ActiveState"] == "inactive"
                 and state["Result"] == "success", "stopped unit not clean: " + name)
        elif name in started:
            need(state["MainPID"] > 0 and state["MainPID"] != old["MainPID"]
                 and state["NRestarts"] == 0 and state["ActiveState"] == "active"
                 and state["SubState"] == "running" and state["Result"] == "success"
                 and state["ExecMainStartTimestampMonotonic"] > old["ExecMainStartTimestampMonotonic"],
                 "new owning identity not proven: " + name)
            if name == "control":
                need(re.fullmatch(r"[0-9a-f]{32}", state.get("InvocationID", "")) is not None
                     and state["InvocationID"] != old["InvocationID"], "new control invocation not proven")
        else:
            need(state == old, "untouched/early identity drift: " + name)


def row47(before, after, rows, since, until, *, intended, unit):
    need(intended and unit == "project-mai-tai-orb-schwab.service", "reset outside intended orb stop")
    need(before["MainPID"] > 0 and before["ActiveState"] == "active" and before["Result"] == "success"
         and before["NRestarts"] == 0, "preexisting ORB failure")
    need(after["MainPID"] == 0 and after["ActiveState"] == "failed"
         and after["SubState"] == "failed" and after["Result"] == "exit-code"
         and after["ExecMainCode"] == 1 and after["ExecMainStatus"] == 1
         and after["NRestarts"] == 0
         and after["ExecMainStartTimestampMonotonic"] == before["ExecMainStartTimestampMonotonic"],
         "row47 not exact old-process exit1/PID0")
    need(0 <= (until - since).total_seconds() <= 120 and bool(rows), "unbounded/missing shutdown evidence")
    need(re.fullmatch(r"[0-9a-f]{32}", before.get("InvocationID", "")), "old ORB invocation missing")
    own, manager = [], []
    for row in rows:
        need(row.get("_SYSTEMD_UNIT") == unit or row.get("UNIT") == unit, "foreign-unit shutdown evidence")
        stamp = datetime.fromtimestamp(int(row["__REALTIME_TIMESTAMP"]) / 1e6, UTC)
        need(since <= stamp <= until, "shutdown evidence outside intended stop")
        if row.get("_PID") == str(before["MainPID"]):
            need(row.get("_SYSTEMD_UNIT") == unit and row.get("_SYSTEMD_INVOCATION_ID") == before["InvocationID"],
                 "old ORB process invocation mismatch")
            own.append(row["MESSAGE"])
        elif row.get("_PID") == "1":
            need(row.get("UNIT") == unit and row.get("INVOCATION_ID") == before["InvocationID"],
                 "manager old invocation mismatch")
            manager.append(row["MESSAGE"])
        else:
            raise Stop("unbound shutdown process")
    need("asyncio.exceptions.CancelledError" in own, "exact CancelledError missing")
    need("SIGTERM received" in own, "old PID explicit SIGTERM evidence missing")
    need(any(re.fullmatch(r'\s*File "/[^"\n]*/mai-tai-orb-schwab", line \d+, in <module>', line)
             for line in own), "old ORB entrypoint frame missing")
    need(any(message == unit + ": Main process exited, code=exited, status=1/FAILURE" for message in manager),
         "exact systemd exit1 evidence missing")
    need(any(row.get("_PID") == "1" and row.get("JOB_TYPE") == "stop"
             and row.get("MESSAGE", "").startswith("Stopping " + unit + " - ") for row in rows),
         "current-attempt intended manager stop missing")
    need(not any(any(word in message for word in ("STARTUP", "[ORB-SCHWAB] mode=", "SIGKILL", "segfault",
                                                    "Exception:", "Error:")) for message in own + manager),
         "different/new startup failure")
    return {"classification": "row47-intended-old-SIGTERM-CancelledError-exit1", "pid": before["MainPID"]}
