"""Synthetic parser controls only; no warning application or native transport."""

from decimal import Decimal
import hashlib

import pytest

from tools.price1_log_census import normalize


def inventory(tmp_path, rows, **kwargs):
    path = tmp_path / "recorded.log"
    path.write_text("\n".join(rows) + "\n")
    return normalize(path, expected_sha=hashlib.sha256(path.read_bytes()).hexdigest(),
                     log_zone="UTC", **kwargs)


def event(clock, action, symbol="POM", high="1.20"):
    status = "ADMITTED" if action == "ADD" else "DROPPED"
    return (f"2026-10-22 {clock},000 INFO [momentum-paper] [MOMENTUM-WARNING] "
            f"action={action} symbol={symbol} trigger_price={high} "
            f"trigger_price_source=aggregate_high_at_add status={status} reason=fixture")


def test_high_above_one_never_invents_close_or_new_admission(tmp_path):
    result = inventory(tmp_path, [event("08:00:00", "ADD")],
                       prior_closes={"POM": Decimal(".66")}, prior_source="declared_fixture")
    row = result["transitions"][0]
    assert row["old_prior_gate_at_ADD"] is False
    assert row["new_close_gate_at_ADD"] is None
    assert row["new_full_admission"] is None
    assert result["minutes"][0]["potential_gate_expansion_ADD_symbols"] == ["POM"]
    assert result["minutes"][0]["newly_entering_symbols"] is None
    assert result["new_candidate_max"] is None
    assert result["new_vs_16"] == "UNKNOWN"


def test_missing_prior_and_high_below_one(tmp_path):
    result = inventory(tmp_path, [event("08:00:00", "ADD", high=".97")])
    assert result["transitions"][0]["old_prior_gate_at_ADD"] is None
    assert result["transitions"][0]["new_close_gate_at_ADD"] is False
    assert result["old_candidate_max"] is None


def test_minute_carry_is_log_ledger_not_new_model(tmp_path):
    result = inventory(tmp_path, [event("08:00:59", "ADD"), event("08:02:00", "DROP")])
    assert [row["recorded_transition_set_peak"] for row in result["minutes"]] == [1, 1, 1]
    assert result["minutes"][1]["transition_lines"] == []
    assert result["minutes"][1]["old_candidate_count"] is None
    assert result["minutes"][-1]["recorded_transition_set_end"] == []


def test_restart_clears_only_reconstruction_and_exposes_orphan_drop(tmp_path):
    result = inventory(tmp_path, [event("08:00:00", "ADD"),
        "2026-10-22 08:01:00,000 INFO [momentum-paper] [MOMENTUM-PAPER] starting paper observer; live_handoff=True",
        event("08:02:00", "DROP")])
    assert result["boundaries"][0]["kind"] == "start"
    assert result["anomalies"] == [{"line": 3, "reason": "orphan_DROP"}]
    assert result["old_vs_16"] == "UNKNOWN"


def test_sha_mismatch_fails(tmp_path):
    path = tmp_path / "bad.log"
    path.write_text(event("08:00:00", "ADD") + "\n")
    with pytest.raises(ValueError, match="input_sha256_mismatch"):
        normalize(path, expected_sha="0" * 64, log_zone="UTC")


def test_prior_needs_provenance(tmp_path):
    with pytest.raises(ValueError, match="prior_close_provenance_required"):
        inventory(tmp_path, [event("08:00:00", "ADD")], prior_closes={"POM": Decimal(".66")})


def test_malformed_transition_not_silently_discarded(tmp_path):
    with pytest.raises(ValueError, match="malformed_transition"):
        inventory(tmp_path, [event("08:00:00", "ADD").replace("ADMITTED", "DROPPED")])


def test_backward_log_clock_refused(tmp_path):
    with pytest.raises(ValueError, match="nonmonotonic"):
        inventory(tmp_path, [event("08:02:00", "ADD"), event("08:01:00", "DROP")])


def test_duplicate_add_is_visible(tmp_path):
    result = inventory(tmp_path, [event("08:00:00", "ADD"), event("08:00:01", "ADD")])
    assert result["recorded_transition_set_peak"] == 1
    assert result["anomalies"] == [{"line": 2, "reason": "duplicate_ADD"}]


def test_drop_high_is_not_current_close(tmp_path):
    result = inventory(tmp_path, [event("08:00:00", "ADD"), event("08:01:00", "DROP", high=".50")])
    assert result["transitions"][1]["new_close_gate_at_ADD"] is None
