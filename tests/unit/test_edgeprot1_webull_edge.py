"""E1: real edge/attach/ownership chain, isolated broker boundary and card-derived replay."""
from __future__ import annotations

from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from tests.unit.oms_sqlite_fixtures import private_sqlite_engine
from tests.unit.oms_sqlite_fixtures import own_service

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.protocols import (
    BrokerPositionSnapshot, ExecutionReport, ExitPairReleaseResult,
)
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.models import Base, BrokerOrder, OmsManagedPosition, TradeIntent
from project_mai_tai.oms import service as module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

CASE = json.loads((Path(__file__).parents[1] / "fixtures/edgeprot1_mndr_edge.json").read_text())
ACCT, SYM = CASE["account"], CASE["symbol"]
SCHWAB = "live:schwab_1m_v2"
EDGE = datetime.fromisoformat("2026-10-12T09:30:00-04:00")
NOW = datetime.fromisoformat(CASE["edge_attempt_at"]).astimezone(UTC)


class _Redis:
    async def xadd(self, *_args, **_kwargs):
        return "1-0"


class _Wire(WebullBrokerAdapter):
    """Exercise submit_order and the real combo builder; replace only the broker boundary."""
    def __init__(self):
        self.accounts_by_name = {ACCT: SimpleNamespace(account_id="LOCAL-REPLAY")}
        self.settings = SimpleNamespace(webull_native_bracket_enabled=False)
        self.requests, self.legs, self.releases = [], [], []
        self.reject = False
        self.release_outcome = "released"

    async def fetch_armed_native_oco_symbols(self, acct, symbols):
        return set()  # routing.py's actual result for Webull's absent capability

    async def list_account_positions(self, acct):
        return [BrokerPositionSnapshot(acct, SYM, Decimal("120"), Decimal("2.49"))]

    def _submit_exit_pair_blocking(self, account, request):
        self.requests.append(request)
        legs = self._build_exit_only_pair_payload(request)
        if self.reject:
            return [ExecutionReport(event_type="rejected", client_order_id=request.client_order_id,
                                    reason="controlled broker rejection")]
        self.legs.append(legs)
        return [ExecutionReport(event_type="accepted", client_order_id=request.client_order_id,
                                broker_order_id="LOCAL-COMBO", symbol=request.symbol, side="sell",
                                intent_type="close", quantity=request.quantity)]

    def _submit_blocking(self, account, request):
        self.requests.append(request)
        return [ExecutionReport(event_type="rejected", client_order_id=request.client_order_id,
                                symbol=request.symbol, side="sell", intent_type="close",
                                quantity=request.quantity,
                                reason="417 ILLEGAL_PARAMETER limit_price value:null is blank")]

    async def release_exit_pair_for_close(self, **kw):
        self.releases.append(kw)
        return ExitPairReleaseResult(outcome=self.release_outcome)


@pytest.fixture(autouse=True)
def owned_clock(monkeypatch):
    monkeypatch.setattr(module, "utcnow", lambda: NOW)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", _Clock)


@pytest.fixture
def replay(monkeypatch):
    engine = private_sqlite_engine(connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = own_service(OmsRiskService(
        settings=Settings(_env_file=None, oms_adapter="simulated",
                          oms_v2_exit_management_enabled=True,
                          oms_v2_rth_edge_bracket_enabled=True,
                          oms_v2_cw_target_pct=CASE["target_pct"],
                          oms_v2_cw_hard_stop_pct=CASE["stop_pct"],
                          strategy_schwab_1m_v2_webull_account_name=ACCT),
        redis_client=_Redis(), session_factory=factory,
    ))
    object.__setattr__(service.settings, "oms_webull_protect_attempts", 1)
    object.__setattr__(service.settings, "oms_webull_protect_interval_seconds", 0)
    with factory() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2")
        account = service.store.ensure_broker_account(session, ACCT,
                                                     provider="webull", environment="live")
        intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id, symbol=SYM,
                             side="buy", intent_type="open", quantity=Decimal(CASE["quantity"]),
                             reason="card-derived MNDR replay", status="filled", payload={})
        session.add(intent)
        session.flush()
        order = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id,
                            broker_account_id=account.id, client_order_id="MNDR-OWNED-ENTRY",
                            broker_order_id="LOCAL-ENTRY", symbol=SYM, side="buy", status="filled",
                            quantity=Decimal(CASE["quantity"]), order_type="STOP_LIMIT",
                            time_in_force="day", submitted_at=NOW,
                            payload={"fanout_leg": "webull", "native_oco_bracket": "false"})
        session.add(order)
        session.flush()
        service.store.create_managed_position(
            session, strategy_code=strategy.code, broker_account_name=ACCT, symbol=SYM,
            entry_price=Decimal(CASE["entry_price"]), quantity=int(CASE["quantity"]),
            entry_time=datetime.fromisoformat(CASE["entry_at"]).astimezone(UTC),
            entry_order_id=order.id, entry_client_order_id=order.client_order_id,
        )
        session.commit()
    service._managed_v2_symbols = {(ACCT, SYM)}
    wire = _Wire()
    service.broker_adapter = wire

    async def quote(**kw):
        return {"bid_price": 2.49, "ask_price": 2.50, "last_price": 2.49}

    monkeypatch.setattr(service, "_fetch_quote_for_order", quote)
    yield service, factory, wire
    engine.dispose()


def _entry(session):
    return session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == "MNDR-OWNED-ENTRY"))


def _row(session):
    return session.scalar(select(OmsManagedPosition).where(OmsManagedPosition.symbol == SYM))


async def _emit(service, **kw):
    return await service._emit_v2_rth_edge_bracket(acct=ACCT, symbol=SYM, edge_et=EDGE, **kw)


@pytest.mark.asyncio
async def test_mndr_edge_submits_core_pair_with_owned_durable_handle_and_target_floor(replay):
    service, factory, wire = replay
    await service._v2_rth_edge_bracket()
    assert len(wire.legs) == 1, "MNDR edge must route to a pair, never a blank standalone LIMIT"
    request, legs = wire.requests[0], wire.legs[0]
    assert request.quantity == Decimal("120")
    assert request.metadata["webull_exit_only_pair"] == "true"
    assert "exit_only_oco" not in request.metadata
    assert request.metadata["market_session"] == "RTH"
    assert request.metadata["bracket_target_price"] == CASE["raw_target"]
    assert request.metadata["bracket_stop_price"] == CASE["raw_stop"]
    assert {leg["combo_type"] for leg in legs} == {"STOP_PROFIT", "STOP_LOSS"}
    assert all(leg["quantity"] == "120" and leg["support_trading_session"] == "CORE"
               and leg["time_in_force"] == "DAY" and leg["side"] == "SELL" for leg in legs)
    target, stop = legs
    assert target["order_type"] == "LIMIT" and target["limit_price"] == CASE["wire_target"]
    assert stop["order_type"] == "STOP_LOSS" and stop["stop_price"] == CASE["wire_stop"]
    assert target["client_order_id"] != stop["client_order_id"]
    with factory() as session:
        entry = service._find_oco_entry_order(session, ACCT, SYM)
        assert entry.payload["webull_protect_base_client_order_id"] == request.client_order_id
        assert entry.payload[service._WEBULL_PROTECT_STATE_KEY] == "resting"
        assert service._oco_exit_base_for_entry(entry, broker_account_name=ACCT,
                                                symbol=SYM) == request.client_order_id
        binding = service._webull_protect_entry_binding[(ACCT, SYM)]
        assert binding == (str(_row(session).id), str(entry.id), entry.client_order_id,
                           request.client_order_id)
    # Conditional historical-print model: a resting LIMIT can fill at its limit or better.
    # This proves the floor and opportunity, not broker acceptance/fill or queue priority.
    limit = Decimal(target["limit_price"])
    assert Decimal(CASE["first_target_print_price_floor"]) >= limit
    modeled_fill = limit
    assert modeled_fill >= Decimal(CASE["raw_target"])
    assert sum(print_["size"] for print_ in CASE["target_opportunities"]
               if Decimal(print_["price"]) >= limit) >= request.quantity
    assert NOW < datetime.fromisoformat(CASE["first_target_print_at"])
    assert (modeled_fill - Decimal(CASE["entry_price"])) * request.quantity == Decimal("15.60")
    await service._v2_rth_edge_bracket()
    assert len(wire.requests) == 1


@pytest.mark.asyncio
async def test_edge_pair_is_addressable_after_restart_and_release_must_be_confirmed(replay):
    service, factory, wire = replay
    assert await _emit(service)
    base = wire.requests[0].client_order_id
    service._webull_protect_base.clear()
    service._webull_protect_entry_binding.clear()
    with factory() as session:
        assert await _emit(service)  # durable handle also prevents a second attach
        wire.release_outcome = "reserved"
        result = await service._release_exit_reservation_before_close(
            session=session, broker_account_name=ACCT, symbol=SYM)
        assert result.outcome == "reserved"
        assert (ACCT, SYM) not in service._exit_reservation_released
        service._clear_exit_reservation_retry_state((ACCT, SYM))
        wire.release_outcome = "released"
        result = await service._release_exit_reservation_before_close(
            session=session, broker_account_name=ACCT, symbol=SYM)
        assert result.outcome == "released"
    assert len(wire.requests) == 1
    assert all(call["base_client_order_id"] == base for call in wire.releases)


@pytest.mark.asyncio
async def test_working_software_exit_defers_without_latching_and_can_arm_after_clear(replay):
    service, factory, wire = replay
    with factory() as session:
        entry = _entry(session)
        sell = BrokerOrder(intent_id=entry.intent_id, strategy_id=entry.strategy_id,
                           broker_account_id=entry.broker_account_id, client_order_id="WORKING-SELL",
                           symbol=SYM, side="sell", status="accepted", quantity=Decimal("120"),
                           order_type="LIMIT", time_in_force="day", submitted_at=NOW, payload={})
        session.add(sell)
        session.commit()
    await service._v2_rth_edge_bracket()
    assert wire.requests == []
    assert not service._v2_rth_edge_bracket_done
    assert all(n == 0 for n in service._v2_rth_edge_bracket_attempts.values())
    with factory() as session:
        sell = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == "WORKING-SELL"))
        sell.status = "cancelled"
        session.commit()
    service._v2_rth_edge_bracket_last_try.clear()
    await service._v2_rth_edge_bracket()
    assert len(wire.legs) == 1


@pytest.mark.parametrize("state", ["resting", "attach_failed"])
@pytest.mark.asyncio
async def test_known_pair_is_not_duplicated_even_if_attach_state_is_uncertain(replay, state):
    service, factory, wire = replay
    assert await _emit(service)
    with factory() as session:
        entry = _entry(session)
        entry.payload = {**entry.payload, service._WEBULL_PROTECT_STATE_KEY: state}
        session.commit()
    assert await _emit(service, rearm=True)
    assert len(wire.requests) == 1


@pytest.mark.asyncio
async def test_unbound_memory_pair_or_foreign_entry_cannot_authorize_an_attach(replay):
    service, factory, wire = replay
    service._webull_protect_base[(ACCT, SYM)] = "UNBOUND-PAIR"
    assert await _emit(service) is False
    service._webull_protect_base.clear()
    with factory() as session:
        row = _row(session)
        row.entry_client_order_id = "FOREIGN-ENTRY"
        session.commit()
    assert await _emit(service) is False
    assert wire.requests == []


@pytest.mark.asyncio
async def test_existing_attach_and_edge_coalesce_to_one_pair(replay, monkeypatch):
    service, _, wire = replay
    entered, finish = asyncio.Event(), asyncio.Event()
    original = service._broker_symbol_position_state

    async def paused(*args, **kw):
        entered.set()
        await finish.wait()
        return await original(*args, **kw)

    monkeypatch.setattr(service, "_broker_symbol_position_state", paused)
    existing = service._spawn_webull_protection(
        broker_account_name=ACCT, symbol=SYM, quantity=120, entry_price=2.49,
        strategy_code="schwab_1m_v2", entry_client_order_id="MNDR-OWNED-ENTRY")
    await asyncio.wait_for(entered.wait(), 2)
    edge = asyncio.create_task(_emit(service))
    await asyncio.sleep(0)
    finish.set()
    assert await edge and await existing
    assert len(wire.requests) == 1


@pytest.mark.asyncio
async def test_rejected_attach_does_not_latch_edge_or_create_a_handle(replay):
    service, factory, wire = replay
    wire.reject = True
    await service._v2_rth_edge_bracket()
    assert not service._v2_rth_edge_bracket_done
    assert not service._webull_protect_base
    assert list(service._v2_rth_edge_bracket_attempts.values()) == [1]
    with factory() as session:
        assert "webull_protect_base_client_order_id" not in _entry(session).payload
    wire.reject = False
    service._v2_rth_edge_bracket_last_try.clear()
    await service._v2_rth_edge_bracket()
    assert len(wire.legs) == 1


@pytest.mark.asyncio
async def test_confirmed_release_allows_rearm_and_clears_old_release_latch(replay):
    service, factory, wire = replay
    assert await _emit(service)
    with factory() as session:
        assert service._mark_webull_protect_state(session, ACCT, SYM, "released")
        session.commit()
    service._exit_reservation_released.add((ACCT, SYM))
    assert await _emit(service, rearm=True)
    assert len(wire.legs) == 2
    assert wire.requests[0].client_order_id != wire.requests[1].client_order_id
    assert (ACCT, SYM) not in service._exit_reservation_released


@pytest.mark.asyncio
async def test_edge_waiter_cancellation_does_not_cancel_the_existing_attach(replay, monkeypatch):
    service, _, wire = replay
    entered, finish = asyncio.Event(), asyncio.Event()
    original = service._broker_symbol_position_state

    async def paused(*args, **kw):
        entered.set()
        await finish.wait()
        return await original(*args, **kw)

    monkeypatch.setattr(service, "_broker_symbol_position_state", paused)
    edge = asyncio.create_task(_emit(service))
    await asyncio.wait_for(entered.wait(), 2)
    attach = service._webull_protect_inflight[(ACCT, SYM)]
    edge.cancel()
    with pytest.raises(asyncio.CancelledError):
        await edge
    assert not attach.done()
    finish.set()
    assert await attach
    assert len(wire.legs) == 1


@pytest.mark.asyncio
async def test_regular_fill_and_premarket_are_left_to_existing_protection_paths(replay, monkeypatch):
    service, factory, wire = replay
    with factory() as session:
        _row(session).entry_time = EDGE.astimezone(UTC) + timedelta(seconds=1)
        session.commit()
    assert await _emit(service)
    assert wire.requests == []
    monkeypatch.setattr(module, "_is_regular_market_session", lambda now=None: False)
    assert await _emit(service, rearm=True) is False
    assert wire.requests == []


@pytest.mark.asyncio
async def test_schwab_retains_exact_exit_only_oco_shape(replay):
    service, factory, wire = replay
    with factory() as session:
        account = service.store.ensure_broker_account(session, SCHWAB,
                                                     provider="schwab", environment="live")
        entry = _entry(session)
        row = _row(session)
        row.broker_account_name = SCHWAB
        entry.broker_account_id = account.id
        entry.payload = {}
        session.commit()

    async def submit(request):
        wire.requests.append(request)
        return [ExecutionReport(event_type="accepted", client_order_id=request.client_order_id,
                                symbol=SYM, side="sell", intent_type="close",
                                quantity=request.quantity, broker_order_id="LOCAL-SCHWAB-OCO")]

    service.broker_adapter = SimpleNamespace(submit_order=submit)
    await service._emit_v2_rth_edge_bracket(acct=SCHWAB, symbol=SYM, edge_et=EDGE)
    request = wire.requests[0]
    assert request.metadata == {
        "exit_only_oco": "true", "session": "NORMAL", "bracket_target_price": "2.61",
        "bracket_stop_price": "2.29", "oms_v2_rth_edge_bracket": "true",
        "entry_price": "2.4900", "time_in_force": "day",
    }
    assert request.order_type == "limit" and request.broker_account_name == SCHWAB
