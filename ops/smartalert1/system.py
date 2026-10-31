"""Pure SMARTALERT1 L3 checks; no acquisition, clocks, delivery or APP imports.

Payload grammar: ``checks`` is a nonempty list of dictionaries containing
``class``, ``condition_epoch``, ``transition_evidence_id``, ``first_seen``,
``affirmative_refs`` and ``reading``. Epochs identify actual condition transitions,
not this invocation/PID. FALSE needs affirmative source refs, never silence.

Policy expectations: ``version`` and ``subjects`` keyed by subject_id. Each
subject repeats all envelope identity fields and source_epoch, supplies session,
work_expected, expected_running and maintenance_active booleans, plus an
expectation_ref and valid_from/valid_until interval. These are
supplied historical policy operands, not inferred schedules or live permissions.
Class windows require max_age_seconds and the explicitly named operands below.
All windows outside CANDIDATE_REPLAY require qualified policy/calibration/approval.

Readings by class:
 service_down: active bool; restart_loop: before/after integer counters and
 sample_started_at/sample_ended_at; guard_STOP: stop_present, guard_owns_live bools;
 token: dead bool, refresh_expires_at; scanner: progress_complete, last_cycle_at;
 quote: quote_scope_complete, subscribed bools, quote_at;
 OMS: progress_complete bool, tick_lag_seconds, sync_completed_at, sync_outcome;
 DB/API: scope_complete bool, error_count integer, error_kind and window_start/end;
 checker_blindness: source_readable, proof_expired, scope_complete booleans from
 an independently complete observation of the checker/acquisition boundary.
Unknown, partial or expired evidence also emits checker blindness. Nothing here
claims that a missing source proves the watched service failed.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re

SCHEMA = "smartalert1.v1"
CLASSES = (
    "B_service_down", "B_restart_loop", "B_guard_STOP",
    "B_token_dead_or_near_expiry", "B_scanner_silent", "B_quote_stale",
    "B_OMS_stall", "B_DB_write_error", "B_broker_API_error", "B_checker_blindness",
    "B_keepwatch_stuck", "B_bot_reading_lag", "B_bar_lag",
)
EXPORTED_PROGRESS_CLASSES = frozenset((
    "B_keepwatch_stuck", "B_bot_reading_lag", "B_bar_lag",
))
FRAME_BYTES = 1048576
OUTPUT_BYTES = 524288
MAX_FINDINGS = 1000
MAX_REFS = 64
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
_HEX = re.compile(r"[a-f0-9]{64}\Z")
_IDENTITY_FIELDS = ("account", "broker", "bot", "symbol", "service", "subject_id", "execution_mode", "manual")
_ENVELOPE_FIELDS = (
    "schema_version", "source_id", "source_kind", "source_hash", "source_epoch",
    "observed_at", "event_at", "received_at", "coverage_start", "coverage_end",
    "complete", "overflow", "read_error", "evidence_ids", "subject", "payload",
)


class _Invalid(ValueError):
    pass


def _text(value, limit=1200):
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise _Invalid("text_unknown")
    return value


def _refs(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_REFS:
        raise _Invalid("evidence_refs_unknown")
    result = [_reference(v) for v in value]
    if len(set(result)) != len(result):
        raise _Invalid("evidence_refs_duplicate")
    return result


def _reference(value):
    result = _text(value, 256)
    if "://" in result or not re.fullmatch(r"[A-Za-z0-9_./:@-]+", result):
        raise _Invalid("reference_unknown")
    return result


def _time(value):
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise _Invalid("clock_unknown")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _Invalid("clock_unknown") from None
    return parsed.astimezone(timezone.utc)


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 64:
        raise _Invalid("number_unknown")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise _Invalid("number_unknown") from None
    if not number.is_finite() or number < 0 or positive and number == 0:
        raise _Invalid("number_unknown")
    return number


def _integer(value):
    if type(value) is not int or value < 0:
        raise _Invalid("counter_unknown")
    return value


def _boolean(value):
    if type(value) is not bool:
        raise _Invalid("boolean_unknown")
    return value


def _bounded(value, limit):
    try:
        raw = json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise _Invalid("json_unknown") from None
    size = len(raw.encode("utf-8"))
    if size > limit:
        raise _Invalid("frame_overflow")
    return size


def _age(value, now):
    timestamp = _time(value)
    if timestamp > now:
        raise _Invalid("future_clock")
    return Decimal(str((now - timestamp).total_seconds()))


def _qualified(policy):
    return (policy.get("qualified") is True
            and isinstance(policy.get("approval_ref"), str) and bool(policy["approval_ref"])
            and isinstance(policy.get("calibration_ref"), str) and bool(policy["calibration_ref"]))


def _window(policy, class_name):
    windows = policy.get("windows")
    if not isinstance(windows, dict) or not isinstance(windows.get(class_name), dict):
        raise _Invalid("behavioral_window_unqualified")
    if policy["mode"] != "CANDIDATE_REPLAY" and not _qualified(policy):
        raise _Invalid("behavioral_window_unqualified")
    return windows[class_name]


def _limit(window, name):
    return _number(window.get(name), positive=True)


def _subject(evidence):
    subject = evidence.get("subject")
    if not isinstance(subject, dict) or any(k not in subject for k in _IDENTITY_FIELDS):
        raise _Invalid("subject_unknown")
    for field in ("account", "bot", "symbol", "service"):
        if subject[field] is not None:
            _text(subject[field], 128)
    for field in ("bot", "service"):
        if subject[field] is not None and not re.fullmatch(r"[a-z][a-z0-9_.-]{0,80}", subject[field]):
            raise _Invalid("logical_subject_unknown")
    if subject["account"] is not None and not re.fullmatch(r"live:[A-Za-z0-9_.:-]{1,100}", subject["account"]):
        raise _Invalid("account_unknown")
    if subject["symbol"] is not None and not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,31}", subject["symbol"]):
        raise _Invalid("symbol_unknown")
    if subject["broker"] not in {"schwab", "webull", "system", "UNKNOWN"}:
        raise _Invalid("broker_unknown")
    if subject["execution_mode"] not in {"live", "paper", "UNKNOWN"}:
        raise _Invalid("execution_mode_unknown")
    if subject["manual"] is not None:
        _boolean(subject["manual"])
    _reference(subject["subject_id"])
    return subject


def _expectation(evidence, policy, subject, now):
    expectations = policy.get("expectations")
    if not isinstance(expectations, dict):
        raise _Invalid("expectation_unknown")
    _text(expectations.get("version"), 128)
    subjects = expectations.get("subjects")
    expected = subjects.get(subject["subject_id"]) if isinstance(subjects, dict) else None
    if not isinstance(expected, dict) or expected.get("source_epoch") != evidence["source_epoch"]:
        raise _Invalid("historical_profile_unknown")
    _reference(expected.get("expectation_ref"))
    lower, upper = _time(expected.get("valid_from")), _time(expected.get("valid_until"))
    if not lower <= _time(evidence["observed_at"]) <= now < upper:
        raise _Invalid("historical_profile_expired_or_future")
    if any(field not in expected or type(expected[field]) is not type(subject[field])
           or expected[field] != subject[field] for field in _IDENTITY_FIELDS):
        raise _Invalid("subject_identity_mismatch")
    if expected.get("session") not in {"PM", "RTH", "AH", "PREOPEN", "CLOSED"}:
        raise _Invalid("session_unknown")
    for field in ("expected_running", "work_expected", "maintenance_active"):
        _boolean(expected.get(field))
    if subject["broker"] == "UNKNOWN" or subject["execution_mode"] == "UNKNOWN" or subject["manual"] is None:
        raise _Invalid("scope_unknown")
    if subject["manual"] and not set(_refs(expected.get("manual_proof_refs"))).issubset(evidence["evidence_ids"]):
        raise _Invalid("manual_classification_unknown")
    return expected


def _progress_ms(value):
    delta = _time(value) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000


def _exported_progress_reading(class_name, reading, window, now, coverage):
    """C1 09:26/09:57 explicit SHADOW predicates; no entry/native assumptions."""
    if not _boolean(reading.get("bar_scope_complete")):
        raise _Invalid("progress_bar_scope_unknown")
    now_ms = _progress_ms(now.isoformat())
    lower, upper = (_progress_ms(value.isoformat()) for value in coverage)
    if class_name == "B_bar_lag":
        if _limit(window, "lag_milliseconds") != 240000:
            raise _Invalid("bar_lag_policy_changed")
        closed = _progress_ms(reading.get("last_bar_closed_at"))
        if not lower <= closed <= upper <= now_ms:
            raise _Invalid("bar_clock_unknown")
        return now_ms - closed >= 240000, (
            "bar_lag_4min" if now_ms - closed >= 240000 else "market_bar_progress_measured")
    if class_name == "B_bot_reading_lag":
        if _limit(window, "lag_milliseconds") != 180000:
            raise _Invalid("bot_reading_lag_policy_changed")
        if not _boolean(reading.get("reading_scope_complete")):
            raise _Invalid("bot_reading_scope_unknown")
        last = _progress_ms(reading.get("last_reading_at"))
        closed = _progress_ms(reading.get("last_bar_closed_at"))
        if not lower <= last < closed <= upper <= now_ms:
            raise _Invalid("continuing_bars_unproven")
        if now_ms - closed >= 240000:
            raise _Invalid("bars_not_currently_proven_continuing")
        return now_ms - last >= 180000, (
            "bot_reading_lag_180s" if now_ms - last >= 180000 else "bot_reading_progress_measured")
    if _integer(window.get("minimum_closed_minutes")) != 3:
        raise _Invalid("keepwatch_minute_policy_changed")
    if not (_boolean(reading.get("gate_history_complete"))
            and _boolean(reading.get("line_history_complete"))):
        raise _Invalid("keepwatch_history_unknown")
    minutes = reading.get("closed_minutes")
    if not isinstance(minutes, list) or len(minutes) != 3:
        raise _Invalid("keepwatch_closed_minutes_unknown")
    minutes = [_integer(value) for value in minutes]
    if (any(value % 60000 for value in minutes) or minutes[1] != minutes[0] + 60000
            or minutes[2] != minutes[1] + 60000
            or not lower <= minutes[0] < minutes[-1] + 60000 <= upper <= now_ms):
        raise _Invalid("keepwatch_minutes_not_consecutive_closed")
    published = reading.get("last_line_publish_at")
    if published is not None:
        published_ms = _progress_ms(published)
        if not lower <= published_ms <= upper:
            raise _Invalid("line_publish_clock_unknown")
        if published_ms >= minutes[0]:
            return False, "keepwatch_line_publish_progress"
    events = reading.get("hold_events")
    recoveries = reading.get("recovery_event_times")
    if (not isinstance(events, list) or not 1 <= len(events) <= 64
            or not isinstance(recoveries, list) or len(recoveries) > 64):
        raise _Invalid("keepwatch_transition_events_unknown")
    times = []
    for event in events:
        if not isinstance(event, dict):
            raise _Invalid("keepwatch_hold_event_unknown")
        unknown = _boolean(event.get("unknown"))
        entry = event.get("entry_allowed")
        if entry is not None and type(entry) is not bool:
            raise _Invalid("keepwatch_entry_flag_unknown")
        if not unknown and entry is not False:
            raise _Invalid("keepwatch_hold_not_positive")
        value = _progress_ms(event.get("event_at"))
        if not lower <= value <= upper:
            raise _Invalid("keepwatch_hold_clock_unknown")
        times.append(value)
    recovered = [_progress_ms(value) for value in recoveries]
    if any(not lower <= value <= upper for value in recovered):
        raise _Invalid("keepwatch_recovery_clock_unknown")
    mode = reading.get("proof_mode")
    if mode == "recurring_holds":
        # A gate log is observed AFTER the corresponding minute closes.
        # Retain both source clocks; this binds observations, not causation.
        if not all(any(minute + 60000 <= value < minute + 120000 for value in times)
                   for minute in minutes):
            raise _Invalid("keepwatch_recurring_hold_missing")
    elif mode == "unrecovered_hold":
        first = min(times)
        if first > minutes[0] or any(value > first for value in recovered):
            raise _Invalid("keepwatch_hold_recovered_or_too_new")
    else:
        raise _Invalid("keepwatch_proof_mode_unknown")
    return True, "keepwatch_rehold_or_frozen"


def _reading(class_name, reading, expected, window, now, coverage):
    if not isinstance(reading, dict):
        raise _Invalid("reading_unknown")
    if class_name in EXPORTED_PROGRESS_CLASSES:
        return _exported_progress_reading(class_name, reading, window, now, coverage)
    if class_name == "B_checker_blindness":
        readable = _boolean(reading.get("source_readable"))
        expired = _boolean(reading.get("proof_expired"))
        complete = _boolean(reading.get("scope_complete"))
        return not readable or expired or not complete, "checker_coverage_measured"
    if class_name == "B_service_down":
        active = _boolean(reading.get("active"))
        risk = not active and expected["expected_running"] and not expected["maintenance_active"]
        return risk, ("service_down" if risk else "service_active" if active else "expected_schedule_or_maintenance")
    if class_name == "B_restart_loop":
        before, after = _integer(reading.get("before")), _integer(reading.get("after"))
        lower, upper = _time(reading.get("sample_started_at")), _time(reading.get("sample_ended_at"))
        age = Decimal(str((upper - lower).total_seconds()))
        if (after < before or not coverage[0] <= lower < upper <= coverage[1]
                or _age(reading.get("sample_ended_at"), now) > _limit(window, "max_age_seconds")
                or not 0 < age <= _limit(window, "max_sample_seconds")):
            raise _Invalid("restart_baseline_unknown")
        return after - before > _integer(window.get("restart_count_limit")), "restart_delta_measured"
    if class_name == "B_guard_STOP":
        present, live = _boolean(reading.get("stop_present")), _boolean(reading.get("guard_owns_live"))
        return present and live, "live_guard_stop" if present and live else "no_live_guard_stop"
    if class_name == "B_token_dead_or_near_expiry":
        dead = _boolean(reading.get("dead"))
        near = _limit(window, "near_expiry_seconds")
        if dead:
            return True, "token_dead"
        remaining = Decimal(str((_time(reading.get("refresh_expires_at")) - now).total_seconds()))
        return remaining <= near, "refresh_expiry_measured"
    if class_name in {"B_scanner_silent", "B_quote_stale", "B_OMS_stall"} and not expected["work_expected"]:
        return False, "work_not_expected_under_supplied_policy"
    if class_name == "B_scanner_silent":
        if not _boolean(reading.get("progress_complete")):
            raise _Invalid("scanner_progress_unknown")
        return _age(reading.get("last_cycle_at"), now) > _limit(window, "max_silence_seconds"), "scanner_progress_measured"
    if class_name == "B_quote_stale":
        if not _boolean(reading.get("quote_scope_complete")) or not _boolean(reading.get("subscribed")):
            raise _Invalid("quote_scope_unknown")
        return _age(reading.get("quote_at"), now) > _limit(window, "max_quote_age_seconds"), "quote_age_measured"
    if class_name == "B_OMS_stall":
        if not _boolean(reading.get("progress_complete")):
            raise _Invalid("oms_progress_unknown")
        outcome = reading.get("sync_outcome")
        if outcome not in {"ok", "error", "in_progress"}:
            raise _Invalid("sync_outcome_unknown")
        lag = _number(reading.get("tick_lag_seconds"))
        age = _age(reading.get("sync_completed_at"), now)
        lag_limit, sync_limit = _limit(window, "max_tick_lag_seconds"), _limit(window, "max_sync_age_seconds")
        risk = outcome == "error" or lag > lag_limit or age > sync_limit
        return risk, "oms_functional_progress_measured"
    if class_name in {"B_DB_write_error", "B_broker_API_error"}:
        if not _boolean(reading.get("scope_complete")):
            raise _Invalid("error_coverage_unknown")
        start, end = _time(reading.get("window_start")), _time(reading.get("window_end"))
        span = Decimal(str((end - start).total_seconds()))
        if not coverage[0] <= start < end <= coverage[1] or span != _limit(window, "count_window_seconds"):
            raise _Invalid("error_window_unknown")
        count = _integer(reading.get("error_count"))
        kind = reading.get("error_kind")
        allowed = "db_write_failure" if class_name == "B_DB_write_error" else "api_failure"
        if kind == "legal_refusal" and class_name == "B_broker_API_error":
            return False, "expected_refusal_not_api_failure"
        if kind not in {allowed, "none"} or kind == "none" and count != 0:
            raise _Invalid("error_operand_unknown")
        return count > _integer(window.get("error_count_limit")), "actual_error_count_measured"
    raise _Invalid("check_class_unknown")


def _finding(evidence, policy, class_name, *, status="UNKNOWN", reason="evidence_unknown",
             check=None, expected=None, freshness="UNKNOWN"):
    subject = evidence.get("subject") if isinstance(evidence, dict) else None
    subject = subject if isinstance(subject, dict) else {}
    # Only identities already validated by evaluate_system are propagated.
    trusted = evidence.get("_validated_subject", False) is True
    identity = subject if trusted else {}
    source = evidence.get("source_id", "unknown_source")
    source = source if isinstance(source, str) and 0 < len(source) <= 256 else "unknown_source"
    refs = evidence.get("evidence_ids", [])
    refs = refs if trusted else []
    check = check or {}
    epoch = check.get("condition_epoch") if status != "UNKNOWN" else None
    epoch = epoch or "coverage:" + source
    subject_id = identity.get("subject_id")
    key = [SCHEMA, class_name, identity.get("account"), identity.get("broker", "UNKNOWN"),
           identity.get("bot"), identity.get("symbol"), subject_id, epoch]
    qualified = _qualified(policy)
    eligible = (status == "TRUE" and policy.get("mode") == "QUALIFIED_LIVE" and qualified
                and identity.get("execution_mode") == "live" and identity.get("manual") is False)
    action = "UNKNOWN"
    supplied_action = check.get("action")
    if isinstance(supplied_action, dict):
        try:
            code = supplied_action.get("code")
            action_refs = _refs(supplied_action.get("evidence_ids"))
            if (isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code)
                    and set(action_refs).issubset(refs)
                    and _time(supplied_action.get("observed_at")) <= _time(evidence.get("observed_at"))):
                action = dict(code=code, evidence_ids=action_refs)
        except _Invalid:
            pass
    return dict(schema_version=SCHEMA, **{"class": class_name},
        incident_key=json.dumps(key, separators=(",", ":"), ensure_ascii=True),
        subject_id=subject_id, account=identity.get("account"), symbol=identity.get("symbol"),
        bot=identity.get("bot"), broker=identity.get("broker", "UNKNOWN"),
        first_seen=_time(check["first_seen"]).isoformat() if status != "UNKNOWN" else None,
        last_seen=evidence.get("observed_at") if trusted else None, status=status,
        reason_code=reason, evidence_ids=list(refs), action_taken=action,
        recommended_action=("Inspect existing source coverage; do not infer recovery." if status == "UNKNOWN"
                            else "Review the identified system condition; no automatic repair." if status == "TRUE"
                            else "FYI, nothing to do under the supplied fresh evidence."),
        manual=identity.get("manual"), paper=identity.get("execution_mode") == "paper",
        session=expected.get("session", "UNKNOWN") if expected else "UNKNOWN",
        freshness=dict(status=freshness, observed_at=evidence.get("observed_at") if trusted else None,
                       event_at=evidence.get("event_at") if trusted else None,
                       policy_ref=policy.get("calibration_ref"),
                       expectation_ref=expected.get("expectation_ref") if expected else None),
        policyversion=policy.get("policyversion", "UNKNOWN"), paging_eligible=bool(eligible),
        qualification=dict(mode=policy.get("mode", "UNKNOWN"), qualified_policy=qualified,
            coverage_complete=trusted and evidence.get("complete") is True and evidence.get("overflow") is False,
            reason="candidate_only_not_historical_qualification" if policy.get("mode") == "CANDIDATE_REPLAY" else reason,
            evidence_refs=list(refs)))


def evaluate_system(evidence: dict, policy: dict, *, as_of: str) -> list[dict]:
    """Evaluate supplied observations only. OFF is inert; UNKNOWN never clears."""
    if isinstance(policy, dict) and (policy.get("enabled", False) is False or policy.get("mode") == "OFF"):
        return []
    safe = {}
    safe_policy = {}
    try:
        _bounded([evidence, policy], FRAME_BYTES)
        if not isinstance(evidence, dict) or not isinstance(policy, dict):
            raise _Invalid("frame_shape_unknown")
        if type(policy.get("enabled")) is not bool or policy["enabled"] is not True:
            raise _Invalid("enabled_unknown")
        if policy.get("schema_version") != SCHEMA or policy.get("mode") not in {"REPORT_ONLY", "CANDIDATE_REPLAY", "QUALIFIED_LIVE"}:
            raise _Invalid("policy_unknown")
        safe_policy = {"mode": policy["mode"], "policyversion": _text(policy.get("policyversion"), 128)}
        if type(policy.get("qualified")) is not bool or any(k not in policy for k in ("approval_ref", "calibration_ref", "windows", "expectations")):
            raise _Invalid("policy_unknown")
        for field in ("approval_ref", "calibration_ref"):
            if policy[field] is not None:
                _reference(policy[field])
        if any(k not in evidence for k in _ENVELOPE_FIELDS) or evidence["schema_version"] != SCHEMA:
            raise _Invalid("envelope_unknown")
        _reference(evidence["source_id"])
        _text(evidence["source_kind"], 128)
        _text(evidence["source_epoch"], 256)
        if not isinstance(evidence["source_hash"], str) or not _HEX.fullmatch(evidence["source_hash"]):
            raise _Invalid("source_hash_unknown")
        refs = _refs(evidence["evidence_ids"])
        subject = _subject(evidence)
        now = _time(as_of)
        observed, event, received = (_time(evidence[k]) for k in ("observed_at", "event_at", "received_at"))
        start, end = _time(evidence["coverage_start"]), _time(evidence["coverage_end"])
        if not start <= end <= observed <= now or not event <= received <= observed:
            raise _Invalid("causal_clock_unknown")
        safe = dict(evidence, subject=dict(subject), evidence_ids=refs, _validated_subject=True,
                    observed_at=observed.isoformat(), event_at=event.isoformat(), received_at=received.isoformat())
        if (not _boolean(evidence["complete"]) or _boolean(evidence["overflow"])
                or evidence["read_error"] is not None):
            raise _Invalid("source_coverage_unknown")
        expected = _expectation(evidence, policy, subject, now)
        checks = evidence["payload"].get("checks") if isinstance(evidence["payload"], dict) else None
        if not isinstance(checks, list) or not 1 <= len(checks) < MAX_FINDINGS:
            raise _Invalid("checks_overflow_or_unknown")
        findings = []
        for check in checks:
            class_name = check.get("class") if isinstance(check, dict) else None
            if class_name not in CLASSES:
                raise _Invalid("check_class_unknown")
            freshness = "UNKNOWN"
            try:
                window = _window(policy, class_name)
                if _age(evidence["observed_at"], now) > _limit(window, "max_age_seconds"):
                    freshness = "STALE"
                    raise _Invalid("source_expired")
                freshness = "FRESH"
                _reference(check.get("condition_epoch"))
                if check.get("transition_evidence_id") not in refs:
                    raise _Invalid("condition_transition_unknown")
                if _time(check.get("first_seen")) > observed:
                    raise _Invalid("condition_history_unknown")
                if class_name in EXPORTED_PROGRESS_CLASSES:
                    exported = check.get("reading")
                    if not isinstance(exported, dict):
                        raise _Invalid("reading_unknown")
                    if not set(_refs(exported.get("coverage_refs"))).issubset(refs):
                        raise _Invalid("progress_coverage_reference_unknown")
                risk, reason = _reading(class_name, check.get("reading"), expected, window, now, (start, end))
                if not risk and not set(_refs(check.get("affirmative_refs"))).issubset(refs):
                    raise _Invalid("affirmative_recovery_unknown")
                findings.append(_finding(safe, policy, class_name, status="TRUE" if risk else "FALSE",
                    reason=reason, check=check, expected=expected, freshness=freshness))
            except _Invalid as exc:
                findings.append(_finding(safe, policy, class_name, reason=str(exc), expected=expected, freshness=freshness))
            except (TypeError, KeyError, ValueError, OverflowError):
                findings.append(_finding(safe, policy, class_name, reason="malformed_operand", expected=expected, freshness=freshness))
        if any(f["status"] == "UNKNOWN" for f in findings) and not any(f["class"] == "B_checker_blindness" for f in findings):
            findings.append(_finding(safe, policy, "B_checker_blindness", reason="system_check_evidence_unknown", expected=expected))
        _bounded(findings, OUTPUT_BYTES)
        return findings
    except _Invalid as exc:
        return [_finding(safe, safe_policy, "B_checker_blindness", reason=str(exc))]
    except (TypeError, KeyError, ValueError, OverflowError):
        return [_finding(safe, safe_policy, "B_checker_blindness", reason="malformed_frame")]
