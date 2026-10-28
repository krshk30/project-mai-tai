"""CONTROLLED grammar only; no actual log parse, boot, PG or permission proof."""

from hashlib import sha256

import pytest

from tools.l12_recorded_boot_timeline import (
    OLD_READY_RETURNS, c11810_candidate_from_records, old_window_candidates_from_records,
    parse_old_log_lines,
)
from tests.unit.test_l12_c11810_carried_windows import fleet


def emitted(slot, reason="leg_skipped_country_other_venue"):
    return ("2026-10-23 11:38:02,000 INFO controlled | [V2-RESTING-RETURN] "
        "symbol=YMAT opportunity_id=1792758487033 bar_ms=1792755420000 "
        f"slot={slot} reason={reason}\n")


@pytest.mark.parametrize("slot,account", [
    ("first", "live:schwab_1m_v2"), ("reclaim", "live:schwab_1m_v2"),
    ("first", "live:orb"), ("reclaim", "live:orb"),
])
def test_four_actual_emitter_shapes_keep_raw_identity_and_never_imply_readiness(slot, account):
    raw = emitted(slot + ":" + account)
    row, = parse_old_log_lines(raw.encode())
    assert row["kind"] == "ACCOUNT_LEG_CONTEXT_NOT_READINESS"
    assert row["slot"] == slot + ":" + account
    assert row["base_slot"] == slot
    assert row["account"] == account
    assert row["reason"] == "leg_skipped_country_other_venue"
    assert row["readiness_witness"] is False
    assert row["reason"] not in OLD_READY_RETURNS
    assert row["ordinal"] == 1
    assert row["raw"] == raw[:-1]
    assert row["raw_line_sha256"] == sha256(raw[:-1].encode()).hexdigest()
    assert row["original_ns"] == 1792755482000000000
    assert row["closed_bar_ms"] == 1792755420000


@pytest.mark.parametrize("slot,reason", [
    ("first:live:unknown", "leg_skipped_country_other_venue"),
    ("first:paper:orb", "leg_skipped_country_other_venue"),
    ("other:live:orb", "leg_skipped_country_other_venue"),
    ("first:live:orb:extra", "leg_skipped_country_other_venue"),
    ("first:live:orb", "track_long_no_new_rest"),
    ("first:live:orb", "track_line_not_ready"),
    ("first:live:orb", "leg_skipped_other_reason"),
    ("first:live:", "leg_skipped_country_other_venue"),
])
def test_unknown_account_or_qualified_reason_stops_instead_of_broadening_grammar(slot, reason):
    with pytest.raises(ValueError, match="OLD_return_grammar_STOP_line_1"):
        parse_old_log_lines(emitted(slot, reason).encode())


def test_revised_candidate_retains_each_leg_but_no_ready_or_not_ready_count():
    raw = emitted("first:live:orb") + emitted("reclaim:live:schwab_1m_v2")
    records = parse_old_log_lines(raw.encode())
    result = c11810_candidate_from_records(fleet(), records, ruling_sha256="a" * 64)
    assert result["account_leg_context"] == records
    assert [row["ordinal"] for row in result["account_leg_context"]] == [1, 2]
    window = next(row for row in result["windows"] if row["event_id"] == "YMAT:CONFIRM")
    assert window["OLD"]["not_ready_closed_minutes"] == 0
    assert window["OLD"]["first_observed_ready_return"] is None
    assert window["OLD"]["ready_status"] == "NO_OBSERVED_READY_UNKNOWN_NOT_ZERO_READY"
    assert result["permission_granted"] is False
    assert result["native_availability"] == "UNKNOWN"


def test_historical_geometry_also_keeps_legs_separate_from_readiness():
    records = parse_old_log_lines(emitted("first:live:orb").encode())
    result = old_window_candidates_from_records(records, fleet(), review_sha256="a" * 64)
    assert result["account_leg_context"] == records
    window = result["baseline_candidate"]["windows"]["YMAT:CONFIRM"]
    assert window["ready_status"] == "UNKNOWN"
    assert window["not_ready_closed_minutes"] == 0


def test_ordinary_positive_return_remains_independent_of_account_leg_context():
    raw = emitted("first:live:orb") + emitted("first", "track_long_no_new_rest")
    records = parse_old_log_lines(raw.encode())
    result = c11810_candidate_from_records(fleet(), records, ruling_sha256="a" * 64)
    window = next(row for row in result["windows"] if row["event_id"] == "YMAT:CONFIRM")
    assert window["OLD"]["first_observed_ready_return"]["ordinal"] == 2
    assert window["OLD"]["first_observed_ready_return"]["slot"] == "first"
    assert [row["ordinal"] for row in result["account_leg_context"]] == [1]


def test_malformed_known_line_is_not_hidden_by_a_valid_leg_before_it():
    with pytest.raises(ValueError, match="OLD_return_grammar_STOP_line_2"):
        parse_old_log_lines((emitted("first:live:orb") + emitted("first:live:orb", "unknown")).encode())
