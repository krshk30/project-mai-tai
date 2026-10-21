from datetime import UTC, datetime, timedelta
from dataclasses import replace
from decimal import Decimal as D
import inspect
import textwrap
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal, Signal
from project_mai_tai.momentum_live.execution import ExecutionPorts, LiveEntryExecutor
from project_mai_tai.momentum_live_sec import CountryRoute


def setup(first_report=None, *, result_quantity=0, is_us=True):
    now = [datetime(2026, 10, 14, 14, tzinfo=UTC)]
    quote = {"ask": "3.001", "received_at": now[0]}
    def route_for(symbol, when):
        local = when.astimezone(ZoneInfo("America/New_York"))
        if not 4 <= local.hour < 16:
            return CountryRoute(None, "refused_hours", "UNKNOWN", None, None, None)
        if local.hour < 7:
            return CountryRoute("webull", "all_before_0700", "READY" if is_us is not None else "UNKNOWN", is_us, None, None)
        return CountryRoute("schwab" if is_us is True else "webull",
            "sec_us" if is_us is True else "sec_non_us" if is_us is False else "sec_unknown_at_decision",
            "READY" if is_us is not None else "UNKNOWN", is_us, None, None)
    async def submit(request):
        if first_report is not None and len(sent) == 0:
            fields = first_report
        else:
            fields = {"event_type": "accepted", "origin": "broker"}
        sent.append(request)
        return [ExecutionReport(client_order_id=request.client_order_id, **fields)]
    sent = []
    ports = ExecutionPorts(lambda: now[0], lambda symbol: quote, route_for,
        AsyncMock(return_value=False), AsyncMock(return_value=True), AsyncMock(), AsyncMock(),
        submit, AsyncMock(return_value={"quantity": D(result_quantity)}), AsyncMock(),
        refuse_unsent_entry=AsyncMock())
    book = EpisodeLedger.restore([])
    executor = LiveEntryExecutor(book, ports, schwab_account="live:schwab_1m_v2", webull_account="live:orb")
    signal = Signal("2026-10-14", "SCNI", "actual-source-id", 100, 200, D(3), int(now[0].timestamp()*1e9), 50000)
    return SimpleNamespace(now=now, quote=quote, ports=ports, book=book, executor=executor, signal=signal, sent=sent)


@pytest.mark.asyncio
async def test_entry_actual_wire_limit_and_durable_claim_before_wire():
    env = setup()
    episode, request, _ = await env.executor.admit(env.signal)
    assert len(env.sent) == 1 and env.ports.durable_claim.await_count == 1 and env.ports.durable_order.await_count == 1
    assert request.quantity == 33 and request.metadata["limit_price"] == "3.01"
    assert request.metadata["cap_price"] == "3.045"
    assert env.book.episodes[episode].first_submit_ns is not None
    assert (await env.executor.admit(env.signal))[1] is None and len(env.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("origin,event_type,quantity", [("client", "rejected", 0), ("unknown", "rejected", 0),
    ("broker", "accepted", 0), ("broker", "partially_filled", 1), ("broker", "rejected", 1)])
async def test_all_non_authoritative_or_partial_reports_never_resend(origin, event_type, quantity):
    env = setup({"event_type": event_type, "origin": origin, "filled_quantity": D(quantity),
                 "reason": "Opening transactions for this security must be placed with a broker."}, result_quantity=quantity)
    await env.executor.admit(env.signal)
    assert len(env.sent) == 1 and env.ports.durable_order.await_count == 1
    assert env.executor.schwab_refusals == int(origin == "broker" and event_type == "rejected")


@pytest.mark.asyncio
async def test_definitive_ineligible_zero_fill_schwab_refusal_never_resends(caplog):
    env = setup({"event_type": "rejected", "origin": "broker", "filled_quantity": D(0),
                 "reason": "Opening transactions for this security must be placed with a broker."})
    episode, request, _ = await env.executor.admit(env.signal)
    assert len(env.sent) == 1 and request.broker_account_name == "live:schwab_1m_v2"
    assert request.metadata["episode_id"] == env.sent[0].metadata["episode_id"] == episode
    assert request.metadata["cap_price"] == "3.045"
    assert len(env.book.episodes) == 1 and env.book._active_count == 1
    assert env.executor.schwab_refusals == 1
    assert "resend=never" in caplog.text
    assert env.ports.record_route.call_args.args[-1] == "sec_us"
    await env.executor.admit(env.signal)
    assert len(env.sent) == 1 and env.executor.schwab_refusals == 1


@pytest.mark.asyncio
async def test_route_report_persistence_delay_cannot_trigger_second_broker_dispatch():
    env = setup({"event_type": "rejected", "origin": "broker", "filled_quantity": D(0),
                 "reason": "Opening transactions for this security must be placed with a broker."})
    async def slow_cache(*args):
        env.now[0] += timedelta(seconds=2)
        env.quote["received_at"] = env.now[0]
    env.ports.record_route.side_effect = slow_cache
    await env.executor.admit(env.signal)
    assert len(env.sent) == 1 and env.book._active_count == 1


@pytest.mark.asyncio
async def test_refusal_does_not_even_query_other_broker_ownership():
    env = setup({"event_type": "rejected", "origin": "broker", "filled_quantity": D(0),
                 "reason": "Opening transactions for this security must be placed with a broker."})
    env.ports.foreign_owner.side_effect = [False, True]
    await env.executor.admit(env.signal)
    assert len(env.sent) == 1
    env.ports.foreign_owner.assert_awaited_once_with("live:schwab_1m_v2", "SCNI")


@pytest.mark.asyncio
@pytest.mark.parametrize("is_us,account", [(True, "live:schwab_1m_v2"), (False, "live:orb"), (None, "live:orb")])
async def test_upfront_sec_route_known_at_decision_no_ineligible_list(is_us, account):
    env = setup(is_us=is_us)
    _, request, _ = await env.executor.admit(env.signal)
    assert len(env.sent) == 1 and request.broker_account_name == account


@pytest.mark.asyncio
@pytest.mark.parametrize("is_us", [True, False, None])
async def test_five_am_always_webull_including_us_and_unknown(is_us):
    env = setup(is_us=is_us)
    env.now[0] = datetime(2026, 10, 14, 9, tzinfo=UTC)
    env.quote["received_at"] = env.now[0]
    env.signal = replace(env.signal, observed_ns=int(env.now[0].timestamp()*1e9))
    _, request, _ = await env.executor.admit(env.signal)
    assert request.broker_account_name == "live:orb" and len(env.sent) == 1


@pytest.mark.asyncio
async def test_unknown_classification_is_logged_as_unknown_not_us(caplog):
    import logging
    env = setup(is_us=None)
    with caplog.at_level(logging.INFO):
        await env.executor.admit(env.signal)
    assert "classification=UNKNOWN at_decision=true" in caplog.text
    assert "live:orb" == env.sent[0].broker_account_name


@pytest.mark.asyncio
async def test_route_is_selected_once_not_reclassified_between_db_and_wire():
    env = setup(is_us=None)
    chosen = env.ports.route_for
    calls = []
    def one_decision(symbol, when):
        calls.append(symbol)
        if len(calls) > 1:
            raise AssertionError("classification reconsulted after decision")
        return chosen(symbol, when)
    env.ports.route_for = one_decision
    _, request, _ = await env.executor.admit(env.signal)
    assert calls == ["SCNI"] and request.broker_account_name == "live:orb"


@pytest.mark.asyncio
async def test_no_resend_control_detects_reintroduced_second_broker_wire_mutant():
    from project_mai_tai.momentum_live import execution
    source = textwrap.dedent(inspect.getsource(LiveEntryExecutor.admit))
    wire = "reports = await self.ports.submit(request)"
    assert source.count(wire) == 1
    mutant = source.replace(wire, wire + "\n    await self.ports.submit(replace(request, "
        "broker_account_name='live:orb', client_order_id=request.client_order_id+'-fallback'))")
    namespace = dict(vars(execution), replace=replace)
    exec(compile(mutant, "<controlled-second-wire-mutant>", "exec"), namespace)
    env = setup({"event_type": "rejected", "origin": "broker", "filled_quantity": D(0),
                 "reason": "Opening transactions for this security must be placed with a broker."})
    env.executor.admit = MethodType(namespace["admit"], env.executor)
    await env.executor.admit(env.signal)
    with pytest.raises(AssertionError):
        assert len(env.sent) == 1
    assert len(env.sent) == 2


@pytest.mark.asyncio
async def test_unknown_submit_ack_never_second_wire_or_released_slot():
    env = setup()
    async def unknown(request):
        env.sent.append(request)
        raise TimeoutError
    env.ports.submit = unknown
    with pytest.raises(TimeoutError):
        await env.executor.admit(env.signal)
    assert env.book._active_count == 1
    await env.executor.admit(env.signal)
    assert len(env.sent) == 1


@pytest.mark.asyncio
async def test_quote_rechecked_after_durable_save_future_refuses_before_wire():
    env = setup()
    async def changed(*args):
        env.quote["received_at"] = env.now[0]+timedelta(microseconds=1)
    env.ports.save_episode.side_effect = changed
    with pytest.raises(Refusal, match="future"):
        await env.executor.admit(env.signal)
    assert not env.sent
