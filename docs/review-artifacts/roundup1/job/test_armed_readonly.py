"""Explicitly controlled published-state completeness tests; no Redis calls."""
from datetime import datetime, timedelta, timezone
import json

import pytest
import armed_readonly as armed
import release_policy as policy

NOW = datetime(2026, 10, 6, 20, 10, tzinfo=timezone.utc)


def event():
    return dict(event_id="00000000-0000-4000-8000-000000000001", event_type="isolated_bot_state",
                source_service="schwab-1m-v2", produced_at=NOW.isoformat(),
                payload=dict(strategy_code="schwab_1m_v2", account_name="live:schwab_1m_v2", cw_armed_segments=[]))


def reply():
    return [str(int(NOW.timestamp() * 1000)) + "-0", json.dumps(event())]


def test_explicit_zero_state_has_exact_identity_raw_hash_and_field_proof():
    result = armed.proof(reply(), NOW)
    assert result["armed_count"] == 0 and result["completeness"] == "explicit-field-present"
    assert result["raw_sha256"] == policy.digest(reply()[1].encode())


@pytest.mark.parametrize("change", [lambda e: e["payload"].pop("cw_armed_segments"),
    lambda e: e["payload"].update(cw_armed_segments=None), lambda e: e["payload"].update(cw_armed_segments={}),
    lambda e: e["payload"].update(cw_armed_segments=False),
    lambda e: e["payload"].update(cw_armed_segments=[{"symbol": "IPDN"}]),
    lambda e: e["payload"].update(account_name="paper:schwab_1m_v2"),
    lambda e: e["payload"].update(strategy_code="orb"), lambda e: e.update(source_service="strategy-engine"),
    lambda e: e.update(event_type="strategy_state"), lambda e: e.update(payload=[]),
    lambda e: e.update(produced_at=(NOW - timedelta(seconds=61)).isoformat()),
    lambda e: e.update(produced_at=(NOW + timedelta(seconds=1)).isoformat()),
    lambda e: e.update(produced_at="2026-10-06T20:10:00"), lambda e: e.pop("produced_at"),
    lambda e: e.update(event_id="unknown")])
def test_missing_malformed_nonzero_wrong_identity_stale_never_zero(change):
    value = event()
    change(value)
    with pytest.raises((policy.Stop, KeyError, ValueError)):
        armed.proof([reply()[0], json.dumps(value)], NOW)


@pytest.mark.parametrize("value", [None, [], ["id"], ["id", "{}"], [reply()[0], ""],
    [reply()[0], "{"], [reply()[0], "x" * (armed.MAX_BYTES + 1)],
    ["1-0", reply()[1]], [str(int((NOW.timestamp()+1)*1000))+"-0", reply()[1]],
    [reply()[0], '{"payload":{},"payload":{}}']])
def test_empty_failed_duplicate_oversized_malformed_query_not_zero(value):
    with pytest.raises((policy.Stop, ValueError)):
        armed.proof(value, NOW)


def test_only_readonly_eval_one_entry_bounded_before_response():
    calls = []
    class Client:
        def execute_command(self, *args):
            calls.append(args)
            return reply()
    assert armed.collect(Client(), "mai_tai:strategy-state-isolated", NOW)["armed_count"] == 0
    assert calls == [("EVAL_RO", armed.READ, 1, "mai_tai:strategy-state-isolated")]
    assert "'COUNT', 1" in armed.READ and "262144" in armed.READ and "#fields ~= 2" in armed.READ
    assert "snapshot-batches" not in armed.READ


def test_unsupported_or_failed_readonly_query_propagates_not_clear():
    class Client:
        def execute_command(self, *args):
            raise OSError("controlled query failure")
    with pytest.raises(OSError):
        armed.collect(Client(), "mai_tai:strategy-state-isolated", NOW)


def foreign_reply(index=0):
    # Actual 17:41 read: isolated_bot_state/source orb/strategy orb/paper:orb.
    value = event()
    value.update(source_service="orb", produced_at=(NOW - timedelta(milliseconds=index)).isoformat())
    value["payload"].update(strategy_code="orb", account_name="paper:orb")
    return [str(int(NOW.timestamp() * 1000) - index) + "-0", json.dumps(value)]


def test_recorded_orb_newest_shared_state_selects_older_v2_not_foreign_empty_armed():
    calls = []
    rows = [foreign_reply(), [str(int(NOW.timestamp() * 1000) - 1) + "-0", reply()[1]]]
    class Client:
        def execute_command(self, *args):
            calls.append(args)
            return rows[len(calls) - 1]
    result = armed.collect(Client(), "mai_tai:strategy-state-isolated", NOW)
    assert result["event"]["source_service"] == "schwab-1m-v2"
    assert result["entries_examined"] == 2
    assert result["discarded_other_producers"] == [dict(entry_id=rows[0][0], source_service="orb")]
    assert calls[1][-1] == "(" + rows[0][0]


@pytest.mark.parametrize("change", [lambda e: e["payload"].pop("cw_armed_segments"),
    lambda e: e["payload"].update(cw_armed_segments=[{"symbol": "IPDN"}]),
    lambda e: e["payload"].update(account_name="paper:schwab_1m_v2"),
    lambda e: e.update(produced_at=(NOW - timedelta(seconds=61)).isoformat())])
def test_foreign_zero_never_masks_invalid_or_armed_v2(change):
    own = event()
    change(own)
    rows = iter([foreign_reply(), [str(int(NOW.timestamp() * 1000) - 1) + "-0", json.dumps(own)]])
    class Client:
        def execute_command(self, *args):
            return next(rows)
    with pytest.raises((policy.Stop, KeyError, ValueError)):
        armed.collect(Client(), "mai_tai:strategy-state-isolated", NOW)


def test_shared_state_scan_is_bounded_and_never_defaults_missing_v2_to_zero():
    calls = []
    class Client:
        def execute_command(self, *args):
            calls.append(args)
            return foreign_reply(len(calls) - 1)
    with pytest.raises(policy.Stop, match="bounded shared-stream scan"):
        armed.collect(Client(), "mai_tai:strategy-state-isolated", NOW)
    assert len(calls) == 25
    assert all(args[:4] == ("EVAL_RO", armed.READ, 1, "mai_tai:strategy-state-isolated") for args in calls)


def test_shared_state_nonadvancing_cursor_blocks():
    class Client:
        def execute_command(self, *args):
            return foreign_reply()
    with pytest.raises(policy.Stop, match="cursor did not advance"):
        armed.collect(Client(), "mai_tai:strategy-state-isolated", NOW)
