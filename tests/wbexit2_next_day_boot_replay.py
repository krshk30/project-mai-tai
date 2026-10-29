"""Explicit WBEXIT2 NEXT-DAY component replay driver, SOURCE/UNRUN.

Uses the surviving controlled OMS/SQLite fixture, actual production boot hook,
actual managed quote/close entry point, and exact durable owner rows. NOT a
process-kill, systemd, provider, PG or full-fleet boot receipt. Main alone runs.
"""
from dataclasses import replace
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic

from sqlalchemy import select

from project_mai_tai.db.models import BrokerOrder, SystemIncident
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.oms import wbexit2
from project_mai_tai.oms.bot_control_runtime import ControlledBrokerAdapter
from tests.unit.test_wbattach1_riskoff import closes, restarted
from tests.unit.test_wbexit2_route import ControlledOwnerBroker, owner_case, close_case, saved

DAY1 = datetime(2026, 10, 28, 14, 0, tzinfo=UTC)
BOOT = datetime(2026, 10, 29, 8, 0, tzinfo=UTC)  # NEXT calendar/trading day04:00ET.
OBSERVE = datetime(2026, 10, 29, 13, 31, tzinfo=UTC)


class BootBroker(ControlledOwnerBroker):
    substitute_working = False
    forbid_list = False

    async def confirm_exit_pair_from_today_list(self, **binding):
        assert not self.forbid_list, "durable positive original terminal receipt must not need a new today's list"
        result = await super().confirm_exit_pair_from_today_list(**binding)
        if self.substitute_working:
            return replace(result, outcome="reserved", reports=tuple(replace(r,
                event_type="accepted", broker_order_id="NOT-ORIGINAL-" + r.broker_order_id,
                filled_quantity=Decimal(0), broker_fill_id=None, fill_price=None) for r in result.reports))
        return result


async def runtime(service, broker, *, provision=False):
    """Actual existing control store/state, never a synthetic permission bool."""
    with service.session_factory() as session:
        from project_mai_tai.db.models import Strategy
        strategy_id = session.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
    control = service._bot_control_runtime
    if provision:
        control.store.seed(strategy_id)  # Fixture provisioning DAY1 only, never startup fallback.
    else:
        control.store.hydrate(strategy_id)  # Missing scope MUST fail; never silently recreate RUN.
    await control.refresh()
    service.broker_adapter = ControlledBrokerAdapter(broker, control)


def profile(service):
    # No dummy missing fields: Main must compose actual reviewed Settings/core.
    for field in ("strategy_schwab_1m_v2_wbexit2_enabled", "orb_country_semantic_admission_enabled",
                  "strategy_schwab_1m_v2_keepwatch1_enabled", "strategy_schwab_1m_v2_brokerdecides1_enabled"):
        if field not in type(service.settings).model_fields:
            raise ValueError("wbexit2_boot_requires_Main_composed_profile:" + field)
        object.__setattr__(service.settings, field, True)
    assert service.settings.bot_control_enabled is True


async def replay_next_day(make_case, *, scenario="original_terminal"):
    if scenario not in {"original_terminal", "durable_terminal", "durable_terminal_corrupt",
                        "orphan_nonce", "replacement_working", "entry_time_changed", "no_prior_owner"}:
        raise ValueError("wbexit2_boot_unreviewed_scenario")
    case = await owner_case(lambda: make_case(control=True), port_type=BootBroker, at_clock=DAY1)
    service, factory, broker, entry_id, clock = case
    profile(service)
    await runtime(service, broker, provision=True)
    broker.lag = True
    if scenario in {"durable_terminal", "durable_terminal_corrupt"}:
        broker.lag = False
        broker.positions_unknown = True  # Real terminal fact stored; later read fails, no SELL.
    original = None
    if scenario != "no_prior_owner":
        await close_case(case, "CW_FLIP")
        original = saved(case)
        assert original["invocation"] is None and original["sell"] is None
        assert original["original_attempt_outcome"] == "FAIL"
        assert len(broker.cancelled) == 1 and not closes(broker)
        if scenario == "orphan_nonce":
            with factory.begin() as session:
                wbexit2.transition(session, original["identity"], original["generation"], operation="acquire",
                    now=clock[0], invocation="CONTROLLED-prior-physical-state-UNKNOWN")
    await service._shutdown_symbol_tick_work()  # Existing drain; no thread/SDK death is inferred.
    assert not service.__dict__.get("_wbattach_exit_owners")
    assert not service._confirmation_exit_recovery_tasks and not service.__dict__.get("_symbol_tick_work")
    before_counts = len(broker.requests), len(broker.cancelled), broker.positions_reads
    with factory() as session:
        entry = session.get(BrokerOrder, entry_id)
        entry_stamps = entry.submitted_at  # BrokerOrder has no created_at column
    case = restarted(case)  # Fresh actual OMS object, SAME persisted DB and controlled venue book.
    service, _, broker, _, clock = case
    service.__dict__.pop("_v2_eod_handover_ready", None)
    service.__dict__.pop("_fetch_quote_for_order", None)  # Real NEW instance methods, no old bound self.
    clock[0] = BOOT
    profile(service)
    await runtime(service, broker)
    service._rehydrate_managed_v2_symbols()  # Actual existing boot step, not patched/hardcoded ready.
    boot = await wbexit2.hydrate_at_boot(service)  # EXACT proposed service.run hook.
    assert service._wbexit2_boot_complete is True
    assert before_counts == (len(broker.requests), len(broker.cancelled), broker.positions_reads)
    if original is not None:
        assert boot["owners"] == [original["owner_id"]]
    else:
        assert boot["owners"] == []
    await service._handle_quote_tick_event(QuoteTickEvent(source_service="CONTROLLED-NEXT-DAY-BOOT",
        produced_at=clock[0], payload=QuoteTickPayload(symbol=broker.symbol,
            bid_price=Decimal("9.40"), ask_price=Decimal("9.41"))))
    tasks = tuple(service.__dict__.get("_symbol_tick_work", {}).values())
    if tasks:
        await asyncio.gather(*tasks)  # Join admitted work without closing/replacing quote scheduler.
    assert before_counts == (len(broker.requests), len(broker.cancelled), broker.positions_reads)
    clock[0] = OBSERVE
    observation_origin = monotonic()
    async def quote_observation():
        clock[0] = OBSERVE + timedelta(seconds=monotonic() - observation_origin)
        await service._handle_quote_tick_event(QuoteTickEvent(source_service="CONTROLLED-NEXT-DAY-BOOT",
            produced_at=clock[0], payload=QuoteTickPayload(symbol=broker.symbol,
                bid_price=Decimal("9.40"), ask_price=Decimal("9.41"))))
        admitted = tuple(service.__dict__.get("_symbol_tick_work", {}).values())
        if admitted:
            await asyncio.gather(*admitted)
    if scenario == "entry_time_changed":
        with factory.begin() as session:
            entry = session.get(BrokerOrder, entry_id)
            entry.submitted_at = OBSERVE  # Negative corruption input, NEVER positive boot mapping.
    broker.substitute_working = scenario == "replacement_working"
    if scenario in {"durable_terminal", "durable_terminal_corrupt"}:
        assert original["terminal_receipts"]
        broker.forbid_list = True  # Yesterday's list unavailable; never treat omission as terminal.
        broker.positions_unknown = False
        if scenario == "durable_terminal_corrupt":
            with factory.begin() as session:
                row = session.get(SystemIncident, wbexit2.address(original["identity"]))
                from copy import deepcopy
                bad = deepcopy(row.payload)
                receipts = next(iter(bad["terminal_receipts"].values()))
                receipts[0]["broker_order_id"] = "CONTROLLED-NOT-ORIGINAL"
                row.payload = bad  # Negative corruption seam, never positive permission.
            # Boot cache isn't a native permission: per-invocation real read/CAS must reject.
    if scenario == "original_terminal":
        await quote_observation()  # Actual quote->existing shared path; unknown still holds NEXT DAY.
        assert before_counts == (len(broker.requests), len(broker.cancelled), broker.positions_reads)
        assert saved(case)["originals"] == original["originals"] and saved(case)["sell"] is None
        # Keep the ACTUAL existing monotonic retry spacing. No set-to-zero,
        # backdating, fake clock or bypass. Only Main executes this finite wait.
        await asyncio.sleep(service._WEBULL_CW_EXIT_RETRY_SECONDS + .01)
    broker.lag = False  # Only controlled external read changes, not APP permission/state.
    await quote_observation()
    await service._shutdown_symbol_tick_work()
    if scenario in {"original_terminal", "durable_terminal"}:
        final = saved(case)
        assert final["phase"] == "RESOLVED" and len(closes(broker)) == 1
        assert len(broker.cancelled) == 1 and broker.working_reads == 1 and broker.total == 1000
        for key in ("owner_id", "originals", "first_cancel_at", "entry_anchor_at",
                    "opened_at", "original_action_deadline_at", "original_attempt_outcome"):
            assert final[key] == original[key], "original responsibility/clock/FAIL must not be rebased"
    elif original is not None:
        final = saved(case)
        assert not closes(broker) and len(broker.cancelled) == before_counts[1]
        assert final["owner_id"] == original["owner_id"] and final["originals"] == original["originals"]
        if scenario == "orphan_nonce":
            assert final["invocation"] == "CONTROLLED-prior-physical-state-UNKNOWN"
    else:
        with factory() as session:
            assert not session.scalars(select(SystemIncident).where(
                SystemIncident.payload["source"].as_string() == "wbexit2")).all()
        final = None
        assert not closes(broker) and not broker.cancelled
    if scenario != "entry_time_changed":
        with factory() as session:
            entry = session.get(BrokerOrder, entry_id)
            assert entry.submitted_at == entry_stamps
    return {"kind": "CONTROLLED_NEXT_DAY_WBEXIT2_COMPONENT_REPLAY", "scenario": scenario,
        "day1": DAY1.isoformat(), "boot": BOOT.isoformat(), "observe": OBSERVE.isoformat(),
        "inventory": boot, "owner_id": original["owner_id"] if original else None,
        "final_phase": final["phase"] if final else "NO_PRIOR_OWNER_NO_PERMISSION",
        "sell_count": len(closes(broker)), "cancel_count": len(broker.cancelled),
        "native_provider": "ABSENT_UNKNOWN", "actual_process_restart": "NOT_COVERED",
        "full_fleet_ALLON_boot": "NOT_COVERED_REQUIRES_Main_literal_composition_and_actual_gate"}
