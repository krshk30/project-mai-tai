"""Lane B controlled replays: recorded inputs, never invented broker receipts."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, ExitPairReleaseResult, OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.models import BrokerOrder, Fill, OmsManagedPosition
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.oms import service as module
from project_mai_tai.settings import Settings
from tests.unit.test_v2_cw_managed_exit import ACCT, SYM, WEBULL_ACCT, _arm, _make_sf, _svc
from tests.unit.managed_entry_fixtures import set_protect_base


class Broker:
    def __init__(self):
        self.requests = []

    async def submit_order(self, request):
        self.requests.append(request)
        cancelled = request.intent_type == "cancel"
        return [ExecutionReport(
            event_type="cancelled" if cancelled else "accepted", origin="broker",
            client_order_id=request.client_order_id, symbol=request.symbol, side=request.side,
            intent_type=request.intent_type, quantity=request.quantity,
            broker_order_id=request.metadata.get("broker_order_id") or f"controlled-{request.client_order_id}",
            metadata=dict(request.metadata),
        )]


def setup(monkeypatch, account=ACCT, *, entry="2.49", session="NORMAL"):
    sf = _make_sf()
    broker = Broker()
    svc = _svc(sf, dual=True, adapter=broker)
    svc._cw_target_pct, svc._cw_stop_pct = 5.0, 8.0
    monkeypatch.setattr(module, "_extended_hours_session", lambda now=None: None if session == "NORMAL" else session)
    monkeypatch.setattr(module, "_is_regular_market_session", lambda now=None: session == "NORMAL")
    monkeypatch.setattr(svc, "_market_is_fillable", lambda: True)
    _arm(svc, sf, account=account, entry=float(entry), qty=120)
    svc._managed_v2_symbols = {(account, SYM)}
    return svc, sf, broker


def quote(bid, *, at=None):
    return QuoteTickEvent(source_service="controlled-edgeprot1", produced_at=at or datetime.now(UTC),
        payload=QuoteTickPayload(symbol=SYM, bid_price=Decimal(str(bid)), ask_price=Decimal(str(bid)) + Decimal(".01")))


async def drain(svc):
    while svc.__dict__.get("_symbol_tick_work", {}):
        await asyncio.gather(*tuple(svc._symbol_tick_work.values()))


def current_settings():
    flags = Path(__file__).resolve().parents[2] / "docs/review-artifacts/edgeprot1-lane-b/live-flags.env"
    return Settings(_env_file=flags, strategy_schwab_1m_v2_account_name=ACCT,
        strategy_schwab_1m_v2_webull_account_name=WEBULL_ACCT)


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
@pytest.mark.parametrize("session", ["AM", "NORMAL", "PM"])
@pytest.mark.parametrize("entry,bid,floor", [("2.49", "2.64", "2.62"), ("1.53", "1.61", "1.61"), ("0.25", "0.27", "0.2625")])
async def test_target_floor_survives_both_adapter_wire_prices(monkeypatch, account, session, entry, bid, floor):
    svc, _, broker = setup(monkeypatch, account, entry=entry, session=session)
    svc._latest_quotes_by_symbol[SYM] = {"bid": float(bid), "ask": float(bid) + .01, "received_at": datetime.now(UTC)}
    await svc._evaluate_v2_managed_exit(account, SYM)
    request, = broker.requests
    assert request.order_type == "limit"
    assert request.metadata["limit_price"] == floor
    raw_target = Decimal(entry) * Decimal("1.05")
    webull_wire, _, _, error = WebullBrokerAdapter._prepare_single_leg_prices(
        request=request, order_type="LIMIT", limit_price=Decimal(request.metadata["limit_price"]), stop_price=None)
    assert error is None
    schwab_wire = Decimal(str(SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)._build_order_payload(request)["price"]))
    assert webull_wire >= raw_target and schwab_wire >= raw_target


@pytest.mark.parametrize("raw,floor,existing", [("2.6145", "2.62", "2.61"), ("1.6065", "1.61", "1.61"), ("0.26251", "0.2626", "0.2625")])
def test_webull_attach_target_floor_keeps_other_native_rounding(raw, floor, existing):
    request = OrderRequest(client_order_id="controlled-native-target", broker_account_name=WEBULL_ACCT,
        strategy_code="schwab_1m_v2", symbol=SYM, side="sell", intent_type="close", quantity=Decimal("120"), reason="controlled target-floor replay",
        metadata={"bracket_target_price": raw, "bracket_stop_price": "2.2908",
            "reference_price": "2.49", "bracket_entry_type": "LIMIT", "limit_price": "2.49"})
    webull = WebullBrokerAdapter.__new__(WebullBrokerAdapter)
    target, stop = webull._build_exit_only_pair_payload(request)
    assert target["limit_price"] == floor
    assert stop["order_type"] == "STOP_LOSS" and stop["stop_price"] == "2.29"
    _, native_target, native_stop = webull._build_combo_payload(request)
    assert native_target["limit_price"] == existing and native_stop["stop_price"] == "2.29"
    schwab = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
    assert Decimal(str(schwab._bracket_exit_leg(request, order_type="LIMIT", price=Decimal(raw))["price"])) == Decimal(existing)
    assert schwab._bracket_exit_leg(request, order_type="STOP", price=Decimal("2.2908"))["stopPrice"] == 2.29


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
async def test_serial_quote_wait_does_not_block_new_quote(monkeypatch, account):
    svc, _, _ = setup(monkeypatch, account)
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def evaluate(acct, symbol):
        seen.append((acct, svc._latest_quotes_by_symbol[symbol]["bid"]))
        if len(seen) == 1:
            entered.set()
            await release.wait()  # controlled protection/DB wait on one leg

    monkeypatch.setattr(svc, "_evaluate_v2_managed_exit", evaluate)
    first = asyncio.create_task(svc._handle_quote_tick_event(quote("2.52")))
    await entered.wait()
    try:
        await asyncio.wait_for(asyncio.shield(first), .05)
        await svc._handle_quote_tick_event(quote("2.64"))
        assert len(svc._symbol_tick_work) == 1
        assert svc._latest_quotes_by_symbol[SYM]["bid"] == 2.64
    finally:
        release.set()
        await first
        await drain(svc)
    assert seen == [(account, 2.52), (account, 2.64)]


@pytest.mark.asyncio
async def test_named_five_second_guard_refuses_late_decision(monkeypatch):
    svc, _, broker = setup(monkeypatch)
    svc._latest_quotes_by_symbol[SYM] = {"bid": 2.64, "received_at": datetime.now(UTC) - timedelta(seconds=6)}
    await svc._evaluate_v2_managed_exit(ACCT, SYM)
    assert broker.requests == []


@pytest.mark.asyncio
async def test_quote_arriving_during_slow_row_read_is_used_for_decision(monkeypatch):
    svc, _, broker = setup(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = svc._run_db
    async def delayed(work, **kwargs):
        entered.set()
        await release.wait()
        return await original(work, **kwargs)
    monkeypatch.setattr(svc, "_run_db", delayed)
    await svc._handle_quote_tick_event(quote("2.52"))
    await entered.wait()
    await svc._handle_quote_tick_event(quote("2.64"))
    release.set()
    await drain(svc)
    request, = broker.requests
    assert request.metadata["limit_price"] == "2.62"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
@pytest.mark.parametrize("session", ["AM", "PM"])
async def test_transient_target_cross_captured_before_slow_row_read_returns(monkeypatch, account, session):
    svc, _, broker = setup(monkeypatch, account, session=session)
    entered = asyncio.Event()
    original, returned_at = svc._run_db, []
    async def slow_read(work, **kwargs):
        if not entered.is_set():
            entered.set()
            await asyncio.sleep(.05)
        result = await original(work, **kwargs)
        returned_at.append(datetime.now(UTC))
        return result
    monkeypatch.setattr(svc, "_run_db", slow_read)
    await svc._handle_quote_tick_event(quote("2.52"))
    await entered.wait()
    await svc._handle_quote_tick_event(quote("2.64"))
    await asyncio.sleep(.01)
    await svc._handle_quote_tick_event(quote("2.52"))
    await drain(svc)
    request, = broker.requests  # Required: capture the transient cross, even if its limit cannot fill.
    assert request.order_type == "limit" and request.metadata["limit_price"] == "2.62"
    assert datetime.fromisoformat(request.metadata["cw_decided_at"]) < returned_at[0]
    await svc._handle_quote_tick_event(quote("2.50"))
    await drain(svc)
    assert broker.requests == [request]


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
async def test_all_on_current_proc_flags_controlled_mndr_replay(monkeypatch, account):
    svc, sf, broker = setup(monkeypatch, account)
    settings = current_settings()
    assert settings.oms_v2_exit_release_reservation_enabled
    assert settings.oms_v2_rth_edge_bracket_enabled
    assert settings.oms_native_oco_stand_down_enabled
    assert settings.oms_v2_cw_target_pct == 5 and settings.oms_v2_cw_hard_stop_pct == 8
    svc = module.OmsRiskService(settings, redis_client=svc.redis, session_factory=sf, broker_adapter=broker)
    svc._rehydrate_managed_v2_symbols()
    monkeypatch.setattr(svc, "_market_is_fillable", lambda: True)
    # Recorded archive reception, explicitly substituted for missing OMS arrival time.
    now = datetime(2026, 10, 12, 13, 30, 17, 456869, tzinfo=UTC)
    monkeypatch.setattr(module, "utcnow", lambda: now)
    await svc._handle_quote_tick_event(quote("2.62", at=now))
    await drain(svc)
    request, = broker.requests
    assert request.order_type == "limit" and request.metadata["limit_price"] == "2.62"
    assert request.metadata["cw_decided_at"].startswith("2026-10-12T13:30:17.456869")


@pytest.mark.asyncio
async def test_clwt_recorded_stop_fill_resolves_without_software_sell(monkeypatch):
    svc, sf, broker = setup(monkeypatch, WEBULL_ACCT, entry="2.24")
    set_protect_base(svc, sf, WEBULL_ACCT, SYM, "controlled-clwt-pair")
    with sf.begin() as s:
        row = s.scalar(select(OmsManagedPosition).where(OmsManagedPosition.broker_account_name == WEBULL_ACCT))
        row.current_quantity = row.initial_quantity = 133
        entry = s.scalar(select(BrokerOrder).where(BrokerOrder.side == "buy", BrokerOrder.symbol == SYM))
        entry.quantity = 133
    async def filled_pair(**kwargs):
        return ExitPairReleaseResult(outcome="resolved_by_fill", reports=(ExecutionReport(
            event_type="filled", origin="broker", client_order_id="controlled-clwt-pairS",
            broker_order_id="controlled-clwt-stop", symbol=SYM, side="sell", intent_type="close",
            quantity=Decimal("133"), filled_quantity=Decimal("133"), fill_price=Decimal("2.03"),
            reported_at=datetime(2026, 10, 12, 13, 44, 2, 377000, tzinfo=UTC)),))
    monkeypatch.setattr(broker, "release_exit_pair_for_close", filled_pair, raising=False)
    svc._latest_quotes_by_symbol[SYM] = {"bid": 2.03, "received_at": datetime.now(UTC)}
    await svc._evaluate_v2_managed_exit(WEBULL_ACCT, SYM)
    assert broker.requests == []
    with sf() as s:
        fill, = s.scalars(select(Fill).where(Fill.side == "sell")).all()
        assert fill.price == Decimal("2.03") and fill.quantity == 133


@pytest.mark.asyncio
async def test_decision_precedes_webull_protection_wait_and_submission(monkeypatch):
    svc, _, broker = setup(monkeypatch, WEBULL_ACCT)
    entered, release = asyncio.Event(), asyncio.Event()
    original = svc._prepare_confirmation_webull_leg

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(svc, "_prepare_confirmation_webull_leg", delayed)
    svc._latest_quotes_by_symbol[SYM] = {"bid": 2.64, "received_at": datetime.now(UTC)}
    before_wait = datetime.now(UTC)
    task = asyncio.create_task(svc._evaluate_v2_managed_exit(WEBULL_ACCT, SYM))
    await entered.wait()
    await asyncio.sleep(.02)
    released_at = datetime.now(UTC)
    assert broker.requests == []
    release.set()
    await task
    request, = broker.requests
    decided = datetime.fromisoformat(request.metadata["cw_decided_at"])
    submitted = datetime.fromisoformat(request.metadata["cw_submitted_at"])
    assert before_wait <= decided < released_at <= submitted


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [ACCT, WEBULL_ACCT])
@pytest.mark.parametrize("session", ["AM", "NORMAL", "PM"])
@pytest.mark.parametrize("safety,bid", [("CW_HARD_STOP", 2.20), ("CW_FLIP", 2.50), ("CW_FLOOR", 2.50)])
async def test_unfilled_target_no_chase_safety_cancels_before_one_sell(monkeypatch, account, session, safety, bid):
    svc, sf, broker = setup(monkeypatch, account, session=session)
    svc._latest_quotes_by_symbol[SYM] = {"bid": 2.64, "received_at": datetime.now(UTC)}
    await svc._evaluate_v2_managed_exit(account, SYM)
    target, = broker.requests
    assert target.order_type == "limit"
    svc._latest_quotes_by_symbol[SYM] = {"bid": 2.61, "received_at": datetime.now(UTC)}
    await svc._evaluate_v2_managed_exit(account, SYM)
    assert broker.requests == [target]
    with sf() as s:
        row = s.scalar(select(OmsManagedPosition).where(OmsManagedPosition.broker_account_name == account))
        row_id = str(row.id)
    if safety == "CW_FLIP":
        svc._arm_cw_flip_pending((account, SYM), bar_time_ms=int(datetime.now(UTC).timestamp() * 1000), managed_row_id=row_id)
    elif safety == "CW_FLOOR":
        svc._cw_floor_exit_enabled = True
        svc._cw_floor_armed.add((account, SYM))
        svc._cw_floor_pct = 1.0
    svc._latest_quotes_by_symbol[SYM] = {"bid": bid, "received_at": datetime.now(UTC)}
    await svc._evaluate_v2_managed_exit(account, SYM)
    assert [r.intent_type for r in broker.requests] == ["close", "cancel", "close"]
    assert broker.requests[-1].reason.endswith(safety)
    await svc._evaluate_v2_managed_exit(account, SYM)
    assert len(broker.requests) == 3
    with sf() as s:
        orders = s.scalars(select(BrokerOrder).where(BrokerOrder.side == "sell")).all()
        assert sum(o.status == "accepted" for o in orders) == 1


@pytest.mark.asyncio
async def test_quote_hotpath_225_per_second_60_seconds(monkeypatch):
    initial, sf, broker = setup(monkeypatch)
    svc = module.OmsRiskService(current_settings(), redis_client=initial.redis,
        session_factory=sf, broker_adapter=broker)
    svc._rehydrate_managed_v2_symbols()
    monkeypatch.setattr(svc, "_market_is_fillable", lambda: True)
    evaluations = 0
    original = svc._run_db
    async def slow_db(work, **kwargs):
        nonlocal evaluations
        evaluations += 1
        await asyncio.sleep(.05)
        return await original(work, **kwargs)
    monkeypatch.setattr(svc, "_run_db", slow_db)
    max_stall = 0.0
    stop = asyncio.Event()
    async def probe():
        nonlocal max_stall
        previous = monotonic()
        while not stop.is_set():
            await asyncio.sleep(.001)
            now = monotonic()
            max_stall = max(max_stall, (now - previous - .001) * 1000)
            previous = now
    meter = asyncio.create_task(probe())
    start = monotonic()
    try:
        for i in range(13500):
            await asyncio.sleep(max(0, start + i / 225 - monotonic()))
            await svc._handle_quote_tick_event(quote("2.52"))
            assert len(svc._symbol_tick_work) <= 1
        await asyncio.sleep(max(0, start + 60 - monotonic()))
    finally:
        stop.set()
        await meter
        await drain(svc)
    elapsed = monotonic() - start
    print(f"EDGEPROT1 quotes=13500 seconds={elapsed:.6f} rate={13500 / elapsed:.3f} stall_ms={max_stall:.3f} evaluations={evaluations}")
    assert elapsed >= 60 and 13500 / elapsed >= 200 and max_stall < 50
