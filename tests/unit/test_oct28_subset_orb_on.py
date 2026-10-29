"""Exact-subset ORB component: real MACD/session guards, controlled SDK only.

No historical/native replay or whole-service boot claim. The original ORBROUTE
fixture owns orb.sqlite; its two permissive guard stubs are removed below.
Main's coordinator supplies the other subset components and runtime authority.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import (
    BrokerOrder, OmsManagedPosition, StrategyBarHistory, VirtualPosition,
)
from project_mai_tai.momentum_live_sec import CountryRecord, CountryRouter, CountryStore
from project_mai_tai.oms import service as oms_module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.orb_country_route import select_orb_country_route
from project_mai_tai.orb_schwab_macd import MacdVerdict, schwab_completed_bar_macd_gate
from project_mai_tai.orb_schwab_order_route import (
    build_orb_schwab_open_intent, build_orb_schwab_reprice_intent,
    orb_schwab_intent_refusal,
)
from project_mai_tai.settings import Settings
from tests.unit.test_orbroute1_oms_chain import (
    OPEN, SCHWAB, WEBULL, Redis, setup as setup,
)


PROFILE = {
    "orb_country_semantic_admission_enabled": True,
    "scanner_confirmed_capture_session_day_enabled": True,
    "strategy_schwab_1m_v2_keepwatch1_enabled": True,
    "strategy_schwab_1m_v2_brokerdecides1_enabled": True,
    "scanner_nuweref1_enabled": False,
}


class SessionFactsRedis(Redis):
    """Exact controlled Redis values, not a replacement for the session guard."""

    def __init__(self):
        self.facts = {}
        self.reads = []

    async def get(self, key):
        assert isinstance(key, str)
        self.reads.append(key)
        if key not in self.facts:
            raise AssertionError(f"unbound_controlled_redis_key:{key}")
        value = self.facts[key]
        assert value is None or isinstance(value, str)
        return value


@pytest.fixture
def subset(setup, monkeypatch):
    initial, factory, broker = setup
    # Restore the real public function before constructing or dispatching the
    # subset service; no wrapper returning an ALLOWED verdict remains.
    monkeypatch.setattr(oms_module, "schwab_completed_bar_macd_gate", schwab_completed_bar_macd_gate)
    del initial.__dict__["_get_session_symbol_block_reason"]
    assert initial._get_session_symbol_block_reason.__func__ is OmsRiskService._get_session_symbol_block_reason
    redis = SessionFactsRedis()
    settings = initial.settings.model_copy(update=PROFILE)
    service = OmsRiskService(settings=settings, redis_client=redis,
        session_factory=factory, broker_adapter=broker)
    assert "_get_session_symbol_block_reason" not in service.__dict__
    assert service._get_session_symbol_block_reason.__func__ is OmsRiskService._get_session_symbol_block_reason
    assert oms_module.schwab_completed_bar_macd_gate is schwab_completed_bar_macd_gate
    for name, value in PROFILE.items():
        assert getattr(service.settings, name) is value
    clock = [OPEN]

    def now():
        return clock[0]

    monkeypatch.setattr(oms_module, "utcnow", now)
    for account in (SCHWAB, WEBULL):
        for symbol in ("ABC", "SAFE"):
            redis.facts[service._session_symbol_block_key(account_name=account, symbol=symbol)] = None
    # Only adapter receipt clocks follow the explicitly supplied model clock.
    # The original typed broker's request recording and order numbering remain.
    submit = broker.submit_order

    async def submit_at_clock(request):
        return [replace(report, reported_at=clock[0]) for report in await submit(request)]

    broker.submit_order = submit_at_clock
    return service, factory, broker, redis, clock


def add_history(factory, symbol="ABC", *, first=0, count=40):
    """Synthetic rising Schwab-shaped closed bars; actual gate computes MACD."""
    with factory.begin() as session:
        for index in range(first, first + count):
            close = Decimal("4") + Decimal(index) * Decimal("0.01")
            session.add(StrategyBarHistory(strategy_code="schwab_1m_v2", symbol=symbol,
                interval_secs=60, bar_time=OPEN - timedelta(minutes=40 - index),
                open_price=close, high_price=close, low_price=close, close_price=close,
                volume=100, source="rest"))


def country(factory, symbol, is_us, stamp):
    CountryStore(factory).put(CountryRecord(symbol, "0000000001" if is_us is not None else None,
        "NY" if is_us else "A6" if is_us is False else None,
        "DE" if is_us else "A6" if is_us is False else None,
        is_us, stamp, "READY" if is_us is not None else "INCOMPLETE", OPEN.date()))


def plan(subset, *, is_us=True, symbol="ABC", history=True):
    service, factory, _broker, _redis, _clock = subset
    if history:
        add_history(factory, symbol)
        verdict, reason, value = schwab_completed_bar_macd_gate(factory, symbol, OPEN)
        assert verdict is MacdVerdict.ALLOWED and reason == "nonnegative" and value >= 0
    country(factory, symbol, is_us, OPEN - timedelta(days=1))
    router = CountryRouter(CountryStore(factory))
    router._records = router.store.load()
    selected = select_orb_country_route(router, symbol, OPEN,
        schwab_account=SCHWAB, webull_account=WEBULL)
    event = build_orb_schwab_open_intent(service.settings, symbol, Decimal(5), selection=selected)
    assert orb_schwab_intent_refusal(event, service.settings, OPEN) is None
    country(factory, symbol, is_us, OPEN - timedelta(hours=1))
    return event, selected


def test_subset_code_defaults_remain_off_and_constructed_profile_is_literal(subset):
    service, _factory, _broker, _redis, _clock = subset
    for flag in ("orb_country_semantic_admission_enabled", "scanner_confirmed_capture_session_day_enabled"):
        assert Settings.model_fields[flag].default is False
        assert getattr(service.settings, flag) is True
    assert service.settings.scanner_nuweref1_enabled is False


@pytest.mark.parametrize("is_us", [True, False])
def test_subset_stamp_only_open_then_legal_rth_replace_real_guards(subset, is_us):
    service, factory, broker, redis, clock = subset
    event, selected = plan(subset, is_us=is_us)
    original_stamp = event.payload.metadata["orb_country_as_of"]
    replacements = []

    async def sdk_replace(request, old_id):
        replacements.append((request, old_id))
        return ExecutionReport("accepted", request.client_order_id,
            broker_order_id="subset-replaced", symbol=request.symbol, side="buy",
            intent_type="open", quantity=request.quantity, reported_at=clock[0], origin="broker",
            metadata={**request.metadata, "orb_replace_confirmation": "confirmed"})

    broker.replace_bracket_order = sdk_replace

    async def exercise():
        await service.process_trade_intent(event)
        assert len(broker.sent) == 1
        with factory() as session:
            parent = session.scalar(select(BrokerOrder))
            parent_id, coid, native = parent.id, parent.client_order_id, parent.broker_order_id
            assert parent.payload["orb_country_as_of"] == original_stamp
        assert service._session_symbol_block_key(account_name=event.payload.broker_account_name,
            symbol="ABC") in redis.reads
        clock[0] = OPEN.replace(minute=30, second=10)
        add_history(factory, first=40, count=2)  # now-closed09:28 and09:29 only
        assert schwab_completed_bar_macd_gate(factory, "ABC", clock[0])[0] is MacdVerdict.ALLOWED
        stamp = clock[0] - timedelta(seconds=30)
        country(factory, "ABC", is_us, stamp)
        refreshed = replace(selected, country=replace(selected.country, as_of=stamp))
        reprice = build_orb_schwab_reprice_intent(service.settings, "ABC", Decimal(6), selection=refreshed)
        assert orb_schwab_intent_refusal(reprice, service.settings, clock[0]) is None
        broker.readback = AtrBuyReadback("cancelled_empty", "terminal_cancel_explicit_zero",
            Decimal(0), terminal_cancel=True, broker_status="CANCELLED", broker_order_id=native)
        await service.process_trade_intent(reprice)
        with factory() as session:
            parents = session.scalars(select(BrokerOrder).order_by(BrokerOrder.submitted_at)).all()
            original = session.get(BrokerOrder, parent_id)
            assert original.client_order_id == coid
            assert original.payload["orb_country_as_of"] == original_stamp
            assert original.quantity == Decimal(10)
            if is_us:
                assert len(parents) == 1 and parents[0].id == parent_id
                assert original.broker_order_id == "subset-replaced"
                assert len(replacements) == 1 and replacements[0][1] == native
                assert replacements[0][0].client_order_id == coid
                assert replacements[0][0].prewire_check is not None
                assert len(broker.sent) == 1 and len(broker.previews) == 2
            else:
                assert len(parents) == 2 and original.broker_order_id == native
                buys = [request for request in broker.sent if request.intent_type == "open"]
                cancels = [request for request in broker.sent if request.intent_type == "cancel"]
                assert len(buys) == 2 and len(cancels) == 1 and not replacements
                assert buys[-1].metadata["orb_replacement_of"] == str(parent_id)
                assert cancels[0].client_order_id == coid
                assert cancels[0].metadata["broker_order_id"] == native
        assert all(request.strategy_code == "orb_schwab" and request.quantity == Decimal(10)
                   and request.broker_account_name == event.payload.broker_account_name for request in broker.sent)

    asyncio.run(exercise())


@pytest.mark.parametrize("was_us,now_us", [(True, False), (False, True), (False, None), (None, False)])
def test_subset_real_route_or_ready_incomplete_change_refuses(subset, was_us, now_us):
    service, factory, broker, _redis, _clock = subset
    event, _selected = plan(subset, is_us=was_us)
    country(factory, "ABC", now_us, OPEN - timedelta(hours=1))
    asyncio.run(service.process_trade_intent(event))
    assert not broker.sent and not broker.previews


@pytest.mark.parametrize("is_us", [True, False])
@pytest.mark.parametrize("owner_kind", ["virtual", "managed"])
def test_subset_actual_foreign_owner_refuses_without_reparent(subset, is_us, owner_kind):
    service, factory, broker, _redis, _clock = subset
    event, _selected = plan(subset, is_us=is_us)
    with factory.begin() as session:
        owner = service.store.ensure_strategy(session, "schwab_1m_v2", name="v2", execution_mode="live")
        account = service.store.ensure_broker_account(session, event.payload.broker_account_name,
            provider="schwab" if is_us else "webull", environment="production")
        if owner_kind == "virtual":
            held = VirtualPosition(strategy_id=owner.id, broker_account_id=account.id,
                symbol="ABC", quantity=Decimal(91), average_price=Decimal(5))
        else:
            held = OmsManagedPosition(strategy_code="schwab_1m_v2",
                broker_account_name=account.name, symbol="ABC", status="open",
                entry_price=Decimal(5), original_quantity=91, current_quantity=91, entry_time=OPEN)
        session.add(held)
        session.flush()
        row_id = held.id
    asyncio.run(service.process_trade_intent(event))
    assert not broker.sent and not broker.previews
    with factory() as session:
        retained = session.get(type(held), row_id)
        assert (retained.quantity if owner_kind == "virtual" else retained.current_quantity) == 91
        assert not session.scalars(select(BrokerOrder)).all()


@pytest.mark.parametrize("is_us", [True, False])
def test_subset_only_semantic_flag_off_retains_exact_stamp_refusal(subset, is_us):
    service, _factory, broker, _redis, _clock = subset
    event, _selected = plan(subset, is_us=is_us)
    service.settings.orb_country_semantic_admission_enabled = False
    assert service.settings.scanner_confirmed_capture_session_day_enabled is True
    asyncio.run(service.process_trade_intent(event))
    assert not broker.sent and not broker.previews


@pytest.mark.parametrize("enabled", [False, True])
def test_subset_legacy_no_country_event_unchanged_by_semantic_switch(subset, enabled):
    service, factory, broker, _redis, _clock = subset
    add_history(factory)
    service.settings.orb_country_semantic_admission_enabled = enabled
    # Build the original legacy payload, without changing current service policy.
    legacy_producer = service.settings.model_copy(update={"orb_country_routing_enabled": False})
    event = build_orb_schwab_open_intent(legacy_producer, "ABC", Decimal(5))
    assert "orb_country_route" not in event.payload.metadata
    assert service.settings.orb_country_routing_enabled is True
    assert orb_schwab_intent_refusal(event, service.settings, OPEN) == "orb_country_route_required"
    asyncio.run(service.process_trade_intent(event))
    assert not broker.sent and not broker.previews


@pytest.mark.parametrize("missing", ["macd", "redis_block"])
def test_subset_real_missing_history_or_redis_block_refuses_zero_wire(subset, missing):
    service, factory, broker, redis, _clock = subset
    event, _selected = plan(subset, history=missing != "macd")
    key = service._session_symbol_block_key(account_name=SCHWAB, symbol="ABC")
    if missing == "macd":
        assert schwab_completed_bar_macd_gate(factory, "ABC", OPEN)[0] is MacdVerdict.BAR_NOT_YET
    else:
        redis.facts[key] = "CONTROLLED_SESSION_BLOCK"
        assert asyncio.run(service._get_session_symbol_block_reason(account_name=SCHWAB,
            symbol="ABC")) == "CONTROLLED_SESSION_BLOCK"
    result = asyncio.run(service.process_trade_intent(event))
    assert not broker.sent and not broker.previews
    if missing == "redis_block":
        assert key in redis.reads and result
        assert result[0].payload.reason == "CONTROLLED_SESSION_BLOCK"


def test_subset_one_blocked_symbol_does_not_create_whole_account_hold(subset):
    service, _factory, broker, redis, _clock = subset
    blocked, _ = plan(subset, symbol="ABC")
    lawful, _ = plan(subset, symbol="SAFE")
    redis.facts[service._session_symbol_block_key(account_name=SCHWAB, symbol="ABC")] = "CONTROLLED_SESSION_BLOCK"

    async def exercise():
        await service.process_trade_intent(blocked)
        assert not broker.sent
        await service.process_trade_intent(lawful)
        assert len(broker.sent) == 1 and broker.sent[0].symbol == "SAFE"

    asyncio.run(exercise())


def test_subset_webull_cancel_ack_without_terminal_zero_cannot_replace(subset):
    service, factory, broker, _redis, clock = subset
    event, selected = plan(subset, is_us=False)

    async def exercise():
        await service.process_trade_intent(event)
        assert len(broker.sent) == 1
        clock[0] = OPEN.replace(minute=30, second=10)
        add_history(factory, first=40, count=2)
        assert schwab_completed_bar_macd_gate(factory, "ABC", clock[0])[0] is MacdVerdict.ALLOWED
        reprice = build_orb_schwab_reprice_intent(service.settings, "ABC", Decimal(6), selection=selected)
        broker.readback = AtrBuyReadback("unknown", "controlled_no_terminal_zero",
                                        broker_order_id="parent-1")
        await service.process_trade_intent(reprice)
        await service.process_trade_intent(reprice)
        assert len([request for request in broker.sent if request.intent_type == "open"]) == 1
        assert len([request for request in broker.sent if request.intent_type == "cancel"]) == 1
        with factory() as session:
            parents = session.scalars(select(BrokerOrder)).all()
            assert len(parents) == 1
            assert parents[0].payload["orb_replace_hold"]["phase"] == "cancelling"
            assert parents[0].broker_order_id == "parent-1"

    asyncio.run(exercise())


def test_subset_schwab_ack_without_replace_confirmation_retains_unknown_handle(subset):
    service, factory, broker, _redis, clock = subset
    event, selected = plan(subset, is_us=True)
    replacements = []

    async def sdk_unknown_confirmation(request, old_id):
        replacements.append((request, old_id))
        return ExecutionReport("accepted", request.client_order_id,
            broker_order_id="unconfirmed-new-handle", symbol=request.symbol, side="buy",
            intent_type="open", quantity=request.quantity, metadata={},
            reported_at=clock[0], origin="broker")

    broker.replace_bracket_order = sdk_unknown_confirmation

    async def exercise():
        await service.process_trade_intent(event)
        assert len(broker.sent) == 1
        clock[0] = OPEN.replace(minute=30, second=10)
        add_history(factory, first=40, count=2)
        assert schwab_completed_bar_macd_gate(factory, "ABC", clock[0])[0] is MacdVerdict.ALLOWED
        reprice = build_orb_schwab_reprice_intent(service.settings, "ABC", Decimal(6), selection=selected)
        await service.process_trade_intent(reprice)
        await service.process_trade_intent(reprice)
        assert len(replacements) == 1 and replacements[0][1] == "parent-1"
        assert len(broker.sent) == 1
        with factory() as session:
            parents = session.scalars(select(BrokerOrder)).all()
            assert len(parents) == 1
            assert parents[0].broker_order_id == "unconfirmed-new-handle"
            assert parents[0].payload["orb_replace_hold"]["old_id"] == "parent-1"
            assert parents[0].payload["orb_replace_hold"]["new_id"] == "unconfirmed-new-handle"

    asyncio.run(exercise())
