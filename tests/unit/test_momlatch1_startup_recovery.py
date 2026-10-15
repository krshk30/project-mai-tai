"""Controlled retained receipts, not live deployment or performance proof."""

import json

import pytest

from tests.unit.test_momwarn1_d2_startup import advance, first_due
from tests.unit import test_momwarn1_live_health as native_controls
from tests.unit.test_momwarn1_live_health import NOW, NS, line

setup = native_controls.setup


async def timed_out(env):
    env.reader.bind_gateway_start("controlled-recovery-gateway", NOW)
    initial = await env.reader.read_live(env.client, "test")
    deadline = first_due(NOW) + 5 * NS
    assert initial.v2_startup_pending
    assert initial.v2_startup_deadline_ns == deadline
    advance(env, deadline + NS)
    timeout = await env.reader.read_live(env.client, "test")
    assert timeout.v2_startup_pending
    assert timeout.v2_startup_deadline_ns == deadline
    assert timeout.v2_bar_lateness_seconds is None
    assert env.reader._gateway_startup["ready"] is None
    return deadline


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_seconds,read_seconds", [(2, 2), (5, 5), (2, 12)])
async def test_later_current_ontime_all_watched_recovers_after_original_timeout(
    setup, receipt_seconds, read_seconds,
):
    env = setup(pending=True)
    deadline = await timed_out(env)
    due = first_due(NOW) + 60 * NS
    advance(env, due + receipt_seconds * NS, symbols=("AAA", "BBB"))
    if read_seconds != receipt_seconds:
        advance(env, due + read_seconds * NS)
    health = await env.reader.read_live(env.client, "test")
    assert env.clock.ns > deadline
    assert not health.v2_startup_pending and not health.v2_not_applicable
    assert health.v2_bar_lateness_seconds == receipt_seconds
    assert env.reader._gateway_startup["ready"] == {
        "v2_instance_id": "13:101", "due_ns": due,
        "completed_ns": due + receipt_seconds * NS,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "partial", "late", "unclosed", "warmup", "failure", "old_bar"])
async def test_later_invalid_completion_keeps_original_timeout_and_unknown_lateness(setup, case):
    env = setup(pending=True)
    deadline = await timed_out(env)
    due = first_due(NOW) + 60 * NS
    symbols = () if case in {"missing", "old_bar"} else (
        ("AAA",) if case == "partial" else ("AAA", "BBB")
    )
    receipt_offset = {"late": 6 * NS, "unclosed": -NS}.get(case, 2 * NS)
    advance(env, due + 7 * NS, symbols=symbols, late_ns=receipt_offset,
            outcome=case if case in {"warmup", "failure"} else "published")
    if case == "old_bar":
        with (env.directory / "schwab-1m-v2.log").open("a") as stream:
            for symbol in ("AAA", "BBB"):
                stream.write(line(due + NS, f"[V2-LINE-RESTORE] sym={symbol} "
                    f"current_bar_ms={due // 1_000_000 - 120_000} outcome=published"))
    health = await env.reader.read_live(env.client, "test")
    assert health.v2_startup_pending
    assert health.v2_startup_deadline_ns == deadline
    assert health.v2_bar_lateness_seconds is None
    assert env.reader._gateway_startup["ready"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["stale_state", "wrong_producer", "changed_identity", "missing_identity"])
async def test_later_completion_cannot_bypass_current_state_or_identity(setup, case):
    env = setup(pending=True)
    await timed_out(env)
    old_state = env.state()
    due = first_due(NOW) + 60 * NS
    advance(env, due + 2 * NS, symbols=("AAA", "BBB"))
    if case in {"stale_state", "wrong_producer"}:
        state = old_state if case == "stale_state" else env.state()
        if case == "wrong_producer":
            state["source_service"] = "foreign-source"
        env.client.xrevrange.side_effect = lambda *a, **kw: [("1-0", {"data": json.dumps(state)})]
    else:
        path = env.proc / "13/stat"
        if case == "missing_identity":
            path.unlink()
        else:
            path.write_text(path.read_text().rsplit(" ", 1)[0] + " 999")
    with pytest.raises((ValueError, OSError)):
        await env.reader.read_live(env.client, "test")
    assert env.reader._gateway_startup["ready"] is None


@pytest.mark.asyncio
async def test_ontime_receipts_at_gateway_baseline_do_not_grant_readiness(setup):
    due = first_due(NOW) + 60 * NS
    env = setup(pending=True, now=due + 2 * NS)
    advance(env, due + 2 * NS, symbols=("AAA", "BBB"))
    env.reader.bind_gateway_start("controlled-new-baseline", due + 2 * NS)
    health = await env.reader.read_live(env.client, "test")
    assert health.v2_startup_pending and health.v2_bar_lateness_seconds is None
    assert env.reader._gateway_startup["ready"] is None
