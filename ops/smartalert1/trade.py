"""SMARTALERT1 L2 pure trade-risk evaluation; no acquisition or delivery.

Consumes Main's smartalert1.v1 envelope. Lane payload grammar:
  expectation_ref, source_head, session, checks: list[dict]. Each check has
  class (A1..A7), condition_epoch (causal identity), first_seen (offset time),
  action_taken (one code below), operands (typed fields used by _risk).
Policy expectations: version, source_head, settings_ref, routing_ref,
  calendar_ref, routes {"bot|session": [broker,...]}, and legal_holds [codes].
Each class window: freshness_seconds; A2/A5/A6/A7 and A4's optional
intent_without_broker_order assertion also require wait_seconds for TRUE risk.
The lifecycle cohort grammar is documented in _wbattach_lifecycle and the
external WBATTACH proposal; accepted placement never attests current protection.
Candidate windows never qualify paging. Other modes require reviewed policy.
This successor is SHADOW ONLY: even QUALIFIED_LIVE cannot enable delivery.

Operand booleans are positive source attestations, not defaults inferred from
missing rows. Source evidence IDs must resolve to their complete, as-of identity
sets in Main's retained acquisition manifest. No checker repairs or trading.
"""

import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

SCHEMA = "smartalert1.v1"
CLASSES = ("A1", "A2", "A3", "A4", "A5", "A6", "A7")
BOTS = ("schwab_1m_v2", "orb_schwab", "momentum_warn_live")
MODES = ("OFF", "REPORT_ONLY", "CANDIDATE_REPLAY", "QUALIFIED_LIVE")
ACTIONS = ("none_recorded", "protection_restored", "exit_filled", "cancel_requested",
           "refusal_logged", "software_exit_active", "awaiting_trigger", "owner_hold")
ENVELOPE = ("schema_version", "source_id", "source_kind", "source_hash", "source_epoch",
            "observed_at", "event_at", "received_at", "coverage_start", "coverage_end",
            "complete", "overflow", "read_error", "evidence_ids", "subject", "payload")


class EvidenceUnknown(ValueError):
    """Only fixed internal codes escape into findings; never exception strings."""


def _ref(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) is not None


def _code(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value) is not None


def _clock(value):
    if (not isinstance(value, str) or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
            value) is None):
        raise EvidenceUnknown("clock_unknown")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise EvidenceUnknown("clock_unknown") from None


def _stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def _json_bytes(value, cap):
    """Bound encoding work/output; refuse cycles, non-JSON and excessive nesting."""
    stack = [(value, 0)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if depth > 32 or visited > cap:
            raise EvidenceUnknown("input_resource_overflow")
        if type(item) is dict:
            if len(item) > 1000 or any(type(k) is not str for k in item):
                raise EvidenceUnknown("json_shape_unknown")
            stack.extend((v, depth + 1) for v in item.values())
        elif type(item) is list:
            if len(item) > 1000:
                raise EvidenceUnknown("input_record_overflow")
            stack.extend((v, depth + 1) for v in item)
        elif type(item) is str and len(item) > cap:
            raise EvidenceUnknown("input_resource_overflow")
        elif type(item) is int and item.bit_length() > 4096:
            raise EvidenceUnknown("json_value_unknown")
        elif item is not None and type(item) not in (str, bool, int, float):
            raise EvidenceUnknown("json_shape_unknown")
    size = 0
    try:
        for chunk in json.JSONEncoder(allow_nan=False, separators=(",", ":")).iterencode(value):
            size += len(chunk.encode("utf-8"))
            if size > cap:
                raise EvidenceUnknown("input_resource_overflow")
    except (ValueError, OverflowError, UnicodeError, RecursionError):
        raise EvidenceUnknown("json_value_unknown") from None
    return size


def _bool(data, key):
    value = data.get(key)
    if type(value) is not bool:
        raise EvidenceUnknown("operand_boolean_unknown")
    return value


def _decimal(data, key):
    value = data.get(key)
    if not isinstance(value, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,18})?", value) is None:
        raise EvidenceUnknown("operand_decimal_unknown")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise EvidenceUnknown("operand_decimal_unknown") from None
    if not result.is_finite():
        raise EvidenceUnknown("operand_decimal_unknown")
    return result


def _ids(data, key, evidence_ids):
    value = data.get(key)
    if (type(value) is not list or len(value) > 64 or len(set(x for x in value if _ref(x))) != len(value)
            or any(not _ref(x) or x not in evidence_ids for x in value)):
        raise EvidenceUnknown("operand_identity_unknown")
    return value


def _seconds(data, key):
    value = data.get(key)
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise EvidenceUnknown("window_unknown")
    return value


def _window(policy, cls):
    windows = policy.get("windows")
    window = windows.get(cls) if type(windows) is dict else None
    if type(window) is not dict:
        raise EvidenceUnknown("window_unknown")
    _seconds(window, "freshness_seconds")
    if cls in ("A2", "A5", "A6", "A7"):
        _seconds(window, "wait_seconds")
    if policy.get("mode") != "CANDIDATE_REPLAY" and not (
            policy.get("qualified") is True and _ref(policy.get("approval_ref"))
            and _ref(policy.get("calibration_ref")) and window.get("qualified") is True):
        raise EvidenceUnknown("window_unqualified")
    return window


def _legal(operands, expectations):
    hold = operands.get("legal_hold")
    if hold is None:
        raise EvidenceUnknown("hold_reason_unknown")
    if hold == "none":
        return False
    approved = expectations.get("legal_holds")
    if (type(approved) is not list or not _code(hold) or hold not in approved
            or not _bool(operands, "legal_hold_proven")):
        raise EvidenceUnknown("hold_reason_unknown")
    return True


def _wbattach_lifecycle(data, refs, elapsed, window, subject):
    """Only an exact positive journal explains the no-BO attach lifecycle.

    This says nothing about CURRENT native WORKING state or A1 protection.
    Full typed row absence is represented by explicit None + cohort_complete;
    missing fields/partial reads remain UNKNOWN. Never emit private row payloads.
    """
    for key in ("cohort_complete", "entry_owned_proven", "attempts_complete"):
        if not _bool(data, key):
            raise EvidenceUnknown("attach_cohort_unproven")

    def retained(reason):
        if elapsed < _seconds(window, "wait_seconds"):
            raise EvidenceUnknown("observation_window_open")
        return True, reason

    linked = _ids(data, "linked_order_ids", refs)
    intent = data.get("intent")
    if type(intent) is not dict:
        raise EvidenceUnknown("attach_intent_identity_unknown")
    fields = ("id", "account", "bot", "broker", "symbol")
    if any(not _ref(intent.get(k)) for k in fields):
        raise EvidenceUnknown("attach_row_identity_unknown")
    if (intent["id"] != subject.get("subject_id")
            or any(intent[k] != subject.get(k) for k in fields[1:])):
        return retained("lifecycle_intent_identity_mismatch")
    status = intent.get("status")
    if type(status) is not str or status not in (
            "accepted", "pending", "submitted", "filled", "rejected", "cancelled", "expired", "abandoned"):
        raise EvidenceUnknown("attach_intent_status_unknown")
    if status in ("filled", "rejected", "cancelled", "expired", "abandoned"):
        return False, "positive_terminal_intent"
    if linked:
        if not _bool(data, "linked_orders_bound"):
            raise EvidenceUnknown("linked_order_identity_unknown")
        return False, "positive_bound_order_exists"
    for key in ("intent", "entry", "managed"):
        if key not in data:
            raise EvidenceUnknown("attach_row_missing_from_payload")
        if data[key] is None:
            return retained("lifecycle_owner_row_proven_missing")
        if type(data[key]) is not dict:
            raise EvidenceUnknown("attach_row_shape_unknown")
    intent, entry, managed = (data[k] for k in ("intent", "entry", "managed"))
    for row in (intent, entry, managed):
        if any(not _ref(row.get(k)) for k in fields):
            raise EvidenceUnknown("attach_row_identity_unknown")
        if any(row[k] != subject.get(k) for k in ("account", "bot", "broker", "symbol")):
            return retained("lifecycle_owner_identity_mismatch")
    if intent["id"] != subject.get("subject_id"):
        return retained("lifecycle_intent_identity_mismatch")
    if (subject.get("broker") != "webull" or subject.get("bot") != "schwab_1m_v2"
            or intent.get("reason") != "WBATTACH1_PROTECTION"
            or intent.get("side") != "sell" or intent.get("intent_type") != "close"):
        return retained("lifecycle_no_matching_attach_contract")
    metadata, journal = intent.get("metadata"), entry.get("journal")
    if type(metadata) is not dict or type(journal) is not dict:
        raise EvidenceUnknown("attach_journal_unknown")
    if (entry.get("side") != "buy" or not _ref(entry.get("broker_order_id"))
            or not _ref(entry.get("client_order_id"))
            or metadata.get("entry_order_id") != entry["id"]
            or metadata.get("managed_row_id") != managed["id"]
            or managed.get("entry_order_id") != entry["id"]
            or managed.get("entry_client_order_id") != entry["client_order_id"]
            or journal.get("entry_id") != entry["id"] or journal.get("managed_id") != managed["id"]):
        return retained("lifecycle_entry_association_unproven")
    attempts = journal.get("attempts")
    if type(attempts) is not list or len(attempts) > 1000 or any(type(a) is not dict for a in attempts):
        raise EvidenceUnknown("attach_attempt_set_unknown")
    matches = [a for a in attempts if a.get("intent_id") == intent["id"]]
    if len(matches) != 1:
        return retained("lifecycle_attempt_identity_ambiguous")
    attempt = matches[0]
    base = metadata.get("webull_protect_base_client_order_id")
    combo = metadata.get("webull_protect_combo_id")
    if (not _ref(base) or not _ref(combo) or attempt.get("base") != base
            or attempt.get("combo_id") != combo or intent.get("status") != "accepted"
            or attempt.get("phase") != "accepted"):
        return retained("lifecycle_ack_missing_or_mismatched")
    if len([a for a in attempts if a.get("base") == base]) != 1:
        return retained("lifecycle_attempt_base_ambiguous")
    if "exit_phase" not in journal:
        raise EvidenceUnknown("attach_exit_phase_unknown")
    if _bool(attempt, "cancel_requested") or journal["exit_phase"] is not None:
        return retained("lifecycle_attach_exit_or_cancel_unresolved")
    if any(a is not attempt and a.get("phase") != "released" for a in attempts):
        return retained("lifecycle_competing_attempt_unresolved")
    quantity = _decimal(intent, "quantity")
    if quantity <= 0 or _decimal(attempt, "quantity") != quantity:
        return retained("lifecycle_attach_quantity_mismatch")
    # Literal stdlib transcription of Webull._combo_leg_coid(base, suffix),
    # webull.py2613–2619 at source32. Never import a broker/APP module here.
    if [metadata.get("webull_protect_target_client_order_id"),
            metadata.get("webull_protect_stop_client_order_id")] != [base[:39] + "T", base[:39] + "S"]:
        return retained("lifecycle_attach_child_identity_mismatch")
    return False, "positive_journaled_attach_no_bo_by_design"


def _stops_armed_shadow(data, refs, window, subject, observed):
    """Held b706 evidence tightening, not a native-protection assertion.

    Complete supplied as-of rows only; None is an explicit absent row. The
    caller supplies the reviewed settle window: no invented numeric default.
    Missing old payloads remain UNKNOWN until explained in the shadow ledger.
    Native-negative proof, if supplied, overrides a persisted resting handle.
    """
    if not _bool(data, "cohort_complete"):
        raise EvidenceUnknown("stops_shadow_cohort_incomplete")
    if "position" not in data or data["position"] is None:
        raise EvidenceUnknown("stops_shadow_position_unknown")
    position = data["position"]

    def bound(row):
        if type(row) is not dict or not _ref(row.get("id")):
            raise EvidenceUnknown("stops_shadow_row_unknown")
        if any(row.get(k) != subject.get(k) for k in ("account", "broker", "bot", "symbol")):
            raise EvidenceUnknown("stops_shadow_row_identity_unknown")
        return row

    bound(position)
    if position["id"] != subject.get("subject_id"):
        raise EvidenceUnknown("stops_shadow_position_identity_unknown")
    quantity = _decimal(position, "quantity")
    opened = _clock(position.get("opened_at"))
    if opened > observed:
        raise EvidenceUnknown("stops_shadow_open_clock_unknown")
    if quantity == 0:
        return False, "stops_shadow_positive_owned_flat"
    if (observed - opened).total_seconds() <= _seconds(window, "settle_seconds"):
        raise EvidenceUnknown("stops_shadow_settle_window_open")
    for name in ("armed_stop", "managed", "entry", "native_negative"):
        if name not in data:
            raise EvidenceUnknown("stops_shadow_cohort_missing_field")
    armed, managed, entry = (bound(data[k]) if data[k] is not None else None
                             for k in ("armed_stop", "managed", "entry"))
    negative = data["native_negative"]
    if negative is not None:
        bound(negative)
        if (entry is None or managed is None or negative.get("position_id") != position["id"]
                or negative.get("entry_order_id") != entry["id"]
                or negative.get("managed_row_id") != managed["id"]
                or managed.get("entry_order_id") != entry["id"]
                or not _ref(managed.get("entry_client_order_id"))
                or managed["entry_client_order_id"] != entry.get("client_order_id")
                or not _ref(entry.get("pair_base"))
                or negative.get("pair_base") != entry["pair_base"]):
            raise EvidenceUnknown("stops_shadow_native_episode_unknown")
        if not _bool(negative, "complete") or not _bool(negative, "absence_proven"):
            raise EvidenceUnknown("stops_shadow_native_negative_unproven")
        if not _ids(negative, "evidence_ids", refs):
            raise EvidenceUnknown("stops_shadow_native_negative_unproven")
        at = _clock(negative.get("observed_at"))
        if not 0 <= (observed - at).total_seconds() <= window["freshness_seconds"]:
            raise EvidenceUnknown("stops_shadow_native_negative_stale")
        return True, "stops_shadow_positive_native_protection_absent"
    if subject["broker"] != "webull" or subject["bot"] != "schwab_1m_v2":
        covered = armed is not None or (managed is not None and managed.get("status") == "open")
        return not covered, "stops_shadow_existing_stop_or_managed" if covered else "stops_shadow_no_protection_row"
    if managed is None or entry is None:
        return True, "stops_shadow_bound_protection_row_absent"
    if managed.get("status") != "open":
        raise EvidenceUnknown("stops_shadow_open_managed_selection_unknown")
    if (managed.get("entry_order_id") != entry["id"]
            or not _ref(managed.get("entry_client_order_id"))
            or managed["entry_client_order_id"] != entry.get("client_order_id")):
        raise EvidenceUnknown("stops_shadow_entry_binding_unknown")
    if _decimal(managed, "current_quantity") < quantity:
        return True, "stops_shadow_insufficient_managed_quantity"
    for key in ("protection_state", "pair_base", "exit_phase"):
        if key not in entry:
            raise EvidenceUnknown("stops_shadow_protection_payload_unknown")
    if entry["protection_state"] is None:
        raise EvidenceUnknown("stops_shadow_legacy_payload_unexplained")
    if entry["protection_state"] in ("released", "attach_failed", "submitting"):
        return True, "stops_shadow_recorded_pair_not_resting"
    if entry["protection_state"] != "resting":
        raise EvidenceUnknown("stops_shadow_protection_state_unknown")
    if (entry.get("side") != "buy" or not _ref(entry.get("broker_order_id"))
            or not _ref(entry["pair_base"])):
        raise EvidenceUnknown("stops_shadow_attachment_identity_unknown")
    if entry["exit_phase"] is not None:
        if not _code(entry["exit_phase"]):
            raise EvidenceUnknown("stops_shadow_exit_phase_unknown")
        return True, "stops_shadow_exit_handoff_recorded"
    return False, "stops_shadow_persisted_attachment_evidence_only"


def _risk(cls, data, expectations, refs, elapsed, window, subject, observed):
    """Each branch requires affirmative coverage before a negative risk result."""
    if not _bool(data, "identity_complete"):
        raise EvidenceUnknown("identity_set_incomplete")
    if cls == "A4" and data.get("assertion") == "intent_without_broker_order":
        return _wbattach_lifecycle(data, refs, elapsed, window, subject)
    if cls == "A1" and data.get("assertion") == "stops_armed_shadow":
        return _stops_armed_shadow(data, refs, window, subject, observed)
    if cls == "A1":
        quantity = _decimal(data, "owned_quantity")
        native = _decimal(data, "native_protected_quantity")
        software = _decimal(data, "software_protected_quantity")
        if not _bool(data, "book_complete") or not _bool(data, "ownership_complete"):
            raise EvidenceUnknown("protection_book_unknown")
        if software and not (_bool(data, "software_owner_live") and _bool(data, "software_policy_allowed")):
            raise EvidenceUnknown("software_protection_unknown")
        # Coverage operands describe disjoint owned lots, never a sum of OCO legs.
        if not _bool(data, "coverage_disjoint") or native + software > quantity:
            raise EvidenceUnknown("protection_quantity_ambiguous")
        risk = native + software < quantity
        return risk, "owned_quantity_unprotected" if risk else "positive_protection_or_flat"
    if cls in ("A2", "A6"):
        if not _bool(data, "opportunity_complete") or not _bool(data, "outcome_complete"):
            raise EvidenceUnknown("opportunity_coverage_unknown")
        eligible = _bool(data, "entry_eligible")
        triggered = _bool(data, "trigger_reached")
        legal = _legal(data, expectations)
        orders = _ids(data, "matched_order_ids", refs)
        fills = _ids(data, "matched_fill_ids", refs)
        if legal or not eligible or not triggered:
            return False, "positive_lawful_nontrade"
        if orders or fills:
            return False, "positive_order_or_fill"
        if _bool(data, "software_rest_waiting_cross"):
            # A proved cross cannot simultaneously still be waiting for that cross.
            raise EvidenceUnknown("software_rest_cross_contradiction")
        if elapsed < window["wait_seconds"]:
            raise EvidenceUnknown("observation_window_open")
        return True, "eligible_opportunity_without_order"
    if cls == "A3":
        if not _bool(data, "refusal_proven") or not _bool(data, "admission_and_route_complete"):
            raise EvidenceUnknown("refusal_evidence_unknown")
        refusal = data.get("refusal_code")
        if not _code(refusal):
            raise EvidenceUnknown("refusal_reason_unknown")
        if _legal(data, expectations):
            return False, refusal
        if (not _bool(data, "admission_allowed") or not _bool(data, "quote_request_complete")
                or not _bool(data, "unexpected_refusal_proven")):
            raise EvidenceUnknown("unexpected_refusal_unproven")
        return True, refusal
    if cls == "A4":
        if not _bool(data, "book_complete") or not _bool(data, "ownership_complete"):
            raise EvidenceUnknown("ownership_book_unknown")
        native = _decimal(data, "native_owned_quantity")
        bot = _decimal(data, "bot_owned_quantity")
        unowned = _ids(data, "unowned_order_ids", refs)
        risk = native != bot or bool(unowned)
        return risk, "positive_orphan_mismatch" if risk else "positive_ownership_reconciliation"
    if cls == "A5":
        if not _bool(data, "exit_signal_proven") or not _bool(data, "exit_outcomes_complete"):
            raise EvidenceUnknown("exit_signal_or_outcome_unknown")
        required = _decimal(data, "required_exit_quantity")
        sold = _decimal(data, "matched_sell_filled_quantity")
        protected = _decimal(data, "reprotected_remainder_quantity")
        if sold > required or protected > required - sold or not _bool(data, "allocation_complete"):
            raise EvidenceUnknown("exit_allocation_unknown")
        fills = _ids(data, "matched_sell_fill_ids", refs)
        if sold and not fills:
            raise EvidenceUnknown("exit_fill_identity_unknown")
        if required == sold or protected == required - sold:
            return False, "positive_sold_or_reprotected"
        if elapsed < window["wait_seconds"]:
            raise EvidenceUnknown("observation_window_open")
        return True, "exit_quantity_still_at_risk"
    if cls == "A7":
        if not _bool(data, "entry_membership_complete") or not _bool(data, "gate_history_complete"):
            raise EvidenceUnknown("gate_history_unknown")
        if _legal(data, expectations) or not _bool(data, "entry_eligible"):
            return False, "positive_lawful_gate"
        blocked = _bool(data, "blocked")
        reason = data.get("block_reason")
        if not _code(reason):
            raise EvidenceUnknown("gate_reason_unknown")
        if not blocked:
            if not _bool(data, "recovery_proven"):
                raise EvidenceUnknown("gate_recovery_unknown")
            return False, "positive_gate_recovery"
        if elapsed < window["wait_seconds"]:
            raise EvidenceUnknown("observation_window_open")
        return True, reason
    raise EvidenceUnknown("trade_class_unknown")


def _finding(evidence, policy, cls, check, *, status="UNKNOWN", reason="evidence_unknown",
             fresh="UNKNOWN", qualified=False, observed=None, event=None, first=None):
    subject = evidence.get("subject") if type(evidence.get("subject")) is dict else {}
    safe = lambda value: value if _ref(value) else None
    account, bot = safe(subject.get("account")), safe(subject.get("bot"))
    broker = subject.get("broker") if subject.get("broker") in ("schwab", "webull") else "UNKNOWN"
    symbol = subject.get("symbol")
    if not isinstance(symbol, str) or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", symbol) is None:
        symbol = None
    identity, epoch = safe(subject.get("subject_id")), safe(check.get("condition_epoch"))
    manual = subject.get("manual") if type(subject.get("manual")) is bool else None
    if not all((identity, epoch, account, symbol, broker != "UNKNOWN")) or not (bot or manual is True):
        status, qualified, first = "UNKNOWN", False, None
        identity = "coverage:" + (safe(evidence.get("source_id")) or "unidentified")
        epoch = safe(evidence.get("source_epoch")) or "unknown"
    key = json.dumps([SCHEMA, cls, account, broker, bot, symbol, identity, epoch], separators=(",", ":"))
    ids = evidence.get("evidence_ids")
    refs = ids if type(ids) is list and len(ids) <= 64 and all(_ref(x) for x in ids) else []
    paper = True if subject.get("execution_mode") == "paper" else False if subject.get("execution_mode") == "live" else None
    payload = evidence.get("payload") if type(evidence.get("payload")) is dict else {}
    session = payload.get("session") if payload.get("session") in ("PM", "RTH", "AH", "PREOPEN", "CLOSED") else "UNKNOWN"
    mode = policy.get("mode") if policy.get("mode") in MODES else "UNKNOWN"
    action = check.get("action_taken") if check.get("action_taken") in ACTIONS else "UNKNOWN"
    return {"schema_version": SCHEMA, "class": cls, "incident_key": key, "subject_id": identity,
            "account": account, "symbol": symbol, "bot": bot, "broker": broker,
            "first_seen": _stamp(first) if first else None,
            "last_seen": _stamp(observed) if observed else None,
            "status": status, "reason_code": reason, "evidence_ids": list(refs),
            "action_taken": action if status != "UNKNOWN" else "UNKNOWN",
            "recommended_action": "FYI, nothing to do" if status == "FALSE" else
                "Review referenced trade evidence; no automatic trading action" if status == "TRUE" else
                "Review missing or expired evidence; do not infer absence",
            "manual": manual, "paper": paper, "session": session,
            "freshness": {"status": fresh, "observed_at": _stamp(observed) if observed else None,
                          "event_at": _stamp(event) if event else None,
                          "policy_ref": safe(policy.get("calibration_ref"))},
            "policyversion": safe(policy.get("policyversion")),
            # C1 17:41:37/20:23:14: this entire lane is shadow log/ledger only.
            "paging_eligible": False,
            "qualification": {"mode": mode, "qualified_policy": qualified,
                              "delivery_mode": "SHADOW", "shadow_only": True,
                              "coverage_complete": fresh == "FRESH", "reason": reason,
                              "evidence_refs": list(refs)}}


def evaluate_trade(evidence: dict, policy: dict, *, as_of: str) -> list[dict]:
    """No side effects. All caller-supplied frames/policies remain unchanged."""
    evidence = evidence if type(evidence) is dict else {}
    policy = policy if type(policy) is dict else {}
    if policy.get("enabled", False) is False or policy.get("mode") == "OFF":
        return []
    try:
        _json_bytes(evidence, 1048576)
        _json_bytes(policy, 1048576)
        if (policy.get("enabled") is not True or policy.get("mode") not in MODES
                or policy.get("schema_version") != SCHEMA or not _ref(policy.get("policyversion"))):
            raise EvidenceUnknown("policy_unknown")
        if any(key not in evidence for key in ENVELOPE) or evidence.get("schema_version") != SCHEMA:
            raise EvidenceUnknown("envelope_unknown")
        if not all(_ref(evidence.get(key)) for key in ("source_id", "source_kind", "source_epoch")):
            raise EvidenceUnknown("source_identity_unknown")
        if not isinstance(evidence.get("source_hash"), str) or re.fullmatch(r"[0-9a-f]{64}", evidence["source_hash"]) is None:
            raise EvidenceUnknown("source_hash_unknown")
        now, observed, event, received, start, end = map(_clock,
            (as_of, evidence["observed_at"], evidence["event_at"], evidence["received_at"],
             evidence["coverage_start"], evidence["coverage_end"]))
        if not (start <= event <= observed <= received <= now and observed <= end <= now):
            raise EvidenceUnknown("causal_clock_unknown")
        if (evidence["complete"] is not True or evidence["overflow"] is not False
                or evidence["read_error"] is not False):
            raise EvidenceUnknown("coverage_incomplete")
        refs = evidence["evidence_ids"]
        if type(refs) is not list or not refs or len(refs) > 64 or any(not _ref(x) for x in refs) or len(set(refs)) != len(refs):
            raise EvidenceUnknown("evidence_reference_unknown")
        subject, payload = evidence["subject"], evidence["payload"]
        if type(subject) is not dict or type(payload) is not dict:
            raise EvidenceUnknown("subject_payload_unknown")
        if subject.get("execution_mode") == "paper":
            return []
        if (subject.get("execution_mode") != "live" or type(subject.get("manual")) is not bool
                or not (subject.get("bot") in BOTS or subject["manual"] is True and subject.get("bot") is None)
                or subject.get("broker") not in ("schwab", "webull")):
            raise EvidenceUnknown("live_ownership_unknown")
        if (not _ref(subject.get("account")) or not _ref(subject.get("subject_id"))
                or not isinstance(subject.get("symbol"), str)
                or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", subject["symbol"]) is None):
            raise EvidenceUnknown("trade_identity_unknown")
        expectations = policy.get("expectations")
        if type(expectations) is not dict or not all(_ref(expectations.get(k)) for k in
                ("version", "source_head", "settings_ref", "routing_ref", "calendar_ref")):
            raise EvidenceUnknown("expectation_unknown")
        if payload.get("expectation_ref") != expectations["version"] or payload.get("source_head") != expectations["source_head"]:
            raise EvidenceUnknown("expectation_epoch_mismatch")
        session = payload.get("session")
        if session not in ("PM", "RTH", "AH", "PREOPEN", "CLOSED"):
            raise EvidenceUnknown("session_unknown")
        routes = expectations.get("routes")
        allowed = routes.get(str(subject.get("bot")) + "|" + session) if type(routes) is dict else None
        if subject["manual"] is False and (type(allowed) is not list or subject["broker"] not in allowed):
            raise EvidenceUnknown("route_expectation_unknown")
        checks = payload.get("checks")
        if type(checks) is not list or not checks or len(checks) > 1000:
            raise EvidenceUnknown("check_set_unknown")
    except EvidenceUnknown as exc:
        return [_finding(evidence, policy, "B_checker_blindness", {}, reason=exc.args[0])]

    result = []
    seen = set()
    for check in checks:
        check = check if type(check) is dict else {}
        cls = check.get("class") if check.get("class") in CLASSES else "B_checker_blindness"
        window = None
        try:
            if cls not in CLASSES or not _ref(check.get("condition_epoch")):
                raise EvidenceUnknown("condition_identity_unknown")
            first = _clock(check.get("first_seen"))
            if not start <= first <= observed:
                raise EvidenceUnknown("condition_history_unknown")
            window = _window(policy, cls)
            if (now - observed).total_seconds() > window["freshness_seconds"]:
                raise EvidenceUnknown("source_stale")
            data = check.get("operands")
            if type(data) is not dict:
                raise EvidenceUnknown("operand_set_unknown")
            if subject["manual"]:
                if not _bool(data, "manual_allocation_proven"):
                    raise EvidenceUnknown("manual_allocation_unknown")
                risk, reason = False, "positive_manual_digest_only"
            else:
                # Inputs have <=38 significant digits; sums/differences must not
                # round at the process-global Decimal context (often28 digits).
                with localcontext() as context:
                    context.prec = 80
                    risk, reason = _risk(cls, data, expectations, refs, (observed - first).total_seconds(), window, subject, observed)
            qualified = (policy.get("qualified") is True and _ref(policy.get("approval_ref"))
                         and _ref(policy.get("calibration_ref")) and window.get("qualified") is True)
            finding = _finding(evidence, policy, cls, check, status="TRUE" if risk else "FALSE",
                               reason=reason, fresh="FRESH", qualified=qualified,
                               observed=observed, event=event, first=first)
        except EvidenceUnknown as exc:
            finding = _finding(evidence, policy, cls, check, reason=exc.args[0],
                               fresh="STALE" if exc.args[0] == "source_stale" else "UNKNOWN",
                               observed=observed, event=event)
        if finding["incident_key"] in seen:
            return [_finding(evidence, policy, "B_checker_blindness", {}, reason="duplicate_condition_identity")]
        seen.add(finding["incident_key"])
        result.append(finding)
    try:
        _json_bytes(result, 524288)
    except EvidenceUnknown:
        return [_finding(evidence, policy, "B_checker_blindness", {}, reason="output_resource_overflow")]
    return result
