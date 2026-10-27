"""Private controlled responses, NOT actual native coverage/latency proof."""
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from project_mai_tai.brokerdecides1 import AdmissionSnapshot, assess, decode, encode
from project_mai_tai.brokerdecides1_collect import accepted_schwab_window
from test_brokerdecides1_native import order, schwab, webull

POLICY = "schwab_reviewed_window_20261022_145124"
NOW = datetime(2026, 10, 22, tzinfo=UTC)
REFUSAL = {"message": "fromEnteredTime range exceeds maximum 60 days"}


@pytest.mark.asyncio
async def test_initial_181_acceptance_is_one_read():
    calls = []

    async def fetch(lower, upper, cap):
        calls.append((lower, upper, cap))
        return 200, []

    assert await accepted_schwab_window(fetch, NOW) == (181, [])
    assert calls == [(NOW - timedelta(days=181), NOW, 500)]


@pytest.mark.asyncio
async def test_explicit_range_refusal_finds_widest_whole_day_not_guessed_default():
    calls = []

    async def fetch(lower, upper, cap):
        days = (upper - lower).days
        calls.append(days)
        return (200, [{"orderId": "retained"}]) if days <= 60 else (400, REFUSAL)

    assert await accepted_schwab_window(fetch, NOW) == (60, [{"orderId": "retained"}])
    assert calls[0] == 181 and 60 in calls and 61 in calls
    assert len(calls) <= 9 and len(calls) == len(set(calls))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (503, REFUSAL), (429, REFUSAL), (401, REFUSAL),
    (400, {"message": "invalid request"}),
    (400, {"message": "fromEnteredTime maximum range token unauthorized"}),
    (200, {"error": "not an orders list"}),
])
async def test_nonrange_failure_never_probes_smaller_windows(status, body):
    calls = []

    async def fetch(*args):
        calls.append(args)
        return status, body

    with pytest.raises(ValueError, match="not_range_UNKNOWN"):
        await accepted_schwab_window(fetch, NOW)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_transport_exception_does_not_start_fallback():
    calls = []

    async def fetch(*args):
        calls.append(args)
        raise TimeoutError("controlled no native response")

    with pytest.raises(TimeoutError):
        await accepted_schwab_window(fetch, NOW)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failure_during_range_search_does_not_reuse_smaller_success():
    calls = []

    async def fetch(lower, upper, cap):
        days = (upper - lower).days
        calls.append(days)
        if len(calls) == 4:
            return 503, {}
        return (200, []) if days <= 60 else (400, REFUSAL)

    with pytest.raises(ValueError, match="fallback_nonrange_failure_UNKNOWN"):
        await accepted_schwab_window(fetch, NOW)
    assert calls == [181, 90, 45, 67]


def snapshot(book, **changes):
    value = AdmissionSnapshot("private-epoch", 1, 1000, 1200, (book, webull()),
        frozenset(), frozenset(), True, "private controlled native shapes")
    return replace(value, **changes)


def decision(value):
    return assess(value, accounts=("live:schwab", "live:webull"), symbol="WORX",
                  segment=900, now_ms=1300, max_age_ms=20_000)


def test_accepted_residual_is_explicit_roundtrip_scope_not_global_hold():
    value = snapshot(schwab(scope=POLICY, window_days=60))
    decoded = decode(encode(value))
    assert decoded == value and decoded.books[0].window_days == 60
    assert decoded.books[0].scope_policy == POLICY and decision(decoded).allowed


@pytest.mark.parametrize("fault,reason", [
    ("working", "native_working_order"), ("position", "native_position"),
    ("inflight", "own_inflight_UNKNOWN_or_working"), ("fill", "buy_fill_in_segment"),
    ("incomplete", "native_coverage_UNKNOWN"), ("local", "snapshot_UNKNOWN"),
])
def test_reviewed_window_never_waives_other_evidence(fault, reason):
    book = schwab(scope=POLICY, window_days=60, orders=[order()] if fault == "working" else [])
    if fault == "position":
        book = replace(book, positions=frozenset({"WORX"}))
    if fault == "incomplete":
        book = replace(book, complete=False)
    value = snapshot(book, inflight=frozenset({"WORX"}) if fault == "inflight" else frozenset(),
        filled_segments=frozenset({("WORX", 900)}) if fault == "fill" else frozenset(),
        local_complete=fault != "local")
    assert not decision(value).allowed and decision(value).reason == reason


@pytest.mark.parametrize("days", [None, 0, 182, True, "60"])
def test_reviewed_scope_requires_exact_explicit_window(days):
    with pytest.raises(ValueError, match="reviewed_window_UNKNOWN"):
        schwab(scope=POLICY, window_days=days)


@pytest.mark.parametrize("field", ["scope_policy", "window_days"])
def test_wire_book_cannot_omit_scope_fields(field):
    value = json.loads(encode(snapshot(schwab(scope=POLICY, window_days=60))))
    value["books"][0].pop(field)
    with pytest.raises(ValueError, match="scope_policy_missing_UNKNOWN"):
        decode(json.dumps(value))


def test_reviewed_window_missing_positions_still_unknown():
    with pytest.raises(ValueError, match="positions_completeness_UNKNOWN"):
        schwab(scope=POLICY, window_days=60,
            account_body={"securitiesAccount": {"accountNumber": "123"}})
