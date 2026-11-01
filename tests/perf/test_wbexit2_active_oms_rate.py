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

from project_mai_tai.db.models import OmsManagedPosition, Strategy
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.oms.bot_control_runtime import ControlledBrokerAdapter
from tests.perf.test_wbattach1_o14_active_oms_rate import LoopStallMonitor
from tests.unit.test_wbattach1_riskoff import make_case as make_case, closes
from tests.unit.test_wbexit2_route import owner_case, saved


@pytest.mark.timing
@pytest.mark.asyncio
async def test_WBEXIT2_real_quote_callback_pending_resume_60s_unchanged_limits(make_case):
    case = await owner_case(lambda: make_case(control=True))
    service, factory, broker, _, clock = case
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
    broker.lag = True
    baseline = clock[0]
    count, callback_max, elapsed = 0, 0., 0.
    actor = monitor_task = monitor = None
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
        while monotonic() - start < 60:
            due = start + count / 240
            if due > monotonic():
                await asyncio.sleep(due - monotonic())
            clock[0] = baseline + timedelta(seconds=monotonic() - start)
            event = QuoteTickEvent(source_service="CONTROLLED-WBEXIT2-OMS",
                produced_at=clock[0], payload=QuoteTickPayload(symbol=broker.symbol,
                    bid_price=Decimal(10), ask_price=Decimal("10.01")))
            before = monotonic()
            await service._handle_quote_tick_event(event)
            callback_max = max(callback_max, monotonic() - before)
            count += 1
            if actor.done():
                actor.result()
        end = monotonic()
        elapsed = end - start
        monitor.finish(end)
        assert elapsed >= 60 and count / elapsed >= 200
        assert monitor.max_stall < .05 and callback_max < .05
        assert actor.done() and actor.result() is None
        assert len(closes(broker)) == 1 and len(broker.cancelled) == 1 and broker.working_reads == 1
        assert broker.total == 1000 and saved(case)["phase"] == "RESOLVED"
        assert not service.__dict__.get("_wbattach_exit_owners")
        assert not service._confirmation_exit_recovery_tasks
    except BaseException as exc:
        primary = exc
        raise
    finally:
        end = monotonic()
        if monitor is not None and not monitor.stopped:
            monitor.finish(end)
        tasks = [task for task in (actor, monitor_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await service._shutdown_symbol_tick_work()  # Existing physical drain before fixture DB disposal.
        except BaseException as cleanup:
            if primary is None:
                raise
            print("WBEXIT2_SECONDARY_DRAIN_ERROR=" + type(cleanup).__name__)
        print("WBEXIT2_CONTROLLED_OMS_RATE=" + json.dumps(dict(events=count,
            elapsed_seconds=elapsed or end-start, callback_max_ms=callback_max * 1000,
            loop_max_stall_ms=monitor.max_stall * 1000 if monitor is not None else None,
            joined=sum(task.done() for task in tasks), owned=len(tasks),
            native="ABSENT_UNKNOWN", full_fleet_L11="NOT_COVERED"), sort_keys=True))
