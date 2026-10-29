"""CONTROLLED supplied findings/policy; UNRUN source, no historical qualification.

No upstream APP copy, new test runner, native input, clock/feed adaptation or
transport. Main executes with the existing rig after exact source review.
"""

from copy import deepcopy
import fcntl
import hashlib
import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from ops.smartalert1 import delivery as d
from ops.smartalert1 import shadow_ledger as ledger

T0 = "2026-10-28T20:00:00Z"
T1 = "2026-10-28T20:01:00Z"
T2 = "2026-10-28T20:02:00Z"
T3 = "2026-10-28T20:03:00Z"
FIRST = "2026-10-28T19:59:00Z"
SOURCE = "CONTROLLED-source"
EVIDENCE = "CONTROLLED-evidence"


def policy():
    return {"schema_version": d.SCHEMA, "enabled": True, "mode": "SHADOW",
        "policyversion": "CONTROLLED-delivery", "approval_ref": "CONTROLLED-review",
        "calibration_ref": "CONTROLLED-NOT-real-calibration",
        "source_bindings": {"L2": {"source_head": "1" * 40, "file_sha256": "2" * 64},
                            "L3": {"source_head": "3" * 40, "file_sha256": "4" * 64}},
        "upstream_policyversions": {"L2": "CONTROLLED-L2", "L3": "CONTROLLED-L3"},
        "severity_map": {lane: {cls: {"unprotected": 1, "worsened": 2, "more_worse": 3}}
                         for lane, cls in (("L2", "A1"), ("L3", "B_guard_STOP"))},
        "required_digest_coverage": ["CONTROLLED-required-scope"],
        "close_calendar": {"schema_version": "smartalert1.close-calendar.v1",
            "calendar_ref": EVIDENCE, "approval_ref": SOURCE, "timezone": "America/New_York",
            "late_policy": "NO_BACKDATED_DIGEST", "windows": [{
                "window_id": "CONTROLLED-day", "open_session": True,
                "session_proof_ref": EVIDENCE, "trading_date": "2026-10-28", "close_kind": "REGULAR",
                "start": "2026-10-28T19:00:00Z", "end": T3}]}}


def state(p):
    return d.initial_state(campaign_id="CONTROLLED-campaign", ledger_epoch="CONTROLLED-ledger", policy=p)


def item(*, status="TRUE", lane="L2", epoch="CONTROLLED-condition-1", reason="unprotected",
         stamp=T0, manual=False, paper=False, action=None):
    cls = "A1" if lane == "L2" else "B_guard_STOP"
    f = {"schema_version": d.FINDING_SCHEMA, "class": cls,
        "account": "live:CONTROLLED", "broker": "webull", "bot": "schwab_1m_v2", "symbol": "ABC",
        "subject_id": "CONTROLLED-position", "first_seen": FIRST, "last_seen": stamp,
        "status": status, "reason_code": reason, "evidence_ids": [EVIDENCE],
        "action_taken": action if action is not None else "none_recorded", "recommended_action": "NOT USED",
        "manual": manual, "paper": paper, "session": "RTH", "policyversion": "CONTROLLED-" + lane,
        "paging_eligible": False,
        "freshness": {"status": "FRESH", "observed_at": stamp, "event_at": stamp, "policy_ref": "CONTROLLED-cal"},
        "qualification": {"mode": "CANDIDATE_REPLAY", "qualified_policy": False,
                          "coverage_complete": True, "reason": reason, "evidence_refs": [EVIDENCE]}}
    f["incident_key"] = json.dumps([d.FINDING_SCHEMA, cls, f["account"], f["broker"],
                                   f["bot"], f["symbol"], f["subject_id"], epoch], separators=(",", ":"))
    if status == "UNKNOWN":
        f.update(first_seen=None, last_seen=None, evidence_ids=[], action_taken="UNKNOWN")
        f["freshness"]["status"] = "UNKNOWN"
        f["qualification"].update(coverage_complete=False, evidence_refs=[])
    return {"lane": lane, "finding": f, "source_epoch": "CONTROLLED-publisher",
            "expectation_ref": "CONTROLLED-expectation", "source_ref": SOURCE,
            "source_hash": "a" * 64, "evaluated_at": stamp}


def batch(p, prior, items, *, stamp=T0, batch_id=None):
    return {"schema_version": d.SCHEMA, "source_bindings": deepcopy(p["source_bindings"]),
        "policy_sha256": d.fingerprint(p, d.FRAME_BYTES),
        "batch_id": batch_id or "CONTROLLED-batch-" + str(prior["revision"] + 1),
        "sequence": prior["revision"] + 1, "findings": items,
        "manifest": {"ref": "CONTROLLED-manifest", "sha256": "f" * 64, "as_of": stamp,
            "complete": True, "overflow": False, "read_error": False,
            "evidence": {SOURCE: "a" * 64, EVIDENCE: "b" * 64},
            "coverage": {"CONTROLLED-required-scope": {"complete": True, "evidence_ids": [EVIDENCE],
                "coverage_start": "2026-10-28T19:00:00Z", "coverage_end": stamp, "as_of": stamp}}}}


def run(p, prior, items, *, stamp=T0, links=(), window=None):
    supplied = batch(p, prior, items, stamp=stamp)
    result = d.plan_shadow(supplied, prior, as_of=stamp, policy=p, resolution_links=links, digest_window=window)
    return result, supplied


def kinds(result):
    return [row["kind"] for row in result["records"] if row["kind"] != "OBSERVATION"]


def resolution(old_item, clearing):
    return {"incident_key": d.incident_id(old_item["lane"], old_item["finding"]),
        "clearing_key": d.incident_id(clearing["lane"], clearing["finding"]),
        "prior_source_epoch": old_item["source_epoch"], "current_source_epoch": clearing["source_epoch"],
        "reason_code": clearing["finding"]["reason_code"], "affirmative_refs": [EVIDENCE]}


def test_open_dedup_worse_only_preserves_original_clock_and_inputs():
    p = policy()
    before = state(p)
    original = deepcopy(before)
    first = item()
    untouched = deepcopy(first)
    result, _ = run(p, before, [first])
    assert kinds(result) == ["WOULD_OPEN"] and before == original and first == untouched
    prior = result["next_state"]
    for stamp, reason, expected in ((T1, "unprotected", []), (T2, "worsened", ["WOULD_ESCALATE"]),
                                    (T3, "unprotected", [])):
        result, _ = run(p, prior, [item(stamp=stamp, reason=reason)], stamp=stamp)
        assert result["status"] == "SHADOW" and kinds(result) == expected
        prior = result["next_state"]
    incident = next(iter(prior["incidents"].values()))
    assert incident["first_seen"] == FIRST and incident["max_rank"] == 2 and incident["observations"] == 4


def test_exact_batch_and_all_side_inputs_are_idempotent_not_reinterpreted():
    p = policy()
    result, supplied = run(p, state(p), [item()])
    repeated = d.plan_shadow(supplied, result["next_state"], as_of=T0, policy=p)
    assert repeated["status"] == "IDEMPOTENT" and not repeated["records"]
    changed = d.plan_shadow(supplied, result["next_state"], as_of=T0, policy=p,
                            digest_window={"new": "CONTROLLED"})
    assert changed["status"] == "REFUSED" and changed["next_state"] is None


@pytest.mark.parametrize("observation", ["UNKNOWN", "STALE", "ABSENT", "FALSE_WITHOUT_LINK"])
def test_missing_or_nonaffirmative_observation_never_clears(observation):
    p = policy()
    result, _ = run(p, state(p), [item()])
    prior = result["next_state"]
    rows = [] if observation == "ABSENT" else [item(status="UNKNOWN" if observation == "UNKNOWN" else "FALSE", stamp=T1)]
    if observation == "STALE":
        rows[0]["finding"]["freshness"]["status"] = "STALE"
    result, _ = run(p, prior, rows, stamp=T1)
    assert "WOULD_CLEAR" not in kinds(result)
    actual = result["next_state"] if result["status"] == "SHADOW" else prior
    entry = next(iter(actual["incidents"].values()))
    assert entry["opened"] and not entry["cleared"] and entry["first_seen"] == FIRST
    if observation == "UNKNOWN":
        assert entry["last_verified_seen"] == T0


@pytest.mark.parametrize("new_epoch", [False, True])
def test_affirmative_clear_once_with_exact_link_then_new_epoch_recurrence(new_epoch):
    p = policy()
    first = item()
    result, _ = run(p, state(p), [first])
    clearing = item(status="FALSE", reason="positive_protection_or_flat", stamp=T1,
                    epoch="CONTROLLED-clear-epoch" if new_epoch else "CONTROLLED-condition-1")
    link = resolution(first, clearing)
    result, _ = run(p, result["next_state"], [clearing], stamp=T1, links=[link])
    assert result["status"] == "SHADOW" and kinds(result) == ["WOULD_CLEAR"]
    again = deepcopy(clearing)
    again["finding"]["last_seen"] = again["evaluated_at"] = T2
    again["finding"]["freshness"].update(observed_at=T2, event_at=T2)
    result, _ = run(p, result["next_state"], [again], stamp=T2, links=[link])
    assert result["status"] == "SHADOW" and kinds(result) == []
    result, _ = run(p, result["next_state"], [item(stamp=T3, epoch="CONTROLLED-new-risk")], stamp=T3)
    assert kinds(result) == ["WOULD_OPEN"]


@pytest.mark.parametrize("fault", ["foreign_target", "wrong_epoch", "missing_refs", "foreign_refs"])
def test_bad_resolution_link_refuses_whole_batch_without_advancing(fault):
    p = policy()
    first = item()
    result, _ = run(p, state(p), [first])
    prior = result["next_state"]
    clearing = item(status="FALSE", stamp=T1)
    link = resolution(first, clearing)
    if fault == "foreign_target":
        link["incident_key"] = "NOT-ORIGINAL"
    elif fault == "wrong_epoch":
        link["prior_source_epoch"] = "NOT-ORIGINAL"
    else:
        link["affirmative_refs"] = [] if fault == "missing_refs" else ["NOT-BOUND"]
    unchanged = deepcopy(prior)
    result, _ = run(p, prior, [clearing], stamp=T1, links=[link])
    assert result["status"] == "REFUSED" and result["next_state"] is None and prior == unchanged


@pytest.mark.parametrize("manual,paper", [(True, False), (False, True)])
def test_manual_digest_only_and_paper_excluded(manual, paper):
    p = policy()
    result, _ = run(p, state(p), [item(manual=manual, paper=paper)])
    assert result["status"] == "SHADOW" and kinds(result) == []
    assert len(result["next_state"]["incidents"]) == (0 if paper else 1)


def test_L2_string_L3_object_actions_and_shadow_metadata_remain_distinct():
    p = policy()
    l3 = item(lane="L3", action={"code": "guard_stopped", "evidence_ids": [EVIDENCE]})
    l3["finding"]["shadow"] = {"mode": "SHADOW", "delivery_action": "NONE", "existing_senders_unchanged": True,
                              "would_page_under_supplied_policy": False, "candidate_detection": True}
    result, _ = run(p, state(p), [item(), l3])
    assert result["status"] == "SHADOW" and len(result["next_state"]["incidents"]) == 2
    assert type(result["records"][0]["action_taken"]) is str
    assert type(result["records"][1]["action_taken"]) is dict
    assert result["records"][1]["upstream_shadow"] == l3["finding"]["shadow"]
    for row in result["records"]:
        assert set(row["message"]) == {"what", "since_when", "system_did", "what_to_do"}
        assert row["delivery_action"] == "NONE" and row["paging_eligible"] is False


@pytest.mark.parametrize("reason", ["stops_shadow_persisted_attachment_evidence_only",
                                  "stops_shadow_existing_stop_or_managed"])
def test_held_stops_persisted_marker_cannot_clear_known_risk(reason):
    p = policy()
    first = item()
    result, _ = run(p, state(p), [first])
    prior = result["next_state"]
    clearing = item(status="FALSE", stamp=T1, reason=reason)
    result, _ = run(p, prior, [clearing], stamp=T1, links=[resolution(first, clearing)])
    assert result["status"] == "REFUSED" and result["next_state"] is None
    assert next(iter(prior["incidents"].values()))["cleared"] is False


def test_cross_broker_subject_identity_never_merges_or_cross_clears():
    p = policy()
    first, other = item(), item()
    other["finding"].update(account="live:CONTROLLED_SCHWAB", broker="schwab")
    fields = json.loads(other["finding"]["incident_key"])
    fields[2:4] = ["live:CONTROLLED_SCHWAB", "schwab"]
    other["finding"]["incident_key"] = json.dumps(fields, separators=(",", ":"))
    result, _ = run(p, state(p), [first, other])
    assert result["status"] == "SHADOW" and len(result["next_state"]["incidents"]) == 2
    clearing = deepcopy(other)
    clearing["finding"].update(status="FALSE", last_seen=T1)
    clearing["finding"]["freshness"].update(observed_at=T1, event_at=T1)
    clearing["evaluated_at"] = T1
    failed, _ = run(p, result["next_state"], [clearing], stamp=T1, links=[resolution(first, clearing)])
    assert failed["status"] == "REFUSED" and failed["next_state"] is None


@pytest.mark.parametrize("fault", ["policy", "source", "source_hash", "sequence", "duplicate", "first_seen",
                                   "severity", "live_input", "upstream_policy", "future"])
def test_binding_conflicts_never_repair_or_advance(fault):
    p = policy()
    first, _ = run(p, state(p), [item()])
    prior = first["next_state"]
    supplied = batch(p, prior, [item(stamp=T1)], stamp=T1)
    if fault == "policy":
        p["approval_ref"] = "DIFFERENT"
    elif fault == "source":
        supplied["source_bindings"]["L2"]["source_head"] = "f" * 40
    elif fault == "source_hash":
        supplied["findings"][0]["source_hash"] = "c" * 64
    elif fault == "sequence":
        supplied["sequence"] += 1
    elif fault == "duplicate":
        supplied["findings"].append(deepcopy(supplied["findings"][0]))
    elif fault == "first_seen":
        supplied["findings"][0]["finding"]["first_seen"] = T0
    elif fault == "severity":
        supplied["findings"][0]["finding"]["reason_code"] = "not_calibrated"
    elif fault == "live_input":
        supplied["findings"][0]["finding"]["paging_eligible"] = True
    elif fault == "upstream_policy":
        supplied["findings"][0]["finding"]["policyversion"] = "NOT-BOUND"
    else:
        supplied["findings"][0]["finding"]["last_seen"] = T2
    snapshot = deepcopy(prior)
    result = d.plan_shadow(supplied, prior, as_of=T1, policy=p)
    assert result["status"] == "REFUSED" and result["records"] == [] and prior == snapshot


def test_digest_requires_calendar_and_all_bound_coverage_counts_unique_incidents():
    p = policy()
    result, _ = run(p, state(p), [item()])
    assert result["digest"]["status"] == "UNKNOWN"
    window = {"window_id": "CONTROLLED-day", "calendar_ref": EVIDENCE,
              "start": "2026-10-28T19:00:00Z", "end": T3, "coverage_complete": True}
    prior = result["next_state"]
    for stamp, reason in ((T1, "worsened"), (T2, "more_worse")):
        result, _ = run(p, prior, [item(stamp=stamp, reason=reason)], stamp=stamp)
        prior = result["next_state"]
    supplied = batch(p, prior, [item(stamp=T3)], stamp=T3)
    supplied["manifest"]["coverage"] = {}
    incomplete = d.plan_shadow(supplied, prior, as_of=T3, policy=p, digest_window=window)
    assert incomplete["digest"]["status"] == "UNKNOWN"
    result, supplied = run(p, prior, [item(stamp=T3)], stamp=T3, window=window)
    assert result["digest"]["status"] == "SHADOW_DIGEST"
    assert result["digest"]["counts"]["opened"] == 1 and result["digest"]["counts"]["escalated"] == 1
    assert result["digest"]["counts"]["still_open"] == 1 and result["digest"]["counts"]["observations"] == 4
    repeated = d.plan_shadow(supplied, result["next_state"], as_of=T3, policy=p, digest_window=window)
    assert repeated["status"] == "IDEMPOTENT" and not repeated["records"]


@pytest.mark.parametrize("scope", ["batch", "policy", "state", "count"])
def test_resource_caps_refuse_without_truncation(scope):
    p = policy()
    prior = state(p)
    supplied = batch(p, prior, [item()])
    if scope == "batch":
        supplied["padding"] = "x" * d.FRAME_BYTES
    elif scope == "policy":
        p["padding"] = "x" * d.FRAME_BYTES
    elif scope == "state":
        prior["padding"] = "x" * d.STATE_BYTES
    else:
        supplied["findings"] = [{}] * (d.MAX_ITEMS + 1)
    result = d.plan_shadow(supplied, prior, as_of=T0, policy=p)
    assert result["status"] == "REFUSED" and result["next_state"] is None


@pytest.mark.parametrize("incident", ["KEEPWATCH_20261028_0700", "GUARD_STOP_20261028_0340", "SPRC_20261026_0753"])
def test_real_narrative_without_typed_provenance_cannot_be_a_replay_or_page(incident):
    p = policy()
    before = state(p)
    # These names identify REAL narrative gaps, not synthetic positive preimages.
    supplied = batch(p, before, [])
    supplied["manifest"] = {"ref": incident}  # Actual sealed typed inputs NOT supplied.
    result = d.plan_shadow(supplied, before, as_of=T0, policy=p)
    assert result["status"] == "REFUSED" and result["next_state"] is None
    assert result["paging_eligible"] is False and result["records"] == []


def test_OFF_is_inert_and_writer_never_accesses_file_ports(monkeypatch):
    marker = {"CONTROLLED": "unchanged"}
    assert d.plan_shadow(None, marker, as_of=None, policy=None)["next_state"] is marker
    def forbidden(*args, **kwargs):
        raise AssertionError("OFF file I/O")
    monkeypatch.setattr(ledger, "_input", forbidden)
    monkeypatch.setattr(ledger, "_directory", forbidden)
    assert ledger.write_shadow()["status"] == "OFF"
    assert ledger.write_shadow(mode="QUALIFIED_LIVE")["status"] == "REFUSED"


@pytest.fixture
def files(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    inputs, output = root / "inputs", root / "ledger"
    inputs.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    p = policy()
    before = state(p)
    supplied = batch(p, before, [item()])
    def save(name, value, raw=None):
        data = d.encoded(value) if raw is None else raw
        path = inputs / name
        path.write_bytes(data)
        path.chmod(0o600)
        return str(path), hashlib.sha256(data).hexdigest()
    pp, ph = save("policy.json", p)
    bp, bh = save("batch1.json", supplied)
    ip, ih = save("initial.json", before)
    args = dict(mode="SHADOW", ledger_dir=str(output), batch_path=bp, policy_path=pp,
        batch_sha256=bh, policy_sha256=ph, as_of=T0, expected_revision=0,
        expected_generation_sha256=None, initialization_path=ip, initialization_sha256=ih)
    return args, output, p, save


def test_writer_readback_chain_idempotence_and_no_existing_state_reset(files):
    args, output, p, save = files
    first = ledger.write_shadow(**args)
    assert first["status"] == "SHADOW_COMMITTED"
    gen1 = output / ("generation-" + first["generation_sha256"] + ".json")
    old_raw = gen1.read_bytes()
    before = json.loads(old_raw)["state"]
    reset = ledger.write_shadow(**args)
    assert reset["status"] == "REFUSED" and gen1.read_bytes() == old_raw
    supplied = batch(p, before, [item(stamp=T1)], stamp=T1)
    bp, bh = save("batch2.json", supplied)
    second_args = dict(args, batch_path=bp, batch_sha256=bh, as_of=T1, expected_revision=1,
        expected_generation_sha256=first["generation_sha256"], initialization_path=None, initialization_sha256=None)
    second = ledger.write_shadow(**second_args)
    assert second["status"] == "SHADOW_COMMITTED" and gen1.read_bytes() == old_raw
    repeat = ledger.write_shadow(**dict(second_args, expected_revision=2,
                                        expected_generation_sha256=second["generation_sha256"]))
    assert repeat["status"] == "IDEMPOTENT" and len(list(output.glob("generation-*.json"))) == 2
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())


@pytest.mark.parametrize("fault", ["hash", "duplicate_json", "permissions", "input_symlink", "ledger_symlink", "missing_initial"])
def test_writer_input_and_initialization_fail_closed(files, fault):
    args, output, _, save = files
    if fault == "hash":
        args["batch_sha256"] = "0" * 64
    elif fault == "duplicate_json":
        args["batch_path"], args["batch_sha256"] = save("duplicate.json", None, raw=b'{"x":1,"x":2}')
    elif fault == "permissions":
        os.chmod(args["batch_path"], 0o644)
    elif fault == "input_symlink":
        link = output.parent / "batch-link"
        link.symlink_to(args["batch_path"])
        args["batch_path"] = str(link)
    elif fault == "ledger_symlink":
        link = output.parent / "ledger-link"
        link.symlink_to(output, target_is_directory=True)
        args["ledger_dir"] = str(link)
    else:
        args["initialization_path"] = args["initialization_sha256"] = None
    result = ledger.write_shadow(**args)
    assert result["status"] == "REFUSED" and not (output / "current.json").exists()
    assert not list(output.glob("generation-*.json"))


def test_concurrent_writer_refused_without_consuming_batch(files):
    args, output, _, _ = files
    lock = os.open(str(output / ".writer.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = ledger.write_shadow(**args)
        assert result["status"] == "REFUSED" and result["reason_code"] == "writer_already_owned"
        assert not (output / "current.json").exists()
    finally:
        os.close(lock)


def test_orphan_generation_never_claims_commit_or_drops_receipts(files, monkeypatch):
    args, output, _, _ = files
    actual = ledger._new_file
    def fail_before_pointer(fd, name, data):
        if name.startswith("current-"):
            raise OSError("CONTROLLED-before-pointer")
        return actual(fd, name, data)
    monkeypatch.setattr(ledger, "_new_file", fail_before_pointer)
    result = ledger.write_shadow(**args)
    assert result["status"] == "REFUSED" and not (output / "current.json").exists()
    assert len(list(output.glob("generation-*.json"))) == 1


def test_failed_postpublication_readback_is_unproven_not_success(files, monkeypatch):
    args, output, _, _ = files
    actual = ledger._read
    def fail_readback(fd, name, cap, expected=None):
        if name == "current.json" and expected is not None:
            raise OSError("CONTROLLED-readback")
        return actual(fd, name, cap, expected)
    monkeypatch.setattr(ledger, "_read", fail_readback)
    result = ledger.write_shadow(**args)
    assert result["status"] == "COMMIT_UNPROVEN" and (output / "current.json").exists()
    assert len(list(output.glob("generation-*.json"))) == 1


@pytest.mark.parametrize("fault", ["missing", "unreviewed", "changed_close", "wrong_day", "closed", "late"])
def test_digest_calendar_is_exact_reviewed_membership_not_a_guessed_close(fault):
    p = policy()
    before = state(p)
    supplied = batch(p, before, [], stamp=T3)
    window = {"window_id": "CONTROLLED-day", "calendar_ref": EVIDENCE,
              "start": "2026-10-28T19:00:00Z", "end": T3, "coverage_complete": True}
    if fault == "missing":
        p.pop("close_calendar")
    elif fault == "unreviewed":
        p["close_calendar"]["approval_ref"] = "NOT-RETAINED"
    elif fault == "changed_close":
        window["end"] = T2
    elif fault == "wrong_day":
        p["close_calendar"]["windows"][0]["trading_date"] = "2026-10-29"
    elif fault == "closed":
        p["close_calendar"]["windows"][0]["open_session"] = False
    else:
        p["close_calendar"]["late_policy"] = "BACKDATE_ALLOWED"
    # Re-provision only this controlled policy, so the calendar branch is reached.
    before = state(p)
    supplied = batch(p, before, [], stamp=T3)
    result = d.plan_shadow(supplied, before, as_of=T3, policy=p, digest_window=window)
    assert result["status"] == "REFUSED" and result["next_state"] is None


def test_manual_reclassification_cannot_clear_a_known_bot_risk():
    p = policy()
    first = item()
    opened, _ = run(p, state(p), [first])
    clearing = item(status="FALSE", manual=True, stamp=T1)
    result, _ = run(p, opened["next_state"], [clearing], stamp=T1,
                    links=[resolution(first, clearing)])
    assert result["status"] == "REFUSED"
    assert result["reason_code"] == "manual_reclassification_not_risk_recovery"


def test_nonregular_input_is_refused_before_open_not_a_blocking_fifo(files, monkeypatch):
    args, output, _, _ = files
    actual_stat = os.stat
    def named_stat(name, *a, **k):
        if name == "batch1.json":
            return SimpleNamespace(st_mode=stat.S_IFIFO | 0o600)
        return actual_stat(name, *a, **k)
    monkeypatch.setattr(os, "stat", named_stat)
    actual = os.open
    def guarded(name, *a, **k):
        assert name != "batch1.json", "nonregular input reached open"
        return actual(name, *a, **k)
    monkeypatch.setattr(os, "open", guarded)
    assert ledger.write_shadow(**args)["status"] == "REFUSED"


def test_system_message_names_actual_subject_without_fabricated_stock_or_bot():
    finding = item(lane="L3")["finding"]
    finding.update(symbol=None, bot=None, account=None, broker="system", subject_id="service:oms")
    message = d.four_parts(finding, FIRST)
    assert "service:oms" in message["what"] and "ABC" not in message["what"]
    assert set(message) == {"what", "since_when", "system_did", "what_to_do"}


def test_legacy_writer_cli_cannot_activate_L4_without_the_bundle(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["shadow_ledger", "--mode", "SHADOW"])
    def forbidden(**kwargs):
        raise AssertionError("legacy CLI bypassed producer")
    monkeypatch.setattr(ledger, "write_shadow", forbidden)
    assert ledger.main() == 1
    assert json.loads(capsys.readouterr().out)["reason_code"] == "bundled_producer_entrypoint_required"


def test_digest_cannot_reuse_coverage_for_a_shorter_or_different_cutoff():
    p = policy()
    before = state(p)
    supplied = batch(p, before, [item(stamp=T3)], stamp=T3)
    supplied["manifest"]["coverage"]["CONTROLLED-required-scope"]["coverage_end"] = T2
    window = {"window_id": "CONTROLLED-day", "calendar_ref": EVIDENCE,
              "start": "2026-10-28T19:00:00Z", "end": T3, "coverage_complete": True}
    result = d.plan_shadow(supplied, before, as_of=T3, policy=p, digest_window=window)
    assert result["status"] == "SHADOW" and result["digest"]["status"] == "UNKNOWN"
    assert not result["next_state"]["digests"]
