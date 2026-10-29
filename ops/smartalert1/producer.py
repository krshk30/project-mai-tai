"""SMART L4 finite producer; Main-bound five-path SOURCE successor.

Finite sealed FILE acquisition -> real L2/L3 -> same CAS shadow writer.
No provider, DB, process, environment, live clock, scheduler or phone port.
Input files are typed evidence exports, not unparsed SDK/log narratives.
"""

from contextlib import ExitStack
from copy import deepcopy
import argparse
import json
import os

from .delivery import (
    FRAME_BYTES, MAX_ITEMS, Refused, clock, encoded, fingerprint, hexhash,
    need, policy_binding, ref,
)
from .shadow_ledger import _directory, _input, _parse, _read, write_shadow

SCHEMA = "smartalert1.producer.v1"


def _evaluate(lane, evidence, policy, *, as_of):
    # Main must compose the reviewed source closure; there is no fallback to
    # a different evaluator, fabricated finding, or one-lane partial bundle.
    if lane == "L2":
        from .trade import evaluate_trade
        return evaluate_trade(evidence, policy, as_of=as_of)
    from .system_shadow import evaluate_system_shadow
    return evaluate_system_shadow(evidence, policy, as_of=as_of)


def producer_review(index, policy, *, as_of):
    """References resolve in a Main-sealed index; not self-issued approvals."""
    need(type(index) is dict and index.get("schema_version") == SCHEMA
         and index.get("mode") == "SHADOW", "producer_index_unknown")
    need(clock(index.get("as_of")) == clock(as_of), "producer_cutoff_changed")
    policy_binding(policy)
    need(index.get("source_bindings") == policy["source_bindings"], "producer_source_binding_mismatch")
    manifest = index.get("manifest")
    need(type(manifest) is dict and manifest.get("complete") is True
         and manifest.get("overflow") is False and manifest.get("read_error") is False,
         "producer_manifest_incomplete")
    need(clock(manifest.get("as_of")) == clock(as_of), "manifest_cutoff_changed")
    evidence = manifest.get("evidence")
    need(type(evidence) is dict and len(evidence) <= MAX_ITEMS, "producer_evidence_index_unknown")
    for key, value in evidence.items():
        ref(key)
        hexhash(value)
    review = policy.get("producer_review")
    need(type(review) is dict and review.get("status") == "APPROVED_SHADOW_OPERANDS",
         "producer_operands_needs_ruling")
    for key in ("approval_ref", "calibration_ref", "acquisition_ref", "schedule_ref", "source_closure_ref"):
        value = ref(review.get(key))
        need(value in evidence, "producer_review_evidence_missing")
    need(review["approval_ref"] == policy["approval_ref"]
         and review["calibration_ref"] == policy["calibration_ref"], "producer_review_policy_mismatch")
    need(review.get("severity_map_sha256") == fingerprint(policy["severity_map"], FRAME_BYTES),
         "reviewed_severity_map_changed")
    upstream = index.get("upstream_policies")
    need(type(upstream) is dict and set(upstream) == {"L2", "L3"}, "both_upstream_policies_required")
    hashes = review.get("upstream_policy_sha256")
    need(type(hashes) is dict and set(hashes) == {"L2", "L3"}, "upstream_policy_review_missing")
    for lane in ("L2", "L3"):
        selected = upstream[lane]
        need(type(selected) is dict and selected.get("enabled") is True
             and selected.get("mode") in ("REPORT_ONLY", "CANDIDATE_REPLAY", "QUALIFIED_LIVE")
             and selected.get("policyversion") == policy["upstream_policyversions"][lane],
             "upstream_policy_not_bound")
        need(hexhash(hashes[lane]) == fingerprint(selected, FRAME_BYTES), "upstream_policy_changed")
    # A referenced calendar alone is insufficient: resolve every explicit
    # session proof. No date, cadence, rank or cutoff is invented here.
    calendar = policy.get("close_calendar")
    need(type(calendar) is dict and calendar.get("schema_version") == "smartalert1.close-calendar.v1"
         and calendar.get("timezone") == "America/New_York"
         and calendar.get("late_policy") == "NO_BACKDATED_DIGEST", "close_calendar_needs_ruling")
    for key in ("calendar_ref", "approval_ref"):
        need(ref(calendar.get(key)) in evidence, "calendar_review_evidence_missing")
    windows = calendar.get("windows")
    need(type(windows) is list and 0 < len(windows) <= MAX_ITEMS, "calendar_windows_needs_ruling")
    seen = set()
    for window in windows:
        need(type(window) is dict, "calendar_window_unknown")
        key = ref(window.get("window_id"))
        need(key not in seen and type(window.get("open_session")) is bool, "calendar_window_ambiguous")
        seen.add(key)
        need(ref(window.get("session_proof_ref")) in evidence, "calendar_session_proof_missing")
        if window["open_session"]:
            from .delivery import reviewed_close_window
            reviewed_close_window(dict(window, calendar_ref=calendar["calendar_ref"]), manifest, policy)
    return manifest


def build_shadow_batch(index, frames, policy, *, as_of):
    """Pure typed assembly; frames are already hash-checked by FILE wrapper.

    Source-closure/approval refs are Main attestations; their actual production
    authority is not inferred from this pure function accepting a supplied ref.
    """
    encoded(index, FRAME_BYTES)
    encoded(frames, FRAME_BYTES)
    manifest = producer_review(index, policy, as_of=as_of)
    descriptors = index.get("frames")
    need(type(descriptors) is list and 0 < len(descriptors) <= MAX_ITEMS
         and type(frames) is list and len(frames) == len(descriptors), "producer_frames_unknown")
    need(all(type(row) is dict and row.get("lane") in ("L2", "L3") for row in descriptors)
         and {row["lane"] for row in descriptors} == {"L2", "L3"},
         "both_lanes_required_no_dead_module_bundle")
    present, findings, refs = set(), [], set()
    for descriptor, frame in zip(descriptors, frames):
        need(type(descriptor) is dict and descriptor.get("lane") in ("L2", "L3"), "frame_lane_unknown")
        lane = descriptor["lane"]
        present.add(lane)
        source_ref = ref(descriptor.get("source_ref"))
        need(source_ref not in refs, "duplicate_source_frame")
        refs.add(source_ref)
        digest = hexhash(descriptor.get("sha256"))
        need(manifest["evidence"].get(source_ref) == digest, "frame_manifest_hash_mismatch")
        need(type(frame) is dict and frame.get("schema_version") == "smartalert1.v1", "typed_envelope_required")
        need(fingerprint(frame, FRAME_BYTES) == hexhash(descriptor.get("canonical_sha256")),
             "typed_frame_changed")
        source_epoch = ref(frame.get("source_epoch"))
        expectation = ref(descriptor.get("expectation_ref"))
        rows = _evaluate(lane, frame, index["upstream_policies"][lane], as_of=as_of)
        need(type(rows) is list, "evaluator_result_unknown")
        for finding in rows:
            need(type(finding) is dict and finding.get("paging_eligible") is False,
                 "upstream_nonshadow_result")
            findings.append({"lane": lane, "finding": finding, "source_epoch": source_epoch,
                "expectation_ref": expectation, "source_ref": source_ref, "source_hash": digest,
                "evaluated_at": as_of})
            need(len(findings) <= MAX_ITEMS, "producer_finding_overflow")
    need(present == {"L2", "L3"}, "both_lanes_required_no_dead_module_bundle")
    result = {"schema_version": policy["schema_version"], "source_bindings": deepcopy(policy["source_bindings"]),
        "policy_sha256": policy_binding(policy), "batch_id": ref(index.get("batch_id")),
        "sequence": index.get("sequence"), "findings": findings, "manifest": deepcopy(manifest),
        "resolution_links": deepcopy(index.get("resolution_links", [])),
        "digest_window": deepcopy(index.get("digest_window"))}
    encoded(result, FRAME_BYTES)
    return result


def produce_shadow(*, mode="OFF", input_dir=None, index_name=None, index_sha256=None,
                   policy_path=None, policy_sha256=None, as_of=None, ledger_dir=None,
                   expected_revision=None, expected_generation_sha256=None,
                   initialization_path=None, initialization_sha256=None, request_sha256=None):
    """One finite supplied-FILE acquisition; no discovery/clock/loop/network.

    No output file or ledger operation until every input/review/real evaluator
    has succeeded. Caller supplies an immutable index and exact CAS operands.
    """
    if mode == "OFF":
        return {"status": "OFF", "delivery_action": "NONE", "paging_eligible": False}
    if mode != "SHADOW":
        return {"status": "REFUSED", "reason_code": "shadow_only", "delivery_action": "NONE", "paging_eligible": False}
    try:
        with ExitStack() as stack:
            fd = _directory(input_dir)
            stack.callback(os.close, fd)
            raw, _ = _read(fd, index_name, FRAME_BYTES, hexhash(index_sha256))
            index = _parse(raw)
            policy, _ = _input(policy_path, FRAME_BYTES, hexhash(policy_sha256))
            producer_review(index, policy, as_of=as_of)  # no reads of frames if unreviewed
            descriptors = index.get("frames")
            need(type(descriptors) is list and 0 < len(descriptors) <= MAX_ITEMS, "producer_frames_unknown")
            frames, total, names = [], 0, {index_name}
            for descriptor in descriptors:
                need(type(descriptor) is dict, "producer_frame_descriptor_unknown")
                name = descriptor.get("name")
                need(type(name) is str and name not in names, "duplicate_frame_file")
                names.add(name)
                raw, _ = _read(fd, name, FRAME_BYTES - total, hexhash(descriptor.get("sha256")))
                total += len(raw)
                frames.append(_parse(raw))
            batch = build_shadow_batch(index, frames, policy, as_of=as_of)
            # The writer receives the in-memory, already verified batch/policy;
            # it must not re-open a mutable pathname and silently change inputs.
            return write_shadow(mode="SHADOW", ledger_dir=ledger_dir, as_of=as_of,
                expected_revision=expected_revision, expected_generation_sha256=expected_generation_sha256,
                initialization_path=initialization_path, initialization_sha256=initialization_sha256,
                supplied_batch=batch, supplied_policy=policy, producer_index_sha256=index_sha256,
                supplied_policy_file_sha256=policy_sha256, producer_request_sha256=request_sha256)
    except Refused as exc:
        reason = exc.args[0]
        return {"status": "NEEDS_RULING" if reason.endswith("needs_ruling") else "REFUSED",
                "reason_code": reason, "delivery_action": "NONE", "paging_eligible": False}
    except (OSError, TypeError, ValueError, KeyError, AttributeError, ImportError, RecursionError):
        return {"status": "REFUSED", "reason_code": "producer_inputs_or_reviews_unbound",
                "delivery_action": "NONE", "paging_eligible": False}


def produce_request(*, mode="OFF", request_dir=None, pointer_name="ready.json"):
    """One Main-published immutable invocation, suitable for a PATH trigger.

    No directory scan, polling, current-state adoption, retry, pointer update
    or clock. CAS arguments come from the sealed request, not a guessed boot.
    """
    if mode == "OFF":
        return {"status": "OFF", "delivery_action": "NONE", "paging_eligible": False}
    if mode != "SHADOW":
        return {"status": "REFUSED", "reason_code": "shadow_only", "delivery_action": "NONE", "paging_eligible": False}
    try:
        fd = _directory(request_dir)
        try:
            raw, pointer_sha = _read(fd, pointer_name, 4096)
            pointer = _parse(raw)
            need(type(pointer) is dict and set(pointer) == {"schema_version", "file", "sha256"}
                 and pointer["schema_version"] == "smartalert1.invocation.v1", "invocation_pointer_unknown")
            digest = hexhash(pointer["sha256"])
            need(pointer["file"] == "request-" + digest + ".json", "invocation_filename_unbound")
            raw, _ = _read(fd, pointer["file"], FRAME_BYTES, digest)
            request = _parse(raw)
        finally:
            os.close(fd)
        need(type(request) is dict and set(request) == {"schema_version", "request_id", "arguments"}
             and request["schema_version"] == "smartalert1.invocation.v1", "invocation_unknown")
        ref(request["request_id"])
        arguments = request["arguments"]
        required = {"mode", "input_dir", "index_name", "index_sha256", "policy_path", "policy_sha256",
                    "as_of", "ledger_dir", "expected_revision", "expected_generation_sha256",
                    "initialization_path", "initialization_sha256"}
        need(type(arguments) is dict and set(arguments) == required and arguments["mode"] == "SHADOW",
             "invocation_arguments_unbound")
        result = produce_shadow(**arguments, request_sha256=digest)
        return dict(result, request_file_sha256=digest, request_pointer_sha256=pointer_sha)
    except Refused as exc:
        return {"status": "REFUSED", "reason_code": exc.args[0], "delivery_action": "NONE", "paging_eligible": False}
    except (OSError, TypeError, ValueError, KeyError, AttributeError, RecursionError):
        return {"status": "REFUSED", "reason_code": "invocation_file_unavailable",
                "delivery_action": "NONE", "paging_eligible": False}


def main():
    parser = argparse.ArgumentParser(description="One finite sealed SMART shadow bundle; no phones")
    parser.add_argument("--mode", choices=("OFF", "SHADOW"), default="OFF")
    for name in ("input-dir", "index-name", "index-sha256", "policy-path", "policy-sha256", "as-of",
                 "ledger-dir", "expected-generation-sha256", "initialization-path", "initialization-sha256"):
        parser.add_argument("--" + name)
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--request-dir")
    parser.add_argument("--request-pointer")
    options = vars(parser.parse_args())
    request_dir = options.pop("request_dir")
    pointer = options.pop("request_pointer")
    if options["mode"] == "OFF":
        result = produce_shadow()
    elif request_dir is not None and all(value is None for key, value in options.items() if key != "mode"):
        result = produce_request(mode="SHADOW", request_dir=request_dir, pointer_name=pointer or "ready.json")
    elif request_dir is not None or pointer is not None:
        result = {"status": "REFUSED", "reason_code": "mixed_invocation_and_direct_arguments",
                  "delivery_action": "NONE", "paging_eligible": False}
    else:
        result = produce_shadow(**options)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in ("OFF", "IDEMPOTENT", "SHADOW_COMMITTED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
