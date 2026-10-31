"""SMARTALERT1 L4 supplied-findings policy. Pure, SHADOW ONLY, no acquisition.

CONTROLLED test policies are not real calibration. Approval/provenance references
are caller attestations bound by Main's manifest, never phone permissions.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from zoneinfo import ZoneInfo

SCHEMA = "smartalert1.delivery.v1"
FINDING_SCHEMA = "smartalert1.v1"
FRAME_BYTES = 1048576
STATE_BYTES = 4194304
MAX_ITEMS = 1000
MAX_REFS = 64
L2_TRUE_RULE_SHA256 = "5f2106c3451456d4baab9dd57618c8774aae111a31832a097e19eff9ab7c400a"


class Refused(ValueError):
    """Fixed internal code only; never format caller data or original errors."""


def need(condition, code):
    if not condition:
        raise Refused(code)


def ref(value):
    need(type(value) is str and re.fullmatch(r"[A-Za-z0-9_.:/@-]{1,256}", value) is not None
         and "://" not in value, "reference_unknown")
    return value


def code(value):
    need(type(value) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,95}", value) is not None,
         "code_unknown")
    return value


def clock(value):
    need(type(value) is str and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value) is not None,
        "clock_unknown")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise Refused("clock_unknown") from None


def integer(value):
    need(type(value) is int and value >= 0, "counter_unknown")
    return value


def hexhash(value):
    need(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None, "hash_unknown")
    return value


def encoded(value, cap=STATE_BYTES):
    """Refuse cycles/depth/resources and foreign objects BEFORE JSON encoding."""
    stack, visited = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        need(depth <= 32 and visited <= cap, "resource_overflow")
        if type(item) is dict:
            need(all(type(k) is str for k in item), "json_shape_unknown")
            stack.extend((v, depth + 1) for v in item.values())
        elif type(item) is list:
            stack.extend((v, depth + 1) for v in item)
        elif type(item) is str:
            need(len(item) <= cap, "resource_overflow")
        elif type(item) is int:
            need(item.bit_length() <= 4096, "json_number_unknown")
        elif type(item) is float:
            need(math.isfinite(item), "json_number_unknown")
        else:
            need(item is None or type(item) is bool, "json_shape_unknown")
    try:
        chunks, size = [], 0
        for chunk in json.JSONEncoder(sort_keys=True, ensure_ascii=True, allow_nan=False,
                                      separators=(",", ":")).iterencode(value):
            part = chunk.encode("ascii")
            size += len(part)
            need(size <= cap, "resource_overflow")
            chunks.append(part)
        return b"".join(chunks)
    except Refused:
        raise
    except (ValueError, UnicodeError, OverflowError, RecursionError):
        raise Refused("json_encoding_unknown") from None


def fingerprint(value, cap=STATE_BYTES):
    return hashlib.sha256(encoded(value, cap)).hexdigest()


def references(values, *, empty=False):
    need(type(values) is list and (0 if empty else 1) <= len(values) <= MAX_REFS,
         "evidence_refs_unknown")
    for value in values:
        ref(value)
    need(len(set(values)) == len(values), "evidence_refs_duplicate")
    return set(values)


def policy_binding(policy):
    encoded(policy, FRAME_BYTES)
    need(type(policy) is dict and policy.get("schema_version") == SCHEMA
         and policy.get("enabled") is True and policy.get("mode") == "SHADOW", "policy_unknown")
    for key in ("policyversion", "approval_ref", "calibration_ref"):
        ref(policy.get(key))
    bindings = policy.get("source_bindings")
    need(type(bindings) is dict and set(bindings) == {"L2", "L3"}, "source_bindings_unknown")
    for binding in bindings.values():
        need(type(binding) is dict and set(binding) == {"source_head", "file_sha256"}, "source_bindings_unknown")
        need(type(binding["source_head"]) is str and re.fullmatch(r"[a-f0-9]{40}", binding["source_head"]) is not None,
             "source_head_unknown")
        hexhash(binding["file_sha256"])
    need(policy.get("severity_map", {}) == {}, "numeric_ranking_uncalibrated")
    category_binding(policy)
    versions = policy.get("upstream_policyversions")
    need(type(versions) is dict and set(versions) == {"L2", "L3"}, "upstream_policy_unknown")
    for version in versions.values():
        ref(version)
    return fingerprint(policy, FRAME_BYTES)


def category_binding(policy):
    selected = policy.get("category_map")
    need(type(selected) is dict and set(selected) == {"schema_version", "default", "exact", "source_bound_true"}
         and selected["schema_version"] == "smartalert1.category.v1"
         and selected["default"] == "DIGEST", "category_policy_unknown")
    exact = selected["exact"]
    need(type(exact) is dict and set(exact) <= {"L2", "L3"}, "category_lane_unknown")
    for lane, classes in exact.items():
        need(type(classes) is dict, "category_class_unknown")
        for cls, reasons in classes.items():
            code(cls)
            need(type(reasons) is dict, "category_reason_unknown")
            for reason, category in reasons.items():
                code(reason)  # '*' is deliberately not an accepted selector.
                need(category in ("PAGE", "DIGEST"), "category_value_unknown")
    dynamic = selected["source_bound_true"]
    need(type(dynamic) is dict and set(dynamic) <= {"L2"}, "dynamic_category_lane_unknown")
    rules = dynamic.get("L2", {})
    need(type(rules) is dict and set(rules) <= {"A3", "A7"}, "dynamic_category_class_unknown")
    for cls, rule in rules.items():
        need(type(rule) is dict and set(rule) == {"category", "reason_field", "source_file_sha256"}
             and rule["category"] == "PAGE"
             and rule["reason_field"] == ("refusal_code" if cls == "A3" else "block_reason"),
             "dynamic_category_rule_unknown")
        need(rule["source_file_sha256"] == L2_TRUE_RULE_SHA256
             and policy["source_bindings"]["L2"]["file_sha256"] == L2_TRUE_RULE_SHA256,
             "dynamic_category_source_mismatch")
        need(cls not in exact.get("L2", {}), "dynamic_and_exact_category_overlap")
    return selected


def shadow_category(lane, finding, policy):
    if finding["status"] != "TRUE" or finding.get("manual") is not False or finding.get("paper") is not False:
        return "DIGEST"
    selected = policy["category_map"]  # validated and sealed by policy_binding
    exact = selected["exact"].get(lane, {}).get(finding["class"], {})
    if finding["reason_code"] in exact:
        return exact[finding["reason_code"]]
    if lane == "L2" and finding["class"] in selected["source_bound_true"].get("L2", {}):
        return "PAGE"  # exact qualified L2 bytes preserve all original TRUE guards
    return "DIGEST"


def initial_state(*, campaign_id, ledger_epoch, policy):
    """Explicit provisioning data, never called automatically on missing state."""
    return {"schema_version": SCHEMA, "campaign_id": ref(campaign_id), "ledger_epoch": ref(ledger_epoch),
            "revision": 0, "policy_sha256": policy_binding(policy), "last_as_of": None,
            "batch_receipts": {}, "incidents": {}, "digests": {}, "calibration": "UNCALIBRATED"}


def incident_id(lane, finding):
    need(lane in {"L2", "L3"}, "lane_unknown")
    key = finding.get("incident_key")
    need(type(key) is str and len(key) <= 4096, "incident_identity_unknown")
    try:
        fields = json.loads(key)
    except (ValueError, TypeError, RecursionError):
        raise Refused("incident_identity_unknown") from None
    expected = [FINDING_SCHEMA] + [finding.get(k) for k in
        ("class", "account", "broker", "bot", "symbol", "subject_id")]
    need(type(fields) is list and len(fields) == 8 and fields[:7] == expected, "incident_identity_unknown")
    ref(fields[7])
    return encoded([lane, key]).decode("ascii")


def _subject(finding):
    code(finding.get("class"))
    for field in ("account", "bot", "symbol", "subject_id"):
        if finding.get(field) is not None:
            ref(finding[field])
    need(finding.get("broker") in {"schwab", "webull", "system", "UNKNOWN"}, "broker_unknown")
    return [finding.get(k) for k in ("class", "account", "broker", "bot", "symbol", "subject_id")]


def four_parts(finding, first_seen):
    """Only constrained subject/code text; ignore arbitrary recommended_action."""
    subject = _subject(finding)
    named_subject = finding.get("symbol") or finding.get("subject_id")
    what = " / ".join(str(v) if v is not None else "UNKNOWN" for v in
                      (named_subject, finding.get("bot"), subject[2], subject[0]))
    action = finding.get("action_taken")
    if type(action) is dict:
        need(set(action) == {"code", "evidence_ids"}, "action_shape_unknown")
        label = code(action.get("code"))
        need(references(action.get("evidence_ids")) <= references(finding.get("evidence_ids"), empty=True),
             "action_proof_unknown")
        did = "Evidence-bound action: " + label
    else:
        label = code(action)
        did = "Action not verified" if label in {"UNKNOWN", "none_recorded"} else "Recorded action: " + label
    advice = ("FYI, nothing to do under the supplied affirmative evidence." if finding["status"] == "FALSE"
              else "Review missing or expired evidence; do not infer recovery." if finding["status"] == "UNKNOWN"
              else "Review referenced evidence; no automatic trading action.")
    return {"what": what + ": " + code(finding.get("reason_code")),
            "since_when": first_seen or "UNKNOWN", "system_did": did, "what_to_do": advice}


def _finding(item, manifest, as_of, policy):
    need(type(item) is dict and item.get("lane") in {"L2", "L3"}, "finding_binding_unknown")
    finding = item.get("finding")
    need(type(finding) is dict and finding.get("schema_version") == FINDING_SCHEMA, "finding_schema_unknown")
    need(finding.get("policyversion") == policy["upstream_policyversions"][item["lane"]], "upstream_policy_mismatch")
    key = incident_id(item["lane"], finding)
    identity = _subject(finding)
    need(finding.get("status") in {"TRUE", "FALSE", "UNKNOWN"}, "finding_status_unknown")
    need(finding.get("paging_eligible") is False, "nonshadow_input_refused")
    ref(item.get("source_epoch"))
    ref(item.get("expectation_ref"))
    source_ref = ref(item.get("source_ref"))
    evidence = manifest.get("evidence")
    need(type(evidence) is dict and source_ref in evidence, "source_provenance_missing")
    hexhash(evidence[source_ref])
    need(hexhash(item.get("source_hash")) == evidence[source_ref], "source_provenance_mismatch")
    refs = references(finding.get("evidence_ids"), empty=finding["status"] == "UNKNOWN")
    for evidence_ref in refs:
        need(evidence_ref in evidence, "evidence_provenance_missing")
        hexhash(evidence[evidence_ref])
    need(clock(item.get("evaluated_at")) == as_of, "evaluation_cutoff_mismatch")
    freshness, qualification = finding.get("freshness"), finding.get("qualification")
    need(type(freshness) is dict and type(qualification) is dict, "qualification_unknown")
    need(set(qualification) <= {"mode", "qualified_policy", "coverage_complete", "reason", "evidence_refs",
                                "delivery_mode", "shadow_only"}, "qualification_shape_unknown")
    need(qualification.get("mode") in {"OFF", "REPORT_ONLY", "CANDIDATE_REPLAY", "QUALIFIED_LIVE", "UNKNOWN"}
         and type(qualification.get("qualified_policy")) is bool
         and type(qualification.get("coverage_complete")) is bool, "qualification_unknown")
    code(qualification.get("reason"))
    need(references(qualification.get("evidence_refs"), empty=True) <= refs, "qualification_refs_unknown")
    shadow = finding.get("shadow")
    if shadow is not None:
        need(type(shadow) is dict and set(shadow) == {"mode", "delivery_action", "existing_senders_unchanged",
            "would_page_under_supplied_policy", "candidate_detection"} and shadow["mode"] == "SHADOW"
            and shadow["delivery_action"] == "NONE" and shadow["existing_senders_unchanged"] is True
            and type(shadow["would_page_under_supplied_policy"]) is bool
            and type(shadow["candidate_detection"]) is bool, "shadow_projection_unknown")
    observed = finding.get("last_seen")
    if observed is not None:
        need(clock(observed) <= as_of, "future_observation")
    first = finding.get("first_seen")
    if first is not None:
        need(observed is not None and clock(first) <= clock(observed), "incident_clock_unknown")
    positive = finding["status"] != "UNKNOWN"
    if positive:
        need(first is not None and observed is not None and freshness.get("status") == "FRESH"
             and qualification.get("coverage_complete") is True, "positive_finding_not_fresh_complete")
        need(clock(freshness.get("observed_at")) == clock(observed)
             and clock(freshness.get("event_at")) <= clock(observed), "embedded_freshness_clock_unknown")
        need(type(finding.get("paper")) is bool and type(finding.get("manual")) is bool, "ownership_unknown")
        need(finding.get("broker") != "UNKNOWN" and finding.get("subject_id") is not None, "positive_scope_unknown")
        if item["lane"] == "L2" and finding["paper"] is False:
            account, symbol, bot = finding.get("account"), finding.get("symbol"), finding.get("bot")
            need(type(account) is str and account.startswith("live:") and type(symbol) is str
                 and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", symbol) is not None
                 and (bot in {"schwab_1m_v2", "orb_schwab", "momentum_warn_live"}
                      or finding["manual"] is True and bot is None), "positive_live_trade_scope_unknown")
    expected = freshness.get("expectation_ref")
    need(expected is None or expected == item["expectation_ref"], "expectation_binding_mismatch")
    four_parts(finding, first)  # Validate constrained message inputs before any transition.
    return key, identity, refs


def reviewed_close_window(window, manifest, policy):
    """Validate an explicitly reviewed window, never invent a market close.

    Calendar correctness is an evidence/approval obligation: this validator
    checks exact sealed membership, date/offset, open-session and late policy.
    It does not obtain a calendar or infer holidays/early closes from weekdays.
    """
    calendar = policy.get("close_calendar")
    need(type(calendar) is dict and calendar.get("schema_version") == "smartalert1.close-calendar.v1",
         "close_calendar_needs_ruling")
    need(calendar.get("timezone") == "America/New_York"
         and calendar.get("late_policy") == "NO_BACKDATED_DIGEST", "close_calendar_policy_unknown")
    evidence = manifest.get("evidence")
    need(type(evidence) is dict, "calendar_evidence_unknown")
    for key in ("calendar_ref", "approval_ref"):
        value = ref(calendar.get(key))
        need(value in evidence, "calendar_review_proof_missing")
        hexhash(evidence[value])
    entries = calendar.get("windows")
    need(type(entries) is list and 0 < len(entries) <= MAX_ITEMS, "close_calendar_windows_unknown")
    selected, seen = None, set()
    for entry in entries:
        need(type(entry) is dict, "close_calendar_entry_unknown")
        identity = ref(entry.get("window_id"))
        need(identity not in seen, "close_calendar_duplicate_window")
        seen.add(identity)
        need(type(entry.get("open_session")) is bool, "close_calendar_session_unknown")
        proof = ref(entry.get("session_proof_ref"))
        need(proof in evidence, "calendar_session_proof_missing")
        hexhash(evidence[proof])
        if identity == window.get("window_id"):
            selected = entry
    need(selected is not None and selected["open_session"] is True, "no_reviewed_open_close_window")
    need(window.get("calendar_ref") == calendar["calendar_ref"], "close_calendar_binding_mismatch")
    for key in ("start", "end"):
        need(clock(window.get(key)) == clock(selected.get(key)), "close_window_changed")
    start, end = clock(selected["start"]), clock(selected["end"])
    need(start < end and selected.get("close_kind") in ("REGULAR", "EARLY", "EXTENDED"),
         "close_calendar_range_unknown")
    day = selected.get("trading_date")
    need(type(day) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is not None,
         "close_calendar_date_unknown")
    need(end.astimezone(ZoneInfo("America/New_York")).date().isoformat() == day,
         "close_calendar_civil_date_mismatch")
    return selected


def _digest(window, state, now, manifest, policy):
    if window is None:
        return {"status": "UNKNOWN", "reason_code": "digest_calendar_missing", "delivery_action": "NONE"}
    need(type(window) is dict, "digest_window_unknown")
    reviewed_close_window(window, manifest, policy)
    key = ref(window.get("window_id"))
    ref(window.get("calendar_ref"))
    start, end = clock(window.get("start")), clock(window.get("end"))
    need(start < end <= now, "digest_window_unknown")
    if key in state["digests"]:
        need(state["digests"][key]["window_sha256"] == fingerprint(window), "digest_window_identity_changed")
        return {"status": "ALREADY_RECORDED", "window_id": key, "delivery_action": "NONE"}
    required = policy.get("required_digest_coverage")
    coverage = manifest.get("coverage")
    complete = type(required) is list and bool(required) and type(coverage) is dict
    if complete:
        for scope in required:
            ref(scope)
            proof = coverage.get(scope)
            if type(proof) is not dict or proof.get("complete") is not True:
                complete = False
                break
            try:
                interval_complete = (clock(proof.get("coverage_start")) <= start
                    and end <= clock(proof.get("coverage_end")) and clock(proof.get("as_of")) == now)
            except Refused:
                interval_complete = False
            if not interval_complete:
                complete = False
                break
            if not references(proof.get("evidence_ids")) <= set(manifest["evidence"]):
                complete = False
                break
            for evidence_ref in proof["evidence_ids"]:
                hexhash(manifest["evidence"][evidence_ref])
    if end != now or window.get("coverage_complete") is not True or not complete:
        return {"status": "UNKNOWN", "reason_code": "digest_cutoff_or_coverage_unproven", "delivery_action": "NONE"}
    counts = {"opened": 0, "escalated": 0, "cleared": 0, "still_open": 0,
              "unknown": 0, "manual_digest_only": 0, "observations": 0, "digest_only_true": 0}
    for entry in state["incidents"].values():
        kinds = set()
        for event in entry["transitions"]:
            if start < clock(event["as_of"]) <= end:
                kinds.add(event["kind"])
        for kind in kinds:
            counts[{"WOULD_OPEN": "opened", "WOULD_ESCALATE": "escalated", "WOULD_CLEAR": "cleared"}[kind]] += 1
        counts["still_open"] += int(entry["opened"] and not entry["cleared"])
        counts["unknown"] += int(entry["last_status"] == "UNKNOWN")
        counts["manual_digest_only"] += int(entry["manual"] is True)
        counts["digest_only_true"] += int(entry["last_status"] == "TRUE" and entry["shadow_category"] == "DIGEST")
        counts["observations"] += entry["observations"]  # Labelled campaign total, not period denominator.
    result = {"status": "SHADOW_DIGEST", "window_id": key, "counts": counts,
              "observation_scope": "CAMPAIGN_TOTAL", "delivery_action": "NONE", "paging_eligible": False,
              "calibration": "UNCALIBRATED", "escalation": "DISABLED_PENDING_FORWARD_CALIBRATION"}
    state["digests"][key] = {"window_sha256": fingerprint(window), "digest_sha256": fingerprint(result)}
    return result


def plan_shadow(batch, prior_state, *, as_of, policy, resolution_links=(), digest_window=None):
    """No source repair, transport, implicit clock or mutation of supplied objects."""
    if policy is None or type(policy) is dict and (policy.get("enabled", False) is False or policy.get("mode") == "OFF"):
        return {"status": "OFF", "next_state": prior_state, "records": [], "delivery_action": "NONE", "paging_eligible": False}
    try:
        binding = policy_binding(policy)
        encoded(batch, FRAME_BYTES)
        encoded(prior_state, STATE_BYTES)
        now = clock(as_of)
        need(type(prior_state) is dict and prior_state.get("schema_version") == SCHEMA
             and prior_state.get("policy_sha256") == binding, "prior_state_or_policy_binding_unknown")
        ref(prior_state.get("campaign_id"))
        ref(prior_state.get("ledger_epoch"))
        revision = integer(prior_state.get("revision"))
        for field in ("incidents", "batch_receipts", "digests"):
            need(type(prior_state.get(field)) is dict, "prior_state_unknown")
        need(len(prior_state["incidents"]) <= MAX_ITEMS, "incident_resource_overflow")
        need(type(batch) is dict and batch.get("schema_version") == SCHEMA
             and batch.get("source_bindings") == policy["source_bindings"]
             and batch.get("policy_sha256") == binding, "batch_bindings_unknown")
        need(type(resolution_links) in (list, tuple) and len(resolution_links) <= MAX_ITEMS, "resolution_links_unknown")
        batch_id = ref(batch.get("batch_id"))
        batch_hash = fingerprint({"batch": batch, "as_of": as_of,
            "resolution_links": list(resolution_links), "digest_window": digest_window})
        sequence = integer(batch.get("sequence"))
        old_receipt = prior_state["batch_receipts"].get(batch_id)
        if old_receipt is not None:
            need(old_receipt == {"sha256": batch_hash, "sequence": sequence}, "duplicate_batch_changed")
            return {"status": "IDEMPOTENT", "next_state": prior_state, "records": [],
                    "delivery_action": "NONE", "paging_eligible": False}
        need(sequence == revision + 1, "batch_sequence_conflict")
        need(prior_state["last_as_of"] is None or clock(prior_state["last_as_of"]) <= now, "observation_order_unknown")
        manifest = batch.get("manifest")
        need(type(manifest) is dict and manifest.get("complete") is True and manifest.get("overflow") is False
             and manifest.get("read_error") is False, "producer_coverage_unknown")
        ref(manifest.get("ref"))
        hexhash(manifest.get("sha256"))
        need(clock(manifest.get("as_of")) == now, "producer_cutoff_mismatch")
        items = batch.get("findings")
        need(type(items) is list and len(items) <= MAX_ITEMS, "finding_resource_overflow")
        links = {}
        for link in resolution_links:
            need(type(link) is dict and type(link.get("clearing_key")) is str, "resolution_link_unknown")
            need(link["clearing_key"] not in links, "ambiguous_resolution_links")
            links[link["clearing_key"]] = link
        state, records, seen, used_links = deepcopy(prior_state), [], set(), set()
        for item in items:
            key, identity, refs = _finding(item, manifest, now, policy)
            need(key not in seen, "duplicate_incident_in_batch")
            seen.add(key)
            finding = item["finding"]
            if finding.get("paper") is True:
                continue
            fresh_binding = {"identity": identity, "source_epoch": item["source_epoch"],
                             "expectation_ref": item["expectation_ref"]}
            old = state["incidents"].get(key)
            if old is not None:
                need(old["binding"] == fresh_binding and
                     (finding["first_seen"] is None or old["first_seen"] in (None, finding["first_seen"])),
                     "original_incident_binding_changed")
            entry = deepcopy(old) if old else {"binding": fresh_binding, "first_seen": finding["first_seen"],
                "opened": False, "cleared": False, "max_rank": None, "observations": 0,
                "transitions": [], "manual": finding.get("manual"), "last_verified_seen": None}
            if entry["first_seen"] is None and finding["first_seen"] is not None:
                entry["first_seen"] = finding["first_seen"]
            entry.update(last_status=finding["status"], last_seen=finding["last_seen"],
                         evidence_ids=sorted(refs), reason_code=finding["reason_code"])
            if finding["status"] != "UNKNOWN":
                entry["last_verified_seen"] = finding["last_seen"]
            entry["observations"] += 1
            category = shadow_category(item["lane"], finding, policy)
            # Finding eligibility, never a notification counter: only a
            # WOULD_OPEN transition represents a new hypothetical page.
            would_page = category == "PAGE" and finding["status"] == "TRUE"
            entry.update(shadow_category=category, would_page=would_page, calibration="UNCALIBRATED")
            kind = "OBSERVATION"
            if finding["status"] == "TRUE" and finding.get("manual") is False:
                # Clearing ends the causal epoch regardless of routing category.
                need(not entry["cleared"], "recurrence_requires_new_causal_epoch")
            if would_page:
                if not entry["opened"]:
                    kind, entry["opened"] = "WOULD_OPEN", True
                # No relative rank or WOULD_ESCALATE until forward calibration.
            elif finding["status"] == "FALSE" and key in links:
                need(finding["reason_code"] not in {"stops_shadow_persisted_attachment_evidence_only",
                     "stops_shadow_existing_stop_or_managed"}, "persisted_handle_not_native_recovery_proof")
                link = links[key]
                used_links.add(key)
                target_key = link.get("incident_key")
                target = entry if target_key == key else state["incidents"].get(target_key)
                need(type(target) is dict and target["opened"], "resolution_target_unknown")
                need(target.get("manual") is False and finding.get("manual") is False,
                     "manual_reclassification_not_risk_recovery")
                need(target["binding"]["identity"] == identity
                     and target["binding"]["expectation_ref"] == item["expectation_ref"]
                     and link.get("prior_source_epoch") == target["binding"]["source_epoch"]
                     and link.get("current_source_epoch") == item["source_epoch"]
                     and link.get("reason_code") == finding["reason_code"], "resolution_identity_unknown")
                affirmative = references(link.get("affirmative_refs"))
                need(affirmative <= refs, "resolution_affirmative_proof_unknown")
                if not target["cleared"]:
                    target["cleared"] = True
                    event = {"kind": "WOULD_CLEAR", "as_of": as_of}
                    target["transitions"].append(event)
                    records.append(dict(event, incident_key=target_key, clearing_key=key,
                        evidence_ids=sorted(affirmative), message=four_parts(finding, target["first_seen"]),
                        delivery_action="NONE", paging_eligible=False, shadow_category="DIGEST",
                        would_page=False, calibration="UNCALIBRATED"))
                if target_key != key:
                    state["incidents"][target_key] = target
            if kind != "OBSERVATION":
                entry["transitions"].append({"kind": kind, "as_of": as_of})
            records.append({"kind": kind, "incident_key": key, "status": finding["status"],
                "action_taken": deepcopy(finding["action_taken"]), "qualification": deepcopy(finding["qualification"]),
                "upstream_shadow": deepcopy(finding.get("shadow")),
                "shadow_category": category, "would_page": would_page, "calibration": "UNCALIBRATED",
                "message": four_parts(finding, entry["first_seen"]), "delivery_action": "NONE", "paging_eligible": False})
            state["incidents"][key] = entry
            need(len(state["incidents"]) <= MAX_ITEMS, "incident_resource_overflow")
        need(used_links == set(links), "unmatched_resolution_link")
        state["revision"], state["last_as_of"] = revision + 1, as_of
        state["batch_receipts"][batch_id] = {"sha256": batch_hash, "sequence": sequence}
        digest = _digest(digest_window, state, now, manifest, policy)
        encoded(state, STATE_BYTES)
        result = {"status": "SHADOW", "next_state": state, "records": records, "digest": digest,
                  "delivery_action": "NONE", "paging_eligible": False, "calibration": "UNCALIBRATED"}
        encoded(result, STATE_BYTES)  # Generation includes state+records, not separate cap loopholes.
        return result
    except Refused as exc:
        return {"status": "REFUSED", "reason_code": exc.args[0], "next_state": None,
                "records": [], "delivery_action": "NONE", "paging_eligible": False}
    except (TypeError, KeyError, ValueError, OverflowError, RecursionError, AttributeError):
        return {"status": "REFUSED", "reason_code": "malformed_input", "next_state": None,
                "records": [], "delivery_action": "NONE", "paging_eligible": False}
