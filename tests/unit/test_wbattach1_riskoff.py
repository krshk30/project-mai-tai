"""Source-authored WBATTACH1 controls. Broker replies are synthetic, not live fills."""

from dataclasses import replace
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot, ExecutionReport, ExitPairReleaseResult, OrderRequest
from project_mai_tai.broker_adapters import protocols as protocols_module
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter, WebullPositionsUnavailable
from project_mai_tai.db.base import Base
from project_mai_tai.db import models as models_module
from project_mai_tai.db.models import AccountPosition, BrokerOrder, BrokerOrderEvent, Fill, OmsManagedPosition, SystemIncident, TradeIntent, VirtualPosition
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import orb_country, orb_schwab_eod, service as oms, store as store_module, webull_attach_riskoff as wb
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_schwab_bracket import build_orb_schwab_bracket_metadata

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)


class Redis:
    async def xadd(self, *_args, **_kwargs):
        return "1-0"


class Broker:
    def __init__(self, service, factory, account):
        self.service, self.factory, self.account = service, factory, account
        self.requests, self.cancelled = [], []
        self.mode = "accepted"
        self.reject_http, self.reject_code = "417", "CONTROLLED_REFUSAL"
        self.child_state = None
        self.confirmed_bases = []
        self.total = Decimal(1010)
        self.age = 0
        self.known = True
        self.child_filled = Decimal(0)
        self.child_applied = False
        self.child_unknown = False
        self.base = None
        self.close_pending = False
        self.close_applied = False
        self.close_barrier = None

    def positions_wire_read_age_seconds(self, _account):
        return self.age

    async def list_account_positions(self, account):
        if not self.known:
            raise WebullPositionsUnavailable("CONTROLLED incomplete positions read")
        return [BrokerPositionSnapshot(broker_account_name=account, symbol="ABC", quantity=self.total,
                                       average_price=Decimal(10))]

    def report(self, coid, event="cancelled", *, filled=Decimal(0), quantity=Decimal(10)):
        return ExecutionReport(event, coid, broker_order_id="broker-" + coid,
            broker_fill_id="fill-" + coid if filled else None, symbol="ABC", side="sell",
            intent_type="close", quantity=quantity, filled_quantity=filled,
            fill_price=Decimal(11) if filled else None, reported_at=NOW, origin="broker",
            metadata={"cancel_outcome": "confirmed", "orbroute1_fresh_read": "true"})

    async def submit_order(self, request):
        self.requests.append(request)
        if request.metadata.get("webull_exit_only_pair") == "true":
            self.base = request.client_order_id
            # The actual OMS must persist the original address before broker await.
            with self.factory() as session:
                entry = session.scalar(select(BrokerOrder).where(BrokerOrder.side == "buy"))
                if request.strategy_code == "schwab_1m_v2":
                    assert entry.payload[wb.KEY]["attempts"][-1]["base"] == self.base
                else:
                    assert entry.payload[orb_country.PROTECTION]["base"] == self.base
            if self.mode == "late":
                raise TimeoutError("CONTROLLED applied pair / lost ACK")
            if self.mode == "reject":
                return [ExecutionReport("rejected", request.client_order_id, symbol="ABC", side="sell",
                    intent_type="close", quantity=request.quantity, origin="broker", reported_at=NOW,
                    metadata={"webull_http_status": self.reject_http, "webull_error_code": self.reject_code})]
            return [ExecutionReport("accepted", request.client_order_id, broker_order_id="pair-root",
                symbol="ABC", side="sell", intent_type="close", quantity=request.quantity,
                origin="broker", reported_at=NOW)]
        if self.close_barrier is not None:
            await self.close_barrier()
        if self.close_pending:
            return [ExecutionReport("accepted", request.client_order_id, broker_order_id="owned-close",
                symbol="ABC", side="sell", intent_type="close", quantity=request.quantity,
                reported_at=NOW, origin="broker")]
        self.total -= request.quantity
        self.close_applied = True
        return [ExecutionReport("filled", request.client_order_id, broker_order_id="owned-close",
            broker_fill_id="owned-close-fill", symbol="ABC", side="sell", intent_type="close",
            quantity=request.quantity, filled_quantity=request.quantity, fill_price=Decimal(10),
            reported_at=NOW, origin="broker")]

    async def cancel_exit_pair(self, **binding):
        self.cancelled.append(binding["base_client_order_id"])
        return []  # ACK is deliberately not terminal evidence.

    async def confirm_exit_pair_terminal(self, **binding):
        base = binding["base_client_order_id"]
        self.confirmed_bases.append(base)
        if self.child_unknown is True:
            return ExitPairReleaseResult(outcome="unanswerable", reports=())
        if self.child_state == "absent":
            return ExitPairReleaseResult(outcome="released", reports=tuple(
                ExecutionReport("cancelled", WebullBrokerAdapter._combo_leg_coid(base, suffix),
                    symbol="ABC", side="sell", intent_type="cancel", quantity=Decimal(0), origin="broker",
                    reported_at=NOW, metadata={"eod_exit_pair_confirmation": "true",
                        "cancel_outcome": "already_absent"}) for suffix in ("T", "S")))
        if self.child_state == "working":
            return ExitPairReleaseResult(outcome="reserved", reports=tuple(
                self.report(WebullBrokerAdapter._combo_leg_coid(base, suffix), event="accepted")
                for suffix in ("T", "S")))
        if not self.child_applied:
            self.total -= self.child_filled
            self.child_applied = True
        reports = tuple(None if self.child_unknown == suffix else
            self.report(WebullBrokerAdapter._combo_leg_coid(base, suffix),
                event="filled" if self.child_filled == 10 and suffix == "T" else "cancelled",
                filled=self.child_filled if suffix == "T" else Decimal(0)) for suffix in ("T", "S"))
        return ExitPairReleaseResult(outcome="released", reports=reports)

    async def confirm_exit_pair_from_today_list(self, *, quantity, after=None, original_attempts=None, **binding):
        # Synthetic complete-census boundary; real paging/parser controls are separate.
        result = await self.confirm_exit_pair_terminal(**binding)
        proof = json.dumps({"broker_account_name": self.account, "symbol": binding["symbol"],
            "base": binding["base_client_order_id"], "complete": True, "truncated": False,
            "original_bases": sorted(a["base"] for a in original_attempts or [{"base": binding["base_client_order_id"]}]),
            "pages": 1, "started_at": wb.datetime.now(UTC).isoformat(),
            "acquired_at": wb.datetime.now(UTC).isoformat()})
        return replace(result, reports=tuple(replace(r, metadata={**r.metadata,
            "wbattach1_list_census": proof}) if r is not None else None for r in result.reports))

    async def fetch_order_update(self, request):
        if request.side == "buy":
            return ExecutionReport("filled", request.client_order_id, broker_order_id="entry-native",
                broker_fill_id="entry-fill", symbol="ABC", side="buy", intent_type="open",
                quantity=Decimal(10), filled_quantity=Decimal(10), fill_price=Decimal(10),
                reported_at=NOW, origin="broker")
        previous = next((r for r in self.requests if r.client_order_id == request.client_order_id
            and r.metadata.get("webull_exit_only_pair") != "true"), None)
        if previous is not None:
            if not self.close_pending and not self.close_applied:
                self.total -= previous.quantity
                self.close_applied = True
            return ExecutionReport("accepted" if self.close_pending else "filled", request.client_order_id,
                broker_order_id="owned-close", broker_fill_id=None if self.close_pending else "owned-close-fill",
                symbol="ABC", side="sell", intent_type="close", quantity=previous.quantity,
                filled_quantity=Decimal(0) if self.close_pending else previous.quantity,
                fill_price=None if self.close_pending else Decimal(10), reported_at=NOW, origin="broker")
        if self.child_unknown is True or self.child_unknown == request.client_order_id[-1:]:
            return None
        filled = self.child_filled if request.client_order_id.endswith("T") else Decimal(0)
        if filled and not self.child_applied:
            self.total -= filled
            self.child_applied = True
        status = "filled" if filled == 10 else "cancelled" if self.cancelled else "partially_filled" if filled else "accepted"
        report = self.report(request.client_order_id, event=status, filled=filled)
        return replace(report, metadata={**report.metadata, **request.metadata})


@pytest.fixture(autouse=True)
def make_case(tmp_path, monkeypatch):
    engines = []
    clock = [NOW]
    monkeypatch.setattr(oms, "utcnow", lambda: clock[0])
    monkeypatch.setattr(oms, "_is_regular_market_session", lambda now=None: orb_country._rth(clock[0]))
    monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: None)

    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    for module in (oms, wb, orb_country, orb_schwab_eod, store_module, models_module, protocols_module):
        monkeypatch.setattr(module, "datetime", Clock)

    def make(strategy="schwab_1m_v2", account="live:orb", *, provenance=True, producer=False,
             control=False, protect_interval=0.0):
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path / ('case-' + str(len(engines)) + '.sqlite')}",
                               connect_args={"check_same_thread": False})
        engines.append(engine)
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        settings = Settings(_env_file=None, bot_control_enabled=control,
            oms_v2_exit_management_enabled=True, oms_v2_exit_close_on_fill_enabled=True,
            oms_v2_exit_release_reservation_enabled=True,
            strategy_schwab_1m_v2_webull_account_name=account,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            orb_webull_account_name=account, orb_live_schwab_orders_enabled=True,
            orb_country_routing_enabled=True)
        if protect_interval is not None:
            object.__setattr__(settings, "oms_webull_protect_interval_seconds", protect_interval)
        service = OmsRiskService(settings=settings, redis_client=Redis(), session_factory=factory,
                                 broker_adapter=None)
        broker = Broker(service, factory, account)
        service.broker_adapter = broker
        async def handover(*_args, **_kwargs):
            return True
        service._v2_eod_handover_ready = handover
        async def quote(**_kwargs):
            return {}
        service._fetch_quote_for_order = quote
        metadata = {"fanout_leg": "webull"}
        if strategy == "orb_schwab":
            original = build_orb_schwab_bracket_metadata(Decimal(10))
            metadata = {"orb_country_route": "true", "orb_country_broker": "webull",
                "orb_country_size": "10", "orb_intended_break_level": "10",
                "stop_price": original["stop_price"], "orb_target_price": original["bracket_target_price"],
                "orb_protect_price": original["bracket_stop_price"]}
        with factory.begin() as session:
            bot = service.store.ensure_strategy(session, strategy, execution_mode="live")
            acct = service.store.ensure_broker_account(session, account, provider="webull", environment="production")
            event = TradeIntentEvent(source_service="CONTROLLED", payload=TradeIntentPayload(
                strategy_code=strategy, broker_account_name=account, symbol="ABC", side="buy",
                intent_type="open", quantity=Decimal(10), reason="CONTROLLED own bare entry", metadata=metadata))
            intent = service.store.create_trade_intent(session, strategy=bot, broker_account=acct, event=event)
            intent.status = "pending" if producer else "filled"
            entry = service.store.get_or_create_order(session, intent=intent, strategy_id=bot.id,
                broker_account_id=acct.id, client_order_id="owned-entry", symbol="ABC", side="buy",
                quantity=Decimal(10), metadata=metadata, broker_order_id="entry-native",
                status="accepted" if producer else "filled")
            if not producer:
                session.add(Fill(order_id=entry.id, strategy_id=bot.id, broker_account_id=acct.id,
                    broker_fill_id="entry-fill", symbol="ABC", side="buy", quantity=Decimal(10),
                    price=Decimal(10), filled_at=NOW, payload={"source": "CONTROLLED"}))
            session.add(VirtualPosition(strategy_id=bot.id, broker_account_id=acct.id, symbol="ABC",
                                        quantity=Decimal(0 if producer else 10), average_price=Decimal(10)))
            session.add(AccountPosition(broker_account_id=acct.id, symbol="ABC", quantity=Decimal(1000 if producer else 1010),
                                       average_price=Decimal(10), source_updated_at=NOW))
            if strategy == "schwab_1m_v2" and not producer:
                managed = service.store.create_managed_position(session, strategy_code=strategy,
                    broker_account_name=account, symbol="ABC", entry_price=Decimal(10), quantity=10,
                    entry_order_id=entry.id, entry_client_order_id=entry.client_order_id, entry_time=NOW)
                if provenance:
                    entry.payload = {**entry.payload, wb.BARE_FILL_PROOF: {
                        "entry_id": str(entry.id), "entry_coid": entry.client_order_id,
                        "managed_id": str(managed.id), "account_id": str(acct.id),
                        "strategy_id": str(bot.id), "source": "new_bare_fill_writer"}}
            entry_id = entry.id
        return service, factory, broker, entry_id, clock
    yield make
    for engine in engines:
        engine.dispose()


async def attach(case):
    service, _, broker, _, _ = case
    return await service._attach_webull_protection(broker_account_name=broker.account,
        symbol="ABC", quantity=10, entry_price=10., strategy_code="schwab_1m_v2",
        entry_client_order_id="owned-entry")


def closes(broker):
    return [r for r in broker.requests if r.metadata.get("webull_exit_only_pair") != "true"]


def restarted(case):
    old, factory, broker, entry_id, clock = case
    service = OmsRiskService(settings=old.settings, redis_client=Redis(), session_factory=factory,
                             broker_adapter=broker)
    service._v2_eod_handover_ready = old._v2_eod_handover_ready
    service._fetch_quote_for_order = old._fetch_quote_for_order
    broker.service = service
    return service, factory, broker, entry_id, clock


def crossed(case, direction):
    service = case[0]
    async def quote(**_kwargs):
        price = 8 if direction == "stop" else 12
        return {"bid_price": price, "ask_price": price, "last_price": price}
    service._fetch_quote_for_order = quote


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
@pytest.mark.parametrize("direction", ["stop", "target"])
async def test_atr_real_owner_crossed_price_market_own_ten_leaves_manual_thousand(make_case, account, direction):
    case = make_case(account=account)
    crossed(case, direction)
    assert await attach(case) is False
    assert len(closes(case[2])) == 1
    request = closes(case[2])[0]
    assert (request.broker_account_name, request.strategy_code, request.quantity, request.order_type) == (
        account, "schwab_1m_v2", Decimal(10), "market")
    assert case[2].total == 1000


@pytest.mark.asyncio
async def test_normal_atr_first_attach_retains_geometry_and_no_close(make_case):
    case = make_case()
    assert await attach(case) is True
    assert len(case[2].requests) == 1 and not closes(case[2])
    assert case[2].requests[0].metadata["bracket_target_price"] == "10.2000"
    assert case[2].requests[0].metadata["bracket_stop_price"] == "9.5000"


@pytest.mark.asyncio
@pytest.mark.parametrize("marker,value", [
    (wb.KEY, None), (wb.KEY, {}), (wb.KEY, False), (wb.KEY, []),
    (wb.KEY, {"attempts": []}), (wb.BARE_FILL_PROOF, None),
    (wb.BARE_FILL_PROOF, {}), (wb.BARE_FILL_PROOF, {"entry_id": "foreign"}),
])
async def test_malformed_strict_marker_never_falls_back_to_legacy(make_case, monkeypatch, caplog, marker, value):
    case = make_case()
    service, factory, broker, entry_id, _ = case
    with factory.begin() as session:
        entry = session.get(BrokerOrder, entry_id)
        entry.payload = {**entry.payload, marker: value}
    async def legacy_quantity(**_kwargs):
        pytest.fail("marked strict episode reached legacy protection")
    monkeypatch.setattr(service, "_v2_webull_protection_owned_quantity", legacy_quantity)
    original_handlers = tuple(service.logger.handlers)
    original_propagate = service.logger.propagate
    added = caplog.handler not in service.logger.handlers
    if added:
        service.logger.addHandler(caplog.handler)
    try:
        assert not await attach(case)
        assert broker.requests == [] and not broker.cancelled
        with factory() as session:
            assert session.get(BrokerOrder, entry_id).payload[marker] == value
            assert session.scalar(select(Fill).where(Fill.side == "sell")) is None
            incident = session.scalar(select(SystemIncident).where(
                SystemIncident.service_name == "oms-risk", SystemIncident.status == "open",
                SystemIncident.payload["source"].as_string() == wb.KEY,
                SystemIncident.payload["entry_id"].as_string() == str(entry_id),
                SystemIncident.payload["account"].as_string() == broker.account,
                SystemIncident.payload["symbol"].as_string() == "ABC"))
            assert incident is not None and incident.severity == "critical"
            assert incident.payload["entry_client_order_id"] == "owned-entry"
            assert incident.payload["entry_identity"] == "canonical"
            assert incident.payload["sell"] == "BLOCKED"
            assert incident.payload["reason"] in {
                "wbattach1_journal_corrupt", "wbattach1_journal_episode_changed",
                "wbattach1_bare_fill_proof_corrupt"}
        assert "WBATTACH1-BLOCKED" in caplog.text
        assert any(record.name == service.logger.name and record.levelno == logging.CRITICAL
                   and "WBATTACH1-BLOCKED" in record.getMessage() for record in caplog.records)
    finally:
        if added:
            service.logger.removeHandler(caplog.handler)
        assert tuple(service.logger.handlers) == original_handlers
        assert service.logger.propagate == original_propagate


@pytest.mark.asyncio
async def test_real_producer_stamp_selects_strict_not_legacy_pair(make_case, monkeypatch):
    case = make_case(producer=True)
    await record_real_bare_fill(case)
    async def legacy_quantity(**_kwargs):
        pytest.fail("producer-stamped episode reached legacy protection")
    monkeypatch.setattr(case[0], "_v2_webull_protection_owned_quantity", legacy_quantity)
    assert await attach(case)
    assert len(case[2].requests) == 1 and not closes(case[2])
    with case[1]() as session:
        entry = session.get(BrokerOrder, case[3])
        assert entry.payload[wb.KEY]["history_proven"] is True
        assert len(session.scalars(select(Fill).where(Fill.side == "buy")).all()) == 1


@pytest.mark.parametrize("marker", [wb.KEY, wb.BARE_FILL_PROOF])
def test_bare_stamp_cannot_overwrite_an_empty_strict_marker(make_case, marker):
    case = make_case(provenance=False)
    with case[1].begin() as session:
        entry = session.get(BrokerOrder, case[3])
        row = session.scalar(select(OmsManagedPosition))
        entry.payload = {**entry.payload, marker: {}}
        assert not wb.stamp_new_bare_fill(session, row, entry_id=entry.id,
            coid=entry.client_order_id, quantity=Decimal(10))
        assert entry.payload[marker] == {}
        if marker == wb.KEY:
            assert wb.BARE_FILL_PROOF not in entry.payload


@pytest.mark.asyncio
async def test_five_proven_refusals_then_same_owner_one_market_exit(make_case):
    case = make_case()
    case[2].mode = "reject"
    await attach(case)
    pairs = [r for r in case[2].requests if r.metadata.get("webull_exit_only_pair") == "true"]
    assert len(pairs) == 5 and len(closes(case[2])) == 1


RETRY_ATTRIBUTES = (
    "oms_webull_protect_attempts", "oms_webull_protect_interval_seconds",
    "oms_webull_protect_backoff_multiplier", "oms_webull_protect_max_interval_seconds",
)


def default_settings_case(make_case, account):
    case = make_case(account=account, protect_interval=None)
    assert isinstance(case[0].settings, Settings)
    assert all(not hasattr(case[0].settings, name) for name in RETRY_ATTRIBUTES)
    return case


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
async def test_real_settings_without_retry_attributes_first_attach(make_case, account):
    case = default_settings_case(make_case, account)
    assert await attach(case) is True
    assert len(case[2].requests) == 1 and not closes(case[2])
    request = case[2].requests[0]
    assert request.metadata["bracket_target_price"] == "10.2000"
    assert request.metadata["bracket_stop_price"] == "9.5000"
    assert request.broker_account_name == account and request.quantity == 10
    assert case[2].total == 1010


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
@pytest.mark.parametrize("direction", ["stop", "target"])
async def test_real_settings_without_retry_attributes_crossed_price_owner(make_case, account, direction):
    case = default_settings_case(make_case, account)
    crossed(case, direction)
    assert await attach(case) is False
    assert len(closes(case[2])) == 1
    request = closes(case[2])[0]
    assert (request.broker_account_name, request.strategy_code, request.quantity, request.order_type) == (
        account, "schwab_1m_v2", Decimal(10), "market")
    assert case[2].total == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
async def test_real_settings_without_retry_attributes_five_proven_refusals(make_case, monkeypatch, account):
    case = default_settings_case(make_case, account)
    broker = case[2]
    broker.total, broker.mode, broker.child_state = Decimal(1010), "reject", "absent"
    broker.reject_code = "TRADE_PLACE_ORDER_REPEAT"
    delays = []

    async def controlled_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(wb.asyncio, "sleep", controlled_sleep)
    assert await attach(case) is False
    pairs = [r for r in broker.requests if r.metadata.get("webull_exit_only_pair") == "true"]
    assert len(pairs) == 5 and len({r.client_order_id for r in pairs}) == 5
    assert broker.confirmed_bases[:5] == [r.client_order_id for r in pairs]
    assert set(broker.confirmed_bases) == {r.client_order_id for r in pairs}
    assert delays == [2.0, 4.0, 8.0, 15.0]
    assert len(closes(broker)) == 1 and closes(broker)[0].quantity == 10
    assert broker.total == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides,pair_count,expected_delays", [
    ({"oms_webull_protect_attempts": 0}, 1, []),
    ({"oms_webull_protect_attempts": 3, "oms_webull_protect_interval_seconds": -1}, 3, []),
    ({"oms_webull_protect_backoff_multiplier": 0.5}, 5, [2.0, 2.0, 2.0, 2.0]),
    ({"oms_webull_protect_max_interval_seconds": -1}, 5, [0.0, 0.0, 0.0, 0.0]),
])
async def test_explicit_retry_fixture_overrides_use_legacy_clamps(
    make_case, monkeypatch, overrides, pair_count, expected_delays
):
    case = default_settings_case(make_case, "live:orb")
    for name, value in overrides.items():
        object.__setattr__(case[0].settings, name, value)
    broker = case[2]
    broker.mode, broker.child_state = "reject", "absent"
    delays = []

    async def controlled_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(wb.asyncio, "sleep", controlled_sleep)
    assert await attach(case) is False
    pairs = [r for r in broker.requests if r.metadata.get("webull_exit_only_pair") == "true"]
    assert len(pairs) == pair_count and len(closes(broker)) == 1
    assert set(broker.confirmed_bases) == {r.client_order_id for r in pairs}
    assert delays == expected_delays
    assert broker.total == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("child_unknown", [False, True])
async def test_applied_attach_lost_ack_original_pair_cancelled_once_or_blocks(make_case, child_unknown):
    case = make_case()
    case[2].mode, case[2].child_unknown = "late", child_unknown
    await attach(case)
    assert case[2].cancelled == [] and closes(case[2]) == []
    await wb.request_atr_exit(case[0], case[2].account, "ABC", "owned-entry", "CONTROLLED_REAL_EXIT")
    assert len(case[2].cancelled) == 1
    assert len(closes(case[2])) == (0 if child_unknown else 1)
    assert len([r for r in case[2].requests if r.metadata.get("webull_exit_only_pair") == "true"]) == 1


@pytest.mark.asyncio
async def test_cancelled_partial_child_deducted_before_residual_eight(make_case):
    case = make_case()
    case[2].mode, case[2].child_filled = "late", Decimal(2)
    await attach(case)
    assert case[2].cancelled == [] and closes(case[2]) == []
    await wb.request_atr_exit(case[0], case[2].account, "ABC", "owned-entry", "CONTROLLED_REAL_EXIT")
    assert [r.quantity for r in closes(case[2])] == [Decimal(8)]
    assert case[2].total == 1000
    with case[1]() as session:
        children = session.scalars(select(BrokerOrder).where(
            BrokerOrder.payload["wbattach1_child"].as_string() == "true")).all()
        assert len(children) == 1 and children[0].status == "cancelled"
        fill = session.scalar(select(Fill).where(Fill.order_id == children[0].id))
        assert fill.quantity == 2 and fill.price == 11
        assert fill.payload["metadata"]["webull_terminal_with_partial_fill"] == "cancelled"
        assert session.scalar(select(VirtualPosition.quantity)) == 0
        assert session.scalar(select(AccountPosition.quantity)) == 1000
    await attach(case)
    assert [r.quantity for r in closes(case[2])] == [Decimal(8)]


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
async def test_orb_initial_attach_sizes_own_lot_not_manual_account_total(make_case, account):
    case = make_case("orb_schwab", account)
    await orb_country.reconcile_one(case[0], case[3])
    pairs = [r for r in case[2].requests if r.metadata.get("webull_exit_only_pair") == "true"]
    assert len(pairs) == 1 and pairs[0].quantity == 10
    assert case[2].total == 1010 and not closes(case[2])
    with case[1]() as session:
        assert session.scalar(select(VirtualPosition.quantity)) == 10
        assert session.scalar(select(AccountPosition.quantity)) == 1010


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
@pytest.mark.parametrize("damage", ["short", "missing", "duplicate", "foreign-account", "nan", "decreased-before-wire"])
async def test_orb_attach_still_refuses_unproven_broker_backing(make_case, account, damage):
    case = make_case("orb_schwab", account)
    broker = case[2]
    reads = []

    async def positions(name):
        reads.append(name)
        quantity = Decimal(9) if damage == "short" or (
            damage == "decreased-before-wire" and len(reads) > 1
        ) else Decimal("NaN") if damage == "nan" else Decimal(1010)
        row = BrokerPositionSnapshot("live:foreign" if damage == "foreign-account" else name,
                                     "ABC", quantity, Decimal(10))
        return [] if damage == "missing" else [row, row] if damage == "duplicate" else [row]

    broker.list_account_positions = positions
    with pytest.raises(ValueError, match="orb_broker_backing_unproven"):
        await orb_country.reconcile_one(case[0], case[3])
    assert not broker.requests and not broker.cancelled
    assert len(reads) == (2 if damage == "decreased-before-wire" else 1)
    with case[1]() as session:
        assert session.scalar(select(VirtualPosition.quantity)) == 10
        assert session.scalar(select(AccountPosition.quantity)) == 1010


@pytest.mark.asyncio
async def test_known_filled_child_recorded_even_when_sibling_read_unknown_no_extra_sell(make_case):
    case = make_case()
    case[2].mode, case[2].child_filled, case[2].child_unknown = "late", Decimal(10), "S"
    await attach(case)
    await wb.request_atr_exit(case[0], case[2].account, "ABC", "owned-entry", "CONTROLLED_REAL_EXIT")
    assert not closes(case[2])
    with case[1]() as session:
        sold = session.scalars(select(Fill).where(Fill.side == "sell")).all()
        assert sum((f.quantity for f in sold), Decimal(0)) == 10
        assert session.scalar(select(VirtualPosition.quantity)) == 0


@pytest.mark.asyncio
async def test_restart_flat_atr_reads_original_sibling_without_recancel_or_sell(make_case):
    case = make_case()
    case[2].mode, case[2].child_filled, case[2].child_unknown = "late", Decimal(10), "S"
    await attach(case)
    await wb.request_atr_exit(case[0], case[2].account, "ABC", "owned-entry", "CONTROLLED_REAL_EXIT")
    case[2].child_unknown = False
    case = restarted(case)
    await wb.request_atr_exit(case[0], case[2].account, "ABC", "owned-entry", "CONTROLLED_REAL_EXIT")
    assert len(case[2].cancelled) == 1 and not closes(case[2])
    with case[1]() as session:
        entry = session.get(BrokerOrder, case[3])
        assert entry.payload[wb.KEY]["exit_phase"] == "resolved_by_child_fill"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_positions", "stale_positions", "no_provenance", "paper", "foreign"])
async def test_unproven_ownership_history_or_role_never_sells(make_case, failure, capsys):
    case = make_case(provenance=failure != "no_provenance")
    crossed(case, "stop")
    if failure == "missing_positions":
        case[2].known = False
    elif failure == "stale_positions":
        case[2].age = 31
    elif failure in {"paper", "foreign"}:
        with case[1].begin() as session:
            entry = session.get(BrokerOrder, case[3])
            if failure == "foreign":
                entry.client_order_id = "another-owner"
            else:
                from project_mai_tai.db.models import Strategy
                session.get(Strategy, entry.strategy_id).execution_mode = "paper"
    original_payload = None
    if failure == "no_provenance":
        with case[1]() as session:
            original_payload = dict(session.get(BrokerOrder, case[3]).payload)
            assert wb.KEY not in original_payload and wb.BARE_FILL_PROOF not in original_payload
    await attach(case)
    assert not closes(case[2])
    with case[1]() as session:
        if failure == "no_provenance":
            # TOP13:0x(2): unmarked legacy is protection-only, with its original
            # durable failure reporting, not a new strict risk-off incident.
            entry = session.get(BrokerOrder, case[3])
            assert entry.payload[case[0]._WEBULL_PROTECT_STATE_KEY] == "attach_failed"
            assert (case[2].account, "ABC") in case[0]._webull_protect_failed
            assert wb.KEY not in entry.payload and wb.BARE_FILL_PROOF not in entry.payload
            assert {key: entry.payload[key] for key in original_payload} == original_payload
            assert session.scalar(select(Fill).where(Fill.side == "sell")) is None
            output = capsys.readouterr().out
            assert "[WEBULL-PROTECT-UNPLACEABLE]" in output
            assert "[WEBULL-PROTECT-FAILED]" in output
        else:
            assert session.scalar(select(SystemIncident.id)) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [11, 20, 22])
async def test_non_rth_does_not_attach_or_sell(make_case, hour):
    case = make_case()
    case[4][0] = NOW.replace(hour=hour)
    await attach(case)
    assert not case[2].requests and not case[2].cancelled


@pytest.mark.asyncio
async def test_existing_c6_claim_prevents_attach_and_second_seller(make_case):
    case = make_case()
    case[0]._control_managed_exit_symbol_owners = {(case[2].account, "ABC"): object()}
    await attach(case)
    assert not case[2].requests and not case[2].cancelled


@pytest.mark.asyncio
async def test_real_c6_managed_owner_claim_spans_riskoff_broker_await(make_case):
    case = make_case()
    service, factory, broker, _, _ = case
    service.settings.bot_control_enabled = True
    crossed(case, "stop")
    entered, release = asyncio.Event(), asyncio.Event()
    async def paused():
        entered.set()
        await release.wait()
    broker.close_barrier = paused
    task = asyncio.create_task(attach(case))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        with factory() as session:
            row = service.store.get_open_managed_position(session, broker_account_name=broker.account, symbol="ABC")
            position, row_id = service._hydrate_v2_position(row), str(row.id)
        outcome = await service._emit_v2_exit_on_loop_claimed(broker.account, "ABC", position, 10.,
            kind="HARD", reference_price=9., reason="oms_v2_managed_exit:CW_HARD_STOP", bid=9.,
            close_on_fill=True, expected_managed_row_id=row_id)
        assert outcome == "refused"
    finally:
        release.set()
        await task
    assert len(closes(broker)) == 1 and broker.total == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["stop", "target"])
@pytest.mark.parametrize("account", ["live:orb", "live:webull-alt"])
async def test_orb_actual_reconcile_hands_off_to_existing_eod_owner(make_case, direction, account):
    case = make_case("orb_schwab", account)
    crossed(case, direction)
    await orb_country.reconcile_one(case[0], case[3])
    assert len(closes(case[2])) == 1
    request = closes(case[2])[0]
    assert (request.strategy_code, request.broker_account_name, request.quantity, request.order_type) == (
        "orb_schwab", account, Decimal(10), "market")
    assert case[2].total == 1000
    with case[1]() as session:
        entry = session.get(BrokerOrder, case[3])
        assert entry.payload[orb_schwab_eod._STATE]["reason"] == wb.REASON


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_orb_unknown_attach_uses_original_children_and_never_resends_pair(make_case, unknown):
    case = make_case("orb_schwab")
    case[2].total, case[2].mode, case[2].child_unknown = Decimal(10), "late", unknown
    with pytest.raises(TimeoutError):
        await orb_country.reconcile_one(case[0], case[3])
    await orb_country.reconcile_one(case[0], case[3])
    assert len(case[2].cancelled) == 1
    assert len([r for r in case[2].requests if r.metadata.get("webull_exit_only_pair") == "true"]) == 1
    assert len(closes(case[2])) == (0 if unknown else 1)


@pytest.mark.asyncio
async def test_orb_five_explicit_refusals_then_one_existing_owner_exit(make_case):
    case = make_case("orb_schwab")
    case[2].total, case[2].mode = Decimal(10), "reject"
    for _ in range(5):
        with pytest.raises(ValueError, match="orb_protection_broker_rejected"):
            await orb_country.reconcile_one(case[0], case[3])
        case[4][0] += timedelta(seconds=16)
    await orb_country.reconcile_one(case[0], case[3])
    assert len(case[2].requests) == 6
    assert len(closes(case[2])) == 1


@pytest.mark.asyncio
async def test_orb_accepted_exit_later_filled_after_restart_reads_original_never_resells(make_case):
    case = make_case("orb_schwab")
    crossed(case, "stop")
    case[2].close_pending = True
    await orb_country.reconcile_one(case[0], case[3])
    assert len(closes(case[2])) == 1
    case = restarted(case)
    case[2].close_pending = False
    await orb_country.reconcile_one(case[0], case[3])
    assert len(closes(case[2])) == 1 and case[2].total == 1000
    with case[1]() as session:
        assert session.scalar(select(VirtualPosition.quantity)) == 0
        assert session.get(BrokerOrder, case[3]).payload[orb_schwab_eod._STATE]["phase"] == "settled"


@pytest.mark.asyncio
async def test_orb_flat_native_unknown_stop_later_terminal_no_recancel_or_sell(make_case):
    case = make_case("orb_schwab")
    case[2].total, case[2].mode = Decimal(10), "late"
    case[2].child_filled, case[2].child_unknown = Decimal(10), "S"
    with pytest.raises(TimeoutError):
        await orb_country.reconcile_one(case[0], case[3])
    await orb_country.reconcile_one(case[0], case[3])
    assert not closes(case[2]) and len(case[2].cancelled) == 1
    case = restarted(case)
    case[2].child_unknown = False
    await orb_country.reconcile_one(case[0], case[3])
    assert not closes(case[2]) and len(case[2].cancelled) == 1
    with case[1]() as session:
        assert session.get(BrokerOrder, case[3]).payload[orb_schwab_eod._STATE]["phase"] == "resolved_by_child_fill"


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [11, 20, 22])
async def test_orb_non_rth_never_calls_riskoff_owner(make_case, hour):
    case = make_case("orb_schwab")
    case[4][0] = NOW.replace(hour=hour)
    crossed(case, "stop")
    # The old normal protection path may refuse the manual total; use no manual
    # holding here so this negative isolates session gating without changing it.
    case[2].total = Decimal(10)
    await orb_country.reconcile_one(case[0], case[3])
    assert not case[2].requests and not case[2].cancelled


@pytest.mark.parametrize("origin,http", [("unknown", "417"), ("broker", "500"), ("broker", "408"),
    ("broker", ""), ("broker", "417"), ("broker", "409"), ("broker", "429")])
def test_typed_rejection_is_not_no_wire_proof_without_actual_client_error(origin, http):
    request = wb.OrderRequest("pair", "live:orb", "schwab_1m_v2", "ABC", "sell", "close", Decimal(10), "test", {})
    report = ExecutionReport("rejected", "pair", symbol="ABC", side="sell", intent_type="close",
        quantity=Decimal(10), origin=origin, metadata={"webull_http_status": http, "webull_error_code": "CONTROLLED"})
    assert not wb.refused_before_creation([report], request)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
@pytest.mark.parametrize("http,code", [("417", "TRADE_PLACE_ORDER_REPEAT"), ("409", "ARBITRARY_CONFLICT")])
@pytest.mark.parametrize("child_state", ["working", "unknown"])
async def test_duplicate_or_conflict_never_replaces_original_tree_or_sells_without_terminal_children(
    make_case, strategy, http, code, child_state,
):
    case = make_case(strategy)
    broker = case[2]
    broker.total, broker.mode = Decimal(10), "reject"
    broker.reject_http, broker.reject_code = http, code
    broker.child_state = "working" if child_state == "working" else None
    broker.child_unknown = child_state == "unknown"
    if strategy == "schwab_1m_v2":
        await attach(case)
        await attach(restarted(case))
    else:
        with pytest.raises(ValueError, match="orb_protection_broker_rejected"):
            await orb_country.reconcile_one(case[0], case[3])
        await orb_country.reconcile_one(case[0], case[3])
        case = restarted(case)
        await orb_country.reconcile_one(case[0], case[3])
    pairs = [r for r in broker.requests if r.metadata.get("webull_exit_only_pair") == "true"]
    assert len(pairs) == 1 and not closes(broker)
    assert broker.confirmed_bases and set(broker.confirmed_bases) == {pairs[0].client_order_id}
    assert len(broker.cancelled) <= 1 and broker.total == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_repeat_response_with_original_target_filled_records_once_and_never_resells(make_case, strategy):
    case = make_case(strategy)
    broker = case[2]
    broker.total, broker.mode, broker.reject_code = Decimal(10), "reject", "TRADE_PLACE_ORDER_REPEAT"
    broker.child_filled, broker.child_unknown = Decimal(10), "S"
    if strategy == "schwab_1m_v2":
        await attach(case)
    else:
        with pytest.raises(ValueError, match="orb_protection_broker_rejected"):
            await orb_country.reconcile_one(case[0], case[3])
        await orb_country.reconcile_one(case[0], case[3])
    assert not closes(broker)
    with case[1]() as session:
        assert session.scalar(select(VirtualPosition.quantity)) == 0
        own_sells = session.scalars(select(Fill).where(Fill.side == "sell")).all()
        assert len(own_sells) == 1 and own_sells[0].quantity == 10
    case = restarted(case)
    broker.child_unknown = False
    if strategy == "schwab_1m_v2":
        await attach(case)
    else:
        await orb_country.reconcile_one(case[0], case[3])
    assert not closes(broker)
    assert len([r for r in broker.requests if r.metadata.get("webull_exit_only_pair") == "true"]) == 1
    with case[1]() as session:
        assert len(session.scalars(select(Fill).where(Fill.side == "sell")).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["schwab_1m_v2", "orb_schwab"])
async def test_five_rejections_need_actual_original_child_absence_before_retries_and_exit(make_case, strategy):
    case = make_case(strategy)
    broker = case[2]
    broker.total, broker.mode, broker.child_state = Decimal(10), "reject", "absent"
    broker.reject_code = "TRADE_PLACE_ORDER_REPEAT"  # Code is NOT absence authority.
    if strategy == "schwab_1m_v2":
        await attach(case)
    else:
        for _ in range(5):
            with pytest.raises(ValueError, match="orb_protection_broker_rejected"):
                await orb_country.reconcile_one(case[0], case[3])
            case[4][0] += timedelta(seconds=16)
        await orb_country.reconcile_one(case[0], case[3])
    pairs = [r for r in broker.requests if r.metadata.get("webull_exit_only_pair") == "true"]
    assert len(pairs) == 5 and len({r.client_order_id for r in pairs}) == 5
    assert set(broker.confirmed_bases) == {r.client_order_id for r in pairs}
    assert len(closes(broker)) == 1 and closes(broker)[0].quantity == 10


@pytest.mark.parametrize("damage", ["symbol", "coid", "origin", "marker", "positive-execution"])
def test_absent_child_proof_requires_exact_original_scope_and_real_reader_contract(damage):
    reports = [ExecutionReport("cancelled", WebullBrokerAdapter._combo_leg_coid("original", suffix),
        symbol="ABC", side="sell", intent_type="cancel", quantity=Decimal(0), origin="broker",
        reported_at=NOW, metadata={"eod_exit_pair_confirmation": "true", "cancel_outcome": "already_absent"})
        for suffix in ("T", "S")]
    changes = {"symbol": {"symbol": "OTHER"}, "coid": {"client_order_id": "foreign"},
        "origin": {"origin": "unknown"}, "marker": {"metadata": {"cancel_outcome": "already_absent"}},
        "positive-execution": {"filled_quantity": Decimal(1)}}
    reports[0] = replace(reports[0], **changes[damage])
    assert not wb.terminal_pair(reports, "original", Decimal(10), NOW, symbol="ABC")


async def record_real_bare_fill(case):
    """Actual report -> Fill/VP -> managed-row producer, not a seeded WB stamp."""
    service, factory, broker, entry_id, _ = case
    spawned = []
    service._spawn_webull_protection = lambda **kwargs: spawned.append(kwargs)
    with factory() as session:
        entry = session.get(BrokerOrder, entry_id)
        intent = session.get(TradeIntent, entry.intent_id)
        event = TradeIntentEvent(source_service="CONTROLLED", payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name=broker.account, symbol="ABC",
            side="buy", intent_type="open", quantity=Decimal(10), reason="CONTROLLED fill",
            metadata=dict(entry.payload)))
        request = OrderRequest(entry.client_order_id, broker.account, "schwab_1m_v2", "ABC",
            "buy", "open", Decimal(10), event.payload.reason, dict(entry.payload))
        report = ExecutionReport("filled", entry.client_order_id, broker_order_id="entry-native",
            broker_fill_id="entry-fill", symbol="ABC", side="buy", intent_type="open",
            quantity=Decimal(10), filled_quantity=Decimal(10), fill_price=Decimal(10),
            reported_at=NOW, origin="broker")
        await service._record_order_reports(session=session, intent=intent, strategy_id=entry.strategy_id,
            broker_account_id=entry.broker_account_id, intent_event=event, request=request, reports=[report])
        session.commit()
    return spawned


@pytest.mark.asyncio
async def test_actual_new_bare_fill_produces_stamp_before_spawn_and_reaches_owner(make_case):
    case = make_case(producer=True)
    spawned = await record_real_bare_fill(case)
    assert len(spawned) == 1 and spawned[0]["entry_client_order_id"] == "owned-entry"
    with case[1]() as session:
        entry = session.get(BrokerOrder, case[3])
        managed = session.scalar(select(OmsManagedPosition))
        assert entry.payload[wb.BARE_FILL_PROOF] == {
            "entry_id": str(entry.id), "entry_coid": entry.client_order_id,
            "managed_id": str(managed.id), "account_id": str(entry.broker_account_id),
            "strategy_id": str(entry.strategy_id), "source": "new_bare_fill_writer"}
        assert session.scalar(select(VirtualPosition.quantity)) == 10
    crossed(case, "stop")
    await attach(case)
    assert len(closes(case[2])) == 1 and case[2].total == 1000


@pytest.mark.asyncio
async def test_existing_managed_row_and_reprotect_never_mint_bare_fill_proof(make_case):
    case = make_case(provenance=False)
    with case[1].begin() as session:
        entry = session.get(BrokerOrder, case[3])
        case[0]._apply_managed_position_after_fill(session=session, strategy_code="schwab_1m_v2",
            broker_account_name=case[2].account, symbol="ABC", side="buy", intent_type="open",
            quantity=Decimal(10), price=Decimal(10), metadata={"fanout_leg": "webull"},
            entry_client_order_id=entry.client_order_id, entry_order_id=entry.id)
        assert wb.BARE_FILL_PROOF not in entry.payload
    crossed(case, "stop")
    await attach(case)
    assert not closes(case[2])
    with case[1]() as session:
        assert wb.BARE_FILL_PROOF not in session.get(BrokerOrder, case[3]).payload


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["paper", "foreign_account", "wrong_coid", "native", "prior_session", "no_fill"])
async def test_new_fill_stamp_requires_exact_canonical_bare_execution(make_case, mutation):
    case = make_case(provenance=False)
    with case[1].begin() as session:
        entry = session.get(BrokerOrder, case[3])
        row = session.scalar(select(OmsManagedPosition))
        if mutation == "paper":
            session.get(models_module.Strategy, entry.strategy_id).execution_mode = "paper"
        elif mutation == "foreign_account":
            row.broker_account_name = "live:foreign"
        elif mutation == "native":
            entry.payload = {**entry.payload, "native_oco_bracket": "true"}
        elif mutation == "prior_session":
            entry.submitted_at = NOW - timedelta(days=1)
        elif mutation == "no_fill":
            for fill in session.scalars(select(Fill)).all():
                session.delete(fill)
            session.flush()
        assert not wb.stamp_new_bare_fill(session, row, entry_id=entry.id,
            coid="foreign-coid" if mutation == "wrong_coid" else entry.client_order_id, quantity=Decimal(10))
        assert wb.BARE_FILL_PROOF not in entry.payload


async def prepare_boundary_contract(case, row, quantity):
    """O14 schema contract fixture, NOT proof that main's emitter calls the hook."""
    service, _, broker, _, _ = case
    metadata = {"oms_managed_row_id": str(row.id), "managed_sell_prepared_at": NOW.isoformat(),
                "oms_v2_managed_exit": "true"}
    event = TradeIntentEvent(source_service="CONTROLLED", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name=broker.account, symbol="ABC", side="sell",
        quantity=Decimal(quantity), intent_type="close", reason=wb.REASON, metadata=dict(metadata)))
    request = OrderRequest("prepared-" + str(event.event_id), broker.account, "schwab_1m_v2", "ABC",
        "sell", "close", Decimal(quantity), wb.REASON, dict(metadata), order_type="market", time_in_force="day")
    def prepare(session):
        entry = session.get(BrokerOrder, case[3])
        strategy = session.get(models_module.Strategy, entry.strategy_id)
        account = session.get(models_module.BrokerAccount, entry.broker_account_id)
        intent = service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        order = service.store.get_or_create_order(session, intent=intent, strategy_id=entry.strategy_id,
            broker_account_id=entry.broker_account_id, client_order_id=request.client_order_id,
            symbol="ABC", side="sell", quantity=request.quantity, metadata=dict(metadata), status="pending",
            order_type="market", time_in_force="day")
        order.submitted_at = None
    await service._run_db(prepare, commit=True)
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "stale", "ceiling", "own_qty", "lost_claim", "session", "child_task", "request_mutation", "foreign_prepared"])
async def test_real_claimed_owner_final_boundary_revalidates_after_preparation(make_case, mutation):
    case = make_case()
    service, factory, broker, _, clock = case
    service.settings.bot_control_enabled = True
    outcomes = []
    async def boundary(_session, row, *, quantity, **_kwargs):
        request = await prepare_boundary_contract(case, row, quantity)
        # Simulate state changing across O14's preparation await, without
        # replacing the upstream real claimed owner or strict native release.
        if mutation == "stale":
            broker.age = 31
        elif mutation == "ceiling":
            broker.total = Decimal(9)
        elif mutation == "own_qty":
            with factory.begin() as session:
                session.scalar(select(VirtualPosition)).quantity = Decimal(9)
        elif mutation == "lost_claim":
            service._control_managed_exit_symbol_owners.clear()
        elif mutation == "session":
            clock[0] = NOW.replace(hour=20)
        elif mutation == "foreign_prepared":
            with factory.begin() as session:
                order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
                foreign = service.store.ensure_broker_account(session, "live:foreign", provider="webull", environment="production")
                order.broker_account_id = foreign.id
        elif mutation == "request_mutation":
            original = broker.list_account_positions
            async def mutated_positions(account):
                request.metadata["route"] = "changed-during-final-await"
                return await original(account)
            broker.list_account_positions = mutated_positions
        try:
            if mutation == "child_task":
                await asyncio.create_task(wb.validate_atr_before_wire(service, request, str(row.id)))
            else:
                await wb.validate_atr_before_wire(service, request, str(row.id))
        except ValueError as exc:
            outcomes.append(str(exc))
        else:
            outcomes.append("validated")
            # Only this positive contract control enters a synthetic broker wire.
            await broker.submit_order(request)
        return []
    service._emit_v2_managed_sell = boundary
    crossed(case, "stop")
    await attach(case)
    assert len(outcomes) == 1
    if mutation is None:
        assert outcomes[0] == "validated"
    else:
        assert outcomes[0] != "validated"
    assert len(closes(broker)) == (1 if mutation is None else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "no_receipt", "other_event", "native_id", "submitted", "fill", "call_id"])
async def test_unsent_cleanup_requires_exact_o14_abort_event_active_call_and_no_wire(make_case, mutation):
    case = make_case()
    service, factory, broker, entry_id, _ = case
    outcomes = []
    async def boundary(_session, row, *, quantity, **_kwargs):
        request = await prepare_boundary_contract(case, row, quantity)
        broker.age = 31
        with pytest.raises(ValueError):
            await wb.validate_atr_before_wire(service, request, str(row.id))
        def abort(session):
            order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
            order.status = "aborted"
            session.get(TradeIntent, order.intent_id).status = "aborted"
            event = BrokerOrderEvent(order_id=order.id, event_type="aborted", event_source="client",
                event_at=NOW, payload={"proven_not_sent": True, "client_order_id": request.client_order_id,
                                      "metadata": dict(order.payload)})
            session.add(event)
            if mutation == "native_id":
                order.broker_order_id = "might-have-landed"
            elif mutation == "submitted":
                order.submitted_at = NOW
            elif mutation == "other_event":
                event.event_source = "broker"
            elif mutation == "fill":
                session.add(Fill(order_id=order.id, strategy_id=order.strategy_id,
                    broker_account_id=order.broker_account_id, broker_fill_id="ambiguous-execution",
                    symbol="ABC", side="sell", quantity=Decimal(1), price=Decimal(10), filled_at=NOW))
            elif mutation == "call_id":
                entry = session.get(BrokerOrder, entry_id)
                entry.payload = {**entry.payload, wb.KEY: {**entry.payload[wb.KEY], "exit_call_id": "other"}}
            session.flush()
            return event.id
        event_id = await service._run_db(abort, commit=True)
        try:
            await wb.release_atr_proven_unsent(service, request, str(row.id),
                abort_event_id=None if mutation == "no_receipt" else event_id)
        except ValueError:
            outcomes.append("blocked")
        else:
            outcomes.append("released")
        return []
    service._emit_v2_managed_sell = boundary
    crossed(case, "stop")
    await attach(case)
    assert outcomes == ["released" if mutation is None else "blocked"]
    assert not closes(broker)
    with factory() as session:
        state = session.get(BrokerOrder, entry_id).payload[wb.KEY]
        assert (state.get("exit_phase") is None) == (mutation is None)
        if mutation is None:
            assert state["unsent_handoffs"][-1]["outcome"] == "PROVEN_NOT_SENT"


@pytest.mark.asyncio
async def test_final_validator_outside_real_owner_refuses_before_database_or_broker(make_case):
    case = make_case()
    request = OrderRequest("not-a-real-call", case[2].account, "schwab_1m_v2", "ABC", "sell", "close",
        Decimal(10), wb.REASON, {}, order_type="market", time_in_force="day")
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("an unbound call reached the database")
    case[0]._run_db = forbidden
    with pytest.raises(ValueError, match="live_owner_call_unknown"):
        await wb.validate_atr_before_wire(case[0], request, "not-a-real-row")
    assert not case[2].requests


@pytest.mark.asyncio
async def test_proven_unsent_next_handoff_requires_new_actual_owner_and_fresh_proof(make_case):
    case = make_case()
    service, factory, broker, entry_id, _ = case
    calls, coids = [], []
    async def boundary(_session, row, *, quantity, **_kwargs):
        request = await prepare_boundary_contract(case, row, quantity)
        calls.append(wb._ATR_CALL.get().call_id)
        coids.append(request.client_order_id)
        if len(calls) == 1:
            broker.age = 31
            with pytest.raises(ValueError):
                await wb.validate_atr_before_wire(service, request, str(row.id))
            def abort(session):
                order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
                order.status = "aborted"
                session.get(TradeIntent, order.intent_id).status = "aborted"
                event = BrokerOrderEvent(order_id=order.id, event_type="aborted", event_source="client",
                    event_at=NOW, payload={"proven_not_sent": True, "client_order_id": request.client_order_id,
                                          "metadata": dict(order.payload)})
                session.add(event)
                session.flush()
                return event.id
            receipt = await service._run_db(abort, commit=True)
            await wb.release_atr_proven_unsent(service, request, str(row.id), abort_event_id=receipt)
            with pytest.raises(ValueError):
                await wb.validate_atr_before_wire(service, request, str(row.id))
        else:
            await wb.validate_atr_before_wire(service, request, str(row.id))
            await broker.submit_order(request)
        return []
    service._emit_v2_managed_sell = boundary
    crossed(case, "stop")
    await attach(case)
    assert not closes(broker)
    broker.age = 0
    await attach(case)
    assert len(calls) == len(set(calls)) == len(set(coids)) == 2
    assert len(closes(broker)) == 1 and broker.total == 1000
    with factory() as session:
        assert len(session.get(BrokerOrder, entry_id).payload[wb.KEY]["unsent_handoffs"]) == 1
