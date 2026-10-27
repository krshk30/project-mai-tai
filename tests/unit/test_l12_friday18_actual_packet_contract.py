"""FIRST actual file decoding only; no PG, provider, replay or boot qualification.

Main must explicitly allow the THREE pinned retained paths in its read fence.
Mapping/ruling hashes below are CONTROLLED control operands, not Main approval.
"""

from pathlib import Path

import pytest

from tools.l12_recorded_boot_timeline import (
    ADAPTER_PINS, FILE_PORT_SHA, FLEET_FILES, FLEET_NAMES,
    RecordedSessionMap, decode_friday_fleet)


PACKET = Path("/Users/velkris/.codex/task-queues/analysis/L12-BOOT-FRIDAY-1023-WATCHED18-20261024")


@pytest.fixture(scope="module")
def operands():
    raw = {name: (PACKET / name).read_bytes() for name in FLEET_FILES}
    mapping = RecordedSessionMap(target_day="2026-10-26", ruling_sha256="a" * 64,
        chronology_review_sha256="b" * 64)
    return raw, mapping


@pytest.fixture(scope="module")
def decoded(operands):
    return decode_friday_fleet(*operands)


def test_actual_delivered_population_and_all_raw_rows_preserved(decoded):
    assert decoded["names"] == list(FLEET_NAMES) and len(decoded["names"]) == 18
    assert sum(map(len, decoded["chronology"].values())) == 62
    assert len(decoded["retained_history"]) == 2751
    massive_ids = set()
    for rows in decoded["file_rows"].values():
        for row in rows:
            if row["origin"] == "C1_MASSIVE_POSTHOC_REFETCH":
                massive_ids.add(row["row_id"])
            if row.get("massive_reference") is not None:
                massive_ids.add(row["massive_reference"]["row_id"])
    assert len(massive_ids) == 11186
    assert decoded["permission_granted"] is False


def test_actual_FADE_only_names_not_mislabelled_as_confirmed(decoded):
    assert set(decoded["missing_confirm_names"]) == {"AARD", "NCPL", "STAK", "POM", "SAIQ", "VEEA", "DKI"}
    assert decoded["chronology"]["AARD"][0]["event_type"] == "FADE"
    assert decoded["first_confirm_by_name"]["AARD"] is None
    assert decoded["first_confirm_by_name"]["FGL"]["original"]["event_at"] == "2026-10-23 12:42:14+00"


def test_actual_AIOK_BOLT_confirm_clock_and_UNKNOWN_provider_facts(decoded):
    assert decoded["first_confirm_by_name"]["AIOK"]["original"]["event_at"] == "2026-10-23 14:08:14+00"
    assert decoded["first_confirm_by_name"]["BOLT"]["original"]["event_at"] == "2026-10-23 14:16:51+00"
    assert decoded["native_availability"] == "UNKNOWN"
    assert decoded["provider_availability"] == "UNKNOWN_NOT_DB_CREATED_AT"
    assert decoded["seed_limit_bars"] is None


def test_existing_helper_builds_explicit_fleet_plan_without_old19_case_population(operands):
    from tools.l12_service_boot_replay import prepare_recorded_fleet_timeline

    raw, mapping = operands
    value = prepare_recorded_fleet_timeline(blobs=raw, mapping=mapping,
        adapter_pins=dict(ADAPTER_PINS), file_port_sha256=FILE_PORT_SHA,
        ruling_1720_sha256="c" * 64, poll_interval_ms=5000, phase_ms=0)
    assert value["seed_denominator"] == 18 and value["pending_bar_limit"] == "UNKNOWN_NO_DEFAULT"
    prepared = value["prepared"]
    assert prepared["original19_case_plan"] == "NOT_USED_AS_FRIDAY_POPULATION"
    assert prepared["source_inputs_actual"] == FLEET_FILES
    deltas = [row for row in value["events"] if row["kind"] == "scanner_delta"]
    assert len(deltas) == 62 and deltas[0]["watchlist"] == ["AARD"]
    # Delta, not fabricated full watchlist; actual runtime starts from the copy.
    assert prepared["scanner_records_by_ref"][deltas[0]["raw_ref"]]["event_type"] == "FADE"
