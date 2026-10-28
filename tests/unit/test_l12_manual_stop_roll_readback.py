"""CONTROLLED post-readback assessment, not actual scanner/PG roll acceptance."""

from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import json

import pytest

from tools.l12_service_boot_replay import manual_stop_roll_readback, replay_service_boot


ANCHOR = 1793001600000


def row(identity="old", *, kind="bot_manual_stop_symbols", current=False, offset=-60000, payload=None):
    stamp = ANCHOR + offset
    value = ({"bots": {"schwab_1m_v2": ["YMAT"]}} if kind == "bot_manual_stop_symbols"
        else {"symbols": ["YMAT"]}) if payload is None else payload
    return dict(id=identity, snapshot_type=kind,
        created_at=datetime.fromtimestamp(stamp // 1000, UTC).isoformat(), created_ms=stamp,
        created_ns=stamp * 1000000, payload=value,
        payload_sha256=sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        matches_current_session=current)


def assess(before, after, *, bots=None, globals_=None):
    return manual_stop_roll_readback(before=before, after=after,
        bot_symbols={} if bots is None else bots, global_symbols=set() if globals_ is None else globals_,
        expected_session_start_ms=ANCHOR)


def test_empty_actual_readbacks_are_observation_not_full_boot_or_permission():
    result = assess([], [])
    assert result["status"] == "OBSERVED_MANSTOP_04_ROLL_EMPTY_NOT_FULL_BOOT"
    assert result["permission_granted"] is False
    assert result["Mirror4_retired"] == "UNKNOWN_UNLESS_SEPARATELY_COMMITTED_FIRST"


def test_exact_two_stale_latest_deletions_are_required():
    before = [row(), row("global", kind="global_manual_stop_symbols")]
    result = assess(before, [])
    assert result["removed_ids"] == ["global", "old"]
    assert result["expected_stale_latest_ids"] == ["global", "old"]
    assert result["status"] == "OBSERVED_MANSTOP_04_ROLL_EMPTY_NOT_FULL_BOOT"


def test_swallowed_purge_exception_cannot_be_accepted_from_empty_loaders():
    before = [row()]
    result = assess(before, deepcopy(before))
    assert result["status"] == "MANSTOP_04_ROLL_UNPROVEN_OR_NONEMPTY_STOP"
    assert result["removed_ids"] == []
    assert result["expected_stale_latest_ids"] == ["old"]


def test_older_ineligible_history_preserved_with_actual_age_not_forced_deletion():
    older, latest = row("older", offset=-120000), row("latest")
    result = assess([older, latest], [older])
    assert result["status"] == "OBSERVED_MANSTOP_04_ROLL_EMPTY_NOT_FULL_BOOT"
    assert result["retained_previous_session_rows"][0]["age_from_target_session_ms"] == 120000
    assert result["removed_ids"] == ["latest"]


def test_real_current_stop_is_not_cleared_or_reported_empty():
    current = row(current=True, offset=0)
    result = assess([current], [current], bots={"schwab_1m_v2": {"YMAT"}})
    assert result["status"] == "MANSTOP_04_ROLL_UNPROVEN_OR_NONEMPTY_STOP"
    assert result["removed_ids"] == []
    assert result["loader_results_match_actual_latest_payloads"] is True


def test_empty_current_record_can_survive_without_false_nonempty_claim():
    current = row(current=True, offset=0, payload={"bots": {}})
    result = assess([current], [current])
    assert result["status"] == "OBSERVED_MANSTOP_04_ROLL_EMPTY_NOT_FULL_BOOT"
    assert result["surviving_current_session_rows"] == [current]


def test_loader_failure_empty_result_disagrees_with_actual_current_payload():
    current = row(current=True, offset=0)
    result = assess([current], [current])
    assert result["loader_results_match_actual_latest_payloads"] is False
    assert result["status"] == "MANSTOP_04_ROLL_UNPROVEN_OR_NONEMPTY_STOP"


@pytest.mark.parametrize("change", ["addition", "mutation", "current_deletion"])
def test_unexpected_row_change_never_gets_roll_acceptance(change):
    current = row(current=True, offset=0, payload={"bots": {}})
    before, after = [current], [deepcopy(current)]
    if change == "addition":
        after.append(row("unexpected", offset=-120000))
    elif change == "mutation":
        after[0]["payload"]["unreviewed"] = True
    else:
        after = []
    assert assess(before, after)["status"] == "MANSTOP_04_ROLL_UNPROVEN_OR_NONEMPTY_STOP"


def test_latest_timestamp_tie_does_not_guess_which_row_service_selected():
    with pytest.raises(ValueError, match="latest_timestamp_tie"):
        assess([row("a"), row("b")], [])


def test_microsecond_order_is_not_collapsed_to_millisecond_tie():
    older, newer = row("older"), row("newer")
    newer["created_ns"] += 1000
    newer["created_at"] = datetime.fromisoformat(newer["created_at"]).replace(microsecond=1).isoformat()
    result = assess([older, newer], [older])
    assert result["expected_stale_latest_ids"] == ["newer"]
    assert result["status"] == "OBSERVED_MANSTOP_04_ROLL_EMPTY_NOT_FULL_BOOT"


@pytest.mark.parametrize("kind,payload", [
    ("bot_manual_stop_symbols", {"bots": "unreadable"}),
    ("global_manual_stop_symbols", {"symbols": "unreadable"}),
])
def test_unreadable_current_payload_is_not_empty(kind, payload):
    current = row(current=True, offset=0, kind=kind, payload=payload)
    with pytest.raises(ValueError, match="payload_unreadable_not_empty"):
        assess([current], [current])


def test_duplicate_pk_readback_stops_instead_of_counting_twice():
    value = row()
    with pytest.raises(ValueError, match="complete_bounded_actual_MANSTOP_readback"):
        assess([value, deepcopy(value)], [])


def test_actual_loader_result_shape_is_required():
    with pytest.raises(ValueError, match="actual_manual_stop_loader_result_types"):
        manual_stop_roll_readback(before=[], after=[], bot_symbols={}, global_symbols=[],
            expected_session_start_ms=ANCHOR)


@pytest.mark.asyncio
@pytest.mark.parametrize("spec,target,profile", [
    ({}, object(), "COMBINED_L11_L12"),
    ({"scanner": None, "writer_guard": None, "expected_session_start_ms": ANCHOR}, None, "COMBINED_L11_L12"),
    ({"scanner": None, "writer_guard": None, "expected_session_start_ms": ANCHOR}, object(), "L12_ONLY"),
])
async def test_connected_roll_refuses_missing_nonce_profile_or_exact_operands_before_case(spec, target, profile):
    with pytest.raises(ValueError, match="explicit_combined_nonce_scanner_roll_operands"):
        await replay_service_boot(None, None, packet=None, blobs=None, stage_operands=None,
            expected_channel=None, restore_lines=None, startup_profile=profile,
            target_capability=target, v2_review={}, scanner_roll=spec)
