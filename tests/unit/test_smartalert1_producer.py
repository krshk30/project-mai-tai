"""SOURCE controls only. Main must compose retained L2/L3 before executing.

CONTROLLED operand reviews below are not real calibration, market calendars,
historical incident frames, provider receipts, or authority to start a timer.
"""
from copy import deepcopy
import hashlib
import json

import pytest

from ops.smartalert1 import delivery as d, producer as p
from tests.unit.test_smartalert1_delivery import (
    EVIDENCE, FIRST, SOURCE, T0, item, policy, state,
)


def inputs():
    selected = policy()
    upstream = {lane: {"schema_version": "smartalert1.v1", "enabled": True,
                      "mode": "CANDIDATE_REPLAY", "policyversion": "CONTROLLED-" + lane}
                for lane in ("L2", "L3")}
    frames = [{"schema_version": "smartalert1.v1", "source_epoch": "CONTROLLED-publisher",
               "CONTROLLED_ONLY": lane} for lane in ("L2", "L3")]
    evidence = {SOURCE: "a" * 64, EVIDENCE: "b" * 64}
    descriptors = []
    for lane, frame in zip(("L2", "L3"), frames):
        raw = d.encoded(frame, d.FRAME_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        source_ref = "CONTROLLED-frame-" + lane
        evidence[source_ref] = digest
        descriptors.append({"name": lane + ".json", "lane": lane, "source_ref": source_ref,
            "sha256": digest, "canonical_sha256": d.fingerprint(frame, d.FRAME_BYTES),
            "expectation_ref": "CONTROLLED-expectation"})
    selected["producer_review"] = {
        "status": "APPROVED_SHADOW_OPERANDS", "approval_ref": selected["approval_ref"],
        "calibration_ref": selected["calibration_ref"], "acquisition_ref": "CONTROLLED-acquisition",
        "schedule_ref": "CONTROLLED-schedule", "source_closure_ref": "CONTROLLED-source-closure",
        "category_map_sha256": d.fingerprint(selected["category_map"], d.FRAME_BYTES),
        "upstream_policy_sha256": {lane: d.fingerprint(value, d.FRAME_BYTES) for lane, value in upstream.items()}}
    for key in ("approval_ref", "calibration_ref", "acquisition_ref", "schedule_ref", "source_closure_ref"):
        evidence[selected["producer_review"][key]] = "c" * 64
    index = {"schema_version": p.SCHEMA, "mode": "SHADOW", "as_of": T0,
        "source_bindings": deepcopy(selected["source_bindings"]), "upstream_policies": upstream,
        "batch_id": "CONTROLLED-produced-batch-1", "sequence": 1, "frames": descriptors,
        "manifest": {"ref": "CONTROLLED-producer-manifest", "sha256": "f" * 64, "as_of": T0,
            "complete": True, "overflow": False, "read_error": False,
            "evidence": evidence, "coverage": {}}, "resolution_links": [], "digest_window": None}
    return selected, index, frames


def controlled_evaluator(lane, evidence, policy, *, as_of):
    assert evidence["CONTROLLED_ONLY"] == lane and policy["policyversion"] == "CONTROLLED-" + lane
    return [item(lane=lane, stamp=as_of)["finding"]]


def test_finite_assembly_invokes_both_lanes_preserves_inputs_and_no_phone(monkeypatch):
    selected, index, frames = inputs()
    before = deepcopy((selected, index, frames))
    calls = []
    def evaluated(lane, evidence, policy, *, as_of):
        calls.append(lane)
        return controlled_evaluator(lane, evidence, policy, as_of=as_of)
    monkeypatch.setattr(p, "_evaluate", evaluated)
    batch = p.build_shadow_batch(index, frames, selected, as_of=T0)
    assert calls == ["L2", "L3"] and before == (selected, index, frames)
    planned = d.plan_shadow(batch, state(selected), as_of=T0, policy=selected)
    assert planned["status"] == "SHADOW" and len(planned["records"]) == 2
    assert all(row["delivery_action"] == "NONE" and row["paging_eligible"] is False
               for row in planned["records"])


@pytest.mark.parametrize("fault", ["review", "category", "upstream_policy", "calendar", "schedule_proof",
                                   "manifest", "cutoff", "one_lane"])
def test_review_and_membership_gaps_refuse_before_evaluation(monkeypatch, fault):
    selected, index, frames = inputs()
    def forbidden(*a, **k):
        raise AssertionError("unreviewed producer invoked evaluator")
    monkeypatch.setattr(p, "_evaluate", forbidden)
    if fault == "review":
        selected.pop("producer_review")
    elif fault == "category":
        selected["category_map"]["exact"]["L2"]["A1"]["unprotected"] = "DIGEST"
    elif fault == "upstream_policy":
        index["upstream_policies"]["L2"]["mode"] = "REPORT_ONLY"
    elif fault == "calendar":
        selected.pop("close_calendar")
    elif fault == "schedule_proof":
        index["manifest"]["evidence"].pop(selected["producer_review"]["schedule_ref"])
    elif fault == "manifest":
        index["manifest"]["complete"] = False
    elif fault == "cutoff":
        index["as_of"] = FIRST
    else:
        index["frames"] = index["frames"][:1]
        frames = frames[:1]
    with pytest.raises(d.Refused):
        p.build_shadow_batch(index, frames, selected, as_of=T0)


def test_modified_typed_frame_cannot_borrow_an_unchanged_seal(monkeypatch):
    selected, index, frames = inputs()
    monkeypatch.setattr(p, "_evaluate", controlled_evaluator)
    frames[0]["CONTROLLED_ONLY"] = "modified"
    with pytest.raises(d.Refused, match="typed_frame_changed"):
        p.build_shadow_batch(index, frames, selected, as_of=T0)


def test_nonshadow_upstream_result_cannot_be_laundered(monkeypatch):
    selected, index, frames = inputs()
    def unsafe(lane, evidence, policy, *, as_of):
        rows = controlled_evaluator(lane, evidence, policy, as_of=as_of)
        rows[0]["paging_eligible"] = True
        return rows
    monkeypatch.setattr(p, "_evaluate", unsafe)
    with pytest.raises(d.Refused, match="upstream_nonshadow_result"):
        p.build_shadow_batch(index, frames, selected, as_of=T0)


def test_producer_OFF_and_unsupported_mode_never_access_files_or_evaluators(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("inactive producer performed work")
    for name in ("_directory", "_input", "_read", "_evaluate", "write_shadow"):
        monkeypatch.setattr(p, name, forbidden)
    assert p.produce_shadow()["status"] == "OFF"
    assert p.produce_shadow(mode="QUALIFIED_LIVE")["status"] == "REFUSED"


@pytest.fixture
def private_inputs(tmp_path):
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    input_dir, ledger_dir = tmp_path / "inputs", tmp_path / "ledger"
    input_dir.mkdir(mode=0o700)
    ledger_dir.mkdir(mode=0o700)
    selected, index, frames = inputs()
    def save(name, value, raw=None):
        raw = d.encoded(value, d.FRAME_BYTES) if raw is None else raw
        path = input_dir / name
        path.write_bytes(raw)
        path.chmod(0o600)
        return str(path), hashlib.sha256(raw).hexdigest()
    for descriptor, frame in zip(index["frames"], frames):
        save(descriptor["name"], frame)
    policy_path, policy_sha = save("policy.json", selected)
    _, index_sha = save("index.json", index)
    initial_path, initial_sha = save("initial.json", state(selected))
    args = dict(mode="SHADOW", input_dir=str(input_dir), index_name="index.json", index_sha256=index_sha,
        policy_path=policy_path, policy_sha256=policy_sha, as_of=T0, ledger_dir=str(ledger_dir),
        expected_revision=0, expected_generation_sha256=None,
        initialization_path=initial_path, initialization_sha256=initial_sha)
    return args, ledger_dir, selected, index, frames, save


def test_sealed_FILE_pipeline_commits_exact_provenance_then_idempotent(private_inputs, monkeypatch):
    args, output, selected, index, frames, save = private_inputs
    monkeypatch.setattr(p, "_evaluate", controlled_evaluator)
    result = p.produce_shadow(**args)
    assert result["status"] == "SHADOW_COMMITTED" and result["paging_eligible"] is False
    raw = (output / ("generation-" + result["generation_sha256"] + ".json")).read_bytes()
    generation = json.loads(raw)
    assert generation["batch_input_kind"] == "PRODUCED_FROM_SEALED_FILES"
    assert generation["batch_file_sha256"] is None
    assert generation["producer_index_sha256"] == args["index_sha256"]
    assert generation["policy_file_sha256"] == args["policy_sha256"]
    repeated = p.produce_shadow(**dict(args, expected_revision=1,
        expected_generation_sha256=result["generation_sha256"], initialization_path=None, initialization_sha256=None))
    assert repeated["status"] == "IDEMPOTENT" and len(list(output.glob("generation-*.json"))) == 1


@pytest.mark.parametrize("fault", ["index_hash", "frame_hash", "traversal", "policy_review", "frame_total"])
def test_FILE_failure_never_opens_a_new_ledger_generation(private_inputs, monkeypatch, fault):
    args, output, selected, index, frames, save = private_inputs
    monkeypatch.setattr(p, "_evaluate", controlled_evaluator)
    if fault == "index_hash":
        args["index_sha256"] = "0" * 64
    elif fault == "frame_hash":
        save("L2.json", {"changed": True})
    elif fault == "traversal":
        index["frames"][0]["name"] = "../outside.json"
    elif fault == "policy_review":
        selected.pop("producer_review")
        args["policy_path"], args["policy_sha256"] = save("policy2.json", selected)
    else:
        for descriptor, frame in zip(index["frames"], frames):
            frame["padding"] = "x" * 600000
            _, digest = save(descriptor["name"], frame)
            descriptor.update(sha256=digest, canonical_sha256=d.fingerprint(frame, d.FRAME_BYTES))
            index["manifest"]["evidence"][descriptor["source_ref"]] = digest
    if fault in ("traversal", "frame_total"):
        _, args["index_sha256"] = save("index.json", index)
    result = p.produce_shadow(**args)
    assert result["status"] in ("REFUSED", "NEEDS_RULING")
    assert not (output / "current.json").exists() and not list(output.glob("generation-*.json"))


@pytest.mark.parametrize("name", ["KEEPWATCH_0703", "GUARD_0340", "SPRC_0753"])
def test_historical_narrative_is_neither_typed_frame_nor_clear(name, monkeypatch):
    selected, index, frames = inputs()
    frames[0] = {"historical_narrative": name, "typed_frame": None, "affirmative_clear": None}
    descriptor = index["frames"][0]
    descriptor["canonical_sha256"] = d.fingerprint(frames[0], d.FRAME_BYTES)
    monkeypatch.setattr(p, "_evaluate", controlled_evaluator)
    with pytest.raises(d.Refused, match="typed_envelope_required"):
        p.build_shadow_batch(index, frames, selected, as_of=T0)


def test_composed_real_L2_L3_interfaces_keep_stale_fact_unknown_without_retiming():
    # Integration SOURCE selector: Main composes actual b0f+71aa modules and
    # their existing controlled factories. No import-skip or fake evaluator.
    from tests.unit.test_smartalert1_trade import AS_OF, envelope, policy as trade_policy
    from tests.unit.test_smartalert1_system import frame as system_frame
    selected, index, _ = inputs()
    trade, trade_p = envelope(), trade_policy()
    trade["subject"]["account"] = "live:orb"  # CONTROLLED canonical logical scope
    system, system_p = system_frame()
    index["upstream_policies"] = {"L2": trade_p, "L3": system_p}
    index["as_of"] = index["manifest"]["as_of"] = AS_OF
    selected["upstream_policyversions"] = {"L2": trade_p["policyversion"], "L3": system_p["policyversion"]}
    selected["category_map"]["exact"]["L2"]["A1"]["owned_quantity_unprotected"] = "PAGE"
    selected["producer_review"]["category_map_sha256"] = d.fingerprint(selected["category_map"], d.FRAME_BYTES)
    selected["producer_review"]["upstream_policy_sha256"] = {
        lane: d.fingerprint(value, d.FRAME_BYTES) for lane, value in index["upstream_policies"].items()}
    for descriptor, evidence in zip(index["frames"], (trade, system)):
        descriptor["sha256"] = descriptor["canonical_sha256"] = d.fingerprint(evidence, d.FRAME_BYTES)
        index["manifest"]["evidence"][descriptor["source_ref"]] = descriptor["sha256"]
        descriptor["expectation_ref"] = ("synthetic-expectations-1" if descriptor["lane"] == "L2"
                                         else "controlled:historical_profile")
        for evidence_ref in evidence["evidence_ids"]:
            index["manifest"]["evidence"][evidence_ref] = "d" * 64
    result = p.build_shadow_batch(index, [trade, system], selected, as_of=AS_OF)
    assert {row["lane"] for row in result["findings"]} == {"L2", "L3"}
    assert any(row["finding"]["status"] == "TRUE" for row in result["findings"] if row["lane"] == "L2")
    assert all(row["finding"]["status"] == "UNKNOWN" for row in result["findings"] if row["lane"] == "L3")
    planned = d.plan_shadow(result, state(selected), as_of=AS_OF, policy=selected)
    assert planned["status"] == "SHADOW" and planned["paging_eligible"] is False


def invocation(args, save):
    request = {"schema_version": "smartalert1.invocation.v1", "request_id": "CONTROLLED-request-1",
               "arguments": dict(args)}
    raw = d.encoded(request, d.FRAME_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    name = "request-" + digest + ".json"
    save(name, request)
    save("ready.json", {"schema_version": "smartalert1.invocation.v1", "file": name, "sha256": digest})
    return request, digest, name


def test_event_trigger_request_is_finite_sealed_and_never_adopts_current_CAS(private_inputs, monkeypatch):
    args, output, _, _, _, save = private_inputs
    monkeypatch.setattr(p, "_evaluate", controlled_evaluator)
    _, digest, _ = invocation(args, save)
    result = p.produce_request(mode="SHADOW", request_dir=args["input_dir"])
    assert result["status"] == "SHADOW_COMMITTED" and result["request_file_sha256"] == digest
    generation = json.loads((output / ("generation-" + result["generation_sha256"] + ".json")).read_bytes())
    assert generation["producer_request_sha256"] == digest
    repeated = p.produce_request(mode="SHADOW", request_dir=args["input_dir"])
    assert repeated["status"] == "REFUSED"  # old expected_revision/init cannot reset a live ledger
    assert len(list(output.glob("generation-*.json"))) == 1


@pytest.mark.parametrize("fault", ["changed_bytes", "unexpected_argument", "live_mode", "pointer_identity"])
def test_invocation_cannot_change_input_membership_or_gain_authority(private_inputs, monkeypatch, fault):
    args, output, _, _, _, save = private_inputs
    request, digest, name = invocation(args, save)
    def forbidden(*a, **k):
        raise AssertionError("unbound invocation reached acquisition")
    monkeypatch.setattr(p, "produce_shadow", forbidden)
    if fault == "changed_bytes":
        request["request_id"] = "changed"
        save(name, request)
    elif fault == "pointer_identity":
        save("ready.json", {"schema_version": "smartalert1.invocation.v1", "file": "other.json", "sha256": digest})
    else:
        if fault == "unexpected_argument":
            request["arguments"]["provider_url"] = "SECRET_https://not-permitted.invalid"
        else:
            request["arguments"]["mode"] = "QUALIFIED_LIVE"
        raw = d.encoded(request, d.FRAME_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        name = "request-" + digest + ".json"
        save(name, request)
        save("ready.json", {"schema_version": "smartalert1.invocation.v1", "file": name, "sha256": digest})
    result = p.produce_request(mode="SHADOW", request_dir=args["input_dir"])
    assert result["status"] == "REFUSED" and "SECRET" not in json.dumps(result)
    assert not (output / "current.json").exists()


def test_invocation_OFF_before_any_private_file_access(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("OFF request performed I/O")
    monkeypatch.setattr(p, "_directory", forbidden)
    assert p.produce_request(request_dir="not-read")["status"] == "OFF"
