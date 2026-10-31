"""Offline bounded replay of supplied records, never acquisition or threshold fitting.

Record grammar (each JSON-compatible dictionary has record_type and origin):
 observation: as_of, evidence; origin=ACTUAL_RETAINED or CONTROLLED.
 label: incident_key, status, evidence_ids, label_asof, adjudicated_by,
        actionability=actionable|informational|UNKNOWN.
 coverage: class, start, end, complete, overflow, evidence_ids. Coverage attests
        the entire supplied class scope, not merely that one event was found.

At most 1000 records and 1048576 canonical ASCII-JSON bytes per input frame,
including policy and the exact {"policy":...,"records":[...]} shell/separators.
Records are consumed once; aggregate overflow refuses before evaluation of the
offending record. Larger retained manifests must be framed
by Main; overflow is refused, never trimmed to a clean replay. Window is exactly
336 elapsed hours. Source observations stay in chronological order; labels and
coverage are retrospective metadata, NEVER evaluator inputs. Calibration joins
only exact incident key AND exact evidence-ID set; no nearest-time matching.
Historical samples require actual observations AND actual adjudication. Controlled
fixtures remain INSUFFICIENT regardless of how many candidate assertions pass.
"""

from datetime import date, timedelta

from .system import (
    CLASSES, FRAME_BYTES, OUTPUT_BYTES, MAX_FINDINGS, SCHEMA, _Invalid, _bounded, _boolean,
    _reference, _refs, _text, _time, evaluate_system,
)

MAX_RECORDS = 1000


def _refusal(reason, records_seen=0):
    return dict(schema_version=SCHEMA, status="UNKNOWN", reason_code=reason,
                records_seen=records_seen, coverage_complete=False,
                qualification="INSUFFICIENT", actual_historical_samples=False,
                classifications_use_labels=False, paging_eligible=False)


def _days(start, end, policy):
    """Validate Main's versioned ET midnight calendar; never load timezone files."""
    expected = policy.get("expectations", {})
    if not isinstance(expected, dict):
        raise _Invalid("calendar_unknown")
    _reference(expected.get("calendar_ref"))
    calendar = expected.get("et_calendar")
    if not isinstance(calendar, list) or not 1 <= len(calendar) <= MAX_RECORDS:
        raise _Invalid("calendar_unknown")
    result = []
    cursor = start
    previous_day, previous_upper = None, None
    for row in calendar:
        if not isinstance(row, dict):
            raise _Invalid("calendar_unknown")
        day = date.fromisoformat(row.get("day", ""))
        raw_lower, raw_upper = row.get("start"), row.get("end")
        lower, upper = _time(raw_lower), _time(raw_upper)
        if (not isinstance(raw_lower, str) or not isinstance(raw_upper, str)
                or raw_lower[:19] != day.isoformat() + "T00:00:00"
                or raw_upper[:19] != (day + timedelta(days=1)).isoformat() + "T00:00:00"
                or raw_lower[-6:] not in {"-04:00", "-05:00"}
                or raw_upper[-6:] not in {"-04:00", "-05:00"}
                or previous_day is not None and day != previous_day + timedelta(days=1)
                or previous_upper is not None and lower != previous_upper
                or not lower <= cursor < upper or upper <= start or lower >= end):
            raise _Invalid("calendar_gap_or_identity_unknown")
        result.append(dict(day=day.isoformat(), start=max(lower, start).isoformat(),
                           end=min(upper, end).isoformat(), partial=lower < start or upper > end))
        cursor, previous_day, previous_upper = min(upper, end), day, upper
    if cursor != end:
        raise _Invalid("calendar_coverage_unknown")
    return result


def _covered(intervals, start, end):
    cursor = start
    for lower, upper in sorted(intervals):
        if upper <= cursor:
            continue
        if lower > cursor:
            return False
        cursor = upper
        if cursor >= end:
            return True
    return False


def replay_system(records, policy: dict, *, window_start: str, window_end: str) -> dict:
    """Read one finite supplied frame; no file, clock, network or APP access."""
    count = 0
    try:
        start, end = _time(window_start), _time(window_end)
        if end - start != timedelta(hours=336):
            raise _Invalid("replay_window_not_336_hours")
        frame_bytes = len(b'{"policy":,"records":[]}') + _bounded(policy, FRAME_BYTES)
        if frame_bytes > FRAME_BYTES:
            raise _Invalid("replay_frame_byte_overflow")
        if not isinstance(policy, dict):
            raise _Invalid("policy_unknown")
        _text(policy.get("policyversion"), 128)
        for field in ("approval_ref", "calibration_ref"):
            if policy.get(field) is not None:
                _reference(policy[field])
        labels, observations, coverage = {}, [], {name: [] for name in CLASSES}
        coverage_refs = {name: [] for name in CLASSES}
        incomplete_coverage = {name: 0 for name in CLASSES}
        prior = None
        for record in records:
            count += 1
            if count > MAX_RECORDS:
                raise _Invalid("replay_frame_record_overflow")
            frame_bytes += _bounded(record, FRAME_BYTES) + (1 if count > 1 else 0)
            if frame_bytes > FRAME_BYTES:
                raise _Invalid("replay_frame_byte_overflow")
            if not isinstance(record, dict) or record.get("origin") not in {"ACTUAL_RETAINED", "CONTROLLED"}:
                raise _Invalid("record_origin_unknown")
            kind = record.get("record_type")
            if kind == "observation":
                as_of = _time(record.get("as_of"))
                if not start <= as_of < end or prior is not None and as_of < prior:
                    raise _Invalid("observation_clock_outside_or_unordered")
                prior = as_of
                findings = evaluate_system(record.get("evidence"), policy, as_of=record["as_of"])
                if len(observations) + len(findings) > MAX_FINDINGS:
                    raise _Invalid("replay_frame_finding_overflow")
                observations.extend((as_of, record["origin"], finding) for finding in findings)
            elif kind == "label":
                key = _text(record.get("incident_key"), 2048)
                refs = _refs(record.get("evidence_ids"))
                if record.get("status") not in {"TRUE", "FALSE", "UNKNOWN"}:
                    raise _Invalid("label_status_unknown")
                when = _time(record.get("label_asof"))
                if when > end or record.get("actionability") not in {"actionable", "informational", "UNKNOWN"}:
                    raise _Invalid("label_time_or_actionability_unknown")
                _reference(record.get("adjudicated_by"))
                identity = (key, tuple(sorted(refs)))
                if identity in labels:
                    raise _Invalid("duplicate_label_identity")
                labels[identity] = record
            elif kind == "coverage":
                name = record.get("class")
                if name not in CLASSES:
                    raise _Invalid("coverage_class_unknown")
                lower, upper = _time(record.get("start")), _time(record.get("end"))
                refs = _refs(record.get("evidence_ids"))
                if not start <= lower < upper <= end:
                    raise _Invalid("coverage_clock_unknown")
                if (_boolean(record.get("complete")) and not _boolean(record.get("overflow"))
                        and record["origin"] == "ACTUAL_RETAINED"):
                    coverage[name].append((lower, upper))
                    coverage_refs[name].extend(refs)
                else:
                    incomplete_coverage[name] += 1
            else:
                raise _Invalid("record_type_unknown")

        days = _days(start, end, policy)
        buckets, summaries = {}, []
        for name in CLASSES:
            selected = [(stamp, origin, finding) for stamp, origin, finding in observations if finding["class"] == name]
            totals = {status: 0 for status in ("TRUE", "FALSE", "UNKNOWN")}
            matrix = {key: 0 for key in ("TP", "FP", "TN", "FN")}
            actual_labeled, label_missing, controlled, label_unknown, prediction_unknown = 0, 0, 0, 0, 0
            actions = {key: 0 for key in ("actionable", "informational", "UNKNOWN")}
            samples = {"TRUE": [], "FALSE": []}
            sample_keys = {"TRUE": set(), "FALSE": set()}
            incidents, would_page, eligible = set(), set(), set()
            for stamp, origin, finding in selected:
                status = finding["status"]
                totals[status] += 1
                incidents.add(finding["incident_key"])
                if status != "UNKNOWN" and finding["freshness"]["status"] == "FRESH" and finding["qualification"]["coverage_complete"]:
                    eligible.add(finding["incident_key"])
                if (status == "TRUE" and not finding["paper"] and finding["manual"] is False
                        and finding["freshness"]["status"] == "FRESH"
                        and finding["qualification"]["coverage_complete"]):
                    would_page.add(finding["incident_key"])
                day = next(row["day"] for row in days if _time(row["start"]) <= stamp < _time(row["end"]))
                identity = (name, day, finding["session"], finding["bot"], finding["broker"])
                bucket = buckets.setdefault(identity, dict(observations=0, TRUE=0, FALSE=0, UNKNOWN=0,
                    complete=0, incomplete=0))
                bucket["observations"] += 1
                bucket[status] += 1
                bucket["complete" if finding["qualification"]["coverage_complete"] else "incomplete"] += 1
                label = labels.get((finding["incident_key"], tuple(sorted(finding["evidence_ids"]))))
                if origin != "ACTUAL_RETAINED":
                    controlled += 1
                elif label is None or label["origin"] != "ACTUAL_RETAINED":
                    label_missing += 1
                elif _time(label["label_asof"]) < stamp:
                    # Adjudication cannot establish a later observation it never saw.
                    label_missing += 1
                elif label["status"] == "UNKNOWN":
                    label_unknown += 1
                    actions["UNKNOWN"] += 1
                else:
                    actual_labeled += 1
                    actions[label["actionability"]] += 1
                    if status in {"TRUE", "FALSE"}:
                        entry = ("TP" if label["status"] == "TRUE" else "FP") if status == "TRUE" else (
                            "FN" if label["status"] == "TRUE" else "TN")
                        matrix[entry] += 1
                    else:
                        prediction_unknown += 1
                    if status == label["status"] and status in samples and finding["incident_key"] not in sample_keys[status]:
                        sample_keys[status].add(finding["incident_key"])
                        if len(samples[status]) < 2:
                            samples[status].append(dict(incident_key=finding["incident_key"],
                                evidence_ids=finding["evidence_ids"], as_of=stamp.isoformat(),
                                label_asof=label["label_asof"], adjudicated_by=label["adjudicated_by"]))
            complete = _covered(coverage[name], start, end)
            historical = complete and len(samples["TRUE"]) == 2 and len(samples["FALSE"]) == 2
            summaries.append(dict(**{"class": name}, observations=len(selected), counts=totals,
                observation_status="OBSERVED" if selected else "UNOBSERVED",
                unique_incidents=len(incidents), eligible_opportunities=len(eligible),
                would_page_incidents=len(would_page), controlled_observations=controlled,
                actual_labeled=actual_labeled, label_missing=label_missing, label_unknown=label_unknown,
                prediction_unknown=prediction_unknown, calibration_unit="matched_observation",
                actionability=actions if actual_labeled or label_unknown else None,
                calibration=matrix if actual_labeled else None,
                coverage="COMPLETE" if complete else "INSUFFICIENT", incomplete_coverage_records=incomplete_coverage[name],
                day_coverage=[dict(day=day["day"], partial=day["partial"],
                    coverage="COMPLETE" if _covered(coverage[name], _time(day["start"]), _time(day["end"])) else "UNKNOWN")
                    for day in days],
                coverage_refs=sorted(set(coverage_refs[name])), samples=samples,
                sample_status="SUFFICIENT_ACTUAL_SAMPLES" if historical else "INSUFFICIENT",
                qualification="REPLAY_EVIDENCE_ONLY_NOT_PAGING_APPROVAL" if historical else "INSUFFICIENT"))
        result = dict(schema_version=SCHEMA, status="REPLAY_RECORDED_NOT_QUALIFICATION",
            window_start=start.isoformat(), window_end=end.isoformat(), elapsed_hours=336,
            et_buckets=days, records_seen=count, input_frame_bytes=frame_bytes, findings=len(observations),
            classifications_use_labels=False, policyversion=policy.get("policyversion", "UNKNOWN"),
            calibration_ref=policy.get("calibration_ref"), classes=summaries,
            buckets=[dict(**{"class": key[0]}, day=key[1], session=key[2], bot=key[3], broker=key[4], **value)
                     for key, value in sorted(buckets.items(), key=lambda item: str(item[0]))],
            paging_eligible=False, actual_paging_or_native_actions=0)
        _bounded(result, OUTPUT_BYTES)
        return result
    except _Invalid as exc:
        return _refusal(str(exc), count)
    except (TypeError, KeyError, ValueError, OverflowError):
        return _refusal("malformed_replay_frame", count)
