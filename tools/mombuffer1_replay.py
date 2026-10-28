"""MAIN ONLY after review/serial claim: local actual-input executor comparison.

No provider, application service, broker or database is instantiated. Durable
ports are controlled recording ports, not proof of the production CAS or fills.
Missing historical quote/read/owner checkpoints yield UNKNOWN, never synthesis.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys


class MissingEvidence(ValueError):
    pass


def read_packet(path):
    manifest = json.loads(path.read_text())
    if manifest.get("schema") != "MOMBUFFER1-REPLAY-1":
        raise MissingEvidence("unsupported manifest schema")
    inputs = {}
    for kind in ("signals", "quotes"):
        record = manifest["files"][kind]
        source = (path.parent / record["path"]).resolve()
        if not source.is_relative_to(path.parent.resolve()):
            raise MissingEvidence("input escapes packet directory")
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise MissingEvidence("input hash mismatch: " + kind)
        inputs[kind] = [json.loads(line) for line in raw.splitlines() if line.strip()]
    signals = inputs["signals"]
    identities = [row["record_id"] for row in signals]
    if len(set(identities)) != len(identities) or len(signals) != manifest["denominator"]:
        raise MissingEvidence("signal denominator/identity mismatch")
    if manifest.get("since") != "2026-10-15" or not manifest.get("cutoff"):
        raise MissingEvidence("since/cutoff missing")
    if manifest.get("declared_complete") is not True or not manifest.get("coverage_provenance"):
        raise MissingEvidence("full outcome denominator provenance missing")
    mode = manifest.get("mode", "RETAINED_HISTORY")
    if mode not in {"RETAINED_HISTORY", "TAPE_CONTROLLED_CLOCK"}:
        raise MissingEvidence("unsupported replay mode")
    if mode == "TAPE_CONTROLLED_CLOCK":
        bind_controlled_packet(manifest, signals, inputs["quotes"])
    indexed = {row["record_id"]: row for row in signals}
    for case in ("SPHL_2026-10-21", "FRGT_2026-10-19", "WORX_2026-10-19"):
        incident = indexed.get(manifest.get("incident_record_ids", {}).get(case))
        symbol, day = case.split("_", 1)
        signal = incident.get("signal", {}) if incident else {}
        if incident is None or signal.get("symbol") != symbol or signal.get("session_date") != day:
            raise MissingEvidence("required actual incident missing: " + case)
    quotes = {row["quote_id"]: row for row in inputs["quotes"]}
    if len(quotes) != len(inputs["quotes"]):
        raise MissingEvidence("duplicate quote identity")
    return manifest, signals, quotes


def bind_controlled_packet(manifest, signals, quotes):
    if __package__:
        from .mombuffer1_export_inputs import bind_exports
    else:
        from mombuffer1_export_inputs import bind_exports

    if not manifest.get("controlled_clock_protocol") or not manifest.get("reviewer_disposition"):
        raise MissingEvidence("controlled-clock protocol/reviewer disposition absent")
    raw = bind_exports(manifest["raw_export_directory"])
    originals = {row["raw"]["signal_logical_id"]: row for row in raw["signals"]}
    if {row["record_id"] for row in signals} != set(originals):
        raise MissingEvidence("controlled packet must retain all 94 actual signals")
    actual_quotes = {(q["source_pointer"]["file"], q["source_pointer"]["csv_record"]): q
                     for episode in raw["episodes"] for q in episode["quote_rows"]}
    primaries = {episode["episode_id"]: episode["primary"]["raw"]["signal_logical_id"]
                 for episode in raw["episodes"]}
    for row in signals:
        original = originals[row["record_id"]]
        if row.get("raw_signal_pointer") != original["source_pointer"]:
            raise MissingEvidence("actual signal source pointer mismatch")
        source = original["raw"]
        row["actual_outcome"] = source["outcome"]
        row["actual_symbol"] = source["symbol"]
        row["actual_episode_id"] = source["episode_id"]
        row["controlled_primary"] = primaries.get(source["episode_id"]) == row["record_id"]
        row["replay_mode"] = "TAPE_CONTROLLED_CLOCK"
        if row.get("signal"):
            signal = row["signal"]
            if (signal.get("symbol") != source["symbol"] or signal.get("session_date") != source["session_date"]
                    or Decimal(signal["signal_price"]) != Decimal(source["signal_price"])
                    or signal.get("source_id") != source["signal_logical_id"]):
                raise MissingEvidence("controlled Signal changed actual signal identity/price")
        if row["controlled_primary"] and not row.get("scenario_setup_assumptions"):
            raise MissingEvidence("declare missing Signal/owner/control setup as assumptions")
    for quote in quotes:
        pointer = quote["raw_quote_pointer"]
        actual = actual_quotes.get((pointer["file"], pointer["csv_record"]))
        if actual is None or quote.get("raw_sha256") != raw["quote_sha256"][pointer["file"]]:
            raise MissingEvidence("controlled quote source/hash mismatch")
        original = actual["raw"]
        if (Decimal(str(quote.get("ask"))) != Decimal(original["ask"])
                or str(quote.get("sip_timestamp_ns")) != original["sip_timestamp_ns"]
                or quote.get("symbol") != pointer["file"].split("-QUOTES-", 1)[1].rsplit("-", 3)[0]):
            raise MissingEvidence("controlled quote differs from actual tape")
        quote["episode_id"] = original["episode_id"]


async def replay_row(row, quotes):
    from project_mai_tai.broker_adapters.protocols import ExecutionReport
    from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal, Signal
    from project_mai_tai.momentum_live.execution import ExecutionPorts, LiveEntryExecutor

    controlled = row.get("replay_mode") == "TAPE_CONTROLLED_CLOCK"
    result = {"record_id": row["record_id"], "actual_outcome": row.get("actual_outcome", "UNKNOWN"),
              "symbol": row.get("signal", {}).get("symbol", row.get("actual_symbol", "UNKNOWN")),
              "wire_count": 0, "status": "UNKNOWN", "actual_cache_replay_proof": False,
              "mode": "TAPE_CONTROLLED_CLOCK" if controlled else "RETAINED_HISTORY"}
    sent, commits = [], []
    try:
        binding = "CONTROLLED_ASSUMPTION" if controlled else "RETAINED"
        if controlled and not row["controlled_primary"]:
            raise MissingEvidence("handoff/merge outcome retained; joint owner/ledger counterfactual UNKNOWN")
        if row.get("quote_read_binding") != binding:
            raise MissingEvidence("historical recheck ask/read identity UNKNOWN")
        if row.get("owner_context_binding") != binding or row.get("control_context_binding") != binding:
            raise MissingEvidence("pre-admission owner/control context UNKNOWN")
        if row.get("schedule_basis") != binding:
            raise MissingEvidence("post-await clock/quote schedule UNKNOWN")
        schedule = iter(row["checkpoints"])
        current = {}

        def advance(stage):
            try:
                point = next(schedule)
            except StopIteration:
                raise MissingEvidence("missing checkpoint: " + stage) from None
            if point["stage"] != stage or point.get("clock_provenance") != binding:
                raise MissingEvidence("checkpoint stage/clock unknown: " + stage)
            clock = datetime.fromisoformat(point["at"])
            if clock.tzinfo is None:
                raise MissingEvidence("naive checkpoint time")
            quote = quotes.get(point.get("quote_id"))
            if quote is None and point.get("quote_missing_assumption" if controlled else "quote_missing_proven") is not True:
                raise MissingEvidence("quote checkpoint absent, not proven missing")
            if controlled and quote is not None and quote["episode_id"] != row["actual_episode_id"]:
                raise MissingEvidence("controlled quote bound to another episode")
            current.update(clock=clock, quote=quote)

        def quote(symbol):
            raw = current["quote"]
            if raw is None:
                return None
            if raw["symbol"] != symbol or raw.get("received_at_provenance") != binding:
                raise MissingEvidence("raw quote arrival/identity UNKNOWN")
            stamp = datetime.fromisoformat(raw["received_at"])
            if stamp.tzinfo is None:
                raise MissingEvidence("naive raw quote arrival")
            return {"ask": raw.get("ask"), "received_at": stamp}

        raw_signal = dict(row["signal"])
        raw_signal["signal_price"] = Decimal(raw_signal["signal_price"])
        signal = Signal(**raw_signal)
        if not signal.country_route or not signal.country_evidence_json:
            raise MissingEvidence("sealed country signal absent")
        book = EpisodeLedger.restore(row["book_before"])
        durable = {}

        async def foreign_owner(*args):
            return row["foreign_owner"]

        async def claim(snapshot, *args):
            if row["durable_claim"] is True:
                durable["episode"] = dict(snapshot)
            return row["durable_claim"]

        async def prepare(identity, request, provider):
            if "request" in durable:
                raise AssertionError("second order for episode")
            durable["request"] = request
            advance("after_durable")

        async def save(snapshot):
            durable["episode"] = dict(snapshot)
            if snapshot["state"] == "submitting":
                advance("after_submission")

        async def reprice(previous, replacement, episode=None):
            from project_mai_tai.momentum_live.admission import Route
            from project_mai_tai.momentum_live.entry_reprice import validate_entry_reprice
            if durable.get("request") != previous:
                raise AssertionError("recording port preimage mismatch")
            validate_entry_reprice(previous, replacement, Route(signal.country_route))
            durable["request"] = replacement
            commits.append({"quantity": str(replacement.quantity), "limit": replacement.metadata["limit_price"]})
            if episode is not None:
                durable["episode"] = dict(episode)
            advance("after_submission" if episode is not None else "after_reprice")

        async def settle(request, reason, context):
            if durable.get("request") != request:
                raise AssertionError("settlement did not use last committed request")
            durable["settled"] = reason

        async def submit(request):
            if durable.get("request") != request:
                raise AssertionError("persisted payload differs from dispatched payload")
            sent.append(request)
            # No fabricated broker ACK. Only the local submit invocation is recorded.
            return [ExecutionReport("rejected", request.client_order_id, origin="unknown", reason="LOCAL_RECORD_ONLY")]

        async def record(*args):
            return {}

        async def route_report(*args):
            return None

        def control_metadata(signal):
            if current.get("quote") is not None and current["quote"].get("control_refusal"):
                raise Refusal(current["quote"]["control_refusal"])
            return dict(row["entry_metadata"])

        def route_for(*args):
            raise AssertionError("sealed country route was reselected")

        advance("initial")
        ports = ExecutionPorts(lambda: current["clock"], quote, route_for, foreign_owner, claim, prepare,
                               save, submit, record, route_report, entry_metadata=control_metadata,
                               refuse_unsent_entry=settle)
        if hasattr(ports, "reprice_unsent_entry"):
            ports.reprice_unsent_entry = reprice
        executor = LiveEntryExecutor(book, ports, schwab_account=row["schwab_account"],
                                     webull_account=row["webull_account"])
        try:
            await executor.admit(signal)
            result.update(status="SEND" if sent else "NO_NEW_EPISODE")
        except Refusal as exc:
            result.update(status="REFUSED", reason=str(exc))
        if len(sent) > 1:
            raise AssertionError("multiple sends")
        result.update(wire_count=len(sent), commits=commits,
                      no_native_acceptance_claim=True, ports="CONTROLLED_RECORDING_NOT_PRODUCTION_PERSISTENCE")
        if sent:
            request = sent[0]
            result.update(limit=request.metadata["limit_price"], quantity=str(request.quantity),
                          cap=request.metadata["cap_price"], client_order_id=request.client_order_id)
            if Decimal(result["limit"]) > Decimal(result["cap"]):
                raise AssertionError("cap exceeded")
    except (MissingEvidence, KeyError, TypeError, ValueError) as exc:
        result.update(status="UNKNOWN", reason=str(exc), wire_count=len(sent), commits=commits)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--export-directory", type=Path,
                        help="bind landed CSVs only; no application imports or replay claims")
    parser.add_argument("--before-report", type=Path)
    args = parser.parse_args()
    if args.export_directory:
        if args.source_root or args.manifest or args.before_report:
            parser.error("export binding cannot be combined with executor replay")
        if __package__:
            from .mombuffer1_export_inputs import bind_exports
        else:
            from mombuffer1_export_inputs import bind_exports
        print(json.dumps(bind_exports(args.export_directory), indent=2, sort_keys=True))
        return
    if not args.source_root or not args.manifest:
        parser.error("executor replay requires --source-root and --manifest")
    manifest, signals, quotes = read_packet(args.manifest.resolve())
    head = subprocess.check_output(["git", "-C", str(args.source_root), "rev-parse", "HEAD"], text=True).strip()
    sys.path.insert(0, str(args.source_root.resolve() / "src"))

    def no_network(*args, **kwargs):
        raise RuntimeError("MOMBUFFER1 replay forbids network")

    socket.socket.connect = no_network
    rows = asyncio.run(run(signals, quotes))
    comparison = []
    if args.before_report:
        before = json.loads(args.before_report.read_text())
        if before["manifest_sha256"] != hashlib.sha256(args.manifest.read_bytes()).hexdigest():
            raise MissingEvidence("before/after manifest mismatch")
        prior = {row["record_id"]: row for row in before["rows"]}
        if set(prior) != {row["record_id"] for row in rows}:
            raise MissingEvidence("before/after denominator mismatch")
        for row in rows:
            old = prior[row["record_id"]]
            forbidden = old.get("reason") in {"rounded_ask_above_cap", "quote_missing_future_or_stale"}
            comparison.append({"record_id": row["record_id"], "before": old["status"], "after": row["status"],
                               "new_cap_or_stale_send": forbidden and row["wire_count"] > 0})
    print(json.dumps({"head": head, "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
                      "scope": "ACTUAL_INPUT_EXECUTOR_RECORDING_PORTS_ONLY", "denominator": len(signals),
                      "mode": manifest.get("mode", "RETAINED_HISTORY"),
                      "controlled_clock_protocol": manifest.get("controlled_clock_protocol"),
                      "reviewer_disposition": manifest.get("reviewer_disposition"),
                      "historical_cache_outcomes": "UNKNOWN unless independently retained; never inferred from SIP",
                      "declared_complete": manifest["declared_complete"], "rows": rows,
                      "before_after": comparison, "unknown": sum(row["status"] == "UNKNOWN" for row in rows)},
                     indent=2, sort_keys=True))


async def run(signals, quotes):
    return [await replay_row(row, quotes) for row in signals]


if __name__ == "__main__":
    main()
