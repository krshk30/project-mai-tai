"""Pure finite SMARTALERT1 L2 replay; never acquires data or sends alerts.

Input grammar (Main supplies immutable references, not this module):
  {kind: observation, as_of: RFC3339, evidence: complete envelope}
  {kind: label, incident_key, status: TRUE|FALSE|UNKNOWN, evidence_ids,
   label_asof: RFC3339, adjudicated_by: stable review reference}
Observation as_of is nondecreasing. Labels are separate adjudication metadata,
never operands to evaluate_trade. Later labels do not rewrite earlier findings.
Coverage records here describe supplied observations ONLY; absent intervals are
UNKNOWN without a separate acquisition manifest. No 336h completeness inferred.

Resource bounds are explicit refusals, not alert thresholds: <=1000 stored
incident identities/labels/buckets, <=64 refs each and <=512KiB output. Overflow
returns a refused/UNKNOWN summary, never a partially clean success. Input is
iterated once; no files, APP imports, environment, clock, provider or delivery.
"""

import json
from datetime import time as civil_time, timedelta
from zoneinfo import ZoneInfo

from ops.smartalert1.trade import (
    CLASSES, SCHEMA, EvidenceUnknown, _clock, _json_bytes, _ref, _stamp, evaluate_trade,
)

LIMIT = 1000
STATUSES = ("TRUE", "FALSE", "UNKNOWN")


def _empty(reason):
    return {"schema_version": SCHEMA, "status": "INSUFFICIENT", "reason_code": reason,
            "historical_coverage": "UNKNOWN", "paging_eligible": False,
            "observations": 0, "finding_observations": 0, "counts": None,
            "buckets": [], "unique_incidents": None, "would_page_incidents": None,
            "calibration": {"status": "INSUFFICIENT", "TP": None, "FP": None,
                            "TN": None, "FN": None, "label_missing": None},
            "samples": {cls: {"status": "INSUFFICIENT", "TRUE": [], "FALSE": []}
                        for cls in CLASSES},
            "no_future_outcome_operands": True}


def _bucket():
    return {"observations": 0, "TRUE": 0, "FALSE": 0, "UNKNOWN": 0,
            "complete_observations": 0, "incomplete_observations": 0,
            "eligible_opportunities": 0, "eligibility_unknown": 0}


def _metrics():
    return {"status": "INSUFFICIENT", "TP": 0, "FP": 0, "TN": 0, "FN": 0,
            "label_missing": 0, "unknown_prediction_or_label": 0,
            "matched_labels": 0, "binary_labeled_incidents": 0}


def _finish_metrics(value):
    if value["binary_labeled_incidents"] == 0:
        for metric in ("TP", "FP", "TN", "FN"):
            value[metric] = None
    else:
        value["status"] = "PARTIAL" if (value["label_missing"] or value["unknown_prediction_or_label"]
            or value.get("orphan_labels", 0)) else "LABELED_SUPPLIED_INCIDENTS_ONLY"


def replay_trade(records, policy: dict, *, window_start: str, window_end: str) -> dict:
    """Evaluate actual as-of operands; report unavailable labels as null, not zero.

    'would_page' is unique predicted TRUE under the supplied candidate/qualified
    window with positively live, nonmanual identity; it is NOT permission to page.
    Calibration is ever TRUE, else UNKNOWN if any observation was unknown, else
    FALSE. Recovery never erases an earlier counterfactual page. Observations
    remain separately counted, not extra incidents.
    Historical two-TRUE/two-FALSE samples require labels AND real evidence refs.
    """
    try:
        start, end = _clock(window_start), _clock(window_end)
        if end - start != timedelta(hours=336):
            raise EvidenceUnknown("replay_window_not_336_elapsed_hours")
        _json_bytes(policy, 1048576)
        if type(policy) is not dict:
            raise EvidenceUnknown("policy_unknown")
        if policy.get("enabled", False) is False or policy.get("mode") == "OFF":
            result = _empty("policy_off")
            result.update(status="OFF", window_start=_stamp(start), window_end=_stamp(end))
            return result
        labels, incidents, buckets, would_page = {}, {}, {}, set()
        first_seen, eligibility, decisions = {}, {}, {}
        samples = {cls: {"status": "INSUFFICIENT", "TRUE": [], "FALSE": []} for cls in CLASSES}
        totals = {status: 0 for status in STATUSES}
        observation_count = finding_count = 0
        previous = None
        label_conflicts = set()
        try:
            records = iter(records)
        except TypeError:
            raise EvidenceUnknown("replay_records_not_iterable") from None
        for record in records:
            _json_bytes(record, 1048576)
            if type(record) is not dict:
                raise EvidenceUnknown("replay_record_unknown")
            kind = record.get("kind")
            if kind == "label":
                key, status = record.get("incident_key"), record.get("status")
                stamp = _clock(record.get("label_asof"))
                refs = record.get("evidence_ids")
                if (not isinstance(key, str) or len(key) > 1200 or status not in STATUSES
                        or not _ref(record.get("adjudicated_by")) or not start <= stamp <= end
                        or type(refs) is not list or not refs or len(refs) > 64
                        or any(not _ref(ref) for ref in refs) or len(set(refs)) != len(refs)):
                    raise EvidenceUnknown("replay_label_unknown")
                value = {"status": status, "evidence_ids": list(refs),
                         "label_asof": _stamp(stamp), "adjudicated_by": record["adjudicated_by"],
                         "origin": record.get("origin") if record.get("origin") in
                            ("retained_real", "synthetic_control") else "UNKNOWN"}
                if key in labels and labels[key] != value:
                    label_conflicts.add(key)
                labels[key] = value
                if len(labels) > LIMIT:
                    raise EvidenceUnknown("replay_resource_overflow")
                continue
            if kind != "observation":
                raise EvidenceUnknown("replay_record_kind_unknown")
            as_of = _clock(record.get("as_of"))
            if not start <= as_of <= end or previous is not None and as_of < previous:
                raise EvidenceUnknown("replay_chronology_unknown")
            previous = as_of
            evidence = record.get("evidence")
            findings = evaluate_trade(evidence, policy, as_of=_stamp(as_of))
            observation_count += 1
            for finding in findings:
                finding_count += 1
                status = finding["status"]
                totals[status] += 1
                key = finding["incident_key"]
                day = as_of.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                dimensions = (finding["class"], day, finding["session"], finding["bot"],
                              finding["broker"], finding["account"])
                bucket_key = json.dumps(dimensions, separators=(",", ":"))
                value = buckets.setdefault(bucket_key, _bucket())
                value["observations"] += 1
                value[status] += 1
                complete = finding["qualification"]["coverage_complete"] and status != "UNKNOWN"
                value["complete_observations" if complete else "incomplete_observations"] += 1
                # Eligibility needs an explicit typed operand in the matching check.
                payload = evidence.get("payload", {}) if type(evidence) is dict else {}
                checks = payload.get("checks", []) if type(payload) is dict else []
                checks = checks if type(checks) is list else []
                matched = [check for check in checks if type(check) is dict
                           and check.get("class") == finding["class"]
                           and json.loads(key)[-1] == check.get("condition_epoch")]
                operands = matched[0].get("operands") if len(matched) == 1 else None
                eligible = operands.get("entry_eligible") if type(operands) is dict else None
                identity = (bucket_key, key)
                if complete and type(eligible) is bool:
                    old = eligibility.get(identity)
                    if old == "unknown":
                        value["eligibility_unknown"] -= 1
                    if eligible and old != "eligible":
                        value["eligible_opportunities"] += 1
                    if old != "eligible":
                        eligibility[identity] = "eligible" if eligible else "ineligible"
                else:
                    if identity not in eligibility:
                        value["eligibility_unknown"] += 1
                        eligibility[identity] = "unknown"
                new_first = finding["first_seen"]
                if new_first is not None:
                    if key in first_seen and first_seen[key] != new_first:
                        raise EvidenceUnknown("condition_first_seen_changed")
                    first_seen[key] = new_first
                incidents[key] = finding
                priority = {"TRUE": 3, "UNKNOWN": 2, "FALSE": 1}
                if key not in decisions or priority[status] > priority[decisions[key]["finding"]["status"]]:
                    decisions[key] = {"finding": finding, "bucket": bucket_key}
                if (status == "TRUE" and finding["manual"] is False and finding["paper"] is False
                        and finding["freshness"]["status"] == "FRESH"):
                    would_page.add(key)
                if len(incidents) > LIMIT or len(buckets) > LIMIT or len(eligibility) > LIMIT:
                    raise EvidenceUnknown("replay_resource_overflow")

        if observation_count == 0:
            raise EvidenceUnknown("replay_observations_missing")
        calibration = {**_metrics(), "label_conflicts": len(label_conflicts),
                       "orphan_labels": len(set(labels) - set(incidents)),
                       "label_origin_counts": {"retained_real": 0, "synthetic_control": 0, "UNKNOWN": 0}}
        by_class = {cls: _metrics() for cls in CLASSES}
        for value in buckets.values():
            value["calibration"] = _metrics()
        for key, decision in sorted(decisions.items()):
            finding = decision["finding"]
            targets = [calibration, buckets[decision["bucket"]]["calibration"]]
            if finding["class"] in by_class:
                targets.append(by_class[finding["class"]])
            label = labels.get(key)
            if (label is None or key in label_conflicts or finding["first_seen"] is not None
                    and _clock(label["label_asof"]) < _clock(finding["first_seen"])):
                for target in targets:
                    target["label_missing"] += 1
                continue
            for target in targets:
                target["matched_labels"] += 1
            calibration["label_origin_counts"][label["origin"]] += 1
            prediction, truth = finding["status"], label["status"]
            if prediction == "UNKNOWN" or truth == "UNKNOWN":
                for target in targets:
                    target["unknown_prediction_or_label"] += 1
                continue
            metric = ("TP" if truth == "TRUE" else "FP") if prediction == "TRUE" else (
                "FN" if truth == "TRUE" else "TN")
            for target in targets:
                target[metric] += 1
                target["binary_labeled_incidents"] += 1
            cls = finding["class"]
            if cls in samples and label["origin"] == "retained_real" and len(samples[cls][truth]) < 2:
                samples[cls][truth].append({"incident_key": key,
                    "prediction": prediction, "evidence_ids": finding["evidence_ids"],
                    "label": label})
        for metric_set in [calibration, *by_class.values(), *(v["calibration"] for v in buckets.values())]:
            _finish_metrics(metric_set)
        for sample in samples.values():
            if len(sample["TRUE"]) == 2 and len(sample["FALSE"]) == 2:
                sample["status"] = "SUPPLIED_LABELED_SAMPLES_NOT_INDEPENDENTLY_VERIFIED"
        output = {"schema_version": SCHEMA, "status": "SUPPLIED_RECORDS_ONLY",
                  "reason_code": "historical_manifest_qualification_required",
                  "window_start": _stamp(start), "window_end": _stamp(end),
                  "elapsed_hours": 336, "et_start": start.astimezone(ZoneInfo("America/New_York")).isoformat(),
                  "et_end": end.astimezone(ZoneInfo("America/New_York")).isoformat(),
                  "partial_boundary_days": {
                      "first": start.astimezone(ZoneInfo("America/New_York")).time() != civil_time.min,
                      "last": end.astimezone(ZoneInfo("America/New_York")).time() != civil_time.min},
                  "historical_coverage": "UNKNOWN",
                  "paging_eligible": False, "observations": observation_count,
                  "finding_observations": finding_count, "counts": totals,
                  "buckets": [{"dimensions": json.loads(key), **value}
                              for key, value in sorted(buckets.items())],
                  "unique_incidents": len(incidents), "would_page_incidents": len(would_page),
                  "eligible_opportunities": len({key for (_bucket_key, key), value in eligibility.items()
                                                  if value == "eligible"}),
                  "calibration": calibration, "calibration_by_class": by_class, "samples": samples,
                  "calibration_prediction": "ever_true_else_any_unknown_else_false",
                  "no_future_outcome_operands": True}
        _json_bytes(output, 524288)
        return output
    except EvidenceUnknown as exc:
        return _empty(exc.args[0])
