"""ORBROUTE1: exact new/legacy sizes, real OMS dispatch and native ownership."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback, scoped_request
from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport, OrderRequest
from project_mai_tai.db.base import Base
from project_mai_tai.db import models as models_module
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, TradeIntent, VirtualPosition
from project_mai_tai.momentum_live_sec import CountryRecord, CountryRouter, CountryStore
from project_mai_tai.oms.orb_country import entry_quantity
from project_mai_tai.oms import orb_country as orb_module, route_owner as owner_module, service as oms_module, store as store_module, webull_attach_riskoff as wb_module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.orb_country_route import select_orb_country_route
from project_mai_tai.orb_schwab_macd import MacdVerdict
from project_mai_tai.orb_schwab_order_route import (
    build_orb_schwab_cancel_intent, build_orb_schwab_open_intent,
    build_orb_schwab_reprice_intent, orb_schwab_intent_refusal,
)
from project_mai_tai.settings import Settings

OPEN = datetime(2026, 10, 15, 13, 28, tzinfo=UTC)
SCHWAB = "live:schwab_1m_v2"
WEBULL = "live:orb"


class Redis:
    async def xadd(self, *_args, **_kwargs):
        return "1-0"


class Broker:
    def __init__(self):
        self.sent = []
        self.previews = []
        self.positions = []
        self.children = {}
        self.cancels = []
        self.readback = AtrBuyReadback("unknown", "unreadable", broker_order_id="parent-1")

    async def list_account_positions(self, _account):
        return self.positions

    async def fetch_order_update(self, request):
        if request.side == "buy":
            return ExecutionReport("accepted", request.client_order_id,
                broker_order_id=request.metadata["broker_order_id"], symbol=request.symbol,
                side="buy", intent_type="open", quantity=request.quantity,
                filled_quantity=Decimal(0), reported_at=OPEN, origin="broker")
        report = self.children.get(request.client_order_id[-1:])
        if report is None:
            return None
        return replace(report, client_order_id=request.client_order_id,
                       metadata={**report.metadata, **request.metadata})

    async def cancel_exit_pair(self, **binding):
        self.cancels.append(binding)
        return []

    async def read_atr_resting_buy_after_cancel(self, _request):
        return self.readback

    async def preview_bracket_order(self, request):
        self.previews.append(request)
        return 200, {"orderStrategy": {"status": "ACCEPTED"}, "orderValidationResult": {}}

    async def submit_order(self, request):
        self.sent.append(request)
        number = sum(r.side == "buy" and r.intent_type == "open" for r in self.sent)
        return [ExecutionReport("accepted", request.client_order_id,
            broker_order_id=f"parent-{number}", symbol=request.symbol, side=request.side,
            intent_type=request.intent_type, quantity=request.quantity,
            metadata=dict(request.metadata), reported_at=OPEN, origin="broker")]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'orb.sqlite'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    settings = Settings(orb_enabled=True, orb_live_schwab_orders_enabled=True,
        orb_country_routing_enabled=True, orb_webull_account_name=WEBULL,
        orb_schwab_atr_entry_gate_enabled=False,
        strategy_schwab_1m_v2_account_name=SCHWAB,
        strategy_schwab_1m_v2_broker_provider="schwab", redis_stream_prefix="test")
    broker = Broker()
    service = OmsRiskService(settings=settings, redis_client=Redis(),
                             session_factory=factory, broker_adapter=broker)
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: OPEN)

    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            value = oms_module.utcnow()
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    # Keep new worker/wrapper clocks and default row stamps on this fixture's retained time.
    for module in (oms_module, orb_module, owner_module, models_module):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr("project_mai_tai.oms.service.schwab_completed_bar_macd_gate",
                        lambda *_: (MacdVerdict.ALLOWED, "nonnegative", 0.1))
    service._get_session_symbol_block_reason = lambda **_: asyncio.sleep(0, result=None)
    yield service, factory, broker
    engine.dispose()


def selected(factory, settings, is_us):
    store = CountryStore(factory)
    if is_us is not None:
        store.put(CountryRecord("ABC", "0000000001", "NY" if is_us else "A6",
            "DE" if is_us else "A6", is_us, OPEN - timedelta(days=1), "READY", OPEN.date()))
    router = CountryRouter(store)
    router._records = store.load()
    return select_orb_country_route(router, "ABC", OPEN,
        schwab_account=SCHWAB, webull_account=settings.orb_webull_account_name)


@pytest.mark.parametrize("is_us,account", [(True, SCHWAB), (False, WEBULL), (None, WEBULL)])
def test_real_oms_one_selected_parent_ten_shares_no_mirror(setup, is_us, account):
    service, factory, broker = setup
    selection = selected(factory, service.settings, is_us)
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal("5.5284"), selection=selection)
    assert orb_schwab_intent_refusal(event, service.settings, OPEN) is None
    asyncio.run(service.process_trade_intent(event))
    assert len(broker.sent) == 1
    request = broker.sent[0]
    assert (request.strategy_code, request.broker_account_name, request.quantity) == ("orb_schwab", account, Decimal(10))
    assert request.metadata["session"] == "NORMAL"
    assert request.metadata["extended_hours"] == "false"
    assert bool(broker.previews) is (is_us is True)
    if is_us:
        assert request.metadata["bracket_target_price"] == "5.81"
        assert request.metadata["bracket_stop_price"] == "5.09"
    else:
        assert not any(key.startswith("bracket") for key in request.metadata)
        assert "native_oco_bracket" not in request.metadata
        assert request.metadata["orb_target_price"] == "5.81"
        assert request.metadata["orb_protect_price"] == "5.09"
    with factory() as session:
        assert len(session.scalars(select(BrokerOrder)).all()) == 1


@pytest.mark.parametrize("hour,minute", [(8, 0), (9, 27), (9, 30), (16, 0), (18, 0)])
def test_new_parent_never_widens_premarket_staging_or_rth_entry_window(setup, hour, minute):
    service, factory, broker = setup
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5),
        selection=selected(factory, service.settings, None))
    now = OPEN.replace(hour=hour + 4, minute=minute)
    assert orb_schwab_intent_refusal(event, service.settings, now) is not None
    assert broker.sent == []


@pytest.mark.parametrize("routed,quantity,allowed", [
    (False, "2", True), (False, "10", False), (True, "10", True),
    (True, "2", False), (True, "NaN", False), (False, "0", False),
])
def test_quantity_binding_does_not_relabel_legacy_entries(routed, quantity, allowed):
    entry = SimpleNamespace(quantity=Decimal(quantity), payload={"orb_country_route": "true"} if routed else {})
    if allowed:
        assert entry_quantity(entry) == Decimal(quantity)
    else:
        with pytest.raises(ValueError, match="quantity_binding_invalid"):
            entry_quantity(entry)


@pytest.mark.parametrize("owner_state", ["position", "working", "unknown_order", "reservation_only"])
def test_foreign_owner_refuses_real_oms_before_new_parent(setup, owner_state, caplog):
    service, factory, broker = setup
    selection = selected(factory, service.settings, None)
    with factory.begin() as session:
        account = service.store.ensure_broker_account(session, WEBULL, provider="webull", environment="test")
        foreign = service.store.ensure_strategy(session, "momentum_warn_live", name="MOM", execution_mode="live")
        if owner_state == "position":
            session.add(VirtualPosition(strategy_id=foreign.id, broker_account_id=account.id,
                symbol="ABC", quantity=Decimal(1), average_price=Decimal(5)))
        else:
            from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
            foreign_event = TradeIntentEvent(source_service="test", payload=TradeIntentPayload(
                strategy_code=foreign.code, broker_account_name=WEBULL, symbol="ABC", side="buy",
                intent_type="open", quantity=Decimal(1), reason="original-owner"))
            intent = service.store.create_trade_intent(session, strategy=foreign, broker_account=account, event=foreign_event)
            if owner_state != "reservation_only":
                order = service.store.get_or_create_order(session, intent=intent, strategy_id=foreign.id,
                    broker_account_id=account.id, client_order_id="first-owner", symbol="ABC", side="buy",
                    quantity=Decimal(1), metadata={})
                order.status = "unknown" if owner_state == "unknown_order" else "accepted"
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5), selection=selection)
    asyncio.run(service.process_trade_intent(event))
    assert broker.sent == []
    assert "[ROUTE-OWNER-CONFLICT]" in caplog.text
    assert "incoming_owner=orb_schwab" in caplog.text
    assert "existing_owner=momentum_warn_live" in caplog.text


def test_reprice_is_new_exposure_but_cleanup_survives_admission_off(setup):
    service, factory, _broker = setup
    selection = selected(factory, service.settings, None)
    reprice = build_orb_schwab_reprice_intent(service.settings, "ABC", Decimal(6), selection=selection)
    cleanup = build_orb_schwab_cancel_intent(service.settings, "ABC", selection=selection)
    service.settings.orb_country_routing_enabled = False
    assert orb_schwab_intent_refusal(reprice, service.settings, OPEN.replace(minute=29)) == "orb_country_disabled"
    assert orb_schwab_intent_refusal(cleanup, service.settings, OPEN) is None


def test_exact_orb_cancel_readback_never_spoofs_atr_or_broadens_generic_orb(setup):
    service, factory, _broker = setup
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5),
        selection=selected(factory, service.settings, None))
    request = OrderRequest("own-parent", WEBULL, "orb_schwab", "ABC", "buy", "cancel", Decimal(10),
        "reprice", {**event.payload.metadata, "broker_order_id": "native-parent",
                    "resting_entry_cancel": "true", "orb_reprice_cancel": "true"})
    assert scoped_request(request)
    assert request.strategy_code == "orb_schwab"
    for key in ("orb_country_route", "orb_country_broker", "orb_reprice_cancel", "broker_order_id"):
        assert not scoped_request(replace(request, metadata={k: v for k, v in request.metadata.items() if k != key}))
    assert not scoped_request(replace(request, strategy_code="orb"))


def seed_filled_webull(setup, monkeypatch, *, quantity=Decimal(10), at=None):
    service, factory, broker = setup
    now = at or OPEN.replace(hour=14, minute=0)
    # Bind both defining producer clocks before any canonical row is created.
    monkeypatch.setattr(oms_module, "utcnow", lambda: now)
    monkeypatch.setattr(store_module, "utcnow", lambda: now)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    monkeypatch.setattr("project_mai_tai.oms.orb_country.datetime", Clock)
    monkeypatch.setattr(wb_module, "datetime", Clock)
    async def placeable(**_kwargs):
        return None
    monkeypatch.setattr(service, "_webull_protect_unplaceable_reason", placeable)
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal("5.5284"),
        selection=selected(factory, service.settings, None))
    with factory.begin() as session:
        strategy = service.store.ensure_strategy(session, "orb_schwab", name="ORB", execution_mode="live")
        account = service.store.ensure_broker_account(session, WEBULL, provider="webull", environment="test")
        intent = service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        entry = service.store.get_or_create_order(session, intent=intent, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id="orb-entry", broker_order_id="entry-native",
            symbol="ABC", side="buy", quantity=Decimal(10), metadata=event.payload.metadata,
            status="filled" if quantity == 10 else "partially_filled")
        session.add(Fill(order_id=entry.id, strategy_id=strategy.id, broker_account_id=account.id,
            broker_fill_id="entry-execution", symbol="ABC", side="buy", quantity=quantity,
            price=Decimal("5.53"), filled_at=now, payload={}))
        session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
            symbol="ABC", quantity=quantity, average_price=Decimal("5.53"), opened_at=now))
        entry_id = entry.id
    with factory() as session:
        actual = session.get(BrokerOrder, entry_id)
        parent = session.get(TradeIntent, actual.intent_id)
        fill = session.scalar(select(Fill).where(Fill.order_id == entry_id))

        def utc_stamp(value):
            assert isinstance(value, datetime)
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

        assert utc_stamp(parent.created_at) == now
        assert utc_stamp(actual.submitted_at) == now
        assert utc_stamp(parent.created_at) <= utc_stamp(actual.submitted_at) <= utc_stamp(fill.filled_at) == now
    broker.positions = [BrokerPositionSnapshot(WEBULL, "ABC", quantity, Decimal("5.53"), as_of=now)]
    return entry_id, now


@pytest.mark.parametrize("quantity", [Decimal(3), Decimal(10)])
def test_actual_post_fill_attach_is_owned_quantity_and_trigger_geometry(setup, monkeypatch, quantity):
    from project_mai_tai.oms.orb_country import reconcile_one
    entry_id, _now = seed_filled_webull(setup, monkeypatch, quantity=quantity)
    service, factory, broker = setup
    asyncio.run(reconcile_one(service, entry_id))
    asyncio.run(reconcile_one(service, entry_id))
    assert len(broker.sent) == 1
    pair = broker.sent[0]
    assert (pair.strategy_code, pair.broker_account_name, pair.quantity) == ("orb_schwab", WEBULL, quantity)
    assert pair.metadata["webull_exit_only_pair"] == "true"
    assert pair.metadata["bracket_target_price"] == "5.81"
    assert pair.metadata["bracket_stop_price"] == "5.09"
    assert pair.metadata["market_session"] == "RTH"
    with factory() as session:
        assert session.get(BrokerOrder, entry_id).payload["orbroute1_protection"]["phase"] == "accepted"


def test_known_target_unknown_stop_books_once_and_never_releases_or_resends(setup, monkeypatch):
    from project_mai_tai.oms.orb_country import reconcile_one, release_protection
    entry_id, now = seed_filled_webull(setup, monkeypatch)
    service, factory, broker = setup
    broker.children["T"] = ExecutionReport("filled", "rebound-exact-child", broker_order_id="target-native",
        broker_fill_id="target-execution", symbol="ABC", side="sell", intent_type="close",
        quantity=Decimal(10), filled_quantity=Decimal(10), fill_price=Decimal("5.81"),
        reported_at=now, origin="broker")
    asyncio.run(reconcile_one(service, entry_id))
    assert asyncio.run(release_protection(service, entry_id)) is False
    assert asyncio.run(release_protection(service, entry_id)) is False
    assert len(broker.sent) == 1
    assert len(broker.cancels) == 1
    with factory() as session:
        position = session.scalar(select(VirtualPosition))
        assert position.quantity == 0
        assert position.realized_pnl == Decimal("2.80")
        assert len(session.scalars(select(Fill).where(Fill.side == "sell")).all()) == 1
        assert session.get(BrokerOrder, entry_id).payload["orbroute1_protection"]["phase"] != "released"
    broker.children["S"] = ExecutionReport("cancelled", "rebound-exact-child", broker_order_id="stop-native",
        symbol="ABC", side="sell", intent_type="close", quantity=Decimal(10),
        filled_quantity=Decimal(0), reported_at=now, origin="broker")
    assert asyncio.run(release_protection(service, entry_id)) is True
    assert len(broker.sent) == 1


def test_close_claim_prevents_reattach_in_release_to_sell_gap(setup, monkeypatch):
    from project_mai_tai.oms.orb_country import reconcile_one
    entry_id, _now = seed_filled_webull(setup, monkeypatch)
    service, factory, broker = setup
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        entry.payload = {**entry.payload, "orb_schwab_eod_close": {"phase": "checking"},
                         "orbroute1_protection": {"phase": "released"}}
    asyncio.run(reconcile_one(service, entry_id))
    assert broker.sent == []


@pytest.mark.parametrize("hour", [8, 12, 20, 22])
def test_no_new_native_pair_outside_rth_even_for_retained_owned_position(setup, monkeypatch, hour):
    from project_mai_tai.oms.orb_country import reconcile_one
    entry_id, _now = seed_filled_webull(setup, monkeypatch, at=OPEN.replace(hour=hour))
    service, _factory, broker = setup
    asyncio.run(reconcile_one(service, entry_id))
    assert broker.sent == []


def test_routed_protection_continues_when_new_country_admission_is_off(setup, monkeypatch):
    from project_mai_tai.oms.orb_country import reconcile_one
    entry_id, _now = seed_filled_webull(setup, monkeypatch)
    service, _factory, broker = setup
    service.settings.orb_country_routing_enabled = False
    asyncio.run(reconcile_one(service, entry_id))
    assert len(broker.sent) == 1
    assert broker.sent[0].side == "sell"
    assert broker.sent[0].metadata["webull_exit_only_pair"] == "true"


@pytest.mark.parametrize("mutation", ["target", "protect", "trigger", "foreign_account_quantity"])
def test_native_pair_never_uses_tampered_geometry_or_unowned_account_excess(setup, monkeypatch, mutation, caplog):
    from project_mai_tai.oms.orb_country import reconcile_one
    if mutation == "foreign_account_quantity":
        # TOP13:0x: same-account excess is manual backing, not a foreign bot lot.
        test_native_pair_preserves_same_account_manual_excess(setup, monkeypatch)
        return
    entry_id, _now = seed_filled_webull(setup, monkeypatch)
    service, factory, broker = setup
    key = {"target": "orb_target_price", "protect": "orb_protect_price", "trigger": "stop_price"}[mutation]
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        entry.payload = {**entry.payload, key: {"target": "5.90", "protect": "5.00", "trigger": "5.54"}[mutation]}
    with pytest.raises(ValueError, match="wbattach1_orb_original_geometry_changed"):
        asyncio.run(reconcile_one(service, entry_id))
    assert broker.sent == []
    assert any(record.levelname == "CRITICAL" and "original_geometry_changed" in record.message
               for record in caplog.records)


def test_native_pair_preserves_same_account_manual_excess(setup, monkeypatch):
    from project_mai_tai.oms.orb_country import reconcile_one
    entry_id, now = seed_filled_webull(setup, monkeypatch)
    service, factory, broker = setup
    broker.positions = [BrokerPositionSnapshot(WEBULL, "ABC", Decimal(11), Decimal("5.53"), as_of=now)]
    asyncio.run(reconcile_one(service, entry_id))
    assert len(broker.sent) == 1
    assert broker.sent[0].quantity == 10
    assert broker.sent[0].metadata["webull_exit_only_pair"] == "true"
    assert broker.positions[0].quantity == 11
    assert not broker.cancels
    with factory() as session:
        assert session.scalar(select(VirtualPosition.quantity)) == 10
        assert session.scalar(select(Fill).where(Fill.side == "sell")) is None


@pytest.mark.parametrize("mutation", ["foreign_bot", "under_backed"])
def test_native_pair_still_refuses_foreign_bot_or_under_backing(setup, monkeypatch, mutation):
    from project_mai_tai.oms.orb_country import reconcile_one
    entry_id, now = seed_filled_webull(setup, monkeypatch)
    service, factory, broker = setup
    if mutation == "under_backed":
        broker.positions = [BrokerPositionSnapshot(WEBULL, "ABC", Decimal(9), Decimal("5.53"), as_of=now)]
    else:
        with factory.begin() as session:
            account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == WEBULL))
            foreign = service.store.ensure_strategy(session, "momentum_warn_live", execution_mode="live")
            session.add(VirtualPosition(strategy_id=foreign.id, broker_account_id=account.id,
                symbol="ABC", quantity=Decimal(1), average_price=Decimal("5.53"), opened_at=now))
    with pytest.raises(ValueError, match="orb_foreign_owner|orb_broker_backing_unproven"):
        asyncio.run(reconcile_one(service, entry_id))
    assert broker.sent == [] and broker.cancels == []
    with factory() as session:
        assert session.scalar(select(Fill).where(Fill.side == "sell")) is None


@pytest.mark.parametrize("routed,quantity", [(True, Decimal(10)), (False, Decimal(2))])
def test_real_hard_stop_episode_uses_exact_new_or_legacy_entry_size(setup, monkeypatch, routed, quantity):
    entry_id, _now = seed_filled_webull(setup, monkeypatch, quantity=quantity)
    service, factory, _broker = setup
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        entry.status = "filled"
        entry.quantity = quantity
        if not routed:
            entry.payload = {}
            account = session.get(BrokerAccount, entry.broker_account_id)
            account.name = SCHWAB
            account.provider = "schwab"
    stop = SimpleNamespace(strategy_code="orb_schwab", broker_account_name=WEBULL if routed else SCHWAB,
                           symbol="ABC", quantity=quantity)
    with factory() as session:
        episode = service._reserve1_hard_stop_episode(session, stop)
        assert episode is not None
        assert episode[1:] == (str(entry_id), "entry-native", quantity)
        stop.quantity = quantity + 1
        assert service._reserve1_hard_stop_episode(session, stop) is None


def test_deferred_bar_proof_is_utc_equivalent_not_callers_offset(setup):
    from zoneinfo import ZoneInfo
    service, factory, _broker = setup
    event = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5),
        deferred_macd_bar_close=OPEN, selection=selected(factory, service.settings, None))
    assert orb_schwab_intent_refusal(event, service.settings,
        (OPEN + timedelta(seconds=20)).astimezone(ZoneInfo("America/New_York"))) is None


@pytest.mark.parametrize("mutation", ["symbol", "side", "intent_type", "quantity", "reason", "entry_link"])
def test_native_child_report_cannot_rebind_another_protection_intent(setup, monkeypatch, mutation):
    from uuid import UUID
    from project_mai_tai.oms.orb_country import reconcile_one, record_protection_reports
    entry_id, _now = seed_filled_webull(setup, monkeypatch)
    service, factory, broker = setup
    asyncio.run(reconcile_one(service, entry_id))
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        intent = session.get(TradeIntent, UUID(entry.payload["orbroute1_protection"]["intent_id"]))
        if mutation == "entry_link":
            intent.payload = {**intent.payload, "metadata": {"entry_order_id": "another-entry"}}
        else:
            setattr(intent, mutation, {"symbol": "MSFT", "side": "buy", "intent_type": "open",
                "quantity": Decimal(11), "reason": "another-purpose"}[mutation])
    with pytest.raises(ValueError, match="intent_mismatch"):
        asyncio.run(record_protection_reports(service, entry_id, [None, None]))
    assert len(broker.sent) == 1
    with factory() as session:
        assert session.scalar(select(VirtualPosition)).quantity == 10


@pytest.mark.parametrize("terminal_zero", [False, True])
def test_actual_webull_reprice_requires_terminal_zero_not_cancel_ack_and_never_retries(setup, monkeypatch, terminal_zero):
    service, factory, broker = setup
    selection = selected(factory, service.settings, None)
    original = build_orb_schwab_open_intent(service.settings, "ABC", Decimal(5), selection=selection)
    asyncio.run(service.process_trade_intent(original))
    now = OPEN.replace(minute=29)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    monkeypatch.setattr("project_mai_tai.oms.orb_country.datetime", Clock)
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: now)
    if terminal_zero:
        broker.readback = AtrBuyReadback("cancelled_empty", "terminal_cancel_explicit_zero",
            Decimal(0), terminal_cancel=True, broker_status="CANCELLED", broker_order_id="parent-1")
    reprice = build_orb_schwab_reprice_intent(service.settings, "ABC", Decimal(6), selection=selection)
    asyncio.run(service.process_trade_intent(reprice))
    asyncio.run(service.process_trade_intent(reprice))
    buys = [r for r in broker.sent if r.side == "buy" and r.intent_type == "open"]
    cancels = [r for r in broker.sent if r.intent_type == "cancel"]
    assert len(cancels) == 1
    assert len(buys) == (2 if terminal_zero else 1)
    assert all(r.strategy_code == "orb_schwab" and r.broker_account_name == WEBULL for r in broker.sent)
    if terminal_zero:
        assert buys[-1].client_order_id == f"orb-r-{reprice.event_id.hex[:24]}"
        assert buys[-1].quantity == 10
        assert buys[-1].metadata["orb_country_reason"] == "sec_unknown"
        assert "orb_replacement_of" in buys[-1].metadata
    else:
        with factory() as session:
            parent = session.scalar(select(BrokerOrder).where(BrokerOrder.side == "buy"))
            assert parent.payload["orb_replace_hold"]["phase"] == "cancelling"


def test_original_two_share_mutation_is_caught_by_actual_oms_chain(setup, monkeypatch):
    monkeypatch.setattr("project_mai_tai.orb_country_route.ORB_ROUTE_QUANTITY", Decimal(2))
    with pytest.raises(AssertionError):
        test_real_oms_one_selected_parent_ten_shares_no_mirror(setup, None, WEBULL)
    assert setup[2].sent[0].quantity == 2


def test_missing_reservation_guard_mutation_is_caught_before_qualification(setup, monkeypatch, caplog):
    monkeypatch.setattr("project_mai_tai.oms.orb_country.orb_route_owner_conflict", lambda *_a, **_k: None)
    with pytest.raises(AssertionError):
        test_foreign_owner_refuses_real_oms_before_new_parent(setup, "reservation_only", caplog)
    assert len(setup[2].sent) == 1


def test_cancel_ack_as_terminal_mutation_is_caught_by_real_reprice_chain(setup, monkeypatch):
    monkeypatch.setattr(AtrBuyReadback, "can_replace", property(lambda _self: True))
    with pytest.raises(AssertionError):
        test_actual_webull_reprice_requires_terminal_zero_not_cancel_ack_and_never_retries(setup, monkeypatch, False)
    assert len([r for r in setup[2].sent if r.intent_type == "open"]) == 2


def test_atr_geometry_mutation_is_caught_by_actual_orb_native_pair(setup, monkeypatch):
    from project_mai_tai.strategy_core import orb_schwab_bracket
    original = orb_schwab_bracket.build_orb_schwab_bracket_metadata
    def atr_geometry(level):
        return {**original(level), "bracket_target_price": "5.64", "bracket_stop_price": "5.25"}
    monkeypatch.setattr(orb_schwab_bracket, "build_orb_schwab_bracket_metadata", atr_geometry)
    with pytest.raises(AssertionError):
        test_actual_post_fill_attach_is_owned_quantity_and_trigger_geometry(setup, monkeypatch, Decimal(10))
    assert setup[2].sent[0].metadata["bracket_target_price"] == "5.64"
