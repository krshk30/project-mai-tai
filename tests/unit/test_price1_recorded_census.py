"""Offline receipt contract controls; fixtures never stand in for delivered logs."""

from decimal import Decimal
import hashlib

import pytest

from tools.price1_log_census import normalize
from tools.price1_recorded_census import INPUTS, check_manifest, collect, day_receipt
from tools import price1_recorded_census as runner


def synthetic_receipt(tmp_path):
    path = tmp_path / "synthetic.log"
    path.write_text("2026-10-22 08:00:00,000 INFO [momentum-paper] "
        "[MOMENTUM-WARNING] action=ADD symbol=POM trigger_price=1.50 "
        "trigger_price_source=aggregate_high_at_add status=ADMITTED reason=observed_warning\n"
        "2026-10-22 08:02:00,000 INFO [fixture] no bar data\n")
    return normalize(path, expected_sha=hashlib.sha256(path.read_bytes()).hexdigest(),
        log_zone="UTC", prior_closes={"POM": Decimal(".66")}, prior_source="SYNTHETIC")


def test_per_minute_observed_counts_are_not_relabelled_as_old_or_new(tmp_path):
    result = day_receipt(synthetic_receipt(tmp_path))
    assert len(result["per_minute"]) == 3
    assert [row["observed_log_candidate_peak"] for row in result["per_minute"]] == [1, 1, 1]
    assert result["observed_log_candidate_max"] == 1
    assert result["observed_log_max_minus_16"] == -15
    for row in result["per_minute"]:
        assert row["old_prior_gate_candidate_count"] is None
        assert row["new_close_gate_candidate_count"] is None
        assert row["newly_eligible_symbols"] is None
        assert row["newly_eligible_POM"] == "UNKNOWN_BAR_CLOSE_MISSING"


def test_pom_prior_refused_does_not_prove_new_admission(tmp_path):
    result = day_receipt(synthetic_receipt(tmp_path))
    assert result["observed_POM_ADD_count"] == 1
    row = result["observed_POM_ADD_records"][0]
    assert row["old_prior_gate_at_ADD"] is False
    assert row["new_close_gate_at_ADD"] is None
    assert row["old_full_admission"] is row["new_full_admission"] is None
    assert result["per_minute"][0]["potential_expansion_ADD_symbols"] == ["POM"]
    assert result["old_prior_gate_candidate_max"] is None
    assert result["new_close_gate_max_vs_16"] == "UNKNOWN"


def test_no_pom_rows_does_not_invent_a_both_days_witness(tmp_path):
    source = synthetic_receipt(tmp_path)
    source["transitions"] = []
    result = day_receipt(source)
    assert result["observed_POM_ADD_count"] == 0
    assert result["observed_POM_ADD_records"] == []
    assert result["newly_eligible_POM"] == "UNKNOWN_BAR_CLOSE_MISSING"


def test_exact_delivered_manifest_contract(tmp_path):
    content = "".join(f"{digest}  {filename}\n" for filename, digest in INPUTS.values())
    (tmp_path / "SHA256SUMS").write_text(content)
    result = check_manifest(tmp_path)
    assert result["sha256"] == hashlib.sha256(content.encode()).hexdigest()


@pytest.mark.parametrize("case", ["duplicate", "wrong_hash", "missing_day", "extra_file"])
def test_manifest_drift_is_not_silently_accepted(tmp_path, case):
    lines = [f"{digest}  {filename}" for filename, digest in INPUTS.values()]
    if case == "duplicate":
        lines.append(lines[0])
    elif case == "wrong_hash":
        lines[0] = "0" * 64 + "  " + next(iter(INPUTS.values()))[0]
    elif case == "missing_day":
        lines.pop()
    else:
        lines.append("0" * 64 + "  extra.log")
    (tmp_path / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="manifest"):
        check_manifest(tmp_path)


@pytest.mark.parametrize("field", ["timezone_source", "prior_source"])
def test_provenance_is_required_before_any_packet_read(tmp_path, field):
    values = {"log_zone": "UTC", "timezone_source": "SYNTHETIC", "prior_source": "SYNTHETIC",
              "pom_prior_close": Decimal(".66")}
    values[field] = ""
    with pytest.raises(ValueError, match="provenance_required"):
        collect(tmp_path / "does-not-exist", **values)


def pinned_synthetic_packet(tmp_path, monkeypatch, *, wrong_day=False):
    """Explicitly substitute synthetic pins ONLY inside these unit controls."""
    inputs = {}
    for day in ("2026-10-21", "2026-10-22"):
        filename = day + "-SYNTHETIC.log"
        stamp = "2026-10-21" if wrong_day else day
        text = (f"{stamp} 08:00:00,000 INFO [momentum-paper] [MOMENTUM-WARNING] "
                "action=ADD symbol=POM trigger_price=1.50 "
                "trigger_price_source=aggregate_high_at_add status=ADMITTED reason=observed_warning\n"
                f"{stamp} 08:01:00,000 INFO [fixture] no recovered bars\n")
        (tmp_path / filename).write_text(text)
        inputs[day] = (filename, hashlib.sha256(text.encode()).hexdigest())
    monkeypatch.setattr(runner, "INPUTS", inputs)
    (tmp_path / "SHA256SUMS").write_text("".join(
        f"{digest}  {filename}\n" for filename, digest in inputs.values()))
    return inputs


def test_complete_runner_wiring_keeps_both_days_and_unknowns(tmp_path, monkeypatch):
    inputs = pinned_synthetic_packet(tmp_path, monkeypatch)
    result = collect(tmp_path, log_zone="UTC", timezone_source="SYNTHETIC declaration",
                     pom_prior_close=Decimal(".66"), prior_source="SYNTHETIC prior")
    assert set(result["days"]) == {"2026-10-21", "2026-10-22"}
    assert result["model_executed"] is result["provider_access"] is False
    assert result["acceptance"] == "LOG_INVENTORY_ONLY_COUNTERFACTUAL_AND_OWNER_CAP_UNKNOWN"
    assert set(result["source_sha256"]) == {"price1_recorded_census.py", "price1_log_census.py"}
    for day, row in result["days"].items():
        assert row["input_sha256"] == inputs[day][1]
        assert row["observed_POM_ADD_count"] == row["observed_log_candidate_max"] == 1
        assert len(row["per_minute"]) == 2
        assert row["old_prior_gate_candidate_max"] is row["new_close_gate_candidate_max"] is None
        assert row["newly_eligible_POM"] == "UNKNOWN_BAR_CLOSE_MISSING"


def test_runner_does_not_trust_manifest_without_actual_file_hash(tmp_path, monkeypatch):
    inputs = pinned_synthetic_packet(tmp_path, monkeypatch)
    path = tmp_path / inputs["2026-10-22"][0]
    path.write_text(path.read_text().replace("1.50", "1.51"))
    with pytest.raises(ValueError, match="input_sha256_mismatch"):
        collect(tmp_path, log_zone="UTC", timezone_source="SYNTHETIC",
                pom_prior_close=Decimal(".66"), prior_source="SYNTHETIC")


def test_runner_rejects_pinned_contents_with_wrong_day_label(tmp_path, monkeypatch):
    pinned_synthetic_packet(tmp_path, monkeypatch, wrong_day=True)
    with pytest.raises(ValueError, match="pinned_log_day_mismatch"):
        collect(tmp_path, log_zone="UTC", timezone_source="SYNTHETIC",
                pom_prior_close=Decimal(".66"), prior_source="SYNTHETIC")
