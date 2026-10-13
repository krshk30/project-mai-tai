"""H baseline controls; controlled release/read errors, never live broker fills."""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import OmsManagedPosition, SystemIncident
from project_mai_tai.broker_adapters.protocols import ExecutionReport, ExitPairReleaseResult, OrderRequest
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.oms import service as module
from tests.unit.managed_entry_fixtures import set_protect_base
from tests.unit.test_confirmation_exit_fanout import SCHWAB, WEBULL, SYMBOL, _FanoutAdapter, _service
from tests.unit.test_v2_eod_oco_transition import (
    ACCT, SYM, _arm_managed, _confirmed_release, _force_due, _make_sf,
    _open_apus_row, _svc,
)


def prepared(monkeypatch):
    sf = _make_sf()
    svc = _svc(sf)
    _open_apus_row(sf)
    _arm_managed(svc)
    _force_due(svc)
    _confirmed_release(svc)
    monkeypatch.setattr(svc, "_session_day_et", lambda: "2026-10-12")
    return svc, sf


@pytest.mark.asyncio
async def test_confirmed_sync_handover_evaluates_software_ladder_without_new_quote(monkeypatch):
    svc, _ = prepared(monkeypatch)
    evaluate = AsyncMock()
    monkeypatch.setattr(svc, "_evaluate_v2_managed_exit", evaluate)
    await svc._v2_eod_oco_transition()
    assert ("2026-10-12", ACCT, SYM) in svc._v2_eod_oco_transitioned
    evaluate.assert_awaited_once_with(ACCT, SYM)


@pytest.mark.asyncio
async def test_unreadable_handover_row_opens_normal_incident_without_sell_authority(monkeypatch):
    svc, sf = prepared(monkeypatch)
    with sf() as session:
        row_id = str(session.scalar(select(OmsManagedPosition)).id)
    def unreadable(*args, **kwargs):
        raise RuntimeError("controlled snapshot read unavailable")
    monkeypatch.setattr(svc, "_read_v2_managed_snapshot", unreadable)
    await svc._v2_eod_oco_transition()
    assert ("2026-10-12", ACCT, SYM) not in svc._v2_eod_oco_transitioned
    with sf() as session:
        incidents = session.scalars(select(SystemIncident).where(SystemIncident.status == "open")).all()
        assert len(incidents) == 1
        assert incidents[0].severity == "critical"
        assert incidents[0].payload["managed_row_id"] == row_id
        assert incidents[0].payload["risk_state"] == "protection_unknown"


def broker_lane(monkeypatch, account):
    adapter = _FanoutAdapter()
    svc, sf = _service(fanout=True, adapter=adapter)
    svc.settings.oms_v2_eod_oco_transition_enabled = True
    svc.settings.oms_native_oco_stand_down_enabled = True
    svc._cw_exit_enabled, svc._cw_floor_exit_enabled = True, False
    svc._cw_target_pct, svc._cw_stop_pct = 5.0, 8.0
    svc._managed_v2_symbols = {(account, SYMBOL)}
    clock = {"now": datetime(2026, 10, 12, 19, 59, 59, tzinfo=UTC)}
    monkeypatch.setattr(module, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(svc, "_session_day_et", lambda: "2026-10-12")
    monkeypatch.setattr(svc, "_v2_eod_oco_transition_due", lambda now=None: clock["now"].hour >= 20)
    monkeypatch.setattr(svc, "_market_is_fillable", lambda now=None: True)
    monkeypatch.setattr(module, "_is_regular_market_session", lambda now=None: clock["now"].hour < 20)
    monkeypatch.setattr(module, "_extended_hours_session", lambda now=None: "PM" if clock["now"].hour >= 20 else None)
    svc._native_oco_armed_confirmed_at[(account, SYMBOL)] = clock["now"]
    order = []
    if account == WEBULL:
        set_protect_base(svc, sf, account, SYMBOL, "controlled-h-core-pair")
        adapter.accounts_by_name = {account: SimpleNamespace(account_id="controlled")}
        adapter.exit_pair_leg_client_order_ids = MethodType(WebullBrokerAdapter.exit_pair_leg_client_order_ids, adapter)
        adapter._combo_leg_coid = WebullBrokerAdapter._combo_leg_coid
        async def confirm_child(_account, request):
            order.append("fresh_child_read")
            return ExecutionReport(event_type="cancelled", origin="broker", client_order_id=request.client_order_id,
                symbol=SYMBOL, side="sell", intent_type="cancel", metadata={"cancel_outcome": "confirmed"})
        adapter._confirm_cancel_order = confirm_child
        adapter.confirm_exit_pair_terminal = MethodType(WebullBrokerAdapter.confirm_exit_pair_terminal, adapter)
        async def no_child_fill(*args):
            return None  # Controlled positive absence; never an unreadable broker response.
        adapter.fetch_oco_exit_fill = no_child_fill
    else:
        original_release = adapter.release_native_oco_for_close
        async def release(*args):
            order.append("native_release_confirm")
            return await original_release(*args)
        adapter.release_native_oco_for_close = release
    return svc, sf, adapter, clock, order


def tick(clock, bid):
    return QuoteTickEvent(source_service="controlled-h-eod", produced_at=clock["now"],
        payload=QuoteTickPayload(symbol=SYMBOL, bid_price=Decimal(str(bid)), ask_price=Decimal(str(bid)) + Decimal(".01")))


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [SCHWAB, WEBULL])
@pytest.mark.parametrize("kind,bid", [("CW_TARGET", 10.51), ("CW_HARD_STOP", 9.19), ("CW_FLIP", 9.90)])
@pytest.mark.parametrize("first", ["sync", "quote"])
@pytest.mark.parametrize("seconds", [1, 301], ids=["1600", "1605"])
async def test_sync_handover_and_quote_share_one_actual_managed_sell(monkeypatch, account, kind, bid, first, seconds):
    svc, sf, adapter, clock, order = broker_lane(monkeypatch, account)
    # The broker-owned RTH target has authority right up to the boundary.
    await svc._handle_quote_tick_event(tick(clock, 10.51))
    await drain_quote_workers(svc)
    assert adapter.submitted == [] and adapter.cancel_pair_calls == [] and adapter.native_release_calls == []
    clock["now"] += timedelta(seconds=seconds)
    svc._latest_quotes_by_symbol[SYMBOL] = {"bid": bid, "ask": bid + .01, "received_at": clock["now"]}
    if kind == "CW_FLIP":
        with sf() as session:
            row = session.scalar(select(OmsManagedPosition).where(OmsManagedPosition.broker_account_name == account))
            svc._arm_cw_flip_pending((account, SYMBOL), bar_time_ms=int((clock["now"] - timedelta(seconds=10)).timestamp()*1000),
                managed_row_id=str(row.id))
    entered, release = asyncio.Event(), asyncio.Event()
    original_submit = adapter.submit_order
    admissions = []
    async def paused_submit(request):
        admissions.append(request)
        order.append("submit")
        entered.set()
        await release.wait()
        return await original_submit(request)
    monkeypatch.setattr(adapter, "submit_order", paused_submit)
    initial = asyncio.create_task(svc._v2_eod_oco_transition() if first == "sync"
        else svc._handle_quote_tick_event(tick(clock, bid)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(svc._handle_quote_tick_event(tick(clock, bid)) if first == "sync"
            else svc._v2_eod_oco_transition(), 1)
        if first == "sync":
            await drain_quote_workers(svc)
        assert len(admissions) == 1 and adapter.submitted == []
    finally:
        release.set()
        await initial
        await drain_quote_workers(svc)
    request, = adapter.submitted
    assert request.reason.endswith(kind) and request.quantity == 1
    assert request.order_type == "limit" and request.metadata["session"] == "PM"
    if kind == "CW_TARGET":
        assert Decimal(request.metadata["limit_price"]) >= Decimal("10.50")
    assert order.index("fresh_child_read" if account == WEBULL else "native_release_confirm") < order.index("submit")
    await svc._v2_eod_oco_transition()
    assert len(adapter.submitted) == 1 and not svc.__dict__.get("_v2_eod_ladder_inflight")
    await drain_recovery(svc)


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [SCHWAB, WEBULL])
@pytest.mark.parametrize("outcome", ["reserved", "unanswerable"])
async def test_unconfirmed_handover_retries_next_fifteen_second_sync_and_pages(monkeypatch, account, outcome):
    svc, sf, adapter, clock, _ = broker_lane(monkeypatch, account)
    clock["now"] += timedelta(seconds=1)
    elapsed = {"now": 100.0}
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: elapsed["now"]))
    calls = []
    async def unresolved(*args, **kwargs):
        calls.append(kwargs["expected_row_id"])
        return ExitPairReleaseResult(outcome=outcome)
    monkeypatch.setattr(svc, "_release_v2_eod_oco", unresolved)
    await svc._v2_eod_oco_transition()
    await svc._handle_quote_tick_event(tick(clock, 10.51))
    await drain_quote_workers(svc)
    assert len(calls) == 1 and adapter.submitted == []
    elapsed["now"] += 15
    clock["now"] += timedelta(seconds=15)
    await svc._v2_eod_oco_transition()
    assert len(calls) == 2 and adapter.submitted == []
    with sf() as session:
        incident, = session.scalars(select(SystemIncident).where(SystemIncident.status == "open")).all()
        assert incident.severity == "critical" and incident.payload["release_outcome"] == outcome
        assert incident.payload["managed_row_id"] == calls[0] == calls[1]


def test_real_webull_core_pair_payload_preserves_stop_type():
    request = OrderRequest(client_order_id="controlled-h-core-pair", broker_account_name=WEBULL,
        strategy_code="schwab_1m_v2", symbol=SYMBOL, side="sell", intent_type="close", quantity=Decimal(1),
        reason="controlled CORE handover input", metadata={"bracket_target_price": "10.50",
            "bracket_stop_price": "9.20", "session": "NORMAL"})
    target, stop = WebullBrokerAdapter.__new__(WebullBrokerAdapter)._build_exit_only_pair_payload(request)
    assert target["support_trading_session"] == stop["support_trading_session"] == "CORE"
    assert target["limit_price"] == "10.50" and stop["stop_price"] == "9.20"
    assert stop["order_type"] == "STOP_LOSS" and target["order_type"] == "LIMIT"


@pytest.mark.asyncio
async def test_real_terminal_read_partial_fill_blocks_until_next_sync_confirms_remainder(monkeypatch):
    svc, sf, adapter, clock, _ = broker_lane(monkeypatch, WEBULL)
    clock["now"] += timedelta(seconds=1)
    elapsed = {"now": 100.0}
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: elapsed["now"]))
    original = adapter._confirm_cancel_order
    partial = {"active": True}
    async def child(account, request):
        if partial["active"] and request.client_order_id.endswith("S"):
            return ExecutionReport(event_type="partially_filled", origin="broker", client_order_id=request.client_order_id,
                symbol=SYMBOL, side="sell", intent_type="cancel", quantity=Decimal(2), filled_quantity=Decimal(1),
                fill_price=Decimal("9.20"), metadata={"cancel_outcome": "confirmed"})
        return await original(account, request)
    adapter._confirm_cancel_order = child
    with sf.begin() as session:
        row = session.scalar(select(OmsManagedPosition).where(OmsManagedPosition.broker_account_name == WEBULL))
        row.original_quantity = 2  # Controlled already-booked partial fill leaves owned remainder1.
    svc._latest_quotes_by_symbol[SYMBOL] = {"bid": 9.19, "ask": 9.20, "received_at": clock["now"]}
    await svc._v2_eod_oco_transition()
    assert adapter.submitted == [] and not svc._v2_eod_oco_transitioned
    with sf() as session:
        incident, = session.scalars(select(SystemIncident).where(SystemIncident.status == "open")).all()
        assert incident.payload["risk_state"] == "protection_unknown"
    elapsed["now"] += 15
    clock["now"] += timedelta(seconds=15)
    svc._latest_quotes_by_symbol[SYMBOL]["received_at"] = clock["now"]
    partial["active"] = False
    await svc._v2_eod_oco_transition()
    request, = adapter.submitted
    assert request.quantity == 1 and request.reason.endswith("CW_HARD_STOP")
    with sf() as session:
        assert session.scalars(select(SystemIncident).where(SystemIncident.status == "open")).all() == []
    await drain_recovery(svc)


async def drain_recovery(svc):
    tasks = tuple(svc.__dict__.get("_confirmation_exit_recovery_tasks", ()))
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 1)


async def drain_quote_workers(svc):
    # Accepted B intake schedules its managed leg; handler return is not completion.
    while svc.__dict__.get("_symbol_tick_work", {}):
        await asyncio.wait_for(asyncio.gather(*tuple(svc._symbol_tick_work.values())), 1)


@pytest.mark.asyncio
async def test_eod_evaluation_exception_releases_only_temporary_claim(monkeypatch):
    svc, _ = prepared(monkeypatch)
    error = RuntimeError("controlled original evaluator error")
    monkeypatch.setattr(svc, "_evaluate_v2_managed_exit_claimed", AsyncMock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert caught.value is error and not svc._v2_eod_ladder_inflight


@pytest.mark.asyncio
async def test_pre_eod_evaluation_does_not_add_a_handover_claim(monkeypatch):
    svc, _ = prepared(monkeypatch)
    _force_due(svc, False)
    original = AsyncMock()
    monkeypatch.setattr(svc, "_evaluate_v2_managed_exit_claimed", original)
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    original.assert_awaited_once_with(ACCT, SYM)
    assert "_v2_eod_ladder_inflight" not in svc.__dict__


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [SCHWAB, WEBULL])
@pytest.mark.parametrize("mode", ["target", "stale", "flip_at_target", "flip_after_capture"])
async def test_accepted_8ca_target_floor_and_capture_guards_at_handover(monkeypatch, account, mode):
    svc, sf, adapter, clock, _ = broker_lane(monkeypatch, account)
    with sf.begin() as session:
        row = session.scalar(select(OmsManagedPosition).where(OmsManagedPosition.broker_account_name == account))
        row.entry_price = Decimal("2.49")
        bound_id = str(row.id)
    await svc._handle_quote_tick_event(tick(clock, 2.64))
    await drain_quote_workers(svc)
    assert adapter.submitted == []
    clock["now"] += timedelta(seconds=1)
    if mode == "flip_at_target":
        svc._arm_cw_flip_pending((account, SYMBOL), bar_time_ms=int((clock["now"]-timedelta(seconds=10)).timestamp()*1000),
            managed_row_id=bound_id)
    captured = []
    if mode == "flip_after_capture":
        original_capture = svc._capture_cw_target_decision
        def flip_after_bound_peak(acct, symbol, quote):
            original_capture(acct, symbol, quote)
            if svc._v2_eod_oco_transitioned and (acct, symbol) in svc._cw_target_decisions:
                captured.append(svc._cw_target_decisions[(acct, symbol)])
                # Controlled post-capture quote drop plus a bound bar-close flip.
                svc._latest_quotes_by_symbol[symbol] = {"bid": 2.50, "ask": 2.51, "received_at": clock["now"]}
                svc._arm_cw_flip_pending((acct, symbol), bar_time_ms=int((clock["now"]-timedelta(seconds=10)).timestamp()*1000),
                    managed_row_id=bound_id)
        monkeypatch.setattr(svc, "_capture_cw_target_decision", flip_after_bound_peak)
    produced_at = clock["now"] - timedelta(seconds=6) if mode == "stale" else clock["now"]
    quote = tick({"now": produced_at}, 2.64)
    await svc._handle_quote_tick_event(quote)
    await drain_quote_workers(svc)
    if mode == "stale":
        assert adapter.submitted == [] and (account, SYMBOL) not in svc._cw_target_decisions
        return
    request, = adapter.submitted
    assert request.quantity == 1 and request.order_type == "limit" and request.metadata["session"] == "PM"
    if mode == "flip_after_capture":
        assert captured and request.reason.endswith("CW_FLIP")
    else:
        # The unchanged shared helper gives target precedence at/above target, even with a flip.
        assert request.reason.endswith("CW_TARGET") and request.metadata["limit_price"] == "2.62"
        assert Decimal(request.metadata["cw_target_entry_price"]) == Decimal("2.49")
        assert request.metadata["cw_target_quantity"] == "1" and request.metadata["cw_target_bound_row_id"] == bound_id
        webull_wire, _, _, refusal = WebullBrokerAdapter._prepare_single_leg_prices(
            request=request, order_type="LIMIT", limit_price=Decimal(request.metadata["limit_price"]), stop_price=None)
        schwab_wire = Decimal(str(SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)._build_order_payload(request)["price"]))
        assert refusal is None and webull_wire >= Decimal("2.6145") and schwab_wire >= Decimal("2.6145")
    await drain_recovery(svc)
