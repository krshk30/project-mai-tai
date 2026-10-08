"""Summarize retained evidence; never infer past broker snapshots from final row state."""

import collections
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DAYS = ("2026-10-02", "2026-10-06", "2026-10-07")
COUNTER = re.compile(
    r"minute=(\S+) endpoint=(\S+) success=(\d+) failure=(\d+) total=(\d+) partial=(\d+)"
)
CENSUS = re.compile(r"(live:[^: ]+): ok=(\d+) failed=(\d+) consecutive_now=(\d+)")
TERMINAL = {"filled", "cancelled", "rejected", "expired", "aborted"}


def when(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def day(value):
    return when(value).astimezone(ET).date().isoformat()


def summarize(data):
    buckets = {}
    census = collections.defaultdict(list)
    decisions = collections.Counter()
    lines = collections.Counter()
    naive = collections.defaultdict(lambda: collections.Counter())
    credentials = collections.defaultdict(lambda: collections.Counter())
    searched = collections.defaultdict(list)
    biya = []
    for event in data["log_events"]:
        text = event["line"]
        current_day = day(event["at"])
        lines[current_day] += 1
        match = COUNTER.search(text)
        if match:
            minute, endpoint, success, failed, total, partial = match.groups()
            naive[(day(minute), endpoint)].update(total=int(total), failure=int(failed))
            key = (minute, endpoint)
            row = dict(success=int(success), failure=int(failed), total=int(total),
                       partial=int(partial), path=event["path"], line=event["line_number"])
            old = buckets.get(key)
            # partial=1 is a cumulative failure snapshot, not another set of calls.
            if old is None or (1 - row["partial"], row["total"]) > (
                1 - old["partial"], old["total"]
            ):
                buckets[key] = row
        if "BROKER-SYNC-CENSUS" in text:
            census[current_day].append(dict(
                at=event["at"], path=event["path"], line=event["line_number"],
                accounts=[dict(account=a, ok=int(ok), failed=int(f), consecutive=int(c))
                          for a, ok, f, c in CENSUS.findall(text)],
            ))
        if any(tag in text for tag in (
            "OMS-FLAT", "VIRTUAL-CLEAR", "OMS-CANCEL-UNCONFIRMED", "OMS-RESERVE",
            "BROKER-SYNC-UNREADABLE", "OMS-OCO-EXIT-MISS", "OMS-ENTRY-OWNERSHIP",
        )):
            decisions[current_day] += 1
            searched[current_day].append(dict(at=event["at"], path=event["path"],
                                             line=event["line_number"], text=text))
        if "missing Alpaca credentials for broker account " in text:
            credentials[current_day][text.rsplit("broker account ", 1)[-1]] += 1
        if "0e40052d917c" in text:
            biya.append(event)

    reports = {}
    for date in DAYS:
        endpoints = collections.defaultdict(lambda: dict(success=0, failure=0, total=0, minutes=0))
        dated_buckets = [(m, e, row) for (m, e), row in buckets.items() if day(m) == date]
        for minute, endpoint, row in dated_buckets:
            for key in ("success", "failure", "total"):
                endpoints[endpoint][key] += row[key]
            endpoints[endpoint]["minutes"] += 1
        failures = [x for x in data["cancel_417"] if day(x["at"]) == date]
        receipts = []
        for failure in failures:
            matching = [o for o in data["orders"] if o["client_order_id"] == failure["coid"]]
            ids = {o["id"] for o in matching}
            before = [e for e in data["order_events"] if e["order_id"] in ids
                      and when(e["event_at"]) <= when(failure["at"])]
            terminal_before = [e for e in before if e["event_type"].lower() in TERMINAL]
            proofs_before = [p for p in data.get("terminal_proofs", [])
                             if p["client_order_id"] == failure["coid"]
                             and when(p["acquired_at"]) <= when(failure["at"])]
            receipts.append(dict(
                **failure, matching_order_rows=len(matching),
                terminal_events_before=terminal_before,
                terminal_detail_proofs_before=proofs_before,
                day_list_status_at_failure="UNMEASURED: response body not retained",
            ))
        reports[date] = dict(
            structured_lines=lines[date], endpoints=dict(endpoints),
            counter_first_minute=min((x[0] for x in dated_buckets), default=None),
            counter_last_minute=max((x[0] for x in dated_buckets), default=None),
            census_windows=len(census[date]),
            census_live_orb_zero=[c for c in census[date] for a in c["accounts"]
                                  if a["account"] == "live:orb" and a["ok"] == 0],
            decision_lines_searched=decisions[date],
            decision_minutes_searched=len({x["at"][:16] for x in searched[date]}),
            search_receipts=searched[date],
            missing_credentials=dict(credentials[date]),
            naive_cumulative_sums={ep: dict(counts) for (date_key, ep), counts in naive.items()
                                   if date_key == date},
            counterfactual_changed_decisions="UNMEASURED: no historical positions snapshots",
            actual_saved_calls="UNMEASURED: no per-read state/due-time trace",
            cancel_417_count=len(failures), cancel_receipts=receipts,
            exit_child_cancel_failures=sum(bool(re.search(r"-protect-.+[TS]$", f["coid"] or ""))
                                          for f in failures),
        )
    # Capacity illustration only: one continuously flat, credentialed account, no direct reads.
    capacity = dict(
        model="not a replay; continuously flat account, ideal timers, 24h",
        baseline_15_seconds=24 * 60 * 4,
        proposed_60_day_300_night=16 * 60 + 8 * 60 // 5,
        maximum_saved=24 * 60 * 4 - (16 * 60 + 8 * 60 // 5),
    )
    return dict(as_of_utc=data["as_of_utc"], source_files=data["source_files"],
                queries=data["queries"], days=reports, biya=biya, flat_capacity_model=capacity,
                population=dict(orders=len(data["orders"]), order_events=len(data["order_events"]),
                                fills=len(data["fills"]), managed=len(data["managed"])))


if __name__ == "__main__":
    source = Path(sys.argv[1])
    result = summarize(json.loads(source.read_text()))
    result["raw_pull_path"] = str(source)
    result["raw_pull_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    print(json.dumps(result, indent=2, sort_keys=True))
