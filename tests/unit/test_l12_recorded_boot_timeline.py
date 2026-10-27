"""Boot-only mapping controls; never actual restored-row timeline proof."""

from copy import deepcopy

import pytest

from tools.l12_recorded_boot_timeline import (
    RecordedSessionMap, map_file_history_rows, timeline_denominator)


FRI_0400 = 1792742400000
MON_0400 = 1793001600000


def mapping():
    return RecordedSessionMap(target_day="2026-10-26", ruling_sha256="a" * 64,
        chronology_review_sha256="b" * 64)


def test_market_only_mapping_preserves_raw_row_and_native_durable_clocks():
    row = dict(stamp=FRI_0400, row_id="retained-row-1", origin="strategy_bar_history",
        available_at_ms=FRI_0400 + 60001, open="1.01", close="1.02",
        native_header_ms=123, durable_created_ms=456)
    before = deepcopy(row)
    result = map_file_history_rows([row], mapping())[0]
    assert result["stamp"] == MON_0400
    assert result["available_at_ms"] == MON_0400 + 60001
    assert result["native_header_ms"] == 123 and result["durable_created_ms"] == 456
    assert result["boot_mapping"]["original"] == before and row == before
    assert mapping().receipt()["original_case_PASS"] is False
    assert mapping().receipt()["economics"] == "NOT_QUALIFIED"


def test_nanosecond_remainder_never_float_projected():
    assert mapping().market_ns(FRI_0400 * 1000000 + 999999) == MON_0400 * 1000000 + 999999


def test_unknown_availability_stays_unknown():
    row = dict(stamp=FRI_0400, row_id="1", origin="strategy_bar_history", available_at_ms=None)
    assert map_file_history_rows([row], mapping())[0]["available_at_ms"] is None


def test_companion_preserves_independent_source_and_maps_same_minute():
    ref = dict(stamp=FRI_0400, row_id="m1", origin="C1_MASSIVE_POSTHOC_REFETCH")
    row = dict(stamp=FRI_0400, row_id="s1", origin="strategy_bar_history", massive_reference=ref)
    result = map_file_history_rows([row], mapping())[0]
    assert result["massive_reference"]["stamp"] == MON_0400
    assert result["massive_reference"]["boot_mapping"]["original"] == ref


@pytest.mark.parametrize("target", ["2026-10-23", "2026-10-24", "2026-10-25"])
def test_target_cannot_reuse_capture_day_or_weekend(target):
    with pytest.raises(ValueError):
        RecordedSessionMap(target_day=target, ruling_sha256="a" * 64,
            chronology_review_sha256="b" * 64)


@pytest.mark.parametrize("value", [True, 1.5, FRI_0400 - 86400000, FRI_0400 + 86400000])
def test_foreign_or_fractional_market_clock_refused(value):
    with pytest.raises(ValueError):
        mapping().market_ms(value)


@pytest.mark.parametrize("field", ["ruling_sha256", "chronology_review_sha256"])
def test_review_digest_not_minted_or_defaulted(field):
    values = dict(target_day="2026-10-26", ruling_sha256="a" * 64, chronology_review_sha256="b" * 64)
    values[field] = None
    with pytest.raises(ValueError):
        RecordedSessionMap(**values)


def test_duplicate_recorded_identity_not_silently_dropped():
    row = dict(stamp=FRI_0400, row_id="1", origin="strategy_bar_history")
    with pytest.raises(ValueError, match="duplicate"):
        map_file_history_rows([row, row], mapping())


def test_companion_wrong_minute_not_joined():
    row = dict(stamp=FRI_0400, row_id="1", origin="strategy_bar_history",
        massive_reference=dict(stamp=FRI_0400 + 60000, row_id="2", origin="C1_MASSIVE_POSTHOC_REFETCH"))
    with pytest.raises(ValueError, match="mismatch"):
        map_file_history_rows([row], mapping())


def test_missing_chronology_and_unexpected_observed_name_stay_in_denominator():
    result = timeline_denominator(recorded_names=["AAA", "BBB"],
        chronology_by_name={"AAA": [{"raw_ref": "retained-record-1"}]}, observed_names={"CCC"})
    assert result["names"] == ["AAA", "BBB", "CCC"]
    assert result["denominator"] == 3 and result["missing_chronology"] == ["BBB", "CCC"]
    assert result["status"] == "UNKNOWN" and result["seed_complete"] == "UNRUN"


@pytest.mark.parametrize("names", [[], ["AAA", "AAA"], [True], [""]])
def test_empty_duplicate_invalid_denominator_never_zero_of_zero_PASS(names):
    with pytest.raises(ValueError):
        timeline_denominator(recorded_names=names, chronology_by_name={}, observed_names=[])


def test_foreign_chronology_not_silently_accepted():
    with pytest.raises(ValueError):
        timeline_denominator(recorded_names=["AAA"], chronology_by_name={"BBB": [1]}, observed_names=[])
