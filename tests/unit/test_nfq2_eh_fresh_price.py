"""Recorded BIYA intent shape; cache ages/price changes are explicit counterfactuals."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

import project_mai_tai.oms.service as oms_module
from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, TradeIntent
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload, TradeIntentEvent
from project_mai_tai.fanout_outcome_consumer import OUTCOME_SNAPSHOT_TYPE, FanoutOutcome
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.settings import Settings
from tests.unit.test_oms_v2_eh_reactive_entry import _oms, _v2_open


NOW = datetime(2026, 10, 7, 12, 20, 0, 439000, tzinfo=UTC)
SLOT = "d585906e-7867-515b-9524-a1ea3a6b5a2b"
SEGMENT = 1791375362370


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(oms_module, "utcnow", lambda: NOW)
    monkeypatch.setattr(oms_module, "_is_regular_market_session", lambda *args: False)
    monkeypatch.setattr(oms_module, "_extended_hours_session", lambda *args: "AM")
    value = _oms(oms_v2_eh_entry_enabled=True, oms_v2_eh_fresh_price_enabled=True,
                 strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=True)
    FanoutSegmentIdentityStore(value.session_factory).record(
        symbol="BIYA", segment_id=SEGMENT, active=True, reason="recorded_biya", now=NOW)
    return value


def biya(*, webull=False, reactive=False):
    md = {"path": "ATR Flip", "entry_price": "2.5400", "resting_level": "2.5400",
          "resting_band_pct": "0.5", "resting_wire_stop_price": "2.5400",
          "resting_wire_limit_price": "2.55", "fanout_slot_id": SLOT,
          "fanout_segment_id": str(SEGMENT), "fanout_slot": "resting",
          "fanout_attempt_id": "schwab_1m_v2-BIYA-open-95b9361fc503" if webull
          else "schwab_1m_v2-BIYA-open-5cbff1fa89d8", "order_type": "limit", "session": "AM"}
    if not reactive:
        md.update(resting_entry="true", eh_resting="true")
    if webull:
        md.update(fanout_leg="webull", fanout_source="reactive" if reactive else "eh_resting")
        md.pop("resting_entry", None)
        md.pop("eh_resting", None)
    event = _v2_open(md, symbol="BIYA", qty="118" if webull else "236")
    event.event_id = UUID("6f993e4c-2c4f-4475-8ba8-fe95cfb9d342" if webull
                          else "4497c490-b501-413b-9552-c0e51c0a1b3f")
    event.produced_at = NOW
    if webull:
        event.payload.broker_account_name = "paper:orb"
    return event


def hold(service, event):
    with service.session_factory() as session:
        strategy = service.store.ensure_strategy(session, "schwab_1m_v2", name="v2", execution_mode="paper")
        account = service.store.ensure_broker_account(session, event.payload.broker_account_name,
                                                       provider="simulated", environment="paper")
        intent = service.store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        assert service._nfq2_pre_submit(session, event, intent)
        assert intent.status == "held"
        session.commit()
    return service._nfq2_holds[service._nfq2_key(event)]


def quote(service, ask="2.54", age=0):
    service._latest_quotes_by_symbol["BIYA"] = {
        "ask": Decimal(ask), "received_at": NOW - timedelta(milliseconds=age)}


def retry(service):
    return TradeIntentEvent.model_validate(service.redis.entries[-1][1])


@pytest.mark.parametrize("webull", [False, True])
@pytest.mark.parametrize("reactive", [False, True])
@pytest.mark.asyncio
async def test_biya_both_paths_and_accounts_hold_then_serially_recheck_one_percent(service, webull, reactive):
    event = biya(webull=webull, reactive=reactive)
    hold(service, event)
    quote(service, "2.56")  # Counterfactual +0.7874%, outside fresh band, inside held band.
    await service._evaluate_nfq2_holds("BIYA")
    message = retry(service)
    assert service._claim_nfq2_retry(message)
    assert not service._claim_nfq2_retry(message)
    with service.session_factory() as session:
        intent = session.scalars(select(TradeIntent)).first()
        pricer = service._apply_v2_eh_reactive_entry if reactive else service._apply_v2_eh_resting_entry
        assert pricer(session=session, event=message, intent=intent) is None
    assert Decimal(message.payload.metadata["limit_price"]) <= Decimal("2.5654")
    assert Decimal(message.payload.metadata["limit_price"]) >= Decimal("2.56")


@pytest.mark.asyncio
async def test_biya_both_legs_have_independent_serial_holds(service):
    hold(service, biya())
    hold(service, biya(webull=True))
    assert len(service._nfq2_holds) == 2
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    assert len(service.redis.entries) == 2
    await service._evaluate_nfq2_holds("BIYA")
    assert len(service.redis.entries) == 2


@pytest.mark.asyncio
async def test_biya_next_quote_above_one_percent_gives_up_with_durable_feedback(service):
    hold(service, biya())
    quote(service, "2.57")
    await service._evaluate_nfq2_holds("BIYA")
    assert not service._nfq2_holds and not service.redis.entries
    with service.session_factory() as session:
        outcomes = session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == OUTCOME_SNAPSHOT_TYPE)).all()
        assert [r.payload["outcome"] for r in outcomes] == ["held_no_fresh_quote", "rejected_client_abort"]
        assert outcomes[-1].payload["reason"] == "v2_eh_nfq2:ask_past_held_cap"


@pytest.mark.asyncio
async def test_biya_cancel_after_enqueue_invalidates_the_serial_copy(service):
    event = biya()
    hold(service, event)
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    queued = retry(service)
    cancel = event.model_copy(deep=True)
    cancel.payload.intent_type = "cancel"
    service._nfq2_observe(cancel)
    assert not service._claim_nfq2_retry(queued)


@pytest.mark.asyncio
async def test_biya_restart_invalidates_old_queue_token_and_dispatch_uncertainty_never_resends(service):
    event = biya()
    hold(service, event)
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    old = retry(service)
    service.__dict__["_eh_price_holds"] = {}
    service._restore_nfq2_holds()
    assert not service._claim_nfq2_retry(old)
    await service._evaluate_nfq2_holds("BIYA")
    current = retry(service)
    assert service._claim_nfq2_retry(current)
    count = len(service.redis.entries)
    service.__dict__["_eh_price_holds"] = {}
    service._restore_nfq2_holds()
    await service._evaluate_nfq2_holds("BIYA")
    assert len(service.redis.entries) == count
    assert service._nfq2_holds[service._nfq2_key(event)].phase == "uncertain"


def test_nfq2_default_off_and_operator_fresh_reactive_cap_is_half_percent():
    assert Settings().oms_v2_eh_fresh_price_enabled is False
    assert Settings().oms_v2_eh_entry_max_cross_pct == 0.5
    assert Settings().oms_v2_mirror_eh_max_cross_pct == 1.0


@pytest.mark.parametrize("reactive", [False, True])
def test_biya_fresh_order_outside_half_percent_is_refused_not_given_held_cap(service, reactive):
    quote(service, "2.56")
    event = biya(reactive=reactive)
    intent = type("Intent", (), {"status": "created", "payload": {}, "id": UUID(int=1)})()
    with service.session_factory() as session:
        assert not service._nfq2_pre_submit(session, event, intent)
        pricer = service._apply_v2_eh_reactive_entry if reactive else service._apply_v2_eh_resting_entry
        assert pricer(session=session, event=event, intent=intent) is not None
    assert event.payload.metadata["abandon_reason_code"] in {"ASK_PAST_CROSS_CAP", "ASK_PAST_BAND"}


@pytest.mark.parametrize("age", [0, 9999, 10000])
def test_biya_ten_second_price_is_fresh_and_never_gets_held_band(service, age):
    quote(service, "2.56", age)
    event = biya(reactive=True)
    with service.session_factory() as session:
        intent = type("Intent", (), {"status": "created", "payload": {}, "id": UUID(int=1)})()
        assert not service._nfq2_pre_submit(session, event, intent)
        assert not service._nfq2_held_dispatch(event)


@pytest.mark.parametrize("age", [-1, 10001])
def test_biya_future_or_stale_price_is_not_a_dispatch_price(service, age):
    quote(service, age=age)
    assert not service._nfq2_reading("BIYA").fresh


@pytest.mark.asyncio
@pytest.mark.parametrize("webull", [False, True])
@pytest.mark.parametrize("reactive", [False, True])
async def test_biya_real_intent_and_serial_consumer_submit_once_after_hold(service, monkeypatch, webull, reactive):
    monkeypatch.setattr(service, "_market_is_fillable", lambda *args: True)
    service._fanout_webull_collision_reason = lambda **kwargs: None
    event = biya(webull=webull, reactive=reactive)
    assert await service.process_trade_intent(event) == []
    assert len(service._nfq2_holds) == 1
    quote(service, "2.56")
    await service._evaluate_nfq2_holds("BIYA")
    message = retry(service)
    await service._handle_stream_message({"data": message.model_dump_json()})
    with service.session_factory() as session:
        orders = session.scalars(select(BrokerOrder)).all()
        assert len(orders) == 1
        assert orders[0].status == "filled"
        assert Decimal(orders[0].payload["limit_price"]) <= Decimal("2.5654")
        outcomes = session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == OUTCOME_SNAPSHOT_TYPE)).all()
        assert not any(r.payload["outcome"] == "rejected_client_abort" for r in outcomes)
    await service._handle_stream_message({"data": message.model_dump_json()})
    with service.session_factory() as session:
        assert len(session.scalars(select(BrokerOrder)).all()) == 1


@pytest.mark.asyncio
async def test_biya_stale_serial_token_cannot_claim_a_requeued_hold(service):
    held = hold(service, biya())
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    stale = retry(service)
    assert service._claim_nfq2_retry(stale)
    service._latest_quotes_by_symbol.clear()
    with service.session_factory() as session:
        intent = session.get(TradeIntent, held.intent_id)
        assert service._nfq2_pre_submit(session, stale, intent)
        session.commit()
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    current = retry(service)
    assert stale.payload.metadata["nfq2_retry_token"] != current.payload.metadata["nfq2_retry_token"]
    assert not service._claim_nfq2_retry(stale)
    assert service._claim_nfq2_retry(current)


def test_biya_cancel_for_old_generation_or_other_account_does_not_retire_new_hold(service):
    event = biya()
    event.payload.metadata["nfq2_generation"] = "recorded-generation"
    held = hold(service, event)
    cancel = event.model_copy(deep=True)
    cancel.payload.intent_type = "cancel"
    cancel.payload.metadata["nfq2_generation"] = "predecessor-generation"
    service._nfq2_observe(cancel)
    assert service._nfq2_holds[service._nfq2_key(event)] is held
    cancel.payload.metadata["nfq2_generation"] = "recorded-generation"
    cancel.payload.broker_account_name = "paper:orb"
    service._nfq2_observe(cancel)
    assert service._nfq2_holds[service._nfq2_key(event)] is held
    cancel.payload.broker_account_name = event.payload.broker_account_name
    service._nfq2_observe(cancel)
    assert not service._nfq2_holds


@pytest.mark.asyncio
async def test_biya_restart_with_own_filled_slot_retires_hold_without_rebuy(service, monkeypatch):
    event = biya()
    hold(service, event)
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    queued = retry(service)
    monkeypatch.setattr(service, "_market_is_fillable", lambda *args: True)
    await service.process_trade_intent(queued)
    service.__dict__["_eh_price_holds"] = {}
    service._restore_nfq2_holds()
    assert not service._nfq2_holds
    with service.session_factory() as session:
        assert len(session.scalars(select(BrokerOrder)).all()) == 1
        outcomes = session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == OUTCOME_SNAPSHOT_TYPE)).all()
        assert outcomes[-1].payload["outcome"] == "filled"


@pytest.mark.asyncio
async def test_biya_price_expiring_before_serial_dispatch_reholds_without_wire(service, monkeypatch):
    monkeypatch.setattr(service, "_market_is_fillable", lambda *args: True)
    event = biya()
    assert await service.process_trade_intent(event) == []
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    queued = retry(service)
    service._latest_quotes_by_symbol.clear()
    await service._handle_stream_message({"data": queued.model_dump_json()})
    assert service._nfq2_holds[service._nfq2_key(event)].phase == "held"
    with service.session_factory() as session:
        assert session.scalars(select(BrokerOrder)).all() == []


@pytest.mark.asyncio
async def test_biya_segment_or_entry_window_end_retires_hold_without_enqueue(service):
    event = biya()
    hold(service, event)
    FanoutSegmentIdentityStore(service.session_factory).record(
        symbol="BIYA", segment_id=SEGMENT, active=False, reason="recorded_cancel",
        now=NOW + timedelta(microseconds=1))
    quote(service)
    await service._evaluate_nfq2_holds()
    await service._evaluate_nfq2_holds("BIYA")
    assert not service._nfq2_holds and not service.redis.entries


def test_biya_v2_reads_primary_hold_and_giveup_without_claiming_the_webull_leg():
    from tests.unit.test_v2_flip_owned_first_entry import _strategy, PRIMARY
    strategy, clock, _, _ = _strategy(dual=True)
    clock[0] = int(NOW.timestamp() * 1000)
    state = strategy.watchlist_state("BIYA")
    state.fanout_segment_id = SEGMENT
    state.resting_is_broker_order = False
    record = FanoutOutcome(uuid4(), NOW, "BIYA", SEGMENT, "resting", SLOT,
                           biya().payload.metadata["fanout_attempt_id"], "held_no_fresh_quote",
                           "biya-held", reason="v2_eh_nfq2:held", broker_account_name=PRIMARY)
    assert strategy.apply_fanout_outcome(record) == "held"
    assert state.resting_active and not state.fanout_webull_claimed
    from dataclasses import replace
    released = replace(record, evidence_id="biya-gave-up", outcome="rejected_client_abort",
                       reason="v2_eh_nfq2:ask_past_held_cap")
    assert strategy.apply_fanout_outcome(released) == "primary_nfq2"
    assert not state.resting_active and not state.fanout_webull_claimed


def test_biya_delayed_hold_feedback_never_rearms_a_filled_slot():
    from tests.unit.test_v2_flip_owned_first_entry import _strategy, PRIMARY
    strategy, clock, _, _ = _strategy(dual=True)
    clock[0] = int(NOW.timestamp() * 1000)
    state = strategy.watchlist_state("BIYA")
    state.fanout_segment_id = SEGMENT
    state.fanout_claim_outcome = "filled"
    record = FanoutOutcome(uuid4(), NOW, "BIYA", SEGMENT, "resting", SLOT, "biya",
                           "held_no_fresh_quote", "delayed-held", reason="v2_eh_nfq2:held",
                           broker_account_name=PRIMARY)
    assert strategy.apply_fanout_outcome(record) == "filled_wins"
    assert not state.resting_active and not strategy.__dict__.get("_nfq2_waits")


@pytest.mark.asyncio
async def test_biya_real_quote_handler_enqueues_both_holds_without_wire_call(service):
    hold(service, biya())
    hold(service, biya(webull=True))
    event = QuoteTickEvent(source_service="market-data", produced_at=NOW,
                          payload=QuoteTickPayload(symbol="BIYA", bid_price="2.53", ask_price="2.54"))
    await service._handle_quote_tick_event(event)
    assert len(service.redis.entries) == 2
    with service.session_factory() as session:
        assert session.scalars(select(BrokerOrder)).all() == []


@pytest.mark.asyncio
async def test_biya_uncertain_dispatch_is_not_released_by_window_or_new_open(service, monkeypatch):
    event = biya()
    held = hold(service, event)
    held.phase = "uncertain"
    with service.session_factory() as session:
        service._nfq2_save(session, held)
        session.commit()
    quote(service)
    replacement = event.model_copy(deep=True)
    replacement.event_id = uuid4()
    replacement.payload.metadata["fanout_slot_id"] = "next-slot"
    with service.session_factory() as session:
        intent = session.get(TradeIntent, held.intent_id)
        assert service._nfq2_pre_submit(session, replacement, intent)
        assert held.phase == "uncertain"
    monkeypatch.setattr(oms_module, "utcnow", lambda: NOW + timedelta(hours=12))
    await service._evaluate_nfq2_holds("BIYA")
    assert service._nfq2_holds[service._nfq2_key(event)].phase == "uncertain"
    assert not service.redis.entries


@pytest.mark.asyncio
@pytest.mark.parametrize("webull", [False, True])
@pytest.mark.parametrize("reactive", [False, True])
async def test_biya_after_hours_route_counterfactual_respects_configured_window_and_serial_hold(
        service, monkeypatch, webull, reactive):
    pm = NOW + timedelta(hours=10)
    monkeypatch.setattr(oms_module, "utcnow", lambda: pm)
    monkeypatch.setattr(oms_module, "_extended_hours_session", lambda *args: "PM")
    monkeypatch.setattr(service, "_market_is_fillable", lambda *args: True)
    service.settings.strategy_schwab_1m_v2_entry_window_end_hour_et = 19
    service.settings.strategy_schwab_1m_v2_entry_window_end_minute_et = 55
    service._fanout_webull_collision_reason = lambda **kwargs: None
    FanoutSegmentIdentityStore(service.session_factory).record(
        symbol="BIYA", segment_id=SEGMENT, active=True, reason="route_counterfactual", now=pm)
    event = biya(webull=webull, reactive=reactive)
    event.produced_at = pm
    event.payload.metadata["session"] = "PM"
    assert await service.process_trade_intent(event) == []
    service._latest_quotes_by_symbol["BIYA"] = {"ask": Decimal("2.56"), "received_at": pm}
    await service._evaluate_nfq2_holds("BIYA")
    message = retry(service)
    await service._handle_stream_message({"data": message.model_dump_json()})
    with service.session_factory() as session:
        orders = session.scalars(select(BrokerOrder)).all()
        assert len(orders) == 1 and orders[0].payload["session"] == "PM"
        assert Decimal(orders[0].payload["limit_price"]) <= Decimal("2.5654")


@pytest.mark.asyncio
async def test_biya_configured_cutoff_never_sends_saved_buy(service, monkeypatch):
    hold(service, biya())
    quote(service)
    monkeypatch.setattr(oms_module, "utcnow", lambda: NOW.replace(hour=20, minute=0))
    await service._evaluate_nfq2_holds()
    await service._evaluate_nfq2_holds("BIYA")
    assert not service._nfq2_holds and not service.redis.entries


@pytest.mark.asyncio
async def test_biya_eh_hold_does_not_migrate_into_unpriced_regular_hours(service, monkeypatch):
    hold(service, biya())
    quote(service)
    monkeypatch.setattr(oms_module, "_is_regular_market_session", lambda *args: True)
    monkeypatch.setattr(oms_module, "_extended_hours_session", lambda *args: None)
    await service._evaluate_nfq2_holds()
    await service._evaluate_nfq2_holds("BIYA")
    assert not service._nfq2_holds and not service.redis.entries


@pytest.mark.asyncio
async def test_biya_serial_rehold_moves_v2_attempt_then_giveup_releases_exact_webull_latch(
        service, monkeypatch):
    from project_mai_tai.fanout_outcome_consumer import FanoutOutcomeJournal
    from tests.unit.test_v2_flip_owned_first_entry import _strategy
    strategy, clock, _, _ = _strategy(dual=True)
    strategy._flip_owner_primary_account = "paper:schwab_1m_v2"
    strategy._flip_owner_webull_account = "paper:orb"
    clock[0] = int(NOW.timestamp() * 1000)
    state = strategy.watchlist_state("BIYA")
    state.fanout_segment_id = SEGMENT
    state.resting_is_broker_order = False
    journal = FanoutOutcomeJournal(service.session_factory)
    monkeypatch.setattr(service, "_market_is_fillable", lambda *args: True)
    service._fanout_webull_collision_reason = lambda **kwargs: None
    event = biya(webull=True)
    await service.process_trade_intent(event)
    journal.poll(strategy.apply_fanout_outcome)
    assert state.fanout_webull_claimed
    quote(service)
    await service._evaluate_nfq2_holds("BIYA")
    message = retry(service)
    service._latest_quotes_by_symbol.clear()
    await service._handle_stream_message({"data": message.model_dump_json()})
    journal.poll(strategy.apply_fanout_outcome)
    assert state.fanout_claim_attempt_id == service._build_client_order_id(message)
    quote(service, "2.57")
    await service._evaluate_nfq2_holds("BIYA")
    journal.poll(strategy.apply_fanout_outcome)
    assert not state.fanout_webull_claimed and not state.webull_resting_active
