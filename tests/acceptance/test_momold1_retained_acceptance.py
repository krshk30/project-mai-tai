"""Mandatory actual MOMOLD1 MUST1/3 gates, separate from portable unit controls.

Run this whole module after required retained inputs arrive. Missing inputs fail;
there are no skips, synthetic replacements or unit-pass acceptance shortcuts.
MUST3 remains selected local copied-table readers, not full production boot.
"""

from datetime import UTC, datetime
import json
import os
from pathlib import Path

from scripts.momold1_copy_boot_proof import run_copy
from scripts.momold1_live_offer_proof import (
    AMOD_EXPORT, ET, EXPORT, compare, compare_amod_roots, handoff_source_proof,
    load_records, offer_method, record_calls, sha, source_pair, verified_amod_packet,
)
from project_mai_tai.services.momentum_paper_app import MomentumPaperService


ROOT = Path(__file__).resolve().parents[2]


def test_actual_1020_warning_detected_offer_bytes_match_pre_post_and_bound_method():
    path = Path(os.environ.get("MAI_TAI_MOMOLD1_1020_EXPORT",
        str(EXPORT / "momentum_paper_events_1020.csv")))
    assert sha(path.read_bytes()) == "30ab970778f89bcd1dcaff783f04a30a1c61735081658c3bce3db7d8d572cb1c"
    records = load_records(path)
    assert all(str(record.session_date) == "2026-10-20" for record in records)
    pre, post = source_pair(ROOT)
    report = compare(pre, post, records)
    assert report["equal"] and report["records"] > 0
    method, metadata = offer_method(post)
    for case in report["cache_cases"]:
        extracted = record_calls(method, records, cache_case=case,
                                 stale_seconds=metadata["snapshot_stale_seconds"])
        bound = record_calls(MomentumPaperService._offer_live_signals, records, cache_case=case,
                             stale_seconds=metadata["snapshot_stale_seconds"])
        assert extracted == bound
        assert len(report["cache_cases"][case]["post"]) == len(records)
        expected_volume = 123456 if case in {"fresh", "at_boundary"} else None
        assert all(row["day_volume"] == expected_volume for row in report["cache_cases"][case]["post"])


def test_actual_amod_1021_0815_offer_bytes_require_exact_retained_payload():
    path = Path(os.environ.get("MAI_TAI_MOMOLD1_AMOD_EXPORT",
        str(AMOD_EXPORT / "events_amod_1021.csv")))
    assert path.is_file(), f"MUST1 BLOCKED: exact retained AMOD payload not delivered: {path}"
    records = [record for record in load_records(path) if record.symbol == "AMOD"
        and datetime.fromtimestamp(record.payload["observed_decision_ns"] / 1e9, UTC)
        .astimezone(ET).strftime("%Y-%m-%d %H:%M") == "2026-10-21 08:15"]
    assert records, "MUST1 BLOCKED: delivered file lacks exact AMOD 10-21 08:15 DETECTED"
    assert compare(*source_pair(ROOT), records)["equal"]
    verified_records, context, packet = verified_amod_packet(path)
    assert [record.logical_id for record in verified_records] == [record.logical_id for record in records]
    assert packet["retained_event_rows"] == 9 and packet["selected_detected_rows"] == 2
    assert packet["log_is_in_three_file_manifest"] is False
    assert context["day_volume"] == 15361 and context["broker"] == "schwab" and context["reason"] == "SEC_US"
    assert len(context["recorded_refusals"]) == 2
    components = handoff_source_proof(ROOT)
    assert components["approved"], components
    assert components["admission_unchanged"] and components["producer_serialization_unchanged"]
    assert components["raw_allowlisted_diff"]
    pre_value = os.environ.get("MAI_TAI_MOMOLD1_PRE_ROOT")
    assert pre_value, "MUST1 BLOCKED: distinct pinned original PRE root required"
    report = compare_amod_roots(Path(pre_value), ROOT, path)
    print(json.dumps(report, sort_keys=True))  # Retain raw allowlist/rows/decisions with pytest -s.
    assert report["equal"]
    for name, case in report["post"]["cache_cases"].items():
        expected = "day_volume_below_50000" if name in {"fresh", "at_boundary"} else "day_volume_unknown"
        assert case == report["pre"]["cache_cases"][name]
        assert case["queue_size"] == 0 and case["ledger"] == []
        for side in ("pre", "post"):
            calls = report[side]["cache_cases"][name]["calls"]
            assert len(calls) == len(records)
            assert all(row["handoff_result"] == expected for row in calls)
            assert all(row["day_volume"] == (15361 if name in {"fresh", "at_boundary"} else None)
                       for row in calls)


def test_actual_tonight_copy_is_required_for_must3_acceptance():
    copy_value = os.environ.get("MAI_TAI_MOMOLD1_TONIGHT_COPY")
    manifest_value = os.environ.get("MAI_TAI_MOMOLD1_TONIGHT_MANIFEST")
    assert copy_value and manifest_value, "MUST3 BLOCKED: actual tonight copy and manifest not delivered"
    copy, manifest = Path(copy_value), Path(manifest_value)
    assert copy.is_file() and manifest.is_file(), "MUST3 BLOCKED: actual copied input files missing"
    assert json.loads(manifest.read_text())["kind"] == "ACTUAL_TONIGHT_COPY"
    result = run_copy(copy, manifest)
    assert result["must3"] == "LOCAL_COPY_READERS_COMPLETED"
    assert result["legacy_rows_before"] == result["legacy_rows_after"]
