"""SOURCE UNRUN. Existing WBATTACH fixture/monitor, not a new benchmark runner.

Main only after literal composition/profile/pins review and fresh quiet grant.
240 aggregate input pacing is the surviving WBATTACH source's contract;
measured >=200/s for60s, loop stall AND callback max strictly<50ms unchanged.
This controlled OMS quote/exit test is NOT the full-fleet L11/OMS-sync gate.
"""
import asyncio
from datetime import timedelta
from decimal import Decimal
import json
from time import monotonic

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import AccountPosition, OmsManagedPosition, Strategy, VirtualPosition
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.oms.bot_control_runtime import ControlledBrokerAdapter
from tests.perf.test_wbattach1_o14_active_oms_rate import LoopStallMonitor
from tests.unit.test_wbattach1_riskoff import make_case as make_case, closes
from tests.unit.test_wbexit2_route import owner_case, saved
from project_mai_tai.oms import wbexit3
from tests.unit.test_webull_adapter import fake_sdk as fake_sdk
from tests.perf.wbexit3_recovery_actor import build_recovery_case, run_recovery, recovery_summary


@pytest.fixture(autouse=True)
def callback_duration_evidence(monkeypatch, request):
    # Actual awaited handler durations; wrapper cost remains inside the original
    # caller's callback limit. No callback/workload or threshold is replaced.
    from project_mai_tai.oms.service import OmsRiskService
    actual = OmsRiskService._handle_quote_tick_event
    durations = []
    async def observed(self, event):
        before = monotonic()
        try:
            return await actual(self, event)
        finally:
            durations.append(monotonic() - before)
    monkeypatch.setattr(OmsRiskService, "_handle_quote_tick_event", observed)
    yield
    ordered = sorted(durations)
    print("WB3_CALLBACK_DURATION_EVIDENCE=" + json.dumps({
        "node": request.node.nodeid, "count": len(ordered),
        "p99_ms": ordered[max(0, (99 * len(ordered) + 99) // 100 - 1)] * 1000 if ordered else None,
        "max_ms": max(ordered) * 1000 if ordered else None,
        "scope": "ACTUAL_AWAITED_HANDLER_INNER_DURATION_OUTER_ORIGINAL_MAX_LIMIT_UNCHANGED"}, sort_keys=True))


@pytest.mark.asyncio
async def test_WBEXIT3_real_quote_callback_pending_resume_60s_unchanged_limits(make_case, monkeypatch, fake_sdk):
    case = await owner_case(lambda: make_case(control=True))
    service, factory, broker, _, clock = case
    object.__setattr__(service.settings, "oms_webull_exit3_enabled", True)
    await wbexit3.hydrate_at_boot(service)
    # C11318: run only on Main's literal composed future source. No dummy
    # attribute may stand in for the independently reviewed ORBCOUNTRY1 field.
    assert "orb_country_semantic_admission_enabled" in type(service.settings).model_fields
    object.__setattr__(service.settings, "orb_country_semantic_admission_enabled", True)
    with factory() as session:
        strategy_id = session.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
        row_id = str(session.scalar(select(OmsManagedPosition)).id)
    runtime = service._bot_control_runtime
    runtime.store.seed(strategy_id)
    await runtime.refresh()
    service.broker_adapter = ControlledBrokerAdapter(broker, runtime)
    service._rehydrate_managed_v2_symbols()
    assert (broker.account, broker.symbol) in service._managed_v2_symbols
    assert service.settings.bot_control_enabled is True
    assert service.settings.strategy_schwab_1m_v2_keepwatch1_enabled is True
    assert service.settings.strategy_schwab_1m_v2_brokerdecides1_enabled is True
    assert service.settings.strategy_schwab_1m_v2_wbexit2_enabled is True
    assert service.settings.orb_country_semantic_admission_enabled is True
    # C10103: a separate private OMS/DB/native book shares this exact event loop
    # and executor; this measures shared-loop contention, not single-DB/fleet cost.
    recovery_case, recovery_evidence = await build_recovery_case(make_case, monkeypatch, clock)
    recovery_service, recovery_factory, recovery_broker, _, _ = recovery_case
    broker.lag = True
    baseline = clock[0]
    count, callback_max, elapsed = 0, 0., 0.
    actor = recovery_actor = monitor_task = monitor = None
    primary = None
    start = monotonic()

    async def trigger():
        await service._webull_cw_exit_on_shared_path(broker.account, broker.symbol,
            tag="CW_FLIP", ref=broker.bid, bid=broker.bid, expected_row_id=row_id)
        assert not closes(broker) and saved(case)["invocation"] is None
        broker.lag = False  # External terminal reply becomes readable, never a fresh WORKING bind.

    try:
        monitor = LoopStallMonitor(start)
        monitor_task = asyncio.create_task(monitor.run())
        await monitor.ready.wait()
        actor = asyncio.create_task(trigger())
        recovery_actor = asyncio.create_task(run_recovery(recovery_case, recovery_evidence))
        while monotonic() - start < 60:
            due = start + count / 240
            if due > monotonic():
                await asyncio.sleep(due - monotonic())
            clock[0] = baseline + timedelta(seconds=monotonic() - start)
            event = QuoteTickEvent(source_service="CONTROLLED-WBEXIT3-OMS",
                produced_at=clock[0], payload=QuoteTickPayload(symbol=broker.symbol,
                    bid_price=Decimal(10), ask_price=Decimal("10.01")))
            before = monotonic()
            await service._handle_quote_tick_event(event)
            callback_max = max(callback_max, monotonic() - before)
            count += 1
            if actor.done():
                actor.result()
            if recovery_actor.done():
                recovery_actor.result()
        end = monotonic()
        elapsed = end - start
        monitor.finish(end)
        assert elapsed >= 60 and count / elapsed >= 200
        assert monitor.max_stall < .05 and callback_max < .05
        assert actor.done() and actor.result() is None
        assert recovery_actor.done() and recovery_actor.result() is None
        assert start <= recovery_evidence["actor_start"] < recovery_evidence["actor_end"] <= end
        assert recovery_broker.positions_reads == 2 and recovery_broker.history_calls == 1
        assert [entry["kind"] for entry in recovery_evidence["sdk_entries"]].count("positions") == 2
        assert [entry["stage"] for entry in recovery_evidence["sdk_entries"]].count("recovery_history") == 1
        assert [entry["stage"] for entry in recovery_evidence["sdk_entries"]].count("original_children") == 1
        assert len(recovery_evidence["sdk_entries"]) == len(recovery_evidence["parser_entries"]) == 4
        assert not recovery_evidence["synchronous_loop_reads"]
        for entry in recovery_evidence["sdk_entries"] + recovery_evidence["parser_entries"]:
            assert entry["thread_id"] != recovery_evidence["loop_thread_id"]
            assert start <= entry["entered_at"] <= entry["returned_at"] <= end
        for entry in recovery_evidence["parser_entries"]:
            if entry["parser"] in {"_wbexit2_positions_blocking", "_wbexit3_day_history_blocking"}:
                assert entry["foreground_reads_at_entry"] >= 1
        assert [request.quantity for request in closes(recovery_broker)] == [Decimal(27)]
        assert recovery_broker.total == 1000 and not recovery_broker.cancelled
        assert saved(recovery_case)["phase"] == "RESOLVED"
        with recovery_factory() as session:
            assert session.scalar(select(AccountPosition.quantity)) == 1000
            assert session.scalar(select(VirtualPosition.quantity)) == 0
            assert session.scalar(select(OmsManagedPosition)).status == "closed"
        assert not recovery_service.__dict__.get("_wbattach_exit_owners")
        assert recovery_broker.history_adapter._query_budget.foreground_reads == 0
        assert not recovery_broker.history_adapter._foreground_query_tasks
        await wbexit3.refresh_pending(recovery_service)
        assert not recovery_service._wbexit3_pending
        assert len(closes(broker)) == 1 and len(broker.cancelled) == 1 and broker.working_reads == 1
        assert broker.total == 1000 and saved(case)["phase"] == "RESOLVED"
        assert not service.__dict__.get("_wbattach_exit_owners")
        assert not service._confirmation_exit_recovery_tasks
        await wbexit3.refresh_pending(service)
        assert not service._wbexit3_pending
    except BaseException as exc:
        primary = exc
        raise
    finally:
        end = monotonic()
        if monitor is not None and not monitor.stopped:
            monitor.finish(end)
        tasks = [task for task in (actor, recovery_actor, monitor_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for owned_service in (service, recovery_service):
            try:
                await owned_service._shutdown_symbol_tick_work()  # Physical drain before either private DB disposal.
            except BaseException as cleanup:
                if primary is None:
                    raise
                print("WBEXIT3_SECONDARY_DRAIN_ERROR=" + type(cleanup).__name__)
        print("WBEXIT3_CONTROLLED_OMS_RATE=" + json.dumps(dict(events=count,
            elapsed_seconds=elapsed or end-start, callback_max_ms=callback_max * 1000,
            loop_max_stall_ms=monitor.max_stall * 1000 if monitor is not None else None,
            joined=sum(task.done() for task in tasks), owned=len(tasks),
            recovery=recovery_summary(recovery_case, recovery_evidence),
            native="ABSENT_UNKNOWN", full_fleet_L11="NOT_COVERED"), sort_keys=True))


@pytest.mark.asyncio
async def test_WBEXIT3_ON_original_both_broker_pipeline_60s(tmp_path, monkeypatch):
    # Original O14 fixture exercises actual Webull attach/riskoff and Schwab
    # managed hard-stop actors. Only Settings construction gains this flag. Its
    # original physical drain and >=200ev/s, 60s, <50ms assertions are unchanged.
    from tests.perf import test_wbattach1_o14_active_oms_rate as original
    original_settings = original.Settings
    def configured(**kwargs):
        kwargs["oms_webull_exit3_enabled"] = True
        settings = original_settings(**kwargs)
        assert settings.oms_webull_exit3_enabled is True
        return settings
    monkeypatch.setattr(original, "Settings", configured)
    await original.test_actual_oms_quote_attach_and_durable_exit_owner_aggregate_60s(tmp_path, monkeypatch)
