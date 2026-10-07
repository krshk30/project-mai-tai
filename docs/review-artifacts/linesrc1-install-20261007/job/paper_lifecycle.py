"""[codex] Only saved morning paper/guard identities may close normally today."""
from copy import deepcopy
from datetime import datetime, time, timedelta
import json
import os
from pathlib import Path

import release_policy as p

ROLES = ("momentum-paper", "option-a-daily-guard")
AUDIT_PATH = "/home/trader/after-hours/2026-10-07/option-a-daily/run-20261007T074001494845Z/option-a-guard.jsonl"


def audit_in_stop_interval(paper, guard, stamp):
    # systemctl show prints these timestamps at whole-second resolution.
    return (p.system_time(paper["InactiveEnterTimestamp"]) <= stamp
            < p.system_time(guard["InactiveEnterTimestamp"]) + timedelta(seconds=1))


def read_close(path, before, current, now):
    from armed_readonly import unique
    path = Path(path)
    p.need(not path.is_symlink(), "guard audit symlink refused")
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        p.need(info.st_uid == 0 and info.st_mode & 0o022 == 0, "guard audit owner/mode unproven")
        offset = max(0, info.st_size - 262144)
        stream.seek(offset)
        raw = stream.read(262145)
        p.need(len(raw) <= 262144 and raw.endswith(b"\n"), "guard close audit truncated/unbounded")
        if offset:
            raw = raw.split(b"\n", 1)[1]
    records = [json.loads(line, object_pairs_hook=unique) for line in raw.splitlines() if line.strip()]
    start = p.system_time(before[ROLES[1]]["ExecMainStartTimestamp"])
    candidates = [row for row in records if isinstance(row, dict) and row.get("action") == "stop_paper"
                  and start <= p.moment(row["at_utc"]) <= now]
    p.need(len(candidates) == 1, "scheduled guard close missing/ambiguous in bounded tail")
    record = candidates[0]
    admit(before, current, now, record)
    return record, dict(path=str(path), inode=info.st_ino, device=info.st_dev, size=info.st_size,
                       offset=offset, tail_sha256=p.digest(raw), bytes=len(raw), raw_tail=raw.decode())


def admit(before, current, now, close):
    p.need(set(before) == set(current) == set(p.SERVICES), "lifecycle fleet scope incomplete")
    normalized = deepcopy(before)
    transitioned = []
    floor = datetime.combine(datetime.fromisoformat(p.DAY).date(), time(9, 40), p.ET)
    for role in ROLES:
        old, new = before[role], current[role]
        if old == new:
            p.need(new["MainPID"] == 0 and new["ActiveState"] == "inactive"
                   and new["SubState"] == "dead" and new["NRestarts"] == 0
                   and new["Result"] == "success" and new["ExecMainStatus"] == 0,
                   "paper/guard not yet clean inactive")
            continue
        p.need(old["MainPID"] > 0 and old["ActiveState"] == "active" and old["SubState"] == "running"
               and old["NRestarts"] == 0 and old["Result"] == "success" and old["ExecMainStatus"] == 0,
               "reviewed paper/guard was not original healthy invocation")
        mutable = {"MainPID", "ActiveState", "SubState", "Result", "NRestarts", "ExecMainCode",
                   "ExecMainStatus", "InactiveEnterTimestamp"}
        p.need(set(old) == set(new) and all(new[key] == old[key] for key in set(old) - mutable),
               "paper/guard invocation/start/configuration drift")
        p.need(new["MainPID"] == 0 and new["ActiveState"] == "inactive" and new["SubState"] == "dead"
               and new["Result"] == "success" and new["NRestarts"] == 0
               and new["ExecMainCode"] == 1 and new["ExecMainStatus"] == 0,
               "paper/guard stop not clean")
        stopped = p.system_time(new["InactiveEnterTimestamp"])
        p.need(floor <= stopped <= now and stopped.astimezone(p.ET).date().isoformat() == p.DAY,
               "paper/guard close outside scheduled dated window")
        p.need(p.system_time(old["ExecMainStartTimestamp"]).astimezone(p.ET).date().isoformat() == p.DAY,
               "paper/guard original start is not today")
        normalized[role] = deepcopy(new)
        transitioned.append(role)
    p.states(normalized, current, 0)
    if transitioned or all(current[role]["ActiveState"] == "inactive" for role in ROLES):
        p.need(isinstance(close, dict) and close.get("action") == "stop_paper"
               and close.get("reason") == "scheduled_session_close" and type(close.get("systemctl_rc")) is int
               and close["systemctl_rc"] == 0, "normal scheduled guard close positive audit absent")
        stamp = p.moment(close["at_utc"])
        p.need(floor <= stamp <= now
               and audit_in_stop_interval(current[ROLES[0]], current[ROLES[1]], stamp),
               "guard close audit outside actual saved invocation stop interval")
    return dict(scope="core11 exact; saved paper/guard scheduled close only", transitioned=transitioned,
                close_audit=close, no_service_actions=True,
                stop_timestamp_precision_seconds=1, audit_upper_bound="strictly before guard exit second + 1s",
                paper=current[ROLES[0]], guard=current[ROLES[1]])
