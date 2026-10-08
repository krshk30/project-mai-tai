"""Codex Install2 parent-bound policy; no system or trading effects."""
from datetime import datetime, time, timezone
import hashlib
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
BOX = "4805ddc81184c76b4d5cef5c483c809edb666fe6"
BINDING_PATH = Path(__file__).resolve().with_name("binding.json")
BINDING = json.loads(BINDING_PATH.read_bytes()) if BINDING_PATH.exists() else {}
APP = BINDING.get("approved_sha", "UNBOUND_PARENT_APP")
TREE = BINDING.get("tree", "UNBOUND_PARENT_TREE")
DAY = "2026-10-06"
BASELINE_GATE = "8cdafcded0f748b6779696953cc8a101409b6bc0ce9a2314938d7f4fa82cf15a"
ADAPTER = "a9f2407612baf48ee4b0577e42ccd0c714be1c1315b8276f5f9638a2999a1aaa"
SCOPE = "install2-two-merges-one-reviewed-batch-20261006"
AUTHORITY = "operator-standing-mechanics-authority"
CHANGED = ("schwab-1m-v2", "strategy", "oms", "control")
CUMULATIVE = ("schwab-1m-v2", "strategy", "oms", "orb-schwab", "control")
SERVICES = ("control", "market-capture", "market-data", "oms", "orb", "orb-schwab",
            "reconciler", "schwab-1m-v2", "strategy", "momentum-paper",
            "option-a-daily-guard", "redis", "postgresql")
PREFIX = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_"
PM = tuple(PREFIX + suffix for suffix in (
    "PM_PRINT_ASK_CONFIRM_ENABLED", "PM_FLIP_WAIT_ENABLED", "PM_REST_REPRICE_ENABLED",
    "ATR_REPRICE_HANDOFF_ENABLED", "GAP_HOLD_ENABLED", "RESTING_BUY_ROUND_UP_ENABLED",
    "LINE_CHART_RESTORATION_ENABLED")) + ("MAI_TAI_OMS_V2_WEBULL_MIRROR_FRESH_PRICE_ENABLED",)
ATR = "MAI_TAI_ORB_SCHWAB_ATR_ENTRY_GATE_ENABLED"
NEW_ENV = (PREFIX + "KEEP_REST_AFTER_BUY_ENABLED", PREFIX + "REMOVED_WAIT_CLEAR_ENABLED",
           "MAI_TAI_OMS_V2_WEBULL_MIRROR_RETAINED_HOLD_ENABLED", "MAI_TAI_WEBULL_LIST_PRIMARY_READS_ENABLED")
RETRY_ENABLED = PREFIX + "RETRY_ONE_ENABLED"
RETRY_MAX = PREFIX + "RETRY_ONE_MAX_RETRIES"
ENV_UPDATES = {key: "true" for key in NEW_ENV}
NUMERIC_ARTIFACT = "expected_numeric.retry-zero.json"
PHASES = (
    ("stop", "schwab-1m-v2"), ("stop", "strategy"), ("stop", "oms"),
    ("start", "oms"), ("start", "schwab-1m-v2"), ("start", "strategy"), ("restart", "control"))


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
    from binding import validate_binding
    validate_binding(BINDING)
    need(release.get("binding") == BINDING, "parent binding missing/drift")
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
