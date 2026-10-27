"""CONTROLLED contract, not actual populated PG/routes/timing qualification."""

from copy import deepcopy

import pytest

from tools.l12_service_boot_replay import (
    RecordedManualStopTransport, compare_manual_stop_twenty,
    manual_stop_pair_p95_ns, manual_stop_twenty_plan,
)


def arm(name, values):
    return dict(arm=name, status="MANSTOP120_ROUTE_STAGES_OBSERVED_NOT_INSTALL_PERMISSION",
        L12_unchanged=True, cursor_pair_ns=values, referenced_parent_23503=True)


def test_literal120_stages_are_bot_and_global20_set_clear_set_each():
    plan = manual_stop_twenty_plan()
    assert len(plan) == 120
    assert [row["scope"] for row in plan].count("bot") == 60
    assert [row["scope"] for row in plan].count("global") == 60
    for scope in ("bot", "global"):
        rows = [row for row in plan if row["scope"] == scope]
        for cycle in range(1, 21):
            assert [row["action"] for row in rows if row["cycle"] == cycle] == ["stop", "resume", "stop"]


def test_plan_calls_are_not_shared_mutable_state():
    first = manual_stop_twenty_plan()
    first[0]["action"] = "UNKNOWN"
    assert manual_stop_twenty_plan()[0] == dict(scope="bot", cycle=1, action="stop")


def test_nearest_rank_p95_uses_all120_no_tail_drop():
    samples = list(range(120))
    original = samples[:]
    assert manual_stop_pair_p95_ns(samples) == 113
    assert samples == original


@pytest.mark.parametrize("samples", [[], [1] * 119, [1] * 121,
    [True] * 120, [1.0] * 120, [-1] * 120, tuple([1] * 120)])
def test_incomplete_or_non_integer_measurement_refuses(samples):
    with pytest.raises(ValueError, match="all120_actual_cursor_pairs"):
        manual_stop_pair_p95_ns(samples)


@pytest.mark.parametrize("delta,allowed", [(4999999, True), (5000000, True), (5000001, False)])
def test_exact_five_ms_bound_without_new_threshold(delta, allowed):
    old = arm("C1_BASELINE_SOURCE21", [1000000] * 120)
    new = arm("UPGRADED_SOURCE21_PLUS2", [1000000 + delta] * 120)
    original = deepcopy((old, new))
    result = compare_manual_stop_twenty(old, new)
    assert result["latency_within_C11329_bound"] is allowed
    assert result["permission_granted"] is False
    assert result["quiet_host"] == "NOT_ESTABLISHED_BY_FUNCTIONAL_CALLER"
    assert (old, new) == original


@pytest.mark.parametrize("fault", ["same_arm", "changed_rows", "partial", "missing_samples"])
def test_two_complete_unchanged_separate_arms_are_required(fault):
    old = arm("C1_BASELINE_SOURCE21", [1] * 120)
    new = arm("UPGRADED_SOURCE21_PLUS2", [1] * 120)
    if fault == "same_arm":
        new["arm"] = old["arm"]
    elif fault == "changed_rows":
        new["L12_unchanged"] = False
    elif fault == "partial":
        new["status"] = "UNKNOWN"
    else:
        new["cursor_pair_ns"] = [1] * 119
    with pytest.raises(ValueError):
        compare_manual_stop_twenty(old, new)


def test_missing_fk_refusal_does_not_become_true_from_valid_latency():
    old = arm("C1_BASELINE_SOURCE21", [1] * 120)
    new = arm("UPGRADED_SOURCE21_PLUS2", [1] * 120)
    new["referenced_parent_23503"] = None
    result = compare_manual_stop_twenty(old, new)
    assert result["referenced_parent_23503"] is False
    assert result["permission_granted"] is False


@pytest.mark.asyncio
async def test_injected_transport_retains120_events_and_refuses121_without_network():
    transport = RecordedManualStopTransport()
    for number in range(120):
        assert await transport.xadd("CONTROLLED-runtime-controls", {"data": str(number)}) == (
            "CONTROLLED_NO_NETWORK_" + str(number + 1))
    assert len(transport.events) == 120
    with pytest.raises(ValueError):
        await transport.xadd("CONTROLLED-runtime-controls", {"data": "121"})
    assert len(transport.events) == 120


@pytest.mark.asyncio
async def test_injected_transport_rejects_unexpected_fields_before_recording():
    transport = RecordedManualStopTransport()
    with pytest.raises(ValueError):
        await transport.xadd("CONTROLLED", {"data": "x", "unexpected": "x"})
    assert transport.events == []
