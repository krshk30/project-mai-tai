"""Offline WBQUIET A/B/C evidence replay; no trading/application imports or IO calls.

Endpoint counters are cumulative per minute. Historical final orders/fills do not
reconstruct position snapshots or an earlier day-list response. Missing evidence
is UNMEASURED, never a zero-impact assertion.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import gzip
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
COUNTER = re.compile(r"minute=(\S+) endpoint=(\S+) success=(\d+) failure=(\d+) total=(\d+) partial=([01])")
CENSUS = re.compile(r"(live:[^: ]+): ok=(\d+) failed=(\d+) consecutive_now=(\d+)")
SYNC = re.compile(r"\[OMS-BROKER-SYNC-PASS\] id=(\d+) phase=(start|end)")
TERMINAL = {"filled", "cancelled", "canceled", "rejected", "expired", "aborted"}
BIYA = "schwab_1m_v2-BIYA-open-0e40052d917c"
DECISION_TAGS = ("OMS-FLAT", "VIRTUAL-CLEAR", "VIRTUAL-RESTORE", "OMS-CANCEL-UNCONFIRMED",
                 "OMS-RESERVE", "BROKER-SYNC-UNREADABLE", "OMS-OCO-EXIT-MISS", "OMS-ENTRY-OWNERSHIP")
MAX_INPUT = 80_000_000


def moment(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return result.astimezone(UTC)


def day(value):
    return moment(value).astimezone(ET).date().isoformat()


def provenance(event):
    return {key: event.get(key) for key in ("at", "path", "line_number")}


def missing_ranges(stamps):
    ranges = []
    for stamp in sorted(set(stamps)):
        at = moment(stamp)
        if ranges and at == moment(ranges[-1]["end_exclusive_utc"]):
            ranges[-1]["end_exclusive_utc"] = (at + timedelta(minutes=1)).isoformat()
            ranges[-1]["minutes"] += 1
        else:
            ranges.append({"start_utc": at.isoformat(),
                "end_exclusive_utc": (at + timedelta(minutes=1)).isoformat(), "minutes": 1})
    return ranges


def archive_coverage(sources, low, high):
    expected, date = [], low.date() + timedelta(days=1)
    while date <= high.date():
        expected.append("oms.log-" + date.strftime("%Y%m%d"))
        date += timedelta(days=1)
    present = {Path(row["path"]).name.removesuffix(".gz") for row in sources}
    missing = []
    for name in expected:
        if name not in present:
            end = datetime.strptime(name.removeprefix("oms.log-"), "%Y%m%d").replace(tzinfo=UTC)
            missing.append({"expected_path": "/var/log/project-mai-tai/" + name + "[.gz]",
                "start_utc": (end - timedelta(days=1)).isoformat(), "end_exclusive_utc": end.isoformat()})
    return {"expected_rotated_suffixes": expected, "missing_rotated_file_intervals": missing,
        "scope": "daily UTC rotation names inferred from the captured source inventory, not continuous request coverage"}


def state_cadence(facts, at):
    """Independent state rule; unknown cannot enter the quiet-flat branch."""
    if facts.get("configured") is False:
        return "no_credentials", None
    if facts.get("held") is True or facts.get("broker_held") is True:
        return "held", 15
    if facts.get("working") is True:
        return "working", 15
    if facts.get("fresh_fill") is True:
        return "fresh-fill", 15
    if facts.get("known") is not True or facts.get("fresh") is not True:
        return "unknown", 15
    # Missing H/W/F fields do not prove absence even if a producer says known.
    if any(facts.get(key) is not False for key in ("held", "working", "fresh_fill")):
        return "unknown", 15
    local = moment(at).astimezone(ET)
    return ("overnight", 300) if local.hour >= 20 or local.hour < 4 else ("flat", 60)


def reader_window(readers):
    retained, unknown = [], 0
    for row in readers:
        try:
            elapsed = float(row["elapsed_seconds"])
            if not math.isfinite(elapsed) or elapsed < 0 or not isinstance(row["reader"], str):
                raise ValueError("ambiguous reader")
            if elapsed <= 60:
                retained.append({"reader": row["reader"], "elapsed_seconds": elapsed})
        except (KeyError, TypeError, ValueError):
            unknown += 1
    return retained, unknown


def terminal_list_match(decision, observations):
    """Positive same-account/order/symbol/side fresh list evidence only."""
    if any(not decision.get(key) for key in ("account", "client_order_id", "symbol", "side")):
        return {"verdict": "UNMEASURED", "reason": "decision_identity_incomplete"}
    at = moment(decision["at"])
    eligible = []
    for row in observations:
        if any(row.get(key) != decision[key] for key in ("account", "client_order_id", "symbol", "side")):
            continue
        if row.get("source") != "/trade/orders/list-today" or not row.get("evidence_path") or not re.fullmatch(
                r"[0-9a-f]{64}", row.get("evidence_sha256", "")):
            continue
        try:
            age = (at - moment(row["acquired_at"])).total_seconds()
            filled = float(row["filled_quantity"])
        except (ValueError, TypeError, KeyError):
            continue
        if not 0 <= age < 2 or not math.isfinite(filled) or filled < 0:
            continue
        if str(row.get("status", "")).lower() not in TERMINAL:
            continue
        eligible.append((row, age, filled))
    if not eligible:
        return {"verdict": "UNMEASURED", "reason": "no_fresh_positive_terminal_day_list_row_at_decision"}
    signatures = {(str(row["status"]).lower(), filled) for row, _, filled in eligible}
    if len(signatures) != 1:
        return {"verdict": "UNMEASURED", "reason": "contradictory_terminal_observations"}
    row, age, filled = eligible[-1]
    return {"verdict": "TERMINAL_EXECUTION_REQUIRES_ATTRIBUTION" if filled > 0 else "PROVEN_TERMINAL_ZERO_REMAINDER",
            "status": row["status"], "filled_quantity": filled, "age_seconds": age,
            "evidence_path": row["evidence_path"], "evidence_sha256": row["evidence_sha256"]}


def counter_buckets(events):
    buckets, naive, malformed = {}, defaultdict(Counter), []
    for event in events:
        match = COUNTER.search(event["line"])
        if not match:
            continue
        minute, endpoint, success, failure, total, partial = match.groups()
        row = {"success": int(success), "failure": int(failure), "total": int(total), "partial": int(partial),
               "source": provenance(event)}
        if row["success"] + row["failure"] != row["total"]:
            malformed.append(provenance(event))
            continue
        key = moment(minute).isoformat(), endpoint
        naive[(day(minute), endpoint)].update(total=row["total"], failure=row["failure"])
        bucket = buckets.setdefault(key, {"rows": [], "row": row})
        bucket["rows"].append(row)
        old = bucket["row"]
        if (1 - row["partial"], row["total"]) > (1 - old["partial"], old["total"]):
            bucket["row"] = row
    for bucket in buckets.values():
        finals = {(row["success"], row["failure"], row["total"]) for row in bucket["rows"] if row["partial"] == 0}
        bucket["ambiguous_final_epochs"] = len(finals) > 1
    return buckets, naive, malformed


def shadow_replay(events, as_of):
    rows, malformed, duplicates, clocks, identities = [], [], [], {}, set()
    for event in sorted(events, key=lambda event: moment(event["at"])):
        if "[WBQUIET-SHADOW] " not in event["line"]:
            continue
        try:
            receipt = json.loads(event["line"].split("[WBQUIET-SHADOW] ", 1)[1])
            if receipt.get("policy_applied") is not False:
                raise ValueError("not log-only")
            at, pid, pass_id = moment(receipt["as_of"]), int(receipt["process_pid"]), int(receipt["pass_id"])
            identity = pid, pass_id
            if identity in identities:
                duplicates.append({**provenance(event), "process_pid": pid, "pass_id": pass_id})
                continue
            identities.add(identity)
            if at > as_of or pid <= 0 or not isinstance(receipt["accounts"], list) or len(receipt["accounts"]) > 16:
                raise ValueError("receipt outside bounds")
            seen_accounts = set()
            for account in receipt["accounts"]:
                name = account["account"]
                if not isinstance(name, str) or name in seen_accounts:
                    raise ValueError("ambiguous account")
                seen_accounts.add(name)
                state, cadence = state_cadence(account["facts_at_pass_start"], at.isoformat())
                actual = account.get("actual_adapter_read")
                key = pid, name
                prior = clocks.get(key)
                if actual == "unreadable":
                    state, cadence = "unknown", 15
                if cadence is None:
                    action = "ignore"
                elif actual == "not_reached":
                    state, action = "unknown", "not_evaluated"
                elif state == "unknown" or prior is None or (at - prior).total_seconds() >= cadence or at < prior:
                    action = "read"
                    clocks[key] = at
                else:
                    action = "skip"
                readers, reader_unknown = reader_window(account.get("covered_readers", []))
                rows.append({**provenance(event), "observed_at": at.isoformat(), "process_pid": pid, "pass_id": pass_id,
                    "account": name, "state": state, "nominal_seconds": cadence, "would": action,
                    "recorded_would": account.get("would"), "actual_adapter_read": actual,
                    "tagged_readers_within_60s": readers, "malformed_reader_tags": reader_unknown,
                    "right_censored_60s_window": at + timedelta(seconds=60) > as_of,
                    "dropped_observations": receipt.get("dropped_observations"),
                    "coverage": "PARTIAL invocation tags, not every external reader or committed-generation proof",
                    "wire_calls_saved": "UNMEASURED"})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            malformed.append(provenance(event))
    return {"rows": rows, "malformed": malformed, "duplicate_receipts": duplicates}


def analyze(data):
    as_of, low = moment(data["as_of_utc"]), moment(data["window_start_utc"])
    events = data["log_events"]
    if not isinstance(events, list) or len(events) > 150000:
        raise ValueError("event population unbounded")
    buckets, naive, malformed = counter_buckets(events)
    shadow = shadow_replay(events, as_of)
    dates = []
    local_day = low.astimezone(ET).date()
    while local_day <= as_of.astimezone(ET).date():
        dates.append(local_day.isoformat())
        local_day += timedelta(days=1)
    reports = {}
    list_evidence = data.get("day_list_observations", [])
    for date in dates:
        day_events = [event for event in events if day(event["at"]) == date]
        day_buckets = [(key, bucket) for key, bucket in buckets.items() if day(key[0]) == date]
        endpoints = defaultdict(lambda: {"success": 0, "failure": 0, "total": 0, "minutes": 0,
                                        "unfinished_minutes": 0, "ambiguous_minutes": 0})
        for (minute, endpoint), bucket in day_buckets:
            row = bucket["row"]
            for field in ("success", "failure", "total"):
                endpoints[endpoint][field] += row[field]
            endpoints[endpoint]["minutes"] += 1
            endpoints[endpoint]["unfinished_minutes"] += row["partial"]
            endpoints[endpoint]["ambiguous_minutes"] += int(bucket["ambiguous_final_epochs"])
        census, searched, sync_counts = [], [], Counter()
        for event in day_events:
            text = event["line"]
            if "BROKER-SYNC-CENSUS" in text:
                census.append({**provenance(event), "accounts": [{"account": account, "ok": int(ok), "failed": int(failed),
                    "consecutive_now": int(consecutive)} for account, ok, failed, consecutive in CENSUS.findall(text)]})
            if any(tag in text for tag in DECISION_TAGS):
                searched.append({**provenance(event), "tag": next(tag for tag in DECISION_TAGS if tag in text)})
            match = SYNC.search(text)
            if match:
                sync_counts[match[2]] += 1
        failed_cancel = [row for row in data.get("cancel_417", []) if day(row["at"]) == date]
        failures, seen_failure = [], set()
        for row in failed_cancel:
            identity = row.get("request_id") or (row["path"], row["line_number"])
            if identity in seen_failure:
                continue
            seen_failure.add(identity)
            coid = row.get("client_order_id")
            child = bool(re.search(r"-protect-.+[TS]$", coid or ""))
            decision = {**row, "symbol": (coid.split("-")[1] if coid and coid.startswith("schwab_1m_v2-") else None),
                "side": "sell" if child else None}
            proof = terminal_list_match(decision, list_evidence)
            failures.append({**row, "exit_child_scope_B": child, "terminal_day_list": proof,
                "identity_source": "nearest SDK exception context; account not reconstructed from latest order"})
        shadow_rows = [row for row in shadow["rows"] if day(row["observed_at"]) == date]
        replay_census = []
        for window in census:
            at = moment(window["at"])
            for account in window["accounts"]:
                observed = [row for row in shadow_rows if row["account"] == account["account"]
                            and at - timedelta(seconds=300) < moment(row["observed_at"]) <= at]
                if not observed:
                    replay_census.append({**provenance(window), "account": account["account"], "actual_ok": account["ok"],
                        "hypothetical_ok_zero": "UNMEASURED: absent per-pass state/due schedule"})
                else:
                    due = sum(row["would"] == "read" for row in observed)
                    replay_census.append({**provenance(window), "account": account["account"], "actual_ok": account["ok"],
                        "observed_nominal_read_opportunities": due,
                        "hypothetical_ok_zero": "UNMEASURED: nominal read is not wire success; reader coverage PARTIAL"})
        position_minutes = [moment(minute).astimezone(ET) for (minute, endpoint), _ in day_buckets
                            if endpoint == "/account/positions"]
        local_start = datetime.fromisoformat(date).replace(tzinfo=ET)
        local_end = min(local_start + timedelta(days=1), as_of.astimezone(ET))
        expected_minutes = []
        tick = max(local_start.astimezone(UTC), low)
        while tick < local_end.astimezone(UTC):
            expected_minutes.append(tick.isoformat())
            tick += timedelta(minutes=1)
        retained_position_minutes = {moment(minute).isoformat() for minute in position_minutes}
        missing_minutes = [stamp for stamp in expected_minutes if stamp not in retained_position_minutes]
        cancel_reconciliation = []
        for (minute, endpoint), bucket in day_buckets:
            if endpoint != "/trade/order/cancel" or not any(row["failure"] for row in bucket["rows"]):
                continue
            cancel_reconciliation.append({"minute": minute, "selected_counter": bucket["row"],
                "all_cumulative_snapshot_rows": bucket["rows"],
                "naive_failure_sum": sum(row["failure"] for row in bucket["rows"]),
                "selected_failure_count": bucket["row"]["failure"],
                "ambiguous_final_epochs": bucket["ambiguous_final_epochs"]})
        quiet_opportunities = sum(1 if 4 <= minute.hour < 20 else int(minute.minute % 5 == 0) for minute in position_minutes)
        # Fills are retained positive hot-state evidence, never proof other minutes were flat.
        fill_stamps = [moment(row["filled_at"]) for row in data.get("fills", []) if row.get("account") == "live:orb"]
        fresh_fill_minutes = [minute for minute in position_minutes if any(
            0 <= (minute - stamp).total_seconds() <= 600 for stamp in fill_stamps)]
        fill_retained_proxy = sum(4 if minute in fresh_fill_minutes else
            1 if 4 <= minute.hour < 20 else int(minute.minute % 5 == 0) for minute in position_minutes)
        reports[date] = {
            "as_of_utc": as_of.isoformat(), "right_censored_day": date == as_of.astimezone(ET).date().isoformat(),
            "complete_ET_day_claim": False,
            "evidence_scope": "OMS log files only, retained SDK endpoint/minute counters and timestamped exceptions; not all-service/account wire coverage",
            "counter_coverage": {"window_start_utc": max(local_start.astimezone(UTC), low).isoformat(),
                "window_end_utc": local_end.astimezone(UTC).isoformat(),
                "positions_minutes_expected_by_clock": len(expected_minutes),
                "positions_minutes_with_counter": len(position_minutes),
                "positions_missing_counter_minutes_utc": missing_minutes,
                "positions_missing_counter_intervals_utc": missing_ranges(missing_minutes),
                "missing_counter_meaning": "UNMEASURED: service downtime, no call, unflushed shutdown tail or missing instrumentation cannot be separated",
                "last_current_minute_unflushed": date == as_of.astimezone(ET).date().isoformat(),
                "other_endpoint_absent_minutes": "not zero request proof; only nonzero minute counters are emitted"},
            "retained_structured_lines": len(day_events), "endpoints": dict(endpoints),
            "counter_first_minute": min((key[0] for key, _ in day_buckets), default=None),
            "counter_last_minute": max((key[0] for key, _ in day_buckets), default=None),
            "naive_cumulative_sums": {ep: dict(counts) for (d, ep), counts in naive.items() if d == date},
            "sync_pass_lines": dict(sync_counts), "census_windows": len(census),
            "actual_live_orb_ok_zero_windows": sum(account["ok"] == 0 for window in census for account in window["accounts"]
                                                       if account["account"] == "live:orb"),
            "census_replay_receipts": replay_census,
            "decision_lines_searched": len(searched), "decision_minutes_searched": len({row["at"][:16] for row in searched}),
            "decision_search_receipts": searched,
            "rule_A": {"verdict": "UNMEASURED", "observed_shadow_account_passes": len(shadow_rows),
                "state_counts": dict(Counter(row["state"] for row in shadow_rows)),
                "nominal_read_skip_counts": dict(Counter(row["would"] for row in shadow_rows)),
                "tagged_reader_invocations_on_nominal_skip": sum(len(row["tagged_readers_within_60s"]) for row in shadow_rows if row["would"] == "skip"),
                "actual_saved_calls": "UNMEASURED", "decision_changing_minutes": "UNMEASURED: no historical positions/generation equivalence",
                "flat_capacity_proxy": {"label": "NOT replay/call savings: one always-flat credentialed account, fixed phase, retained positions minutes only",
                    "covered_counter_minutes": len(position_minutes), "nominal_15s_opportunities": 4 * len(position_minutes),
                    "nominal_quiet_60s_300s_opportunities": quiet_opportunities,
                    "nominal_suppressed_opportunities": 4 * len(position_minutes) - quiet_opportunities,
                    "known_recent_fill_minutes": len(fresh_fill_minutes),
                    "fresh_fill_retained_otherwise_flat_opportunities": fill_retained_proxy,
                    "fresh_fill_retained_suppressed_opportunity_proxy": 4 * len(position_minutes) - fill_retained_proxy,
                    "other_direct_readers": "UNMEASURED", "held_working_recent_fill_history": "UNMEASURED"}},
            "rule_B": {"verdict": "UNMEASURED", "distinct_cancel_417": len(failures),
                "raw_HTTP417_exception_lines": len(failed_cancel),
                "endpoint_failure_reconciliation": cancel_reconciliation,
                "corrected_endpoint_cancel_failures": endpoints["/trade/order/cancel"]["failure"],
                "naive_cumulative_cancel_failures": naive[(date, "/trade/order/cancel")]["failure"],
                "exit_child_failure_requests": sum(row["exit_child_scope_B"] for row in failures),
                "proven_terminal_zero_remainder": sum(row["terminal_day_list"]["verdict"] == "PROVEN_TERMINAL_ZERO_REMAINDER" for row in failures),
                "terminal_execution_requires_attribution": sum(row["terminal_day_list"]["verdict"] == "TERMINAL_EXECUTION_REQUIRES_ATTRIBUTION" for row in failures),
                "terminal_membership_unmeasured": sum(row["terminal_day_list"]["verdict"] == "UNMEASURED" for row in failures),
                "cancel_receipts": failures},
        }
    biya_logs = [{**provenance(event), "line": event["line"]} for event in events if "0e40052d917c" in event["line"]]
    biya_proofs = [row for row in data.get("terminal_proofs", []) if row.get("client_order_id") == BIYA]
    result = {"schema_version": 1, "as_of_utc": as_of.isoformat(), "window_start_utc": low.isoformat(),
        "source_files": data["source_files"], "collection_errors": data.get("collection_errors", {}),
        "source_coverage": {"service_scope": "OMS only; no other process/service logs or account-specific wire attribution",
            "rotated_paths_present": [row["path"] for row in data["source_files"] if not row["path"].endswith("/oms.log")],
            **archive_coverage(data["source_files"], low, as_of),
            "unflushed_shutdown_tails": "UNMEASURED: a process shutdown can lose its current endpoint minute; final counter absence is not zero calls",
            "day_list_bodies": "UNMEASURED; terminal DETAIL proof store is not a retained day-list response history"},
        "days": reports, "shadow_replay": shadow, "malformed_counters": malformed,
        "rule_C": {"verdict": "UNMEASURED", "account": "live:orb", "client_order_id": BIYA,
            "decision_time_utc": "2026-10-07T19:12:15.930000+00:00", "log_receipts": biya_logs,
            "retained_terminal_detail_proofs": biya_proofs,
            "day_list_at_decision": terminal_list_match({"at": "2026-10-07T19:12:15.930000+00:00", "account": "live:orb",
                "client_order_id": BIYA, "symbol": "BIYA", "side": "buy"}, list_evidence),
            "detail_body_None": "UNMEASURED: UNREADABLE/UNCONFIRMED logs do not retain the response body",
            "dead_target_bound_warning": "existing bounded-UNCONFIRMED field is not positive terminal proof"},
        "population": {"fills": len(data.get("fills", [])), "terminal_detail_proofs": len(data.get("terminal_proofs", [])),
                       "day_list_observations": len(list_evidence), "configured_DB_Webull_accounts_not_runtime_routing_proof": data.get("accounts", [])},
        "reader_coverage": "PARTIAL; Agent D follow-up is separate and future observations cannot repair absent historical response bodies",
        "trading_rule_agreement": "UNMEASURED; no AGREE or zero-impact claim from missing decision history",
        "manual_positions": "nominal first seen <=60s daytime / <=300s overnight vs <=15s now, plus lag; ownership unchanged"}
    return result


def markdown(report):
    lines = ["# WBQUIET1 Friday Evidence Preparation", "", "As-of UTC: `" + report["as_of_utc"] + "`.", "",
        "Historical retained **OMS-only** evidence; no cadence/cancel build or production action. No row claims complete all-service/account ET-day coverage. Today's row is right-censored at the exact as-of above.", "",
        "| ET Day | Positions calls / fail | Detail calls / fail | Cancel calls / fail | List calls / fail | Census / actual live:orb ok=0 | Decision lines / minutes | B failures / child / UNMEASURED | Shadow account passes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for date, row in report["days"].items():
        def endpoint(path):
            item = row["endpoints"].get(path, {})
            return str(item.get("total", 0)) + " / " + str(item.get("failure", 0))
        b = row["rule_B"]
        lines.append("| " + " | ".join([date + (" (partial)" if row["right_censored_day"] else ""),
            endpoint("/account/positions"), endpoint("/trade/order/detail"), endpoint("/trade/order/cancel"),
            endpoint("/trade/orders/list-today"), str(row["census_windows"]) + " / " + str(row["actual_live_orb_ok_zero_windows"]),
            str(row["decision_lines_searched"]) + " / " + str(row["decision_minutes_searched"]),
            str(b["distinct_cancel_417"]) + " / " + str(b["exit_child_failure_requests"]) + " / " + str(b["terminal_membership_unmeasured"]),
            str(row["rule_A"]["observed_shadow_account_passes"])]) + " |")
    lines.extend(["", "## Limits And Rule Verdicts", "",
        "A: **UNMEASURED** for actual calls saved, changed decisions and hypothetical census success. Endpoint counts are corrected for cumulative partial counters; they do not identify the caller/account state. No final-order status is backdated. The always-flat opportunity proxy is separate in JSON, not savings.", "",
        "B: **UNMEASURED** where the already-fetched day-list body at the cancel decision is absent. HTTP 417, a later terminal detail proof, or a final DB status is not list membership then. The proven count is an evidence denominator, not evidence that terminal orders never existed.", "",
        "C: **UNMEASURED**, BIYA `schwab_1m_v2-BIYA-open-0e40052d917c` at 2026-10-07 19:12 UTC. UNREADABLE/UNCONFIRMED does not identify a literal detail-body None or a fresh terminal day-list row. Source lines are retained in JSON.", "",
        "Reader coverage remains PARTIAL. Agent D's new hooks are not duplicated here. Future Friday observations will update this repeatable report; no agreement or zero-impact assertion is made today.", "",
        "## Cancel Population Reconciliation", "",
        "| ET Day | Corrected endpoint counter calls / failures | Naive cumulative counter failures | SDK HTTP417 exception lines / distinct requests |",
        "| --- | --- | --- | --- |"])
    for date in ("2026-10-06", "2026-10-07"):
        if date in report["days"]:
            row = report["days"][date]
            endpoint = row["endpoints"].get("/trade/order/cancel", {})
            b = row["rule_B"]
            lines.append(f"| {date} | {endpoint.get('total', 0)} / {endpoint.get('failure', 0)} | "
                         f"{b['naive_cumulative_cancel_failures']} | {b['raw_HTTP417_exception_lines']} / {b['distinct_cancel_417']} |")
    lines.extend(["", "Naive sums include cumulative partial=1 snapshots plus final=0 counters and therefore are not distinct SDK error events. Compare the selected final-per-minute counters with the separately extracted exception lines above; neither proves complete wire traffic. Minute/source/line receipts for every repeated snapshot are in JSON. Account-specific decision membership remains UNMEASURED.", "",
        "## Coverage Gaps", "",
        "| ET Day | Retained positions-counter minutes / clock-window minutes | Missing counter minutes | Right-censored today |",
        "| --- | --- | --- | --- |"])
    for date, row in report["days"].items():
        coverage = row["counter_coverage"]
        lines.append(f"| {date} | {coverage['positions_minutes_with_counter']} / {coverage['positions_minutes_expected_by_clock']} | "
                     f"{len(coverage['positions_missing_counter_minutes_utc'])} | {row['right_censored_day']} |")
    lines.extend(["", "Missing minute timestamps and contiguous ranges are enumerated in JSON. They cannot distinguish service downtime, no calls and unflushed shutdown tails. Counts are retained evidence, not a complete wire ledger.", "",
        "Expected closed UTC rotations: `" + ", ".join(report["source_coverage"]["expected_rotated_suffixes"]) + "`.",
        "Missing rotated intervals: `" + json.dumps(report["source_coverage"]["missing_rotated_file_intervals"]) + "`. Presence does not prove an unflushed process tail was retained.", "",
        "| Captured OMS path | First / last stamped UTC line | Uncompressed SHA256 |",
        "| --- | --- | --- |"])
    for row in report["source_files"]:
        lines.append(f"| `{row['path']}` | {row.get('first_stamp_utc')} / {row.get('last_stamp_utc')} | `{row.get('uncompressed_sha256')}` |")
    lines.extend(["", "Endpoint counters are OMS-local SDK aggregates, not per-account or all-service totals. DB Webull account names are inventory only, not credential/routing proof. Exception client IDs come from nearest SDK context; no historical account is adopted from final order state. Successful day-list response bodies and per-account position snapshots are absent. Unflushed shutdown tails are UNMEASURED; the current live file is right-censored at the stated extraction time.", "",
        "## Reproduce", "", "```sh", "python scripts/wbquiet_history_replay.py --input /path/to/own-pull.json --output /new/report.json --markdown /new/report.md", "```", "",
        "All source paths, uncompressed SHA256s, per-line search/cancel/census receipts, raw pull SHA256 and source-window bounds are in JSON. Missing archive intervals and unflushed shutdown tails remain outside retained-counter coverage."])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    opener = gzip.open if args.input.suffix == ".gz" else open
    with opener(args.input, "rb") as stream:
        raw = stream.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        parser.error("input exceeds 80 MB")
    report = analyze(json.loads(raw))
    report["raw_pull_path"] = str(args.input.resolve())
    report["raw_pull_sha256"] = hashlib.sha256(raw).hexdigest()
    report["raw_pull_hash_scope"] = "decoded original JSON bytes; identical for plain/gzip input"
    with args.output.open("x") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.markdown:
        with args.markdown.open("x") as stream:
            stream.write(markdown(report))
    print(json.dumps({"as_of_utc": report["as_of_utc"], "raw_pull_sha256": report["raw_pull_sha256"],
                      "day_count": len(report["days"]), "verdicts": "A/B/C UNMEASURED; no behavior build"}))


if __name__ == "__main__":
    main()
