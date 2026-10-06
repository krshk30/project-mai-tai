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
