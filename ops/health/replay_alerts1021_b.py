#!/usr/bin/env python3
"""MAIN-only offline actual24h page routing; never polls ntfy or sends a page."""

import argparse
import ast
from collections import Counter
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOPICS = {"mai-tai-preopen-28806a5a97b7", "mai-tai-routine-112964cc8f26787132a29538"}


def load_policy():
    path = ROOT / "src/project_mai_tai/phone_alert_policy.py"
    spec = importlib.util.spec_from_file_location("alerts_b_offline_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mirror_window():
    tree = ast.parse((ROOT / "src/project_mai_tai/backtest/replay.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "LIVE_LOCKED" for t in node.targets):
            values = {kw.arg: ast.literal_eval(kw.value) for kw in node.value.keywords}
            return (values["strategy_schwab_1m_v2_entry_window_end_hour_et"],
                    values["strategy_schwab_1m_v2_entry_window_end_minute_et"])
    raise ValueError("approved LIVE_LOCKED window source missing")


def read_hashed(directory, record):
    path = (directory / record["path"]).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("input path escapes packet")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise ValueError("input hash mismatch: " + record["path"])
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def replay(path):
    manifest = json.loads(path.read_text())
    if (manifest.get("schema") != "ALERTS1021-B-PAGES-1" or manifest.get("complete") is not True
            or not manifest.get("coverage_provenance")):
        raise ValueError("complete actual packet/provenance required; never synthesize drops")
    if set(manifest.get("topics", [])) != TOPICS:
        raise ValueError("both actual phone topics are required")
    since = datetime.fromisoformat(manifest["since"].replace("Z", "+00:00"))
    until = datetime.fromisoformat(manifest["until"].replace("Z", "+00:00"))
    if since.tzinfo is None or until.tzinfo is None or (until - since).total_seconds() != 86400:
        raise ValueError("explicit timezone-aware24h window required")
    events, messages = Counter(), []
    for record in manifest["messages"]:
        for index, row in enumerate(read_hashed(path.parent, record), 1):
            events[row.get("event", "MISSING")] += 1
            if row.get("event") != "message":
                continue
            if row.get("topic") != record["topic"]:
                raise ValueError("MESSAGE topic differs from its exported source")
            if not since.timestamp() <= row["time"] < until.timestamp():
                raise ValueError("MESSAGE outside explicit window; fix export, do not drop")
            messages.append({**row, "source_pointer": {"file": record["path"], "jsonl_record": index}})
    identities = [(row["topic"], row["id"]) for row in messages]
    if len(set(identities)) != len(identities) or len(messages) != manifest["message_denominator"]:
        raise ValueError("actual MESSAGE denominator/unique identity mismatch")
    if set(row["topic"] for row in messages) - set(manifest["topics"]):
        raise ValueError("topic not in complete coverage declaration")
    if set(record["topic"] for record in manifest["messages"]) != set(manifest["topics"]):
        raise ValueError("a phone topic export is missing, including zero-message topic")
    evidence = {}
    if manifest.get("evidence"):
        for row in read_hashed(path.parent, manifest["evidence"]):
            key = (row["topic"], row["message_id"])
            if key not in identities or key in evidence:
                raise ValueError("unmatched/duplicate page evidence")
            evidence[key] = row
    policy, seen_stops, output = load_policy(), set(), []
    hour, minute = mirror_window()
    for raw in sorted(messages, key=lambda r: (r["time"], r["topic"], r["id"])):
        title, body = raw.get("title", ""), raw.get("message", "")
        context = evidence.get((raw["topic"], raw["id"]), {})
        phone, rule = policy.phone_decision(title, body)
        grade = "MESSAGE_ONLY"
        if policy.locked_window_notice_resolved(title, body, hour=hour, minute=minute) and not policy.protected(title, body):
            phone, rule = False, "approved99fc_mirror1545_counterfactual_not_runtime_mute"
        if title.upper().startswith("ORPHAN ORDER"):
            grade = "UNKNOWN_LOOKUP_KEEP_PHONE"
            if (context.get("binding") == "EXACT_RETAINED" and context.get("all_page_findings_bound") is True
                    and context.get("lookup_status") == "COMPLETE_SUCCESS"
                    and context.get("account_alias_coverage_complete") is True):
                absent = policy.manual_only(account=context.get("account"), native_ids=context.get("native_ids", []),
                    requested_ids=context.get("lookup_native_ids", []), bot_ids=context.get("bot_native_ids", []), complete=True)
                grade = "EXACT_ACCOUNT_NATIVE_LOOKUP"
                if absent and not policy.protected(title, body):
                    phone, rule = False, "manual_only_complete_known_no_bot_row"
        if title == "Option A paper STOP":
            key = None
            repo_sender = (context.get("sender_binding") == "EXACT_RETAINED"
                           and context.get("sender_path") ==
                           "/home/trader/project-mai-tai/ops/health/option_a_treatment_guard.py")
            if (repo_sender and context.get("binding") == "EXACT_RETAINED"
                    and context.get("latch_present_at_notification") is True):
                key = policy.stop_event_key(title, body, context.get("safety_stop_latch", {}))
            grade = ("UNKNOWN_OR_UNTOUCHED_SENDER_KEEP_PHONE" if not repo_sender
                     else "EXACT_STOP_EVENT" if key else "UNKNOWN_STOP_ID_KEEP_PHONE")
            if key:
                if key in seen_stops:
                    phone, rule = False, "same_stop_event_already_delivered"
                else:
                    seen_stops.add(key)
                    phone, rule = True, "first_page_for_distinct_stop_event"
        output.append({"id": raw["id"], "topic": raw["topic"], "time": raw["time"], "title": title,
                       "message": body, "source_pointer": raw["source_pointer"],
                       "before": "ACTUALLY_DELIVERED_IN_PACKET", "after_phone": phone,
                       "rule": rule, "evidence_grade": grade})
    return {"scope": "OFFLINE_COUNTERFACTUAL_PHONE_ROUTING_NOT_NATIVE_DELIVERY_OR_DB_REPLAY",
            "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "since": manifest["since"], "until": manifest["until"], "actual_messages": len(output),
            "raw_event_counts": dict(events), "after_phone": sum(row["after_phone"] for row in output),
            "rules": dict(Counter(row["rule"] for row in output)), "rows": output,
            "unknown_lookup_or_event": sum(row["evidence_grade"].startswith("UNKNOWN") for row in output),
            "stop_journal_initial_state": "EMPTY_COUNTERFACTUAL_NOT_HISTORICAL_PROOF; first observed exact event kept",
            "maintenance_and_baseline_monitor_acceptance": "SEPARATE_EXISTING99fc_GATES_NOT_REEXECUTED_HERE"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    print(json.dumps(replay(parser.parse_args().manifest.resolve()), indent=2, sort_keys=True))
