"""Existing local OMS tables and real intent pipeline; never a live broker proof."""

from tests.unit.oms_sqlite_fixtures import owned_sqlite as owned_sqlite
from tests.unit.oms_sqlite_fixtures import private_session_factory as build_test_session_factory
from tests.unit.oms_sqlite_fixtures import own_service
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.atr_country_route import AtrCountryRouteBook
from project_mai_tai.db.models import AccountPosition, BrokerOrder, OmsManagedPosition, TradeIntent, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import service as oms_module
from project_mai_tai.oms import route_owner as owner_module
from project_mai_tai.oms.route_owner import route_owner_conflict
from project_mai_tai.oms.service import OmsRiskService
from tests.unit.test_countryroute_atr import country_router, options

from tests.unit.test_orb_schwab_order_route import _Broker, _Redis

NOW = datetime(2026, 10, 15, 14, tzinfo=UTC)
ACCOUNTS = [("live:schwab_1m_v2", True), ("live:orb", False)]


@pytest.fixture(autouse=True)
def capture_actual_owner_logger(monkeypatch, caplog):
    # OMS basicConfig(force=True) replaces root handlers during construction.
    # Bind capture to the actual emitting logger; restore its handlers per test.
    monkeypatch.setattr(owner_module.logger, "handlers", [*owner_module.logger.handlers, caplog.handler])
    caplog.set_level(logging.INFO, logger=owner_module.logger.name)


@pytest.fixture(autouse=True)
def owned_clock(monkeypatch):
    clock = [NOW]

    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz is not None else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(oms_module, "utcnow", lambda: clock[0])
    monkeypatch.setattr(oms_module, "datetime", Clock)
    monkeypatch.setattr(owner_module, "datetime", Clock)
    return clock


def setup(account, is_us, now):
    settings = options()
    factory = build_test_session_factory()
    broker = _Broker()
    service = own_service(OmsRiskService(settings=settings, redis_client=_Redis(),
                             session_factory=factory, broker_adapter=broker))
    service._get_session_symbol_block_reason = lambda **kwargs: asyncio.sleep(0, result=None)
    service._reconcile_after_intent = lambda *_args: asyncio.sleep(0)
    selected = AtrCountryRouteBook(settings, country_router(is_us).route_for).select("TEST", 100, now)
    event = TradeIntentEvent(source_service="controlled-countryroute", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=account, symbol="TEST", side="buy",
        intent_type="open", quantity=Decimal(150), reason="schwab_1m_v2 ATR Flip controlled",
        metadata={**selected.metadata(), "fanout_segment_id": "100", "entry_price": "2",
                  "reference_price": "2", "entry_size_price": "2", "order_type": "limit", "limit_price": "2"}))
    return service, factory, broker, event


def foreign_row(service, session, account, kind, *, foreign="orb_schwab", status="accepted"):
    strategy = service.store.ensure_strategy(session, foreign, name="CONTROLLED FOREIGN")
    provider = "schwab" if account == "live:schwab_1m_v2" else "webull"
    acct = service.store.ensure_broker_account(session, account, provider=provider, environment="live")
    if kind == "held":
        session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=acct.id,
                                    symbol="TEST", quantity=Decimal(10), average_price=Decimal(2)))
    elif kind == "manual":
        session.add(AccountPosition(broker_account_id=acct.id, symbol="TEST", quantity=Decimal(10), average_price=Decimal(2)))
    elif kind == "managed":
        session.add(OmsManagedPosition(strategy_code=foreign, broker_account_name=account,
            symbol="TEST", status="open", entry_price=Decimal(2),
            original_quantity=10, current_quantity=10))
    else:
        intent = TradeIntent(strategy_id=strategy.id, broker_account_id=acct.id, symbol="TEST",
                             side="buy", intent_type="open", quantity=Decimal(10), reason="CONTROLLED",
                             status=status, created_at=NOW, payload={})
        session.add(intent)
        session.flush()
        if kind == "working":
            session.add(BrokerOrder(intent_id=intent.id, strategy_id=strategy.id,
                broker_account_id=acct.id, client_order_id="CONTROLLED-FOREIGN-PARENT",
                symbol="TEST", side="buy", order_type="LIMIT", time_in_force="DAY",
                quantity=Decimal(10), status=status, payload={}))
    session.flush()
    return acct


def test_owned_defining_clocks_keep_aware_now_and_datetime_semantics(owned_clock):
    owned_clock[0] = NOW.replace(hour=11, minute=30)
    for module in (oms_module, owner_module):
        assert module.datetime.now(UTC) == owned_clock[0]
        assert module.datetime.now() == owned_clock[0].replace(tzinfo=None)
        assert isinstance(owned_clock[0], module.datetime)
        assert module.datetime(2026, 10, 15, 14, tzinfo=UTC) == NOW
        assert module.datetime.fromisoformat(NOW.isoformat()) == NOW
    assert oms_module.utcnow() == owned_clock[0]


@pytest.mark.parametrize("account,is_us", ACCOUNTS)
@pytest.mark.parametrize("kind", ["working", "reservation"])
def test_owned_session_clock_roll_keeps_current_then_logs_previous_session(
    account, is_us, kind, owned_clock, caplog,
):
    service, factory, _, _ = setup(account, is_us, NOW)
    with factory() as session:
        acct = foreign_row(service, session, account, kind)
        session.commit()
        owned_clock[0] = datetime(2026, 10, 16, 7, 59, 59, tzinfo=UTC)
        assert route_owner_conflict(session, account_id=acct.id, account_name=account,
                                    symbol="TEST", incoming_strategy="schwab_1m_v2") == "orb_schwab"
        owned_clock[0] += timedelta(seconds=1)
        with caplog.at_level(logging.INFO, logger="project_mai_tai.oms.route_owner"):
            assert route_owner_conflict(session, account_id=acct.id, account_name=account,
                                        symbol="TEST", incoming_strategy="schwab_1m_v2") is None
        assert "[ROUTE-OWNER-STALE-ROW]" in caplog.text


@pytest.mark.parametrize("defining_clock", ["worker", "helper"])
@pytest.mark.parametrize("kind", ["working", "reservation"])
def test_next_session_defining_clock_mutation_exposes_old_fixture_gap(
    defining_clock, kind, monkeypatch, caplog,
):
    service, factory, _, _ = setup("live:schwab_1m_v2", True, NOW)
    with factory() as session:
        acct = foreign_row(service, session, "live:schwab_1m_v2", kind)
        session.commit()

        def check():
            # Mirrors37's explicit worker-time argument; helper callers use its default.
            clock = {"now": oms_module.datetime.now(UTC)} if defining_clock == "worker" else {}
            return route_owner_conflict(session, account_id=acct.id, account_name=acct.name,
                                        symbol="TEST", incoming_strategy="schwab_1m_v2", **clock)

        assert check() == "orb_schwab"
        target = oms_module if defining_clock == "worker" else owner_module

        class NextSession(target.datetime):
            @classmethod
            def now(cls, tz=None):
                value = NOW + timedelta(days=1)
                return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

        monkeypatch.setattr(target, "datetime", NextSession)
        with caplog.at_level(logging.INFO, logger="project_mai_tai.oms.route_owner"):
            assert check() is None
        assert "[ROUTE-OWNER-STALE-ROW]" in caplog.text
        assert oms_module.utcnow() == NOW  # Freezing only this old seam was insufficient.


@pytest.mark.parametrize("account,is_us", ACCOUNTS)
@pytest.mark.parametrize("kind", ["working", "reservation"])
def test_removing_real_stale_log_fails_original_session_roll_positive(
    account, is_us, kind, owned_clock, caplog, monkeypatch,
):
    caplog.clear()
    monkeypatch.setattr(owner_module.logger, "info", lambda *_args, **_kwargs: None)
    with pytest.raises(AssertionError, match=r"\[ROUTE-OWNER-STALE-ROW\]"):
        test_owned_session_clock_roll_keeps_current_then_logs_previous_session(
            account, is_us, kind, owned_clock, caplog,
        )


def test_sealed_new_entry_refuses_account_provider_mismatch():
    service, _, broker, event = setup("live:schwab_1m_v2", True, NOW)
    service.settings.strategy_schwab_1m_v2_broker_provider = "paper"
    assert service._v2_entry_size_refusal(event) == "v2_country_route_binding_UNKNOWN"
    assert not broker.submitted and not broker.previewed and not broker.replaced


@pytest.mark.asyncio
@pytest.mark.parametrize("account,is_us", ACCOUNTS)
@pytest.mark.parametrize("hour", [11, 14])
@pytest.mark.parametrize("kind", ["held", "working", "reservation", "manual"])
async def test_foreign_first_owner_refuses_without_preview_cancel_or_submit(
    account, is_us, hour, kind, owned_clock, caplog, monkeypatch,
):
    now = NOW.replace(hour=hour, minute=30)
    owned_clock[0] = now
    service, factory, broker, event = setup(account, is_us, now)
    # OMS configures the root handler at construction; capture its own logger.
    monkeypatch.setattr(service.logger, "handlers", [*service.logger.handlers, caplog.handler])
    caplog.set_level(logging.INFO, logger="project_mai_tai.oms.route_owner")
    with factory() as session:
        foreign_row(service, session, account, kind, foreign="momentum_warn_live" if kind == "reservation" else "orb_schwab")
        session.commit()
    rows = await service.process_trade_intent(event)
    if kind == "manual":
        # Explicit03:4x ruling: manual net inventory logs, never blocks a bot.
        assert any(row.payload.status == "accepted" for row in rows)
        assert len(broker.submitted) == 1 and not broker.replaced
        order = broker.submitted[0]
        assert order.broker_account_name == account and order.strategy_code == "schwab_1m_v2"
        assert order.side == "buy" and order.intent_type == "open"
        assert order.quantity == Decimal(300 if is_us else 150)
        assert order.quantity * Decimal(order.metadata["limit_price"]) == Decimal(600 if is_us else 300)
        assert "[ROUTE-OWNER-MANUAL-PRESENT]" in caplog.text
        assert "log_only=1" in caplog.text and "[ROUTE-OWNER-CONFLICT]" not in caplog.text
        with factory() as session:
            position = session.scalar(select(AccountPosition).where(AccountPosition.symbol == "TEST"))
            assert position.quantity == Decimal(10) and position.average_price == Decimal(2)
            assert session.scalar(select(VirtualPosition).where(VirtualPosition.symbol == "TEST")) is None
        return
    assert rows and all(row.payload.status == "rejected" for row in rows)
    assert not broker.submitted and not broker.previewed and not broker.replaced
    assert "[ROUTE-OWNER-CONFLICT]" in caplog.text
    assert "incoming=schwab_1m_v2" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("account,is_us", ACCOUNTS)
@pytest.mark.parametrize("hour", [11, 14])
async def test_removing_real_manual_log_fails_original_manual_positive(
    account, is_us, hour, owned_clock, caplog, monkeypatch,
):
    caplog.clear()
    monkeypatch.setattr(owner_module.logger, "info", lambda *_args, **_kwargs: None)
    with pytest.raises(AssertionError, match=r"\[ROUTE-OWNER-MANUAL-PRESENT\]"):
        await test_foreign_first_owner_refuses_without_preview_cancel_or_submit(
            account, is_us, hour, "manual", owned_clock, caplog, monkeypatch,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("account,is_us", ACCOUNTS)
@pytest.mark.parametrize("hour", [11, 14])
@pytest.mark.parametrize("kind", ["held", "managed", "working", "reservation"])
async def test_manual_inventory_never_exempts_genuine_foreign_owner(
    account, is_us, hour, kind, owned_clock, caplog, monkeypatch,
):
    now = NOW.replace(hour=hour, minute=30)
    owned_clock[0] = now
    service, factory, broker, event = setup(account, is_us, now)
    monkeypatch.setattr(service.logger, "handlers", [*service.logger.handlers, caplog.handler])
    with factory() as session:
        foreign_row(service, session, account, "manual")
        foreign_row(service, session, account, kind,
                    foreign="momentum_warn_live" if kind == "reservation" else "orb_schwab")
        session.commit()
    rows = await service.process_trade_intent(event)
    assert rows and all(row.payload.status == "rejected" for row in rows)
    assert not broker.submitted and not broker.previewed and not broker.replaced
    assert "[ROUTE-OWNER-CONFLICT]" in caplog.text
    assert "incoming=schwab_1m_v2" in caplog.text
    with factory() as session:
        position = session.scalar(select(AccountPosition).where(AccountPosition.symbol == "TEST"))
        assert position.quantity == Decimal(10) and position.average_price == Decimal(2)


@pytest.mark.asyncio
@pytest.mark.parametrize("account,is_us", ACCOUNTS)
@pytest.mark.parametrize("hour", [11, 14])
async def test_no_foreign_owner_allows_only_selected_broker_account(account, is_us, hour, owned_clock):
    now = NOW.replace(hour=hour, minute=30)
    owned_clock[0] = now
    service, _, broker, event = setup(account, is_us, now)
    rows = await service.process_trade_intent(event)
    assert any(row.payload.status == "accepted" for row in rows)
    assert len(broker.submitted) == 1
    assert broker.submitted[0].broker_account_name == account


@pytest.mark.parametrize("account,is_us", ACCOUNTS)
def test_foreign_terminal_unfilled_history_is_not_a_working_owner(account, is_us):
    service, factory, _, _ = setup(account, is_us, NOW)
    with factory() as session:
        acct = foreign_row(service, session, account, "working", status="cancelled")
        assert route_owner_conflict(session, account_id=acct.id, account_name=account,
                                    symbol="TEST", incoming_strategy="schwab_1m_v2") is None


@pytest.mark.parametrize("account,is_us", ACCOUNTS)
def test_unreadable_owner_query_is_not_reported_as_no_foreign_owner(account, is_us):
    service, factory, _, _ = setup(account, is_us, NOW)
    with factory() as session:
        acct = service.store.ensure_broker_account(session, account, provider="paper", environment="paper")
        identity = acct.id

    class Unreadable:
        def scalar(self, *args, **kwargs):
            raise RuntimeError("CONTROLLED_DB_UNREADABLE")

    with pytest.raises(RuntimeError, match="DB_UNREADABLE"):
        route_owner_conflict(Unreadable(), account_id=identity, account_name=account,
                             symbol="TEST", incoming_strategy="schwab_1m_v2")


@pytest.mark.asyncio
async def test_sealed_us_fill_never_triggers_old_oms_webull_copy():
    service, _, broker, event = setup("live:schwab_1m_v2", True, NOW)
    service.settings.strategy_schwab_1m_v2_webull_mirror_enabled = True
    await service._mirror_v2_fill_to_webull(symbol="TEST", quantity=10, schwab_fill_price=2,
                                           source_metadata=event.payload.metadata)
    assert not broker.submitted


@pytest.mark.parametrize("account,is_us", ACCOUNTS)
def test_sealed_route_mismatched_account_refuses_before_wire(account, is_us, owned_clock):
    service, _, broker, event = setup(account, is_us, NOW)
    event.payload.broker_account_name = "live:orb" if is_us else "live:schwab_1m_v2"
    assert service._v2_entry_size_refusal(event) == "v2_country_route_binding_UNKNOWN"
    assert not broker.submitted


@pytest.mark.asyncio
@pytest.mark.parametrize("account,is_us", ACCOUNTS)
@pytest.mark.parametrize("hour", [12, 14])
async def test_exit_remaining_stays_at_buying_broker_after_country_changes(account, is_us, hour, monkeypatch):
    from project_mai_tai.momentum_live_sec import CountryRouter
    from tests.unit.test_oco_duplicate_close_after_close import quantity, seed_episode

    service, factory, row_id, _, _ = seed_episode(account, exited=90, hour=hour)
    selected = AtrCountryRouteBook(options(), country_router(is_us).route_for).select("TEST", 100, NOW)
    with factory() as session:
        row = session.get(OmsManagedPosition, UUID(row_id))
        entry = session.get(BrokerOrder, row.entry_order_id)
        entry.payload = {**entry.payload, **selected.metadata(), "atr_country_symbol": "GLND"}
        session.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("owned exit re-read country instead of its original BUY binding")

    monkeypatch.setattr(CountryRouter, "route_for", forbidden)
    assert await quantity(service, account, row_id) == 60
