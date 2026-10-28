"""Synthetic driver controls only; never original-eleven acceptance evidence."""

import csv
import hashlib
import json
from pathlib import Path

import pytest

from test_orb_pastlevel_replay import SOURCE_PATHS, compare, stamp


AT = "2026-10-21T13:28:02+00:00"


def packet(tmp_path, *, decision_change=None, quote_change=None, manifest_change=None,
           trades=None, events=None):
    root = Path(__file__).resolve().parents[2]
    decision = dict(decision_id="decision", plan_id="fixture-plan", symbol="OLOX",
                    session_date="2026-10-21", decision_at=AT, action="place",
                    level="1.20", quote_id="quote", observed_order_type="STOP_LIMIT",
                    observed_limit_price="1.20", capture_start=AT,
                    capture_end="2026-10-21T13:29:02+00:00", capture_complete="true")
    quote = dict(quote_id="quote", plan_id="fixture-plan", symbol="OLOX", provider="Massive",
                 producer_at=AT, available_at=AT, ask="1.21", bid="1.20",
                 source_event_id="synthetic-event", source_timestamp=AT,
                 raw_record_reference="SYNTHETIC_DRIVER_CONTROL_ONLY")
    decision.update(decision_change or {})
    quote.update(quote_change or {})
    tables = {"decisions": [decision], "quotes": [quote],
              "trades": trades or [], "broker_events": events or []}
    manifest = dict(schema="ORBPASTLEVEL1-input-v2", quote_binding="decision-second-counterfactual",
                    exporter_identity="SYNTHETIC_DRIVER_CONTROL_ONLY",
                    census_receipt="synthetic single-plan fixture, NOT actual census",
                    complete_original11_census=False, missing_inputs=[], quote_max_age_ms=2000,
                    declared_time_bases=["strict-arrival", "declared-event-time-study"],
                    plans=[dict(plan_id="fixture-plan", symbol="OLOX", session_date="2026-10-21",
                                decision_ids=["decision"])], files={},
                    candidate_source_sha256={name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                             for name in SOURCE_PATHS})
    for name, records in tables.items():
        path = tmp_path / f"{name}.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(records[0]) if records else ["plan_id"])
            writer.writeheader()
            writer.writerows(records)
        manifest["files"][name] = dict(path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    manifest.update(manifest_change or {})
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def test_real_selector_comparison_is_never_lifecycle_or_fill_acceptance(tmp_path):
    report = compare(packet(tmp_path), "strict-arrival")
    result = report["results"][0]
    assert result["selection"]["order_type"] == "LIMIT"
    assert result["selection"]["limit_price"] == "1.21"
    assert result["order_type_changed"] is True
    assert result["native_execution"] == "NOT_INFERRED"
    assert result["gate_replay"] == result["lifecycle_authority"] == "NOT_REPLAYED"
    assert report["acceptance"] == "NOT_ESTABLISHED_PRICE_STUDY_ONLY"
    assert report["original11_coverage"] == "UNKNOWN"
    assert report["actual_quote_rows_unmodified"][0]["source_timestamp"] == AT


@pytest.mark.parametrize("change,reason", [
    ({"symbol": "OTHER"}, "quote_plan_identity_mismatch"),
    ({"plan_id": "OTHER"}, "unbound_quote_plan"),
    ({"provider": "UNKNOWN"}, "Massive_quote_provenance_UNKNOWN"),
    ({"raw_record_reference": ""}, "Massive_quote_provenance_UNKNOWN"),
    ({"source_timestamp": ""}, "Massive_quote_provenance_UNKNOWN"),
    ({"available_at": ""}, "quote_availability_UNKNOWN"),
    ({"available_at": "2026-10-21T13:28:03Z"}, "quote_not_yet_available"),
    ({"producer_at": "2026-10-21T13:28:01Z", "available_at": "2026-10-21T13:28:00Z"},
     "quote_arrival_before_event_UNKNOWN"),
])
def test_missing_or_mismatched_quote_never_becomes_a_send(tmp_path, change, reason):
    directory = packet(tmp_path, quote_change=change)
    if reason.startswith("unbound"):
        with pytest.raises(ValueError, match=reason):
            compare(directory, "strict-arrival")
    else:
        report = compare(directory, "strict-arrival")
        assert report["results"][0]["reason"] == reason
        assert report["unknown_or_no_send_decisions"] == ["decision"]


def test_event_time_study_does_not_invent_local_quote_arrival(tmp_path):
    directory = packet(tmp_path, quote_change={"available_at": ""})
    report = compare(directory, "declared-event-time-study")
    assert report["results"][0]["selection"]["order_type"] == "LIMIT"
    assert report["actual_quote_rows_unmodified"][0]["available_at"] == ""
    assert report["quote_binding"] == "decision-second-counterfactual"
    assert report["acceptance"] == "NOT_ESTABLISHED_PRICE_STUDY_ONLY"


@pytest.mark.parametrize("change,reason", [
    ({"candidate_source_sha256": {"arbitrary": "hash"}}, "candidate_source_inventory_missing"),
    ({"census_receipt": ""}, "export_provenance_missing"),
    ({"missing_inputs": None}, "missing_input_inventory_required"),
    ({"quote_max_age_ms": True}, "quote_age_setting_invalid"),
    ({"quote_binding": "SIP_means_consumed"}, "quote_binding_missing"),
])
def test_manifest_cannot_substitute_labels_for_required_bindings(tmp_path, change, reason):
    with pytest.raises(ValueError, match=reason):
        compare(packet(tmp_path, manifest_change=change), "strict-arrival")


def test_missing_declared_decision_remains_in_denominator(tmp_path):
    plans = [dict(plan_id="fixture-plan", symbol="OLOX", session_date="2026-10-21",
                  decision_ids=["decision", "missing-reprice"])]
    report = compare(packet(tmp_path, manifest_change={"plans": plans,
                     "complete_original11_census": True}), "strict-arrival")
    assert report["missing_decisions"] == {"fixture-plan": ["missing-reprice"]}
    assert report["original11_coverage"] == "UNKNOWN"


def test_tampered_input_bytes_rejected_before_comparison(tmp_path):
    directory = packet(tmp_path)
    (directory / "quotes.csv").write_text("tampered")
    with pytest.raises(ValueError, match="hash_mismatch:quotes"):
        compare(directory, "strict-arrival")


@pytest.mark.parametrize("timestamp", ["2026-10-21T13:28:02", "2026-10-21T13:28:02.000000001Z"])
def test_no_naive_time_or_silent_nanosecond_truncation(timestamp):
    with pytest.raises(ValueError):
        stamp(timestamp)


def trade(identity="trade", price="1.26", **updates):
    row = dict(trade_id=identity, plan_id="fixture-plan", symbol="OLOX", price=price,
               event_at=AT, received_at=AT, eligible="true", correction_of="", cancel_of="")
    row.update(updates)
    return row


@pytest.mark.parametrize("update", [{"received_at": ""}, {"eligible": "UNKNOWN"},
                                      {"correction_of": "old"}, {"cancel_of": "old"},
                                      {"symbol": "OTHER"}])
def test_trade_uncertainty_is_retained_not_a_target_fill(tmp_path, update):
    report = compare(packet(tmp_path, trades=[trade(**update)]), "strict-arrival")
    study = report["results"][0]["conditional_tape_study"]
    assert study["verdict"] == "UNKNOWN_trade_provenance_or_eligibility"
    assert study["uncertain_trade_ids"] == ["trade"]
    assert study["native_fill"] == "NOT_INFERRED"


def test_same_time_opposing_touches_remain_ambiguous(tmp_path):
    report = compare(packet(tmp_path, trades=[trade("target"), trade("stop", "1.10")]),
                     "strict-arrival")
    study = report["results"][0]["conditional_tape_study"]
    assert study["same_time_target_stop_ambiguous"] is True
    assert study["native_fill"] == "NOT_INFERRED"


def test_actual_broker_event_is_preserved_without_counterfactual_relabeling(tmp_path):
    event = dict(plan_id="fixture-plan", decision_id="decision", native_status="filled",
                 fill_price="1.20", raw_record_reference="synthetic_control")
    report = compare(packet(tmp_path, events=[event]), "strict-arrival")
    assert report["actual_broker_events_baseline_unmodified"] == [event]
    assert report["native_fills_target_stop_outcomes"] == "NOT_REPLAYED"
