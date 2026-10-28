#!/usr/bin/env python3
"""Offline counterfactual routing replay of the SHA-pinned operator ntfy export.

This is a packet classification replay, not an execution of live DB/systemd checks.
The maintenance window is operator evidence; no historical marker is invented.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
PACKETS = {
    "ntfy-mai-tai-preopen-28806a5a97b7-24h-0826.jsonl":
        "d14fcc2cfcd5684775e2622b6dc8b3d4ff99185c64459b542f1dfee72a070b33",
    "ntfy-mai-tai-routine-112964cc8f26787132a29538-24h-0826.jsonl":
        "db201a4ef23214e7dfa5d91430b89b1ef061a9fee61182b64ae085c1acc88c47",
}
EXPECTED_FIRE = {"8eQ9Ku6Tufoe", "ORRxUPAnGGoC", "UVpOp4eIdUJ6", "Ari39BxFu2xO"}


def load_ops(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def mirror_values() -> dict:
    tree = ast.parse((ROOT / "src/project_mai_tai/backtest/replay.py").read_text())
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "LIVE_LOCKED" for target in targets):
                return {kw.arg: ast.literal_eval(kw.value) for kw in node.value.keywords}
    raise ValueError("LIVE_LOCKED dictionary missing")


def classify(message: dict, *, mirror: dict, scanner, d6, maintenance) -> dict:
    title, body = message["title"], message["message"]
    result = {"id": message["id"], "title": title, "time": message["time"]}
    if title.startswith("RED mai-tai service runtime:"):
        name = title.split(": ", 1)[1]
        start = datetime.fromisoformat("2026-10-21T02:47:00+00:00").timestamp()
        end = datetime.fromisoformat("2026-10-21T02:57:00+00:00").timestamp()
        suppressed = start <= message["time"] < end and maintenance.planned_finding(
            "fleet-runtime:" + name, maintenance.SERVICES)
        group, fire, reason = (1, False, "operator-reported planned window; marker counterfactual") if suppressed else (
            3, True, "unplanned runtime stop; GUARDPIN1 remains separately owned")
    elif title == "RED live config drift":
        import re
        drifts = re.findall(r"(strategy_\w+)\n\s+LIVE_LOCKED=\d+\s+live env='(\d+)'", body)
        if len(drifts) != 2:
            raise ValueError("unexpected drift evidence")
        group, fire, reason = 2, any(mirror.get(key) != int(value) for key, value in drifts), "compare actual drift keys with corrected mirror"
    elif title == "Option A paper STOP":
        group, fire, reason = 3, True, "actual guard STOP; no GUARDPIN1 change in this bundle"
    elif title.startswith("RED mai-tai NOT READY"):
        group, fire, reason = 4, True, "actual UNKNOWN gate; rebaseline acceptance remains required"
    elif title == "scanner capture CHECK UNKNOWN":
        import re
        values = dict(re.findall(r"(rows|symbols|same_weekday_baseline_median|baseline_sessions|feed_age_minutes)=(\d+)", body))
        obs = scanner.DatabaseObservation(int(values["rows"]), int(values["symbols"]),
                                         "2026-10-21 07:47:07", float(values["same_weekday_baseline_median"]),
                                         int(values["baseline_sessions"]))
        verdict = scanner.classify(obs, now_et=datetime.fromtimestamp(message["time"], timezone.utc).astimezone(scanner.EASTERN),
                                   feed_age_minutes=int(values["feed_age_minutes"]))
        group, fire, reason = 5, verdict.exit_code == 1 or "reason=baseline_population_insufficient" not in verdict.line, verdict.line
    elif title == "D6 outcome acceptance NONPASS":
        lines = body.splitlines()
        valid, _ = d6._denominator_contract(lines)
        group, fire, reason = 6, d6.actionable_nonpass("UNEXERCISED", lines, valid), "actual UNEXERCISED report; original nonpass retained in status"
    elif title == "AMBER mai-tai readiness warnings" and body.startswith("SEED-EXPOSURE"):
        group, fire, reason = 7, False, "aggregate gap notice lacks positive watched/untruncated proof; status only"
    else:
        raise ValueError(f"unclassified actual alert {message['id']}: {title}")
    return {**result, "group": group, "would_fire": fire, "reason": reason}


def replay(directory: Path) -> dict:
    messages = []
    events = Counter()
    for filename, expected in PACKETS.items():
        raw = (directory / filename).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"packet SHA mismatch: {filename}")
        for line in raw.splitlines():
            event = json.loads(line)
            events[event.get("event")] += 1
            if event.get("event") == "message":
                messages.append(event)
    ids = [message["id"] for message in messages]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate raw alert IDs")
    modules = {name: load_ops(name) for name in
               ("scanner_capture_check", "fanout_outcome_acceptance_cron", "planned_maintenance")}
    rows = [classify(message, mirror=mirror_values(), scanner=modules["scanner_capture_check"],
                     d6=modules["fanout_outcome_acceptance_cron"], maintenance=modules["planned_maintenance"])
            for message in messages]
    if Counter(row["group"] for row in rows) != {1: 9, 2: 2, 3: 3, 4: 1, 5: 1, 6: 3, 7: 3}:
        raise ValueError("unexpected actual packet population; do not force a denominator")
    if {row["id"] for row in rows if row["would_fire"]} != EXPECTED_FIRE:
        raise ValueError("remaining alerts differ from actual groups #3/#4")
    return {"kind": "offline packet-routing counterfactual; live checks not run",
            "requested_count": 18, "actual_messages": len(rows), "raw_event_counts": dict(events),
            "denominator_mismatch": "22 actual message events, not 18; none removed",
            "would_fire": sum(row["would_fire"] for row in rows), "alerts": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.packet_dir), indent=2))
