"""MUST1: retained-record PRE/POST call-boundary proof; no native admission claim.

Only the exact _offer_live_signals AST is compiled. Neither source module is
imported. The recording handoff does not route, admit, submit or accept orders.
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
import subprocess
from types import SimpleNamespace
from zoneinfo import ZoneInfo


PRE_REF = "99fcbeaacb8e8d9a487ad5534a3862c488fcbdb1"
APP = "src/project_mai_tai/services/momentum_paper_app.py"
EXPORT = Path("/Users/velkris/.codex/task-queues/analysis/MOMOLD1-EXPORT-1020")
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
    def __init__(self):
        self.rows = []

    def offer(self, record, *, day_volume, now_ns):
        self.rows.append({"symbol": record.symbol, "strategy": record.strategy_code,
            "trigger": str(record.payload["detect"]["price"]), "ns": now_ns,
            "day_volume": day_volume, "logical_id": record.logical_id,
            "retained_payload_sha256": sha(json.dumps(record.payload, sort_keys=True).encode())})
        return "RECORDED_CALL_NOT_NATIVE_ACCEPTANCE"


def record_calls(method, records, *, cache_case, stale_seconds):
    recorder = RecordingHandoff()
    for record in records:
        now_ns = record.payload["observed_decision_ns"]
        now = datetime.fromtimestamp(now_ns // 1_000_000_000, UTC).replace(
            microsecond=(now_ns % 1_000_000_000) // 1000)
        cache = {}
        if cache_case != "absent":
            age = {"fresh": 0, "at_boundary": stale_seconds,
                   "stale": stale_seconds + 1, "future": -1}[cache_case]
            cache[record.symbol] = (123456, now - timedelta(seconds=age))
        context = SimpleNamespace(_live_handoff=recorder, _clock=lambda: now,
                                  _live_day_volumes=cache)
        method(context, [record], now_ns)
    return json.dumps(recorder.rows, sort_keys=True, separators=(",", ":")).encode()


def compare(pre, post, records):
    before, before_meta = offer_method(pre)
    after, after_meta = offer_method(post)
    cases = {}
    for cache_case in ("absent", "fresh", "at_boundary", "stale", "future"):
        left = record_calls(before, records, cache_case=cache_case,
                            stale_seconds=before_meta["snapshot_stale_seconds"])
        right = record_calls(after, records, cache_case=cache_case,
                             stale_seconds=before_meta["snapshot_stale_seconds"])
        cases[cache_case] = {"equal": left == right, "pre_sha256": sha(left),
                            "post_sha256": sha(right), "pre": json.loads(left), "post": json.loads(right)}
    equal = (before_meta["method_sha256"] == after_meta["method_sha256"]
             and before_meta["ast_sha256"] == after_meta["ast_sha256"]
             and before_meta["snapshot_stale_seconds"] == after_meta["snapshot_stale_seconds"]
             and all(row["equal"] for row in cases.values()))
    return {"equal": equal, "pre": before_meta, "post": after_meta, "records": len(records),
            "cache_cases": cases, "scope": "RECORDING_HANDOFF_CALL_BYTES_ONLY",
            "controlled_inputs": "now=retained sealed decision ns; cache volumes/times SYNTH",
            "native_admission_or_order_acceptance": "NOT_TESTED"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--events", type=Path, default=EXPORT / "momentum_paper_events_1020.csv")
    parser.add_argument("--amod", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pre, post = source_pair(args.root)
    if sha(args.events.read_bytes()) != "30ab970778f89bcd1dcaff783f04a30a1c61735081658c3bce3db7d8d572cb1c":
        raise ValueError("actual 10-20 event export checksum mismatch")
    records = load_records(args.events)
    if any(record.session_date != date(2026, 10, 20) for record in records):
        raise ValueError("wrong retained 10-20 session")
    report = {"pre_ref": PRE_REF, "10_20": compare(pre, post, records),
              "10_20_input_sha256": sha(args.events.read_bytes())}
    if args.amod.is_file():
        amod = [record for record in load_records(args.amod) if record.symbol == "AMOD"
                and datetime.fromtimestamp(record.payload["observed_decision_ns"] / 1e9, UTC)
                .astimezone(ET).strftime("%Y-%m-%d %H:%M") == "2026-10-21 08:15"]
        if not amod:
            raise ValueError("exact retained AMOD 10-21 08:15 warning DETECTED row missing")
        report["AMOD"] = {**compare(pre, post, amod), "input_sha256": sha(args.amod.read_bytes())}
    else:
        report["AMOD"] = {"status": "BLOCKED_MISSING_ACTUAL_RETAINED_PAYLOAD", "path": str(args.amod)}
    complete = report["10_20"]["equal"] and report["AMOD"].get("equal") is True
    report["must1"] = "PASS_CALL_BOUNDARY_ONLY" if complete else "BLOCKED_OR_DIFFERENT"
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
