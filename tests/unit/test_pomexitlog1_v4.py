"""POMEXITLOG1: existing MOM controlled ports, no provider/new runtime rig.

SOURCE ONLY until Main executes. Diagnostic-noop comparison does not stub any
trading permission/guard/DB/broker result; original controlled fixtures are reused.
The separate source-byte proof binds the APP delta to the reviewed literal v4.
"""

import asyncio
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal as D
from itertools import count
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot
from project_mai_tai.momentum_live.runtime import OmsLiveRuntime
from tests.unit.test_momlive1_actor import setup as actor_setup
from tests.unit.test_momlive1_runtime import setup
from tests.unit.test_momlive1_soft_exit_chase import advance_exit, exit_orders, prepared

GATE = "[MOMLIVE1-EXIT-GATE]"
ATTEMPT = "[MOMLIVE1-EXIT-ATTEMPT-TIMING]"
DIAGNOSTICS = (
    "_exit_gate_fired", "_exit_gate_state", "_exit_gate_cancel_latched", "_exit_gate_open",
    "_exit_gate_change", "_exit_gate_start", "_exit_gate_end", "_exit_gate_attempt",
    "_exit_gate_finish", "_exit_gate_retire",
)


def timing_lines(logger, prefix):
    return [call for call in logger.info.call_args_list if call.args[0].startswith(prefix)]


def helper_case():
    now, broker, service, runtime = setup()
    # Scalar-only helper cases do not start any actor/task or access a provider.
    service.logger = Mock()
    position = SimpleNamespace(episode_id="00000000-0000-0000-0000-000000000001",
                              exit_clock_source="send", exit_clock_ns=1_000_000_000)
    return now, broker, service, runtime, position


def bind_trace(runtime, position, fire=None):
    trace = runtime._exit_gate_open(position, fire)
    assert trace is not None
    trace["identity"] = position.episode_id
    return trace


@pytest.mark.asyncio
async def test_cap20_counts_entry_and_change_and_summary_is_additional():
    _, broker, service, runtime, position = helper_case()
    handle = SimpleNamespace(episode_id=position.episode_id, base_coid="controlled-root")
    runtime._exit_gate_cancel_latched(handle)
    trace = bind_trace(runtime, position)
    state = trace["state"]
    for index in range(37):
        runtime._exit_gate_change(position.episode_id, state, "positions", "CHANGE",
                                  "returned" if index % 2 else "exception",
                                  "exception" if index % 2 else "returned")
    assert len(timing_lines(service.logger, GATE)) == state["lines"] == 20
    assert state["suppressed"] == 17
    trace["attempt_journal_saved"] = True
    values = {"soft_exit_attempt": 1}
    runtime._exit_gate_attempt(trace, position, "time_300s", values, "pre_submit")
    runtime._exit_gate_attempt(trace, position, "time_300s", values, "return_or_error_finally")
    assert len(timing_lines(service.logger, ATTEMPT)) == 1
    assert "suppressed=17" in timing_lines(service.logger, ATTEMPT)[0].args[0] % timing_lines(service.logger, ATTEMPT)[0].args[1:]
    assert len(service.logger.info.call_args_list) == 21
    runtime._exit_gate_finish(trace, position, "time_300s", values)
    assert not runtime._exit_gate_frames and broker.sent == []


@pytest.mark.asyncio
async def test_unchanged_gate_outcome_is_quiet_and_entry_precedes_measurement():
    _, _, service, runtime, position = helper_case()
    runtime._exit_gate_cancel_latched(SimpleNamespace(episode_id=position.episode_id, base_coid="root"))
    trace = bind_trace(runtime, position)
    started = runtime._exit_gate_start(trace, "positions")
    assert len(timing_lines(service.logger, GATE)) == 1  # ENTRY before any outcome.
    runtime._exit_gate_end(trace, "positions", started, "returned")
    assert len(timing_lines(service.logger, GATE)) == 2
    for _ in range(50):
        started = runtime._exit_gate_start(trace, "positions")
        runtime._exit_gate_end(trace, "positions", started, "returned")
    assert len(timing_lines(service.logger, GATE)) == 2
    assert trace["steps"]["positions"][4] == 51
    started = runtime._exit_gate_start(trace, "foreign_vp")
    assert "event=ENTRY" in timing_lines(service.logger, GATE)[-1].args[0] % timing_lines(service.logger, GATE)[-1].args[1:]
    assert trace["active"] == ("foreign_vp", started)  # Never invent a completed hung span.
    runtime._exit_gate_finish(trace, position, "time_300s", {})


@pytest.mark.asyncio
async def test_restored_latch_elapsed_unknown_and_restart_resets_process_cap():
    _, _, service, runtime, position = helper_case()
    runtime.native.handles[position.episode_id] = SimpleNamespace(base_coid="restored-root")
    runtime.runtime[position.episode_id] = {"native_cancel_wires:restored-root": ["controlled-child"]}
    trace = bind_trace(runtime, position)
    assert trace["state"]["post_cancel"] and trace["state"]["latch_ns"] is None
    runtime._exit_gate_start(trace, "positions")
    message = timing_lines(service.logger, GATE)[0]
    assert "since_local_latch_save_ms=None" in message.args[0] % message.args[1:]
    assert "latch_clock=UNKNOWN" in message.args[0] % message.args[1:]
    trace["state"]["lines"] = 20
    restarted = OmsLiveRuntime(runtime.service, clock=runtime.clock, identity="987:654")
    assert restarted._exit_gate_states == {} and restarted._exit_gate_frames == {}
    runtime._exit_gate_finish(trace, position, "time_300s", {})


@pytest.mark.asyncio
async def test_logger_failure_does_not_replace_primary_or_retry_past_cap():
    _, _, service, runtime, position = helper_case()
    runtime._exit_gate_cancel_latched(SimpleNamespace(episode_id=position.episode_id, base_coid="root"))
    trace = bind_trace(runtime, position)
    service.logger.info.side_effect = OSError("controlled log sink failed")
    for _ in range(25):
        runtime._exit_gate_change(position.episode_id, trace["state"], "positions", "CHANGE", "returned", "exception")
    assert trace["state"]["lines"] == trace["state"]["sink_errors"] == 20
    assert trace["state"]["suppressed"] == 5
    trace["attempt_journal_saved"] = True
    runtime._exit_gate_finish(trace, position, "time_300s", {"soft_exit_attempt": 1})
    assert not runtime._exit_gate_frames and trace["state"]["sink_errors"] == 21


@pytest.mark.asyncio
async def test_state_and_frame_bounds_and_retirement_do_not_gate_business():
    _, _, _, runtime, position = helper_case()
    first = bind_trace(runtime, position)
    other = SimpleNamespace(episode_id="00000000-0000-0000-0000-000000000002",
                            exit_clock_source="send", exit_clock_ns=1)
    second = bind_trace(runtime, other)
    overflow = SimpleNamespace(episode_id="00000000-0000-0000-0000-000000000003",
                               exit_clock_source="send", exit_clock_ns=1)
    assert runtime._exit_gate_open(overflow, None) is None
    assert len(runtime._exit_gate_states) == len(runtime._exit_gate_frames) == 2
    runtime._exit_gate_retire(position.episode_id)
    assert position.episode_id in runtime._exit_gate_states
    runtime._exit_gate_finish(first, position, "time_300s", {})
    runtime._exit_gate_finish(second, other, "time_300s", {})
    assert position.episode_id not in runtime._exit_gate_states
    third = bind_trace(runtime, overflow)
    runtime._exit_gate_finish(third, overflow, "time_300s", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("with_actor_fire", [True, False])
async def test_due_fired_uses_supplied_actor_value_or_unknown_without_extra_business_clock(with_actor_fire):
    _, _, service, runtime, position = helper_case()
    runtime.now_ns = Mock(side_effect=AssertionError("diagnostics must not read business clock"))
    fire = None
    if with_actor_fire:
        runtime._exit_gate_fired(position, "time_300s", 301_000_000_000)
        fire = runtime._exit_gate_fire
        runtime._exit_gate_fire = None
    trace = bind_trace(runtime, position, fire)
    trace["attempt_journal_saved"] = True
    runtime._exit_gate_attempt(trace, position, "time_300s", {"soft_exit_attempt": 1}, "pre_submit")
    call, = timing_lines(service.logger, ATTEMPT)
    text = call.args[0] % call.args[1:]
    if with_actor_fire:
        assert "due_business_ns=301000000000 fired_business_ns=301000000000 late_ns=0 clock_proven=True" in text
    else:
        assert "due_business_ns=None fired_business_ns=None late_ns=None clock_proven=False" in text
    runtime.now_ns.assert_not_called()
    runtime._exit_gate_finish(trace, position, "time_300s", {"soft_exit_attempt": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_kind", ["none", "record", "raises"])
async def test_actor_observer_reuses_original_clock_and_cannot_change_exit(observer_kind):
    actor, ports, episode, position = actor_setup(filled=3, terminal=True, now=301_000_000_000)
    await actor.restore_before_serving()
    ports.exit_owned.reset_mock()
    actor._observations.clear()
    calls, observed = [], []
    original_clock = ports.clock_ns

    def counted_clock():
        value = original_clock()
        calls.append(value)
        return value

    def observer(owner, reason, value):
        observed.append((owner.episode_id, reason, value))
        if observer_kind == "raises":
            raise LookupError("controlled diagnostic failure")

    ports.clock_ns = counted_clock
    ports.quote = lambda _: {"bid": D(3), "observed_ns": 301_000_000_000}
    ports.exit_decision_observer = None if observer_kind == "none" else observer
    await actor._service_owned(episode.episode_id)
    ports.exit_owned.assert_awaited_once_with(position, "time_300s")
    # Every clock evaluation remains the original actor's; observer only receives it.
    assert calls and all(value == 301_000_000_000 for value in calls)
    assert observed == ([] if observer_kind == "none" else [(position.episode_id, "time_300s", calls[-1])])
    assert episode.remaining == 3 and position.quantity == 3


async def controlled_exit(monkeypatch, *, diagnostics, hour, case):
    with monkeypatch.context() as patch:
        # Same entry identity inputs in each OFF/ON run; each call within a run
        # remains distinct. Patch only this fixture's module-local UUID source.
        entry_ids = count(1)
        patch.setattr("project_mai_tai.momentum_live.execution.uuid4",
                      lambda: UUID(int=next(entry_ids), version=4))
        if not diagnostics:
            # ONLY these new diagnostic callbacks are disabled. No permission,
            # guard, clock, request, persistence or quantity function is replaced.
            for name in DIAGNOSTICS:
                patch.setattr(OmsLiveRuntime, name, lambda *a, **k: None)
        now, broker, service, runtime, identity = await prepared("webull", hour, send_exit=False)
        original_logger = service.logger
        logger = Mock(wraps=original_logger)
        service.logger = logger
        if case == "diag_full":
            runtime._exit_gate_states = {"occupied-1": {}, "occupied-2": {}}
        if case == "logger_error":
            def fail_new_lines(message, *args, **kwargs):
                if message.startswith((GATE, ATTEMPT)):
                    raise OSError("controlled diagnostic INFO sink error")
                return original_logger.info(message, *args, **kwargs)
            logger.info.side_effect = fail_new_lines
        clocks, guards, result = [], [], "returned"
        original_clock = runtime.now_ns

        def counted_clock():
            value = original_clock()
            clocks.append(value)
            return value

        runtime.now_ns = counted_clock
        for name in ("_mandatory_chase", "_soft_exit_chase_window", "_stop_requested", "_exit_quote"):
            original = getattr(runtime, name)

            def counted(*args, _original=original, _name=name, **kwargs):
                value = _original(*args, **kwargs)  # Original guard/quote evaluated once.
                guards.append((_name, value))
                return value

            setattr(runtime, name, counted)
        if case == "stale":
            now[0] += timedelta(seconds=3)
        if case == "zero":
            async def zero(account):
                return [BrokerPositionSnapshot(account, "SCNI", D(0), D(3), as_of=now[0])]
            broker.list_account_positions = zero  # Typed controlled venue result, not a permission.
        if case in {"positions_error", "cancelled"}:
            async def failed(account):
                if case == "cancelled":
                    raise asyncio.CancelledError()
                raise OSError("controlled positions error")
            broker.list_account_positions = failed
        original_save = runtime._save
        changed = False

        async def controlled_save(episode):
            nonlocal changed
            new_attempt = runtime.runtime[identity].get("soft_exit_sent") and not changed
            if new_attempt and case == "failed_save":
                changed = True
                raise OSError("controlled attempt save failure")
            await original_save(episode)
            if new_attempt and case == "unsent":
                changed = True
                now[0] += timedelta(seconds=3)

        runtime._save = controlled_save
        try:
            try:
                if case == "stop":
                    from tests.unit.test_killsw1_momentum_http import real_controlled_sync
                    service._latest_quotes_by_symbol.pop("SCNI", None)
                    position = (await runtime._observe(identity)).position
                    await runtime._exit(position, "KILLSW1_STOP")
                    assert exit_orders(broker) == []  # Original post-protection receipt guard.
                    now[0] += timedelta(seconds=1)
                    await real_controlled_sync(patch, now, service, position.account)
                    await runtime._exit(position, "KILLSW1_STOP")
                else:
                    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
                if case == "chase":
                    await advance_exit(now, service, runtime, identity, seconds=2, bid="2.907")
            except BaseException as exc:
                result = type(exc).__name__
            values = runtime.runtime[identity]
            economic = {key: values[key] for key in (
                "soft_exit_reason", "soft_exit_sent", "soft_exit_attempt", "soft_exit_coid",
                "soft_exit_attempt_ns", "soft_exit_dispatch_completed_ns", "soft_exit_quote_ns",
                "soft_exit_cancel_completed_ns", "soft_exit_cancel_verified_ns",
            ) if key in values}
            # Existing controlled DB inspector, AFTER the original decision.
            # Do not label an unsaved in-memory dict as durable persistence.
            durable = await runtime._inspect(identity)
            snapshot = {"result": result, "requests": [asdict(request) for request in broker.sent],
                "cancelled": broker.cancelled, "book": runtime.book.snapshot(),
                "active_count": runtime.book._active_count,
                "active_by_symbol": {key: sorted(value) for key, value in runtime.book._active_by_symbol.items()},
                "actor_owners": sorted(runtime.actor.work.owners),
                "actor_dirty": sorted(runtime.actor.work.dirty),
                "dispatch": runtime.actor.dispatch.by_symbol, "actor_restored": runtime.actor.restored,
                "native_handles": {key: asdict(value) for key, value in runtime.native.handles.items()},
                "native_generations": runtime.native.generations,
                "in_memory_runtime_payload": values, "durable_runtime_payload": durable["runtime"],
                "durable_economics": {key: durable[key] for key in (
                    "quantity", "exited", "price", "entry_terminal", "outstanding_exit", "order_count")},
                "runtime_exit": economic, "clock_calls": clocks, "guard_results": guards}
            serialized = json.dumps(snapshot, default=str, sort_keys=True, separators=(",", ":"))
            logs = {"gate": timing_lines(logger, GATE), "attempt": timing_lines(logger, ATTEMPT)}
            return serialized, logs
        finally:
            await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [12, 14], ids=["Webull-PM", "Webull-RTH"])
@pytest.mark.parametrize("case", ["normal", "stale", "zero", "positions_error", "cancelled",
                                  "failed_save", "unsent", "chase"])
async def test_real_exit_decisions_requests_guards_clocks_owner_bytes_conserved(monkeypatch, hour, case):
    baseline, no_lines = await controlled_exit(monkeypatch, diagnostics=False, hour=hour, case=case)
    current, lines = await controlled_exit(monkeypatch, diagnostics=True, hour=hour, case=case)
    assert current.encode() == baseline.encode()  # No quantity/guard/request assertion relaxation.
    expected_result = "OSError" if case in {"positions_error", "failed_save"} else "CancelledError" if case == "cancelled" else "returned"
    parsed = json.loads(current)
    assert parsed["result"] == expected_result and parsed["active_count"] == 1
    actual_exits = [row for row in parsed["requests"]
                    if row["intent_type"] == "close" and row["metadata"].get("exit_reason")]
    wire_count = 2 if case == "chase" and hour == 12 else 1 if case in {"normal", "chase"} else 0
    assert len(actual_exits) == wire_count
    assert no_lines == {"gate": [], "attempt": []}
    expected = 2 if case == "chase" and hour == 12 else 1 if case in {"normal", "unsent", "chase"} else 0
    assert len(lines["attempt"]) == expected
    assert len(lines["gate"]) <= 20
    if case in {"normal", "unsent", "chase"}:
        assert lines["attempt"]
    if case in {"positions_error", "cancelled", "failed_save", "stale", "zero"}:
        assert not lines["attempt"]


@pytest.mark.asyncio
async def test_rth_stop_real_post_protection_receipt_and_market_quantity_conserved(monkeypatch):
    baseline, no_lines = await controlled_exit(monkeypatch, diagnostics=False, hour=14, case="stop")
    current, lines = await controlled_exit(monkeypatch, diagnostics=True, hour=14, case="stop")
    assert current.encode() == baseline.encode()
    parsed = json.loads(current)
    assert parsed["result"] == "returned" and parsed["active_count"] == 1
    exits = [row for row in parsed["requests"]
             if row["intent_type"] == "close" and row["metadata"].get("exit_reason")]
    assert len(exits) == 1 and exits[0]["order_type"] == "market" and D(exits[0]["quantity"]) == D(33)
    assert exits[0]["metadata"]["exit_reason"] == "KILLSW1_STOP"
    assert not no_lines["attempt"] and len(lines["attempt"]) == 1
    assert len(lines["gate"]) <= 20


@pytest.mark.asyncio
async def test_failed_cancel_latch_save_never_fabricates_anchor_or_native_wire():
    _, broker, service, runtime, identity = await prepared("webull", 14, send_exit=False)
    service.logger = Mock(wraps=service.logger)
    original = runtime._save_handle
    failure = OSError("controlled cancel-latch persistence failed")

    async def failed(handle):
        if runtime.runtime[identity].get("native_cancel_wires:" + handle.base_coid):
            raise failure
        return await original(handle)

    runtime._save_handle = failed
    with pytest.raises(OSError) as raised:
        await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert raised.value is failure
    assert runtime._exit_gate_states[identity]["latch_ns"] is None
    assert not runtime._exit_gate_states[identity]["post_cancel"]
    assert broker.cancelled == [] and not exit_orders(broker)
    assert not timing_lines(service.logger, ATTEMPT)
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["diag_full", "logger_error"])
async def test_blind_or_failed_diagnostics_cannot_block_original_owned_sell(monkeypatch, case):
    baseline, _ = await controlled_exit(monkeypatch, diagnostics=False, hour=14, case=case)
    current, lines = await controlled_exit(monkeypatch, diagnostics=True, hour=14, case=case)
    assert current.encode() == baseline.encode()
    parsed = json.loads(current)
    assert parsed["result"] == "returned" and parsed["active_count"] == 1
    assert len([row for row in parsed["requests"]
                if row["intent_type"] == "close" and row["metadata"].get("exit_reason")]) == 1
    if case == "diag_full":
        assert lines == {"gate": [], "attempt": []}  # UNKNOWN diagnostics, not a new trade refusal.
    else:
        assert len(lines["attempt"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [12, 14])
async def test_new_durable_attempt_not_repeated_by_unchanged_cadence(monkeypatch, hour):
    now, broker, service, runtime, identity = await prepared("webull", hour, send_exit=False)
    service.logger = Mock(wraps=service.logger)
    await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    before = (len(timing_lines(service.logger, GATE)), len(timing_lines(service.logger, ATTEMPT)))
    assert before[1] == 1 and len(exit_orders(broker)) == 1
    for _ in range(4):
        await runtime._exit((await runtime._observe(identity)).position, "time_300s")
    assert before == (len(timing_lines(service.logger, GATE)), len(timing_lines(service.logger, ATTEMPT)))
    assert len(exit_orders(broker)) == 1
    await runtime.close()
