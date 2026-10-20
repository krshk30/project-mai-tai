"""Q05/O13 controlled shared-account exits, not retained vendor execution replay.

TOP15:56 approves these quantity caps; M03 admission remains unchanged.
TOP16:16 supersedes blanket latest-BUY chronology and strict underbacking:
terminal zero-fill plans do not replace an entry; the EOD-only UNKNOWN fallback
is broker-bounded within the original target, never a manual-excess bypass.
Only broker responses are controlled. The real durable fallback/EOD writers,
ownership readers, native release coordinator and incident writer stay active.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event as sql_event, select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition,
    Strategy, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.momentum_live_sec import CountryRecord, CountryRouter, CountryStore
from project_mai_tai.orb_country_route import select_orb_country_route
from project_mai_tai.orb_schwab_order_route import build_orb_schwab_open_intent
from tests.unit.test_orb_schwab_order_route import ACCOUNT, OPEN, _EodBroker, _service


SYMBOL = "CAPQ"


class FallbackBroker:
    def __init__(self):
        self.sent = []

    async def submit_order(self, request):
        self.sent.append(request)
        return [ExecutionReport(event_type="accepted", origin="broker",
            client_order_id=request.client_order_id, broker_order_id="controlled-cap-accepted",
            symbol=request.symbol, side=request.side, intent_type=request.intent_type,
            quantity=request.quantity, metadata=dict(request.metadata))]


def fallback_case(monkeypatch, *, owned="95", projection="both", raw="1095",
                  event_quantity="95", request_quantity="95"):
    broker = FallbackBroker()
    service, factory, _ = _service(monkeypatch, broker=broker)
    with factory.begin() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2")
        account = service.store.ensure_broker_account(session, ACCOUNT, provider="schwab", environment="test")
        strategy_id, account_id = strategy.id, account.id
        session.add(AccountPosition(broker_account_id=account_id, symbol=SYMBOL,
            quantity=Decimal("1095"), average_price=Decimal("3")))
        if owned is not None and projection in {"virtual", "both"}:
            session.add(VirtualPosition(strategy_id=strategy_id, broker_account_id=account_id,
                symbol=SYMBOL, quantity=Decimal(owned), average_price=Decimal("3")))
        if owned is not None and projection in {"managed", "both"}:
            session.add(service.store.create_managed_position(session,
                strategy_code="schwab_1m_v2", broker_account_name=ACCOUNT, symbol=SYMBOL,
                entry_price=Decimal("3"), quantity=Decimal(owned)))
        # A different strategy's positive quantity is never an own fallback cap.
        foreign = service.store.ensure_strategy(session, "orb_schwab")
        session.add(VirtualPosition(strategy_id=foreign.id, broker_account_id=account_id,
            symbol=SYMBOL, quantity=Decimal("1000"), average_price=Decimal("3")))
    reads = []

    async def broker_position(**kwargs):
        reads.append(kwargs)
        return None if raw is None else Decimal(raw)

    service._refresh_broker_position_quantity = broker_position
    event = TradeIntentEvent(source_service="controlled-ownmix2", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=ACCOUNT, symbol=SYMBOL,
        side="sell", intent_type="close", quantity=Decimal(event_quantity), reason="HARD_STOP"))
    request = OrderRequest(client_order_id="controlled-ownmix2-rejected-stop",
        broker_account_name=ACCOUNT, strategy_code="schwab_1m_v2", symbol=SYMBOL,
        side="sell", intent_type="close", quantity=Decimal(request_quantity), reason="HARD_STOP")
    return SimpleNamespace(service=service, factory=factory, broker=broker, reads=reads,
        strategy_id=strategy_id, account_id=account_id, event=event, request=request)


async def fallback(case):
    with case.factory.begin() as session:
        return await case.service._process_stop_reject_market_fallback(session=session,
            strategy=session.get(Strategy, case.strategy_id),
            broker_account=session.get(BrokerAccount, case.account_id),
            original_event=case.event, original_request=case.request,
            rejection_reason="CONTROLLED stop rejected")


@pytest.mark.asyncio
@pytest.mark.parametrize("projection", ["virtual", "managed", "both"])
@pytest.mark.parametrize("raw,event_quantity,request_quantity,expected", [
    ("1095", "95", "95", "95"), ("1000", "95", "95", "95"),
    ("20", "95", "95", "20"), ("1095", "7", "95", "7"),
    ("1095", "95", "9", "9"),
])
async def test_schwab_rejected_stop_caps_actual_wire_by_raw_own_and_both_requests(
        monkeypatch, projection, raw, event_quantity, request_quantity, expected):
    case = fallback_case(monkeypatch, projection=projection, raw=raw,
                         event_quantity=event_quantity, request_quantity=request_quantity)
    try:
        await fallback(case)
        request, = case.broker.sent
        assert request.quantity == Decimal(expected)
        assert request.broker_account_name == ACCOUNT and request.strategy_code == "schwab_1m_v2"
        assert request.side == "sell" and request.intent_type == "close" and request.order_type == "market"
        assert request.reason == "STOP_REJECTED_FALLBACK"
        assert request.metadata["fallback_for_client_order_id"] == case.request.client_order_id
        assert len(case.reads) == 1 and case.reads[0]["broker_account_name"] == ACCOUNT
        with case.factory() as session:
            order, = session.scalars(select(BrokerOrder).where(BrokerOrder.side == "sell")).all()
            assert order.quantity == Decimal(expected) and order.strategy_id == case.strategy_id
            assert session.scalar(select(AccountPosition.quantity)) == 1095
            foreign = session.scalar(select(Strategy.id).where(Strategy.code == "orb_schwab"))
            assert session.scalar(select(VirtualPosition.quantity).where(VirtualPosition.strategy_id == foreign)) == 1000
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing-own", "negative-own", "unknown-managed", "book-mismatch", "read-error", "runtime-read-error"])
async def test_schwab_unknown_ownership_never_sells_manual_or_foreign_shares_and_pages(monkeypatch, fault):
    case = fallback_case(monkeypatch, owned=None if fault == "missing-own" else "95", projection="virtual")
    capture = None
    try:
        if fault == "negative-own":
            with case.factory.begin() as session:
                session.scalar(select(VirtualPosition).where(
                    VirtualPosition.strategy_id == case.strategy_id)).quantity = -1
        if fault in {"unknown-managed", "book-mismatch"}:
            with case.factory.begin() as session:
                row = case.service.store.create_managed_position(session,
                    strategy_code="schwab_1m_v2", broker_account_name=ACCOUNT, symbol=SYMBOL,
                    entry_price=Decimal("3"), quantity=94)
                if fault == "unknown-managed":
                    row.status, row.current_quantity = "unknown", 0
        if fault in {"read-error", "runtime-read-error"}:
            # The ownership lookup itself is unreadable, not a broker-flat
            # response. The raw broker ceiling still succeeds once.
            def unreadable(_connection, _cursor, statement, _parameters, _context, _many):
                if statement.lstrip().lower().startswith("select") and "virtual_positions" in statement.lower():
                    error = RuntimeError if fault == "runtime-read-error" else ValueError
                    raise error("CONTROLLED own-book read unavailable")

            capture = unreadable
            sql_event.listen(case.factory.kw["bind"], "before_cursor_execute", capture)
        await fallback(case)
        await fallback(case)  # Same rejected stop must not create incident spam.
        assert not case.broker.sent
        assert len(case.reads) == 2
        with case.factory() as session:
            assert not session.scalars(select(BrokerOrder).where(BrokerOrder.side == "sell")).all()
            incidents = session.scalars(select(SystemIncident)).all()
            assert len(incidents) == 1
            incident, = incidents
            assert incident.severity == "critical" and incident.status == "open"
            assert incident.payload["source"] == "schwab_stop_reject_fallback_ownership"
            assert incident.payload["client_order_id"] == case.request.client_order_id
            assert incident.payload["broker_account_name"] == ACCOUNT
            assert incident.payload["symbol"] == SYMBOL and incident.payload["sell_submitted"] is False
            assert session.scalar(select(AccountPosition.quantity)) == 1095
    finally:
        if capture is not None:
            sql_event.remove(case.factory.kw["bind"], "before_cursor_execute", capture)
        case.factory.kw["bind"].dispose()


@pytest.mark.asyncio
async def test_schwab_page_write_failure_still_never_sells_and_reports_unknown_delivery(monkeypatch, caplog):
    case = fallback_case(monkeypatch, owned=None, projection="virtual")
    try:
        with case.factory.begin() as session:
            parent = TradeIntent(id=uuid4(), strategy_id=case.strategy_id, broker_account_id=case.account_id,
                symbol=SYMBOL, side="sell", intent_type="close", quantity=95, status="rejected",
                reason="CONTROLLED original stop rejection", payload={})
            session.add(parent)
            session.flush()
            original = BrokerOrder(id=uuid4(), intent_id=parent.id, strategy_id=case.strategy_id,
                broker_account_id=case.account_id, client_order_id=case.request.client_order_id,
                broker_order_id=None, symbol=SYMBOL, side="sell", quantity=95, status="rejected",
                order_type="stop", time_in_force="day", payload={})
            session.add(original)
            original_id = original.id
            real_add = session.add

            def page_unavailable(instance, *args, **kwargs):
                if isinstance(instance, SystemIncident):
                    raise RuntimeError("CONTROLLED incident writer unavailable")
                return real_add(instance, *args, **kwargs)

            monkeypatch.setattr(session, "add", page_unavailable)
            await case.service._process_stop_reject_market_fallback(session=session,
                strategy=session.get(Strategy, case.strategy_id),
                broker_account=session.get(BrokerAccount, case.account_id),
                original_event=case.event, original_request=case.request,
                rejection_reason="CONTROLLED stop rejected")
        assert not case.broker.sent
        messages = caplog.messages
        critical = next(i for i, message in enumerate(messages) if "[SCHWAB-FALLBACK-OWNERSHIP-UNKNOWN]" in message)
        undelivered = next(i for i, message in enumerate(messages) if "[SCHWAB-FALLBACK-OWNERSHIP-PAGE-UNKNOWN]" in message)
        assert critical < undelivered
        with case.factory() as session:
            assert session.get(BrokerOrder, original_id).status == "rejected"
            assert session.get(TradeIntent, parent.id).status == "rejected"
            assert not session.scalars(select(SystemIncident)).all()  # Delivery is NOT claimed.
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("projection", ["virtual", "managed"])
@pytest.mark.parametrize("remaining", [0, 5])
async def test_schwab_cap_uses_current_book_after_broker_await_not_cached_session_row(monkeypatch, projection, remaining):
    case = fallback_case(monkeypatch, projection=projection)
    try:
        async def shrink_during_broker_read(**_kwargs):
            with case.factory.begin() as other:
                if projection == "virtual":
                    other.scalar(select(VirtualPosition).where(
                        VirtualPosition.strategy_id == case.strategy_id)).quantity = remaining
                else:
                    other.scalar(select(OmsManagedPosition).where(
                        OmsManagedPosition.strategy_code == "schwab_1m_v2")).current_quantity = remaining
            return Decimal(1000+remaining)

        case.service._refresh_broker_position_quantity = shrink_during_broker_read
        with case.factory.begin() as session:
            if projection == "virtual":
                loaded = session.scalar(select(VirtualPosition).where(VirtualPosition.strategy_id == case.strategy_id))
                assert loaded.quantity == 95
            else:
                loaded = session.scalar(select(OmsManagedPosition).where(OmsManagedPosition.strategy_code == "schwab_1m_v2"))
                assert loaded.current_quantity == 95
            # Hold the entity strongly, matching process_trade_intent's local
            # pre-await virtual_position; expire_on_commit=False is production.
            await case.service._process_stop_reject_market_fallback(session=session,
                strategy=session.get(Strategy, case.strategy_id),
                broker_account=session.get(BrokerAccount, case.account_id),
                original_event=case.event, original_request=case.request,
                rejection_reason="CONTROLLED stop rejected")
        assert [request.quantity for request in case.broker.sent] == ([Decimal(remaining)] if remaining else [])
    finally:
        case.factory.kw["bind"].dispose()


def orb_case(monkeypatch, *, raw="1010", after_release="1010"):
    # Use the actual country selector/builder: mutating a legacy two-share
    # order into ten would bypass ORB's real entry_quantity binding contract.
    broker = _EodBroker()
    service, factory, _ = _service(monkeypatch, broker=broker)
    service.settings.orb_country_routing_enabled = True
    service.settings.orb_webull_account_name = "live:orb"
    store = CountryStore(factory)
    store.put(CountryRecord("CLRO", "0000000001", "NY", "DE", True,
                           OPEN-timedelta(days=1), "READY", OPEN.date()))
    router = CountryRouter(store)
    router._records = store.load()
    selection = select_orb_country_route(router, "CLRO", OPEN,
        schwab_account=ACCOUNT, webull_account="live:orb")
    event = build_orb_schwab_open_intent(service.settings, "CLRO", Decimal("5.5284"), selection=selection)
    asyncio.run(service.process_trade_intent(event))
    bought = OPEN.replace(hour=14, minute=0)
    with factory.begin() as session:
        entry = session.scalar(select(BrokerOrder).where(BrokerOrder.side == "buy"))
        assert entry.quantity == 10 and entry.payload["orb_country_route"] == "true"
        entry.status = "filled"
        entry_id = entry.id
        session.add(Fill(order_id=entry.id, strategy_id=entry.strategy_id,
            broker_account_id=entry.broker_account_id, broker_fill_id="controlled-ownmix2-orb-buy10",
            symbol="CLRO", side="buy", quantity=10, price=Decimal("5.53"), filled_at=bought, payload={}))
        session.add(VirtualPosition(strategy_id=entry.strategy_id, broker_account_id=entry.broker_account_id,
            symbol="CLRO", quantity=10, average_price=Decimal("5.53"), opened_at=bought))
        raw_quantity = Decimal(raw)
        session.add(AccountPosition(broker_account_id=entry.broker_account_id,
            symbol="CLRO", quantity=(raw_quantity if raw_quantity.is_finite() and raw_quantity > 0 else Decimal(1010)),
            average_price=Decimal("5.53")))
    parent_read = broker.fetch_order_update

    async def read_parent(request):
        report = await parent_read(request)
        return replace(report, quantity=Decimal(10), filled_quantity=Decimal(10), origin="broker", reported_at=bought)

    broker.fetch_order_update = read_parent
    # This controlled adapter performs each list anew, never a cached snapshot.
    broker.positions_wire_read_age_seconds = lambda _account: 0.0
    broker.quantity = Decimal(raw)
    broker.after_release_quantity = Decimal(after_release)
    broker.sequence.clear()
    clock = [OPEN.replace(hour=19, minute=55)]
    monkeypatch.setattr("project_mai_tai.oms.service.utcnow", lambda: clock[0])
    return SimpleNamespace(service=service, factory=factory, broker=broker, entry_id=entry_id, clock=clock)


@pytest.mark.parametrize("raw", ["1010", "1010.25"], ids=["manual-whole", "manual-fractional-surplus"])
def test_orb_ten_plus_manual_thousand_closes_only_ten_once_after_pair_release(monkeypatch, raw):
    case = orb_case(monkeypatch, raw=raw, after_release=raw)
    try:
        asyncio.run(case.service._orb_schwab_eod_close())
        asyncio.run(case.service._orb_schwab_eod_close())
        closes = [request for request in case.broker.submitted if request.intent_type == "close"]
        assert len(closes) == 1 and closes[0].quantity == 10
        assert closes[0].broker_account_name == ACCOUNT and closes[0].strategy_code == "orb_schwab"
        assert case.broker.release_calls == [(ACCOUNT, "SCHWAB-ORB-1")]
        assert case.broker.sequence == ["parent", "positions", "release", "positions", "sell"]
        assert case.broker.quantity == Decimal(raw)-10
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 0
            fills = session.scalars(select(Fill).where(Fill.side == "sell")).all()
            assert len(fills) == 1 and fills[0].quantity == 10
            assert session.scalar(select(AccountPosition.quantity)) == Decimal(raw)-10
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("raw", ["0", "NaN", "Infinity"])
def test_orb_zero_or_nonfinite_never_releases_native_pair_or_sells_and_pages(monkeypatch, raw):
    case = orb_case(monkeypatch, raw=raw)
    try:
        asyncio.run(case.service._orb_schwab_eod_close())
        assert not case.broker.release_calls
        assert not [request for request in case.broker.submitted if request.intent_type == "close"]
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 10
            incidents = session.scalars(select(SystemIncident)).all()
            assert incidents and all(row.severity == "critical" for row in incidents)
            assert not session.scalars(select(Fill).where(Fill.side == "sell")).all()
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("after", ["0", "NaN", "Infinity"])
def test_orb_fresh_post_release_zero_or_nonfinite_still_prevents_sell(monkeypatch, after):
    case = orb_case(monkeypatch, after_release=after)
    try:
        asyncio.run(case.service._orb_schwab_eod_close())
        assert len(case.broker.release_calls) == 1
        assert not [request for request in case.broker.submitted if request.intent_type == "close"]
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 10
            assert session.scalar(select(SystemIncident)) is not None
            assert not session.scalars(select(Fill).where(Fill.side == "sell")).all()
    finally:
        case.factory.kw["bind"].dispose()


def later_buy(case, *, status, submitted_mode, fill_quantity=None, malformed=None):
    """Controlled order history; no missing Fill is relabelled a real execution."""
    with case.factory.begin() as session:
        old = session.get(BrokerOrder, case.entry_id)
        at = case.clock[0]-timedelta(seconds=1)
        unsent = status == "aborted" and submitted_mode == "null" and fill_quantity is None and malformed is None
        parent = TradeIntent(id=uuid4(), strategy_id=old.strategy_id,
            broker_account_id=old.broker_account_id, symbol=old.symbol, side="buy", intent_type="open",
            quantity=10, status=status, reason="CONTROLLED later ORB plan", payload={
                **({"refusal_origin": "skipped_before_submit", "refusal_code": "quote_changed_before_wire"} if unsent else {})},
            created_at=at, updated_at=at)
        session.add(parent)
        session.flush()
        assert old.id.int < (1 << 128)-1
        metadata = {key: value for key, value in deepcopy(old.payload).items() if key != "orb_schwab_eod_close"}
        order = BrokerOrder(id=UUID(int=old.id.int+1), intent_id=parent.id, strategy_id=old.strategy_id,
            broker_account_id=old.broker_account_id, client_order_id="controlled-ownmix2-newer-orb",
            broker_order_id=None if unsent else "controlled-newer-root", symbol=old.symbol, side="buy",
            quantity=10, status=status, order_type="limit", time_in_force="day", payload=metadata,
            submitted_at=(old.submitted_at if submitted_mode == "equal" else None
                          if submitted_mode == "null" else at), updated_at=at)
        session.add(order)
        session.flush()
        session.add(BrokerOrderEvent(order_id=order.id, event_type=status, event_at=at,
            event_source="client" if unsent else "broker",
            payload={"client_order_id": order.client_order_id, "broker_order_id": order.broker_order_id,
                "metadata": metadata, "filled_quantity": str(fill_quantity or 0)}))
        if fill_quantity is not None or malformed in {"negative", "wrong-side"}:
            session.add(Fill(order_id=order.id, strategy_id=old.strategy_id, broker_account_id=old.broker_account_id,
                broker_fill_id="controlled-later-orb-fill", symbol=old.symbol,
                side="sell" if malformed == "wrong-side" else "buy",
                quantity=Decimal(-1) if malformed == "negative" else Decimal(fill_quantity or 1),
                price=Decimal("5.53"), filled_at=at, payload={"source": "CONTROLLED"}))
        if malformed == "event-without-fill":
            session.add(BrokerOrderEvent(order_id=order.id, event_type="partially_filled", event_at=at,
                event_source="broker", payload={"client_order_id": order.client_order_id,
                    "broker_order_id": order.broker_order_id, "broker_fill_id": "controlled-unbooked-buy-fill",
                    "filled_quantity": "1", "metadata": metadata}))
        return order.id


@pytest.mark.parametrize("status", ["cancelled", "canceled", "aborted", "rejected", "expired"])
@pytest.mark.parametrize("submitted_mode", ["newer", "equal", "null"])
def test_orb_exact_entry_ignores_later_terminal_zero_fill_or_unsent_plan(monkeypatch, status, submitted_mode):
    case = orb_case(monkeypatch)
    try:
        later_id = later_buy(case, status=status, submitted_mode=submitted_mode)
        asyncio.run(case.service._orb_schwab_eod_close())
        closes = [request for request in case.broker.submitted if request.intent_type == "close"]
        assert len(closes) == 1 and closes[0].quantity == 10
        assert closes[0].metadata["entry_order_id"] == str(case.entry_id)
        assert case.broker.release_calls == [(ACCOUNT, "SCHWAB-ORB-1")]
        assert case.broker.quantity == 1000
        with case.factory() as session:
            assert session.get(BrokerOrder, later_id).status == status
            assert not session.scalars(select(Fill).where(Fill.order_id == later_id)).all()
            assert session.scalar(select(VirtualPosition.quantity)) == 0
            assert session.scalar(select(SystemIncident)) is None
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("read_number", [1, 2], ids=["before-native-release", "after-native-release"])
@pytest.mark.parametrize("submitted_mode", ["newer", "equal", "null"])
@pytest.mark.parametrize("status,fill_quantity", [("filled", 10), ("partially_filled", 1), ("cancelled", 1)])
def test_orb_later_actual_buy_fill_is_still_a_conflict_not_a_zero_fill_plan(monkeypatch, read_number, submitted_mode, status, fill_quantity):
    case = orb_case(monkeypatch)
    original_positions, reads = case.broker.list_account_positions, []

    async def positions(account):
        reads.append(account)
        if len(reads) == read_number:
            later_buy(case, status=status, submitted_mode=submitted_mode, fill_quantity=fill_quantity)
        return await original_positions(account)

    case.broker.list_account_positions = positions
    try:
        asyncio.run(case.service._orb_schwab_eod_close())
        assert len(reads) == read_number
        assert len(case.broker.release_calls) == read_number-1
        assert not [request for request in case.broker.submitted if request.intent_type == "close"]
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 10
            incident = session.scalar(select(SystemIncident))
            assert incident is not None and incident.severity == "critical"
            assert incident.payload["entry_order_id"] == str(case.entry_id)
            assert not session.scalars(select(Fill).where(Fill.side == "sell")).all()
    finally:
        case.factory.kw["bind"].dispose()


def test_orb_exact_filled_parent_does_not_need_timestamp_chronology_to_ignore_no_other_lot(monkeypatch):
    case = orb_case(monkeypatch)
    try:
        with case.factory.begin() as session:
            session.get(BrokerOrder, case.entry_id).submitted_at = None
        asyncio.run(case.service._orb_schwab_eod_close())
        assert len(case.broker.release_calls) == 1
        closes = [request for request in case.broker.submitted if request.intent_type == "close"]
        assert len(closes) == 1 and closes[0].quantity == 10
        assert closes[0].metadata["entry_order_id"] == str(case.entry_id)
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 0
            assert session.scalar(select(SystemIncident)) is None
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("remaining", [0, 5])
def test_orb_owned_balance_change_after_last_broker_read_before_prepare_never_sells(monkeypatch, remaining):
    case = orb_case(monkeypatch)
    original_db, changed = case.service._run_db, []

    async def change_before_prepare(work, **kwargs):
        if work.__name__ == "prepare":
            with case.factory.begin() as other:
                other.scalar(select(VirtualPosition)).quantity = remaining
            changed.append(work.__name__)
        return await original_db(work, **kwargs)

    case.service._run_db = change_before_prepare
    try:
        asyncio.run(case.service._orb_schwab_eod_close())
        assert changed == ["prepare"]
        assert case.broker.sequence == ["parent", "positions", "release", "positions"]
        assert len(case.broker.release_calls) == 1
        assert not [request for request in case.broker.submitted if request.intent_type == "close"]
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == remaining
            assert not session.scalars(select(Fill).where(Fill.side == "sell")).all()
            assert not session.scalars(select(BrokerOrder).where(BrokerOrder.side == "sell")).all()
            incident = session.scalar(select(SystemIncident))
            assert incident is not None and incident.severity == "critical"
            assert incident.payload["entry_order_id"] == str(case.entry_id)
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("before", ["9", "1010"], ids=["underbacked-both-reads", "underbacked-after-release"])
def test_orb_eod_preserves_legacy_broker_bounded_nine_share_close_with_incident(monkeypatch, before):
    # TOP16:16 explicitly replaces the earlier strict raw9 denial. This is
    # EOD-only, not a permission to sell target10 against a broker ceiling9.
    case = orb_case(monkeypatch, raw=before, after_release="9")
    try:
        asyncio.run(case.service._orb_schwab_eod_close())
        closes = [request for request in case.broker.submitted if request.intent_type == "close"]
        assert len(closes) == 1 and closes[0].quantity == 9
        assert closes[0].metadata["entry_order_id"] == str(case.entry_id)
        assert len(case.broker.release_calls) == 1 and case.broker.quantity == 0
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 1  # Never infer flat.
            incident = session.scalar(select(SystemIncident))
            assert incident is not None and incident.severity == "critical"
            assert incident.payload["entry_order_id"] == str(case.entry_id)
            assert "ownership_UNKNOWN" in incident.payload["reason"]
            fill, = session.scalars(select(Fill).where(Fill.side == "sell")).all()
            assert fill.quantity == 9
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("raw", ["9", "10", "1010"])
@pytest.mark.parametrize("fault_at", ["quantity-read", "prepare"])
def test_orb_unknown_own_book_pages_and_only_legacy_broker_within_target_can_close(monkeypatch, raw, fault_at):
    from project_mai_tai.oms import schwab_owned_positions as own_module

    case = orb_case(monkeypatch, raw=raw, after_release=raw)
    actual_own = own_module.own_open_quantity
    fail = [fault_at == "quantity-read"]
    observed = []

    def unreadable(*args, **kwargs):
        observed.append(fail[0])
        if fail[0]:
            raise ValueError("CONTROLLED unreadable own book")
        return actual_own(*args, **kwargs)

    actual_db = case.service._run_db

    async def fail_at_prepare(work, **kwargs):
        if work.__name__ == "prepare" and fault_at == "prepare":
            fail[0] = True
        return await actual_db(work, **kwargs)

    monkeypatch.setattr(own_module, "own_open_quantity", unreadable)
    case.service._run_db = fail_at_prepare
    try:
        asyncio.run(case.service._orb_schwab_eod_close())
        assert True in observed
        closes = [request for request in case.broker.submitted if request.intent_type == "close"]
        with case.factory() as session:
            incident = session.scalar(select(SystemIncident))
            assert incident is not None and incident.severity == "critical"
            assert incident.payload["entry_order_id"] == str(case.entry_id)
            if Decimal(raw) <= 10:
                assert len(closes) == 1 and closes[0].quantity == Decimal(raw)
                assert closes[0].reason == "ORB_END_OF_DAY_CLOSE"
                assert session.scalar(select(VirtualPosition.quantity)) == 10-Decimal(raw)
            else:
                assert not closes  # No fallback from UNKNOWN to manual excess.
                assert session.scalar(select(VirtualPosition.quantity)) == 10
                assert not session.scalars(select(Fill).where(Fill.side == "sell")).all()
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("read_number", [1, 2])
@pytest.mark.parametrize("malformed", ["negative", "wrong-side", "event-without-fill"])
def test_orb_later_terminal_order_with_malformed_or_unbooked_fill_proof_is_not_zero_fill(monkeypatch, read_number, malformed):
    case = orb_case(monkeypatch)
    original_positions, reads = case.broker.list_account_positions, []

    async def positions(account):
        reads.append(account)
        if len(reads) == read_number:
            later_buy(case, status="cancelled", submitted_mode="newer", malformed=malformed)
        return await original_positions(account)

    case.broker.list_account_positions = positions
    try:
        asyncio.run(case.service._orb_schwab_eod_close())
        assert len(reads) == read_number
        assert len(case.broker.release_calls) == read_number-1
        assert not [request for request in case.broker.submitted if request.intent_type == "close"]
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 10
            incident = session.scalar(select(SystemIncident))
            assert incident is not None and incident.severity == "critical"
            assert incident.payload["entry_order_id"] == str(case.entry_id)
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("field", ["broker_order_id", "client_order_id"])
@pytest.mark.parametrize("raw", ["10", "1010"])
def test_orb_exact_native_or_client_identity_change_never_uses_legacy_quantity_fallback(monkeypatch, field, raw):
    case = orb_case(monkeypatch, raw=raw, after_release=raw)
    original_positions = case.broker.list_account_positions

    async def changed(account):
        with case.factory.begin() as session:
            setattr(session.get(BrokerOrder, case.entry_id), field, "CONTROLLED-changed-entry-identity")
        return await original_positions(account)

    case.broker.list_account_positions = changed
    try:
        asyncio.run(case.service._orb_schwab_eod_close())
        assert not case.broker.release_calls
        assert not [request for request in case.broker.submitted if request.intent_type == "close"]
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 10
            incident = session.scalar(select(SystemIncident))
            assert incident is not None and incident.severity == "critical"
            assert incident.payload["entry_order_id"] == str(case.entry_id)
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("raw", ["10", "1010"])
def test_orb_manual_excess_and_legacy_bounded_case_still_require_terminal_native_release(monkeypatch, raw):
    case = orb_case(monkeypatch, raw=raw, after_release=raw)
    case.broker.release_result = "unanswerable"
    try:
        asyncio.run(case.service._orb_schwab_eod_close())
        assert len(case.broker.release_calls) == 1
        assert not [request for request in case.broker.submitted if request.intent_type == "close"]
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 10
            assert session.scalar(select(SystemIncident)).payload["reason"] == "sell_pair_release_unconfirmed"
    finally:
        case.factory.kw["bind"].dispose()


def test_orb_closed_incident_history_does_not_suppress_new_unknown_legacy_exit_page(monkeypatch):
    from project_mai_tai.oms import schwab_owned_positions as own_module

    case = orb_case(monkeypatch, raw="10", after_release="10")
    closed_id, reads = uuid4(), []

    def unreadable(*_args, **_kwargs):
        reads.append("CONTROLLED UNKNOWN")
        raise ValueError("CONTROLLED own projection unreadable")

    monkeypatch.setattr(own_module, "own_open_quantity", unreadable)
    try:
        with case.factory.begin() as session:
            session.add(SystemIncident(id=closed_id, service_name="oms-risk", severity="critical",
                status="closed", title=f"ORB close needs attention: CLRO on {ACCOUNT}",
                opened_at=case.clock[0]-timedelta(days=2), closed_at=case.clock[0]-timedelta(days=1),
                payload={"source": "orb_schwab_eod_close", "entry_order_id": str(case.entry_id),
                    "entry_broker_order_id": "SCHWAB-ORB-1", "broker_account_name": ACCOUNT,
                    "symbol": "CLRO", "reason": "CONTROLLED previously resolved incident",
                    "historical_receipt": {"preserve": "exact closed history"}}))
        with case.factory() as session:
            closed = session.get(SystemIncident, closed_id)
            before = (closed.status, closed.severity, closed.title, closed.opened_at,
                      closed.closed_at, deepcopy(closed.payload))
        asyncio.run(case.service._orb_schwab_eod_close())
        # Both broker quantity reads and locked prepare try the real incident
        # writer. A second EOD pass must neither duplicate the page nor SELL.
        assert len(reads) >= 3
        asyncio.run(case.service._orb_schwab_eod_close())
        closes = [request for request in case.broker.submitted if request.intent_type == "close"]
        assert len(closes) == 1 and closes[0].quantity == 10
        with case.factory() as session:
            rows = session.scalars(select(SystemIncident)).all()
            assert len(rows) == 2
            closed = session.get(SystemIncident, closed_id)
            assert (closed.status, closed.severity, closed.title, closed.opened_at,
                    closed.closed_at, closed.payload) == before
            fresh, = [row for row in rows if row.status == "open"]
            assert fresh.id != closed_id and fresh.severity == "critical" and fresh.closed_at is None
            assert fresh.payload["source"] == "orb_schwab_eod_close"
            assert fresh.payload["entry_order_id"] == str(case.entry_id)
            assert "ownership_UNKNOWN_legacy_eod" in fresh.payload["reason"]
            assert session.scalar(select(VirtualPosition.quantity)) == 0
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("zero", ["0", "0.00", "0.000", "0.0000", "0E-8", 0, 0.0])
def test_orb_terminal_zero_event_quantity_is_numeric_not_a_string_whitelist(monkeypatch, zero):
    case = orb_case(monkeypatch)
    try:
        later_id = later_buy(case, status="cancelled", submitted_mode="newer")
        with case.factory.begin() as session:
            event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == later_id))
            event.payload = {**event.payload, "filled_quantity": zero}
        asyncio.run(case.service._orb_schwab_eod_close())
        closes = [request for request in case.broker.submitted if request.intent_type == "close"]
        assert len(closes) == 1 and closes[0].quantity == 10
        assert closes[0].metadata["entry_order_id"] == str(case.entry_id)
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 0
            assert not session.scalars(select(Fill).where(Fill.order_id == later_id)).all()
            assert session.scalar(select(SystemIncident)) is None
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.parametrize("value", [None, "NaN", "malformed", "-1", "1", False])
def test_orb_present_unreadable_or_nonzero_event_quantity_is_not_absent_zero_fill(monkeypatch, value):
    case = orb_case(monkeypatch)
    try:
        later_id = later_buy(case, status="cancelled", submitted_mode="newer")
        with case.factory.begin() as session:
            event = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == later_id))
            event.payload = {**event.payload, "filled_quantity": value}
        asyncio.run(case.service._orb_schwab_eod_close())
        assert not case.broker.release_calls
        assert not [request for request in case.broker.submitted if request.intent_type == "close"]
        with case.factory() as session:
            assert session.scalar(select(VirtualPosition.quantity)) == 10
            incident = session.scalar(select(SystemIncident))
            assert incident is not None and incident.severity == "critical"
            assert incident.payload["entry_order_id"] == str(case.entry_id)
    finally:
        case.factory.kw["bind"].dispose()
