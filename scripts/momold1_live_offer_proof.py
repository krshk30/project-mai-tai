"""MUST1: retained-record PRE/POST call-boundary proof; no native admission claim.

The service method is compiled from exact PRE/POST ASTs. AMOD additionally uses
the real LiveHandoff/EpisodeLedger offer path, with recorded SEC route context.
No worker, provider, Redis emission or native order is started.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from zoneinfo import ZoneInfo


PRE_REF = "99fcbeaacb8e8d9a487ad5534a3862c488fcbdb1"
APP = "src/project_mai_tai/services/momentum_paper_app.py"
EXPORT = Path("/Users/velkris/.codex/task-queues/analysis/MOMOLD1-EXPORT-1020")
AMOD_EXPORT = Path("/Users/velkris/.codex/task-queues/analysis/MOMOLD1-EXPORT-AMOD-1021")
AMOD_FILES = {
    "events_amod_1021.csv": "1ca01bf7c6987cc634f04e37553df3b97da8e8f87453edbad32d55ea5c320490",
    "quotes_amod_0800_0840.csv.gz": "2238484af91944f74f6cf532ddae0925ac8857722667f55ab37bd6f98612bd8c",
    "trades_amod_0800_0840.csv.gz": "664f28607dc8a456e10545ac58c60a027f3383c2e79795e0757a61a85809756f",
}
AMOD_MANIFEST_SHA256 = "80146ce21aab333ed9088efe34348278cea20407895675b822d207ab25d4111f"
AMOD_LOG_SHA256 = "e34955bd5f28f28a04b402e98da8400d98505ded5fc17a4fc1d39f32cf49d46b"
ET = ZoneInfo("America/New_York")
WARNING_CODES = {"momentum_30s_warn", "momentum_60s_warn"}
CSV_MAX_FIELD_BYTES = 8 * 1024 * 1024


class _BoundedCsvReader(csv.DictReader):
    def __next__(self):
        row = super().__next__()
        if None in row or any(value is None for value in row.values()):
            raise csv.Error("CSV row does not match header")
        for value in row.values():
            if len(value.encode("utf-8")) > CSV_MAX_FIELD_BYTES:
                raise csv.Error("CSV field exceeds explicit 8 MiB UTF-8 byte limit")
        return row


@contextmanager
def bounded_csv_reader(source):
    # Parser character count and decoded UTF-8 byte count are separately bounded.
    previous = csv.field_size_limit(CSV_MAX_FIELD_BYTES)
    try:
        reader = _BoundedCsvReader(source, strict=True)
        if any(len(value.encode("utf-8")) > CSV_MAX_FIELD_BYTES for value in reader.fieldnames or []):
            raise csv.Error("CSV header field exceeds explicit 8 MiB UTF-8 byte limit")
        yield reader
    finally:
        csv.field_size_limit(previous)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def source_pair(root):
    pre = subprocess.run(["git", "show", f"{PRE_REF}:{APP}"], cwd=root,
                         check=True, capture_output=True).stdout
    return pre, (root / APP).read_bytes()


def handoff_source_proof(root):
    result = {}
    for path in ("src/project_mai_tai/momentum_live/handoff.py", "src/project_mai_tai/momentum_live/admission.py"):
        before = subprocess.run(["git", "show", f"{PRE_REF}:{path}"], cwd=root,
                                check=True, capture_output=True).stdout
        after = (root / path).read_bytes()
        result[path] = {"pre_sha256": sha(before), "post_sha256": sha(after), "equal": before == after}
    return {"equal": all(row["equal"] for row in result.values()), "files": result}


def amod_log_context(value):
    routes = re.findall(
        r"\[MOMLIVE1-WARN-ROUTE\] symbol=AMOD route=(\w+) reason=(\w+) status=(\w+) "
        r"country=([^ ]+) as_of=([^ ]+) day_volume=(\d+) informational_only=true", value)
    refusals = [line for line in value.splitlines()
                if "[MOMLIVE1-REFUSED] symbol=AMOD reason=day_volume_below_50000" in line]
    if len(routes) != 1 or len(refusals) != 2:
        raise ValueError("exact AMOD route and two recorded low-volume refusals required")
    broker, reason, status, country, as_of, volume = routes[0]
    if (broker, reason, status, int(volume)) != ("schwab", "SEC_US", "READY", 15361):
        raise ValueError("AMOD retained route/volume context changed")
    return {"broker": broker, "reason": reason, "status": status, "country": country,
            "as_of": as_of, "day_volume": int(volume), "recorded_refusals": refusals,
            "is_us": "UNKNOWN_NOT_RETAINED_AS_BOOLEAN", "cik": "UNKNOWN",
            "provenance": "UNVERIFIED_LOG_TEXT_UNTIL_PACKET_HASH_CHECK"}


def verified_amod_packet(events):
    root = events.parent
    if events.name != "events_amod_1021.csv":
        raise ValueError("delivered AMOD CSV basename required")
    manifest = (root / "SHA256SUMS").read_bytes()
    if sha(manifest) != AMOD_MANIFEST_SHA256:
        raise ValueError("AMOD manifest checksum mismatch")
    declared = {}
    for line in manifest.decode("ascii").splitlines():
        checksum, name = line.split(maxsplit=1)
        declared[name.lstrip("*")] = checksum
    if declared != AMOD_FILES:
        raise ValueError("AMOD manifest must match all three delivered files")
    for name, checksum in AMOD_FILES.items():
        if sha((root / name).read_bytes()) != checksum:
            raise ValueError(f"AMOD input checksum mismatch: {name}")
    log_bytes = (root / "amod_live_log_lines.txt").read_bytes()
    if sha(log_bytes) != AMOD_LOG_SHA256:
        raise ValueError("AMOD separately pinned log checksum mismatch")
    context = amod_log_context(log_bytes.decode("utf-8"))
    context["provenance"] = "ACTUAL_HASH_VERIFIED_AMOD_WARN_ROUTE_LOG"
    with events.open(encoding="utf-8", newline="") as handle, bounded_csv_reader(handle) as reader:
        if len(list(reader)) != 9:
            raise ValueError("AMOD retained event count must remain nine")
    records = [record for record in load_records(events) if record.symbol == "AMOD"
        and record.session_date == date(2026, 10, 21)
        and datetime.fromtimestamp(record.payload["observed_decision_ns"] / 1e9, UTC)
        .astimezone(ET).strftime("%Y-%m-%d %H:%M") == "2026-10-21 08:15"]
    if len(records) != 2 or {record.strategy_code for record in records} != WARNING_CODES:
        raise ValueError("both actual AMOD 08:15 sealed warning DETECTED rows required")
    return records, context, {"manifest_sha256": sha(manifest), "files": dict(AMOD_FILES),
        "separate_log_sha256": sha(log_bytes), "log_is_in_three_file_manifest": False,
        "retained_event_rows": 9, "selected_detected_rows": len(records),
        "raw_quotes_trades_use": "HASH_VERIFICATION_ONLY_NOT_NATIVE_FILL_PARITY"}


def offer_method(source):
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "MomentumPaperService")
    node = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                and node.name == "_offer_live_signals")
    raw = b"".join(source.splitlines(keepends=True)[node.lineno - 1:node.end_lineno])
    constant = next(node for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "_SNAPSHOT_STALE_SECONDS"
                            for target in node.targets))
    stale_seconds = ast.literal_eval(constant.value)
    wrapper = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0), node], type_ignores=[])
    namespace = {"_ET": ET, "_SNAPSHOT_STALE_SECONDS": stale_seconds}
    exec(compile(ast.fix_missing_locations(wrapper), "<exact retained offer method>", "exec"), namespace)
    return namespace["_offer_live_signals"], {
        "method_sha256": sha(raw), "ast_sha256": sha(ast.dump(node, include_attributes=False).encode()),
        "source_file_sha256": sha(source), "method_lines": [node.lineno, node.end_lineno],
        "snapshot_stale_seconds": stale_seconds,
    }


def load_records(path):
    if path.suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as handle, bounded_csv_reader(handle) as reader:
            rows = list(reader)
    else:
        rows = json.loads(path.read_text())
        if isinstance(rows, dict):
            rows = rows["rows"]
    result = []
    for row in rows:
        if row["event_type"] != "DETECTED" or row["strategy_code"] not in WARNING_CODES:
            continue
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        # No fallback to current time or invented detection/reference payloads.
        observed_ns = payload["observed_decision_ns"]
        if type(observed_ns) is not int or observed_ns <= 0:
            raise ValueError("retained observed_decision_ns required")
        Decimal(str(payload["detect"]["price"]))
        result.append(SimpleNamespace(**{**row, "payload": payload, "session_date": date.fromisoformat(row["session_date"])}))
    if not result:
        raise ValueError("no retained warning DETECTED records")
    return result


class RecordingHandoff:
    def __init__(self, delegate=None):
        self.rows = []
        self.delegate = delegate

    def offer(self, record, *, day_volume, now_ns):
        row = {"symbol": record.symbol, "strategy": record.strategy_code,
            "trigger": str(record.payload["detect"]["price"]), "ns": now_ns,
            "day_volume": day_volume, "logical_id": record.logical_id,
            "retained_payload_sha256": sha(json.dumps(record.payload, sort_keys=True).encode())}
        if self.delegate is not None:
            row["handoff_result"] = self.delegate.offer(record, day_volume=day_volume, now_ns=now_ns)
        self.rows.append(row)
        return row.get("handoff_result", "RECORDED_CALL_NOT_NATIVE_ACCEPTANCE")


def record_calls(method, records, *, cache_case, stale_seconds, day_volume=123456, recorder=None):
    recorder = recorder if recorder is not None else RecordingHandoff()
    for record in records:
        now_ns = record.payload["observed_decision_ns"]
        now = datetime.fromtimestamp(now_ns // 1_000_000_000, UTC).replace(
            microsecond=(now_ns % 1_000_000_000) // 1000)
        cache = {}
        if cache_case != "absent":
            age = {"fresh": 0, "at_boundary": stale_seconds,
                   "stale": stale_seconds + 1, "future": -1}[cache_case]
            cache[record.symbol] = (day_volume, now - timedelta(seconds=age))
        context = SimpleNamespace(_live_handoff=recorder, _clock=lambda: now,
                                  _live_day_volumes=cache)
        method(context, [record], now_ns)
    return json.dumps(recorder.rows, sort_keys=True, separators=(",", ":")).encode()


def compare(pre, post, records, *, day_volume=123456, handoff_factory=None):
    before, before_meta = offer_method(pre)
    after, after_meta = offer_method(post)
    cases = {}
    for cache_case in ("absent", "fresh", "at_boundary", "stale", "future"):
        left_recorder = RecordingHandoff(handoff_factory() if handoff_factory is not None else None)
        right_recorder = RecordingHandoff(handoff_factory() if handoff_factory is not None else None)
        left = record_calls(before, records, cache_case=cache_case,
                            stale_seconds=before_meta["snapshot_stale_seconds"], day_volume=day_volume, recorder=left_recorder)
        right = record_calls(after, records, cache_case=cache_case,
                             stale_seconds=before_meta["snapshot_stale_seconds"], day_volume=day_volume, recorder=right_recorder)
        cases[cache_case] = {"equal": left == right, "pre_sha256": sha(left),
                            "post_sha256": sha(right), "pre": json.loads(left), "post": json.loads(right)}
        if handoff_factory is not None:
            cases[cache_case]["real_handoff_state"] = [
                {"queue_size": recorder.delegate.queue.qsize(),
                 "episode_count": len(recorder.delegate.ledger.episodes),
                 "counts": dict(recorder.delegate.counts)}
                for recorder in (left_recorder, right_recorder)]
    equal = (before_meta["method_sha256"] == after_meta["method_sha256"]
             and before_meta["ast_sha256"] == after_meta["ast_sha256"]
             and before_meta["snapshot_stale_seconds"] == after_meta["snapshot_stale_seconds"]
             and all(row["equal"] for row in cases.values()))
    return {"equal": equal, "pre": before_meta, "post": after_meta, "records": len(records),
            "cache_cases": cases, "scope": "RECORDING_HANDOFF_CALL_BYTES_ONLY",
            "controlled_inputs": "now=retained sealed decision ns; cache times CONTROLLED_NOT_HISTORICAL",
            "cache_volume": day_volume, "cache_volume_provenance": "SYNTH_CONTROL_ONLY",
            "native_admission_or_order_acceptance": "NOT_TESTED"}


class NoRedisIO:
    def __getattr__(self, name):
        raise AssertionError(f"Redis/provider IO forbidden in pure handoff proof: {name}")


def real_handoff(context):
    from project_mai_tai.momentum_live.handoff import LiveHandoff
    from project_mai_tai.momentum_live.admission import EpisodeLedger

    target = LiveHandoff(NoRedisIO(), SimpleNamespace(momentum_live_enabled=True), route_for=lambda symbol, now:
        SimpleNamespace(broker=context["broker"], reason=context["reason"], status=context["status"],
                        as_of=datetime.fromisoformat(context["as_of"])))
    # Controlled empty/restored-ready owner book, not an assertion about native or
    # historical OMS ownership. Real Signal.validate rejects low volume before claim.
    target.ledger = EpisodeLedger.restore([])
    return target


def compare_amod(pre, post, records, context):
    result = compare(pre, post, records, day_volume=context["day_volume"],
                     handoff_factory=lambda: real_handoff(context))
    cases_ok = True
    for name, case in result["cache_cases"].items():
        expected = "day_volume_below_50000" if name in {"fresh", "at_boundary"} else "day_volume_unknown"
        case["expected_refusal"] = expected
        case["refusal_matches"] = all(len(case[side]) == len(records)
            and all(row["handoff_result"] == expected for row in case[side]) for side in ("pre", "post"))
        case["no_claim_or_emission"] = all(state["queue_size"] == 0 and state["episode_count"] == 0
                                             for state in case["real_handoff_state"])
        cases_ok = cases_ok and case["refusal_matches"] and case["no_claim_or_emission"]
    result.update(equal=result["equal"] and cases_ok, recorded_route_context=context,
        scope="REAL_LIVEHANDOFF_EPISODELEDGER_PURE_OFFER_AND_REFUSAL_ONLY",
        cache_volume_provenance=context["provenance"],
        unknown_evidence=["original_cache_observed_at", "original_cache_freshness", "native_acceptance",
                          "historical_owner_book", "exact_handoff_callback_clock"],
        limits="Fresh/boundary cases must reproduce recorded low-volume refusal; cache times controlled, not historical reconstruction")
    return result


def run_must1(root, events, amod_events):
    pre, post = source_pair(root)
    components = handoff_source_proof(root)
    if sha(events.read_bytes()) != "30ab970778f89bcd1dcaff783f04a30a1c61735081658c3bce3db7d8d572cb1c":
        raise ValueError("actual 10-20 event export checksum mismatch")
    records = load_records(events)
    if any(record.session_date != date(2026, 10, 20) for record in records):
        raise ValueError("wrong retained 10-20 session")
    amod, context, packet = verified_amod_packet(amod_events)
    report = {"pre_ref": PRE_REF, "handoff_and_admission_source_proof": components,
              "10_20": compare(pre, post, records), "10_20_input_sha256": sha(events.read_bytes()),
              "AMOD": {**compare_amod(pre, post, amod, context), "packet": packet}}
    complete = components["equal"] and report["10_20"]["equal"] and report["AMOD"]["equal"]
    report["must1"] = "PASS_RETAINED_CALLS_AND_AMOD_LOW_VOLUME_PURE_HANDOFF" if complete else "BLOCKED_OR_DIFFERENT"
    report["must3"] = "SEPARATE_MANDATORY_TONIGHT_COPY_GATE_NOT_RUN_BY_THIS_COMMAND"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--events", type=Path, default=EXPORT / "momentum_paper_events_1020.csv")
    parser.add_argument("--amod", type=Path, default=AMOD_EXPORT / "events_amod_1021.csv")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_must1(args.root, args.events, args.amod)
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    return 0 if report["must1"] == "PASS_RETAINED_CALLS_AND_AMOD_LOW_VOLUME_PURE_HANDOFF" else 1


if __name__ == "__main__":
    raise SystemExit(main())
