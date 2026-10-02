"""NFQ1 replays recorded client refusals; clock/tick schedules are controlled scenarios.

Published quotes are projected into a test cache, never claimed as historical OMS consumption.
No fixture here invents a broker reply. The integration adapter is explicitly a simulator.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from uuid import uuid4

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, TradeIntent
from project_mai_tai.fanout_outcome_consumer import FanoutOutcomeJournal, OUTCOME_SNAPSHOT_TYPE
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.fanout_segment_store import SNAPSHOT_TYPE as SEGMENT_SNAPSHOT
from project_mai_tai.oms import service as oms
from project_mai_tai.oms.mirror_fresh_price import GAVE_UP_PREFIX, RETIRED_PREFIX
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.settings import Settings
from tests.unit.test_oms_webull_mirror_deferred_resubmit import (
    ACCOUNT, _integrated_service, _session_factory,
)


FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/nfq1_recorded_cases.json").read_text())
CASES = FIXTURE["cases"]
OUTCOMES = {r["kind"]: r["rows"] for r in json.loads(
    (Path(__file__).parents[1] / "fixtures/nfq1_recorded_outcomes.json").read_text()
)["records"]}


def event_for(case=next(c for c in CASES if c["record"]["symbol"] == "AIXI")):
    row = case["record"]
    metadata = dict(row["payload"])
    metadata.pop("reject_reason", None)
    return TradeIntentEvent(
        source_service="schwab-1m-v2",
        produced_at=datetime.fromisoformat(row["intent_at"]),
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2", broker_account_name=ACCOUNT,
            symbol=row["symbol"], side="buy", quantity=Decimal("1"),
            intent_type="open", reason="recorded NFQ1 replay", metadata=metadata,
        ),
    )


def recorded_report(case):
    row = case["record"]
    payload = row["event_payload"]
    return ExecutionReport(
        event_type="rejected", client_order_id=payload["client_order_id"],
        symbol=row["symbol"], side="buy", intent_type="open", quantity=Decimal("1"),
        filled_quantity=Decimal("0"), reported_at=datetime.fromisoformat(row["event_at"]),
        reason=payload["reason"], metadata=dict(payload["metadata"]), origin="client",
    )


@pytest.fixture
def lane(monkeypatch):
    factory = _session_factory()
    service, adapter = _integrated_service(factory, enabled=True, nfq_enabled=True)
    service.settings.strategy_schwab_1m_v2_entry_window_end_hour_et = 15
    service.settings.strategy_schwab_1m_v2_entry_window_end_minute_et = 45
    clock = [datetime(2026, 10, 2, 18, 0, tzinfo=UTC)]
    monkeypatch.setattr(oms, "utcnow", lambda: clock[0])
    monkeypatch.setattr(oms, "_is_regular_market_session", lambda now=None: True)
    monkeypatch.setattr(oms.OmsRiskService, "_market_is_fillable", lambda self, now=None: True)
    seed_segment(factory, event_for(), clock)
    return service, adapter, factory, clock


def test_defaults_are_mirror_only():
    settings = Settings()
    assert settings.oms_v2_webull_mirror_quote_max_age_ms == 10000
    assert settings.oms_v2_webull_mirror_fresh_price_enabled is True
    assert settings.oms_v2_eh_resting_entry_quote_max_age_ms == 2000


@pytest.mark.parametrize("case", [c for c in CASES if "has no valid" in c["record"]["event_payload"]["reason"]],
                         ids=lambda c: c["record"]["symbol"])
def test_recorded_3_to_9_second_quotes_reach_wire_shape(lane, case):
    service, _, _, clock = lane
    row, quote = case["record"], case["quotes"]["before"][0]
    clock[0] = datetime.fromisoformat(row["submitted_at"])
    event = event_for(case)
    service._latest_quotes_by_symbol[row["symbol"]] = {
        "ask": Decimal(quote["ask_price"]),
        "received_at": datetime.fromisoformat(quote["event_ts"]),
    }
    service._stamp_webull_resting_mirror_market(event)
    request = OrderRequest(
        client_order_id="fixture-shape-only", broker_account_name=ACCOUNT,
        strategy_code="schwab_1m_v2", symbol=row["symbol"], side="buy",
        intent_type="open", quantity=Decimal("1"), metadata=event.payload.metadata,
        reason="recorded shape replay",
    )
    wire = row["event_payload"]["metadata"]
    shape, _, _, metadata, refusal = WebullBrokerAdapter._apply_resting_mirror_market_shape(
        request=request, order_type="STOP_LIMIT",
        wire_limit=Decimal(wire["webull_wire_limit_price"]),
        wire_stop=Decimal(wire["webull_wire_stop_price"]), metadata={}, now=clock[0],
    )
    assert refusal is None
    assert shape == ("LIMIT" if row["symbol"] == "IPDN" else "STOP_LIMIT")
    assert metadata["webull_shape_market_age_ms_at_wire"] != "0"
    assert event.payload.metadata["webull_shape_market_source"] == "ask"


@pytest.mark.asyncio
async def test_absent_price_holds_without_broker_order(lane):
    service, adapter, _, _ = lane
    event = event_for()
    result = await service.process_trade_intent(event)
    assert adapter.requests == []
    assert result[-1].payload.reason == "webull_mirror_no_fresh_quote_held"
    assert event.payload.metadata["fanout_slot_id"] in service._nfq_holds


def test_stamp_cannot_leak_from_an_old_retry_when_cache_is_empty(lane):
    service, _, _, clock = lane
    case = next(c for c in CASES if "age_ms=2305" in c["record"]["event_payload"]["reason"])
    event = event_for(case)
    clock[0] = datetime.fromisoformat(case["record"]["submitted_at"]) + timedelta(seconds=11)
    service._stamp_webull_resting_mirror_market(event)
    assert "webull_shape_market_price" not in event.payload.metadata


def test_premarket_reactive_pricer_still_refuses_three_second_quote(lane, monkeypatch):
    service, _, _, clock = lane
    service._latest_quotes_by_symbol["IPDN"] = {
        "ask": Decimal("5.39"), "received_at": clock[0] - timedelta(seconds=3),
    }
    result = service._band_capped_marketable_limit(
        symbol="IPDN", level=5.38, band_pct=0.5,
        max_age_ms=service.settings.oms_v2_eh_resting_entry_quote_max_age_ms,
    )
    assert result[0] is None
    assert result[1] == "NO_FRESH_QUOTE"


def queued_events(service):
    return [TradeIntentEvent.model_validate(data) for stream, data in service.redis.entries
            if stream.endswith("strategy-intents")]


def set_quote(service, event, clock, price=None):
    service._latest_quotes_by_symbol[event.payload.symbol] = {
        "ask": Decimal(price or event.payload.metadata["stop_price"]) * Decimal("0.99"),
        "received_at": clock[0],
    }


def seed_segment(factory, event, clock):
    FanoutSegmentIdentityStore(factory).record(
        event.payload.symbol, int(event.payload.metadata["fanout_segment_id"]), True,
        "positive strategy identity in controlled replay", now=clock[0],
    )


async def hold_and_queue(lane, event=None):
    service, adapter, factory, clock = lane
    event = event or event_for()
    seed_segment(factory, event, clock)
    await service.process_trade_intent(event)
    assert not adapter.requests
    set_quote(service, event, clock)
    await service._evaluate_nfq_holds(event.payload.symbol)
    return event, queued_events(service)[-1]


def outcomes(factory):
    with factory() as session:
        return [r.payload for r in session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == OUTCOME_SNAPSHOT_TYPE,
        )).all()]


@pytest.mark.asyncio
async def test_next_quote_queues_once_and_only_serial_lane_submits(lane):
    service, adapter, _, clock = lane
    event, retry = await hold_and_queue(lane)
    for _ in range(4):
        await service._evaluate_nfq_holds(event.payload.symbol)
    assert len(queued_events(service)) == 1
    assert not adapter.requests
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert len(adapter.requests) == 1
    assert service._nfq_holds == {}
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_trade_fallback_and_unchanged_nine_second_ask(lane):
    service, _, factory, clock = lane
    case = next(c for c in CASES if c["record"]["submitted_at"].startswith("2026-10-02 17:58"))
    event = event_for(case)
    clock[0] = datetime.fromisoformat(case["record"]["submitted_at"])
    seed_segment(factory, event, clock)
    quote = case["quotes"]["before"][0]
    trade = OUTCOMES["cycu_trades"][0]
    service._latest_quotes_by_symbol["CYCU"] = {
        "ask": Decimal(quote["ask_price"]), "received_at": datetime.fromisoformat(quote["event_ts"]),
    }
    # Controlled projection of the recorded exchange print into the test cache. Its actual
    # capture receipt is later, so this does not claim historical OMS availability.
    service._latest_trades_by_symbol["CYCU"] = {
        "price": Decimal(trade["price"]), "received_at": datetime.fromisoformat(trade["event_ts"]),
    }
    service._stamp_webull_resting_mirror_market(event)
    assert event.payload.metadata["webull_shape_market_source"] == "ask"
    service._latest_quotes_by_symbol.clear()
    service._stamp_webull_resting_mirror_market(event)
    assert event.payload.metadata["webull_shape_market_source"] == "last"
    service._latest_trades_by_symbol.clear()
    await service.process_trade_intent(event)
    service._latest_trades_by_symbol["CYCU"] = {
        "price": Decimal(trade["price"]), "received_at": clock[0],
    }
    await service._evaluate_nfq_holds("CYCU")
    assert len(queued_events(service)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancel", "attempt", "slot", "segment"])
async def test_cancel_or_reprice_retires_exact_queued_generation(lane, change):
    service, adapter, _, _ = lane
    original, retry = await hold_and_queue(lane)
    replacement = original.model_copy(deep=True)
    replacement.event_id = uuid4()
    if change == "cancel":
        replacement.payload.intent_type = "cancel"
    elif change == "slot":
        replacement.payload.metadata["fanout_slot_id"] = str(uuid4())
    elif change == "segment":
        replacement.payload.metadata["fanout_segment_id"] = "1790959999999"
    service._nfq_observe_intent(replacement)
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert not adapter.requests
    assert not service._nfq_holds


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["fanout_segment_id", "fanout_slot_id", "fanout_attempt_id"])
async def test_stale_bound_cancel_cannot_retire_current_hold(lane, field):
    service, _, _, _ = lane
    event, _ = await hold_and_queue(lane)
    cancel = event.model_copy(deep=True)
    cancel.payload.intent_type = "cancel"
    cancel.payload.metadata[field] = "stale"
    service._nfq_observe_intent(cancel)
    assert event.payload.metadata["fanout_slot_id"] in service._nfq_holds


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,minute", [(19, 45), (20, 0)])
async def test_no_price_ever_expires_without_ticks_and_tells_v2(lane, hour, minute):
    service, adapter, factory, clock = lane
    await service.process_trade_intent(event_for())
    clock[0] = clock[0].replace(hour=hour, minute=minute)
    await service._evaluate_nfq_holds()
    await service._evaluate_nfq_holds()
    assert not service._nfq_holds
    assert not adapter.requests
    terminal = [r for r in outcomes(factory) if r["reason"] == GAVE_UP_PREFIX + "resting_window_ended"]
    assert len(terminal) == 1


@pytest.mark.asyncio
async def test_segment_end_retires_without_another_price(lane):
    service, adapter, factory, clock = lane
    event = event_for()
    await service.process_trade_intent(event)
    clock[0] += timedelta(seconds=1)
    FanoutSegmentIdentityStore(factory).record(
        event.payload.symbol, int(event.payload.metadata["fanout_segment_id"]), False,
        "recorded lifecycle end in controlled replay", now=clock[0],
    )
    await service._evaluate_nfq_holds()
    assert not service._nfq_holds
    assert not adapter.requests
    assert outcomes(factory)[-1]["reason"] == RETIRED_PREFIX + "segment_ended"


@pytest.mark.asyncio
@pytest.mark.parametrize("already_queued", [False, True])
async def test_oms_restart_restores_hold_and_invalidates_old_queue(lane, already_queued):
    service, _, factory, clock = lane
    event = event_for()
    if already_queued:
        _, old_retry = await hold_and_queue(lane, event)
    else:
        await service.process_trade_intent(event)
    restarted, adapter = _integrated_service(factory, enabled=True, nfq_enabled=True)
    restarted._restore_nfq_holds()
    assert event.payload.metadata["fanout_slot_id"] in restarted._nfq_holds
    if already_queued:
        await restarted._handle_stream_message({"data": old_retry.model_dump_json()})
        assert not adapter.requests
    set_quote(restarted, event, clock)
    await restarted._evaluate_nfq_holds(event.payload.symbol)
    retry = queued_events(restarted)[-1]
    await restarted._handle_stream_message({"data": retry.model_dump_json()})
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_recorded_expiry_requeue_rejects_old_token_even_when_hold_is_queued(lane, restart):
    service, adapter, factory, clock = lane
    case = next(c for c in CASES if "age_ms=2305" in c["record"]["event_payload"]["reason"])
    original = event_for(case)
    clock[0] = datetime.fromisoformat(case["record"]["submitted_at"])
    seed_segment(factory, original, clock)
    # Replay the recorded no-wire expiry. A repeated report or an OMS restart can
    # invalidate its queued copy before the serial consumer receives that copy.
    with factory() as session:
        service._nfq_observe_reports(session, original, [recorded_report(case)])
        session.commit()
    set_quote(service, original, clock)
    await service._evaluate_nfq_holds(original.payload.symbol)
    stale = queued_events(service)[-1]
    if restart:
        service, adapter = _integrated_service(factory, enabled=True, nfq_enabled=True)
        service._restore_nfq_holds()
    else:
        with factory() as session:
            service._nfq_observe_reports(session, original, [recorded_report(case)])
            session.commit()
    set_quote(service, original, clock)
    await service._evaluate_nfq_holds(original.payload.symbol)
    current = queued_events(service)[-1]
    for key in ("nfq_hold_id", "fanout_segment_id", "fanout_attempt_id"):
        assert stale.payload.metadata[key] == current.payload.metadata[key]
    assert stale.payload.metadata["nfq_retry_token"] != current.payload.metadata["nfq_retry_token"]
    assert service._nfq_holds[original.payload.metadata["fanout_slot_id"]].phase == "queued"
    await service._handle_stream_message({"data": stale.model_dump_json()})
    assert not adapter.requests
    await service._handle_stream_message({"data": current.model_dump_json()})
    await service._handle_stream_message({"data": stale.model_dump_json()})
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("evidence", ["fill_row", "filled_status", "partial_status"])
async def test_recorded_schwab_fill_in_ledger_retires_webull_hold_without_callback(lane, restart, evidence):
    service, adapter, factory, clock = lane
    row = OUTCOMES["schwab_fill"][0]
    event = event_for()
    event.payload.symbol = row["symbol"]
    event.payload.metadata = {k: v for k, v in row["order_metadata"].items()
                              if not k.startswith("bracket") and k != "native_oco_bracket"}
    event.payload.metadata.update(fanout_leg="webull", fanout_source="rth_resting_mirror")
    clock[0] = datetime.fromisoformat(row["event_at"])
    event, stale = await hold_and_queue(lane, event)
    # Project the real CYCU fill into the durable ledger, deliberately without the
    # in-memory report callback. The partial/status-only variants are recovery states,
    # not a claim that this recorded full fill was historically partial.
    with factory() as session:
        intent = session.scalar(select(TradeIntent))
        account = BrokerAccount(name="live:schwab_1m_v2", provider="schwab", environment="live")
        session.add(account)
        session.flush()
        order = BrokerOrder(
            intent_id=intent.id, strategy_id=intent.strategy_id, broker_account_id=account.id,
            client_order_id=row["payload"]["client_order_id"],
            broker_order_id=row["payload"]["broker_order_id"],
            symbol=row["symbol"], side=row["side"], quantity=Decimal(row["quantity"]),
            order_type="STOP_LIMIT", time_in_force="day", payload=dict(row["order_metadata"]),
            status={"fill_row": "accepted", "filled_status": "filled", "partial_status": "partially_filled"}[evidence],
        )
        session.add(order)
        session.flush()
        if evidence == "fill_row":
            session.add(Fill(
                order_id=order.id, strategy_id=order.strategy_id, broker_account_id=account.id,
                broker_fill_id=row["payload"]["broker_fill_id"], symbol=row["symbol"], side="buy",
                quantity=Decimal(row["quantity"]), price=Decimal("4.14"),
                filled_at=clock[0], payload=row["payload"],
            ))
        session.commit()
    if restart:
        service, adapter = _integrated_service(factory, enabled=True, nfq_enabled=True)
        service._restore_nfq_holds()
    set_quote(service, event, clock)
    await service._evaluate_nfq_holds(event.payload.symbol)
    assert not service._nfq_holds
    await service._handle_stream_message({"data": stale.model_dump_json()})
    assert not adapter.requests
    assert outcomes(factory)[-1]["reason"].endswith(":slot_filled")


@pytest.mark.parametrize("start,end,at,allowed,deadline", [
    ((10, 5), (15, 45), (10, 4), False, (15, 45)),
    ((10, 5), (15, 45), (10, 5), True, (15, 45)),
    ((7, 0), (14, 30), (14, 29), True, (14, 30)),
    ((7, 0), (14, 30), (14, 30), False, (14, 30)),
    ((7, 0), (16, 0), (15, 50), True, (16, 0)),
    ((7, 0), (17, 0), (16, 0), False, (16, 0)),
    ((7, 0), (16, 0), (9, 29), False, (16, 0)),
    ((7, 0), (16, 0), (9, 30), True, (16, 0)),
])
def test_nfq_window_and_wire_deadline_follow_v2_configuration(lane, monkeypatch, start, end, at, allowed, deadline):
    from project_mai_tai.strategy_core.entry_gate import within_entry_window
    service, _, _, clock = lane
    monkeypatch.setattr(oms, "_is_regular_market_session", lambda now=None: oms._extended_hours_session(now) is None)
    settings = service.settings
    settings.strategy_schwab_1m_v2_entry_window_start_hour_et, settings.strategy_schwab_1m_v2_entry_window_start_minute_et = start
    settings.strategy_schwab_1m_v2_entry_window_end_hour_et, settings.strategy_schwab_1m_v2_entry_window_end_minute_et = end
    clock[0] = clock[0].replace(hour=at[0] + 4, minute=at[1])
    event = event_for()
    strategy = SchwabV2Strategy(settings)
    assert (within_entry_window(clock[0], settings)
            and not strategy._resting_session_is_eh(clock[0])) is allowed
    assert service._nfq_window_open(event) is allowed
    service._stamp_webull_resting_mirror_market(event)
    actual = datetime.fromisoformat(event.payload.metadata["webull_mirror_entry_deadline_utc"])
    assert actual == clock[0].replace(hour=deadline[0] + 4, minute=deadline[1], second=0, microsecond=0)


def outcome_report(kind):
    row = OUTCOMES[kind][0]
    payload = row["payload"]
    return ExecutionReport(
        event_type=row["event_type"], client_order_id=payload["client_order_id"],
        symbol=row["symbol"], side=row["side"], intent_type="open",
        quantity=Decimal(row["quantity"]),
        filled_quantity=Decimal(row["quantity"]) if row["event_type"] == "filled" else Decimal("0"),
        reason=payload["reason"], metadata=dict(payload["metadata"]),
        reported_at=datetime.fromisoformat(row["event_at"]), origin=row["event_source"],
    )


@pytest.mark.asyncio
async def test_recorded_schwab_fill_while_webull_held_ends_retry(lane):
    service, adapter, factory, clock = lane
    row = OUTCOMES["schwab_fill"][0]
    event = event_for()
    event.payload.symbol = row["symbol"]
    event.payload.metadata = dict(row["order_metadata"])
    event.payload.metadata.update(fanout_leg="webull", fanout_source="rth_resting_mirror")
    event.payload.metadata = {k: v for k, v in event.payload.metadata.items() if not k.startswith("bracket") and k != "native_oco_bracket"}
    _, retry = await hold_and_queue(lane, event)
    primary = event.model_copy(deep=True)
    primary.payload.broker_account_name = "live:schwab_1m_v2"
    primary.payload.metadata = dict(row["order_metadata"])
    with factory() as session:
        service._nfq_observe_reports(session, primary, [outcome_report("schwab_fill")])
        session.commit()
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert not adapter.requests
    assert not service._nfq_holds
    assert outcomes(factory)[-1]["reason"] == RETIRED_PREFIX + "slot_filled"


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_durable_duplicate_buy_guard_rechecks_before_serial_dispatch(lane, restart):
    service, adapter, factory, _ = lane
    event, retry = await hold_and_queue(lane)
    # Ledger race scaffolding, not a claimed broker reply: another buy becomes live after
    # the price tick but before the serial lane. The usual account collision guard is disabled
    # by this harness, so this independently exercises NFQ1's durable duplicate check.
    with factory() as session:
        intent = session.scalar(select(TradeIntent))
        session.add(BrokerOrder(
            intent_id=intent.id, strategy_id=intent.strategy_id,
            broker_account_id=intent.broker_account_id, client_order_id="other-live-buy",
            symbol=event.payload.symbol, side="buy", order_type="STOP_LIMIT",
            time_in_force="day", quantity=Decimal("1"), status="accepted",
            payload=dict(event.payload.metadata),
        ))
        session.commit()
    if restart:
        service, adapter = _integrated_service(factory, enabled=True, nfq_enabled=True)
        service._restore_nfq_holds()
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert not adapter.requests
    assert not service._nfq_holds
    assert outcomes(factory)[-1]["reason"] == "nfq1_existing_webull_buy"
    assert outcomes(factory)[-1]["outcome"] == "working"


@pytest.mark.asyncio
async def test_recorded_wire_expiry_becomes_durable_hold_without_pa1_budget(lane):
    service, adapter, factory, clock = lane
    case = next(c for c in CASES if "age_ms=2305" in c["record"]["event_payload"]["reason"])
    event = event_for(case)
    seed_segment(factory, event, clock)
    with factory() as session:
        service._nfq_observe_reports(session, event, [recorded_report(case)])
        session.commit()
    assert event.payload.metadata["fanout_slot_id"] in service._nfq_holds
    assert not service._webull_mirror_deferred_by_slot
    set_quote(service, event, clock)
    await service._evaluate_nfq_holds(event.payload.symbol)
    retry = queued_events(service)[-1]
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_feedback_clears_only_webull_rest_and_ignores_old_attempt(lane):
    service, _, factory, clock = lane
    event = event_for()
    await service.process_trade_intent(event)
    strategy = SchwabV2Strategy(Settings())
    state = strategy.watchlist_state(event.payload.symbol)
    md = event.payload.metadata
    state.fanout_segment_id = int(md["fanout_segment_id"])
    state.fanout_claim_slot_id = md["fanout_slot_id"]
    state.fanout_claim_slot = "resting"
    state.fanout_claim_attempt_id = md["fanout_attempt_id"]
    state.resting_active = True
    state.resting_is_broker_order = True
    state.resting_level = 1.6897
    journal = FanoutOutcomeJournal(factory)
    journal.bootstrap(strategy.apply_fanout_outcome, active_segments={event.payload.symbol: state.fanout_segment_id}, now=clock[0])
    assert state.webull_resting_active
    assert state.fanout_claim_outcome == "held"
    clock[0] = clock[0].replace(hour=19, minute=45)
    await service._evaluate_nfq_holds()
    journal.poll(strategy.apply_fanout_outcome)
    assert not state.webull_resting_active
    assert state.resting_active and state.resting_is_broker_order
    assert state.resting_level == 1.6897


def install_shape_adapter(adapter, clock, *, wire_delay=timedelta(0)):
    """Run the production wire guard; successful orders use the explicit simulator."""
    submit = adapter.submit_order

    async def shape_submit(request):
        clock[0] += wire_delay
        _, _, _, metadata, refusal = WebullBrokerAdapter._apply_resting_mirror_market_shape(
            request=request, order_type="STOP_LIMIT",
            wire_limit=Decimal(request.metadata["limit_price"]).quantize(Decimal("0.01")),
            wire_stop=Decimal(request.metadata["stop_price"]).quantize(Decimal("0.01")),
            metadata={}, now=clock[0],
        )
        if refusal:
            # This is a real local guard result, not an invented broker answer.
            return [WebullBrokerAdapter._reject(None, request, refusal, origin="client", metadata=metadata)]
        return await submit(request)

    adapter.submit_order = shape_submit


@pytest.mark.asyncio
async def test_ask_past_band_while_held_gives_up_and_reports_exact_leg(lane):
    service, adapter, factory, clock = lane
    event, retry = await hold_and_queue(lane)
    install_shape_adapter(adapter, clock)
    service._latest_quotes_by_symbol[event.payload.symbol]["ask"] = Decimal("1.80")
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert not adapter.requests
    assert not service._nfq_holds
    terminal = [r for r in outcomes(factory) if r["reason"] == GAVE_UP_PREFIX + "ASK_PAST_BAND"]
    assert len(terminal) == 1
    assert terminal[0]["broker_account_name"] == ACCOUNT
    assert terminal[0]["fanout_slot_id"] == event.payload.metadata["fanout_slot_id"]
    assert terminal[0]["fanout_attempt_id"] == service._build_client_order_id(retry)


@pytest.mark.asyncio
async def test_stamp_expiring_at_wire_reholds_then_next_price_retries(lane):
    service, adapter, factory, clock = lane
    event, retry = await hold_and_queue(lane)
    install_shape_adapter(adapter, clock, wire_delay=timedelta(seconds=11))
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert not adapter.requests
    hold = service._nfq_holds[event.payload.metadata["fanout_slot_id"]]
    assert hold.phase == "held"
    assert outcomes(factory)[-1]["outcome"] == "held_no_fresh_quote"
    set_quote(service, event, clock)
    await service._evaluate_nfq_holds(event.payload.symbol)
    assert len(queued_events(service)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_hour,wire_minute", [(19, 45), (20, 0)])
async def test_wire_deadline_stops_inflight_price_wait_at_1545_and_1600(lane, wire_hour, wire_minute):
    service, adapter, factory, clock = lane
    clock[0] = clock[0].replace(hour=19, minute=44, second=59)
    event, retry = await hold_and_queue(lane)
    wire_at = clock[0].replace(hour=wire_hour, minute=wire_minute, second=0)
    install_shape_adapter(adapter, clock, wire_delay=wire_at - clock[0])
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert not adapter.requests
    assert not service._nfq_holds
    assert outcomes(factory)[-1]["reason"] == GAVE_UP_PREFIX + "resting_window_ended"


@pytest.mark.asyncio
async def test_logs_cache_age_and_source_for_sent_held_and_gave_up(lane, caplog):
    service, adapter, _, clock = lane
    event, retry = await hold_and_queue(lane)
    await service._handle_stream_message({"data": retry.model_dump_json()})
    held_event = event_for()
    held_event.payload.metadata["fanout_slot_id"] = str(uuid4())
    service._latest_quotes_by_symbol.clear()
    await service.process_trade_intent(held_event)
    clock[0] = clock[0].replace(hour=19, minute=45)
    await service._evaluate_nfq_holds()
    lines = [r.message for r in caplog.records if "[OMS-NFQ1]" in r.message]
    assert all("cache_age_ms=" in line and "cache_source=" in line for line in lines)
    assert all("oms_consumption_age=unknown" in line for line in lines)
    assert any("decision=sent" in line and "cache_source=ask" in line for line in lines)
    assert any("decision=held" in line and "cache_age_ms=unknown" in line for line in lines)
    assert any("decision=gave_up" in line for line in lines)


@pytest.mark.asyncio
async def test_recorded_price_aggressive_keeps_three_attempt_budget_after_nfq_hold(lane):
    service, _, factory, clock = lane
    row = OUTCOMES["price_aggressive_structured"][0]
    event = event_for()
    event.payload.symbol = row["symbol"]
    event.payload.metadata = dict(row["order_metadata"])
    event.payload.metadata.pop("reject_reason", None)
    seed_segment(factory, event, clock)
    report = outcome_report("price_aggressive_structured")
    service._observe_webull_mirror_deferred_reports(event=event, reports=[report])
    slot = event.payload.metadata["fanout_slot_id"]
    assert slot in service._webull_mirror_deferred_by_slot
    event.payload.metadata["webull_deferred_resubmit_attempt"] = "2"
    service._webull_mirror_deferred_by_slot.clear()
    await service.process_trade_intent(event)
    set_quote(service, event, clock)
    await service._evaluate_nfq_holds(event.payload.symbol)
    retry = queued_events(service)[-1]
    assert retry.payload.metadata["webull_deferred_resubmit_attempt"] == "2"
    service._observe_webull_mirror_deferred_reports(event=retry, reports=[report])
    assert service._webull_mirror_deferred_by_slot[slot].attempts == 2
    service._webull_mirror_deferred_by_slot[slot].attempts = 3
    service._observe_webull_mirror_deferred_reports(event=retry, reports=[report])
    assert slot not in service._webull_mirror_deferred_by_slot


@pytest.mark.asyncio
@pytest.mark.parametrize("age_ms", [2001, 3000, 9999])
async def test_premarket_resting_path_uses_shared_two_seconds_even_with_mirror_ten(lane, monkeypatch, age_ms):
    from tests.unit.test_oms_v2_eh_resting_entry import _eh_resting_meta, _v2_open
    service, adapter, _, clock = lane
    service.settings.strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled = True
    monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: "AM")
    monkeypatch.setattr(oms, "_is_regular_market_session", lambda now=None: False)
    service._latest_quotes_by_symbol["FOO"] = {
        "ask": 9.52, "bid": 9.50, "received_at": clock[0] - timedelta(milliseconds=age_ms),
    }
    result = await service.process_trade_intent(_v2_open(_eh_resting_meta()))
    assert not adapter.requests
    assert "NO_FRESH_QUOTE" in result[-1].payload.reason


@pytest.mark.asyncio
async def test_restart_after_cancel_cannot_resurrect_a_hold(lane):
    service, _, factory, _ = lane
    event, retry = await hold_and_queue(lane)
    cancel = event.model_copy(deep=True)
    cancel.payload.intent_type = "cancel"
    service._nfq_observe_intent(cancel)
    restarted, adapter = _integrated_service(factory, enabled=True, nfq_enabled=True)
    restarted._restore_nfq_holds()
    assert not restarted._nfq_holds
    await restarted._handle_stream_message({"data": retry.model_dump_json()})
    assert not adapter.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["missing", "active_string", "wrong_strategy", "wrong_session", "ended"])
@pytest.mark.parametrize("restart", [False, True])
async def test_unproven_or_ended_segment_cannot_resume(lane, evidence, restart):
    service, adapter, factory, clock = lane
    event = event_for()
    await service.process_trade_intent(event)
    with factory() as session:
        rows = session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == SEGMENT_SNAPSHOT,
        )).all()
        if evidence == "missing":
            session.execute(delete(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SEGMENT_SNAPSHOT,
            ))
        else:
            for row in rows:
                payload = dict(row.payload)
                key, value = {
                    "active_string": ("active", "true"),
                    "wrong_strategy": ("strategy_code", "orb"),
                    "wrong_session": ("session_anchor", "yesterday"),
                    "ended": ("active", False),
                }[evidence]
                payload[key] = value
                row.payload = payload
        session.commit()
    if restart:
        service, adapter = _integrated_service(factory, enabled=True, nfq_enabled=True)
        service._restore_nfq_holds()
    set_quote(service, event, clock)
    await service._evaluate_nfq_holds(event.payload.symbol)
    assert not queued_events(service)
    assert not adapter.requests
    assert not service._nfq_holds
    assert outcomes(factory)[-1]["reason"] == (RETIRED_PREFIX if evidence == "ended" else GAVE_UP_PREFIX) + (
        "segment_ended" if evidence == "ended" else "segment_identity_unproven"
    )


@pytest.mark.asyncio
async def test_uncertain_enqueue_retires_token_and_notifies_exactly_once(lane):
    service, adapter, factory, clock = lane
    event = event_for()
    await service.process_trade_intent(event)
    queued = []

    async def write_then_timeout(stream, fields, **kwargs):
        queued.append(TradeIntentEvent.model_validate_json(fields["data"]))
        raise TimeoutError("controlled lost enqueue acknowledgement")

    service.redis.xadd = write_then_timeout
    set_quote(service, event, clock)
    await service._evaluate_nfq_holds(event.payload.symbol)
    assert len(queued) == 1
    await service._handle_stream_message({"data": queued[0].model_dump_json()})
    service._finish_nfq_retry(queued[0])
    await service._evaluate_nfq_holds(event.payload.symbol)
    assert not adapter.requests
    terminal = [r for r in outcomes(factory) if r["reason"] == GAVE_UP_PREFIX + "serial_enqueue_unconfirmed"]
    assert len(terminal) == 1
    assert not service._nfq_holds


@pytest.mark.asyncio
async def test_ask_past_band_then_finish_has_one_terminal_giveup(lane):
    service, adapter, factory, clock = lane
    event, retry = await hold_and_queue(lane)
    install_shape_adapter(adapter, clock)
    service._latest_quotes_by_symbol[event.payload.symbol]["ask"] = Decimal("1.80")
    await service._handle_stream_message({"data": retry.model_dump_json()})
    service._finish_nfq_retry(retry)
    terminal = [r for r in outcomes(factory) if r["reason"].startswith(GAVE_UP_PREFIX)]
    assert len(terminal) == 1


@pytest.mark.asyncio
async def test_pa1_handoff_drops_nfq_token_but_keeps_attempt_budget(lane):
    service, adapter, factory, clock = lane
    event, retry = await hold_and_queue(lane)
    assert service._claim_nfq_retry(retry)
    retry.payload.metadata["fanout_attempt_id"] = service._build_client_order_id(retry)
    report = outcome_report("price_aggressive_structured")
    service._observe_webull_mirror_deferred_reports(event=retry, reports=[report])
    with factory() as session:
        service._nfq_observe_reports(session, retry, [report])
        session.commit()
    assert not service._nfq_holds
    await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
    pa_retry = queued_events(service)[-1]
    assert "nfq_retry_token" not in pa_retry.payload.metadata
    assert pa_retry.payload.metadata["webull_deferred_resubmit_attempt"] == "1"
    await service._handle_stream_message({"data": pa_retry.model_dump_json()})
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_old_generation_cancel_cannot_kill_repriced_hold(lane):
    service, _, _, _ = lane
    original = event_for()
    original.payload.metadata["webull_mirror_generation_id"] = "original-generation"
    await service.process_trade_intent(original)
    replacement = original.model_copy(deep=True)
    replacement.event_id = uuid4()
    replacement.payload.metadata["webull_mirror_generation_id"] = "replacement-generation"
    await service.process_trade_intent(replacement)
    cancel = original.model_copy(deep=True)
    cancel.payload.intent_type = "cancel"
    cancel.payload.metadata.pop("fanout_attempt_id", None)
    service._nfq_observe_intent(cancel)
    hold = service._nfq_holds[replacement.payload.metadata["fanout_slot_id"]]
    assert hold.event.event_id == replacement.event_id


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancel_intent", "replacement", "cancelled", "expired"])
async def test_ordinary_lifecycle_terminal_is_not_counted_as_nfq_give_up(lane, caplog, terminal):
    service, adapter, factory, clock = lane
    event, stale = await hold_and_queue(lane)
    strategy, state, journal = consume_initial_hold(factory, event, clock)
    if terminal in {"cancel_intent", "replacement"}:
        next_event = event.model_copy(deep=True)
        next_event.event_id = uuid4()
        if terminal == "cancel_intent":
            next_event.payload.intent_type = "cancel"
        service._nfq_observe_intent(next_event)
    else:
        # Controlled terminal delivery tests classification, not broker behaviour.
        report = ExecutionReport(
            event_type=terminal, origin="broker", symbol=event.payload.symbol,
            client_order_id=event.payload.metadata["fanout_attempt_id"], side="buy",
            quantity=event.payload.quantity, intent_type="open", reason="controlled lifecycle " + terminal,
            metadata=dict(event.payload.metadata),
        )
        with factory() as session:
            service._nfq_observe_reports(session, event, [report])
            session.commit()
    journal.poll(strategy.apply_fanout_outcome)
    assert not state.webull_resting_active and not state.fanout_webull_claimed
    assert state.resting_active and state.resting_is_broker_order
    await service._handle_stream_message({"data": stale.model_dump_json()})
    assert not adapter.requests
    assert len([r for r in outcomes(factory) if r["reason"].startswith(RETIRED_PREFIX)]) == 1
    assert not [r for r in outcomes(factory) if r["reason"].startswith(GAVE_UP_PREFIX)]
    assert any("decision=retired" in r.message for r in caplog.records)
    assert not any("decision=gave_up" in r.message for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("held,orders,symbol,expected_selects", [
    (False, 0, "AIXI", 0), (True, 0, "OTHER", 0),
    (True, 0, "AIXI", 2), (True, 1, "AIXI", 4),
])
async def test_nfq_tick_database_read_cost(lane, held, orders, symbol, expected_selects):
    from sqlalchemy import event as sql_event
    service, _, factory, _ = lane
    event = event_for()
    if held:
        await service.process_trade_intent(event)
    if orders:
        with factory() as session:
            intent = session.scalar(select(TradeIntent))
            account = BrokerAccount(name="live:schwab_1m_v2", provider="schwab", environment="live")
            session.add(account)
            session.flush()
            session.add(BrokerOrder(
                intent_id=intent.id, strategy_id=intent.strategy_id, broker_account_id=account.id,
                client_order_id="controlled-live-schwab-sibling", symbol=event.payload.symbol, side="buy",
                quantity=Decimal("2"), order_type="STOP_LIMIT", time_in_force="day", status="accepted",
                payload=dict(event.payload.metadata),
            ))
            session.commit()
    statements = []
    engine = factory.kw["bind"]

    def count_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sql_event.listen(engine, "before_cursor_execute", count_sql)
    try:
        await service._evaluate_nfq_holds(symbol)
    finally:
        sql_event.remove(engine, "before_cursor_execute", count_sql)
    assert len(statements) == expected_selects
    assert all(s.lstrip().startswith("SELECT") for s in statements)


def consume_initial_hold(factory, event, clock):
    strategy = SchwabV2Strategy(Settings())
    state = strategy.watchlist_state(event.payload.symbol)
    md = event.payload.metadata
    state.fanout_segment_id = int(md["fanout_segment_id"])
    state.fanout_claim_slot_id = md["fanout_slot_id"]
    state.fanout_claim_slot = "resting"
    state.fanout_claim_attempt_id = md["fanout_attempt_id"]
    state.resting_active = state.resting_is_broker_order = True
    state.resting_level = 1.6897
    journal = FanoutOutcomeJournal(factory)
    journal.bootstrap(strategy.apply_fanout_outcome,
                      active_segments={event.payload.symbol: state.fanout_segment_id}, now=clock[0])
    assert state.webull_resting_active
    assert state.fanout_claim_outcome == "held"
    return strategy, state, journal


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["ask_past_band", "wire_expiry", "window_ended"])
async def test_retry_feedback_owns_first_terminal_and_preserves_schwab(lane, refusal):
    service, adapter, factory, clock = lane
    event, retry = await hold_and_queue(lane)
    strategy, state, journal = consume_initial_hold(factory, event, clock)
    delay = timedelta(seconds=11) if refusal == "wire_expiry" else timedelta(0)
    if refusal == "window_ended":
        delay = clock[0].replace(hour=19, minute=45) - clock[0]
    install_shape_adapter(adapter, clock, wire_delay=delay)
    if refusal == "ask_past_band":
        service._latest_quotes_by_symbol[event.payload.symbol]["ask"] = Decimal("1.80")
    await service._handle_stream_message({"data": retry.model_dump_json()})
    journal.poll(strategy.apply_fanout_outcome)
    if refusal == "wire_expiry":
        assert state.webull_resting_active
        assert state.fanout_claim_outcome == "held"
        assert state.fanout_claim_attempt_id == service._build_client_order_id(retry)
        clock[0] = clock[0].replace(hour=19, minute=45)
        await service._evaluate_nfq_holds()
        journal.poll(strategy.apply_fanout_outcome)
    assert not state.webull_resting_active
    assert not state.fanout_webull_claimed
    assert state.resting_active and state.resting_is_broker_order
    assert state.resting_level == 1.6897
    assert not adapter.requests
    terminals = [r for r in outcomes(factory) if r["outcome"] == "rejected_client_abort"]
    assert len(terminals) == 1
    assert terminals[0]["reason"].startswith(GAVE_UP_PREFIX)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["acknowledgement", "report_transaction"])
async def test_uncertain_acceptance_keeps_addressable_order_and_restores_without_duplicate(lane, monkeypatch, failure_point):
    service, adapter, factory, clock = lane
    event, retry = await hold_and_queue(lane)
    strategy, state, journal = consume_initial_hold(factory, event, clock)
    if failure_point == "acknowledgement":
        submit = adapter.submit_order

        async def accepted_then_timeout(request):
            await submit(request)
            raise TimeoutError("simulated acceptance with lost acknowledgement")

        monkeypatch.setattr(adapter, "submit_order", accepted_then_timeout)
    else:
        record = service._record_order_reports

        async def recorded_then_rollback(**kwargs):
            await record(**kwargs)
            raise RuntimeError("controlled ledger transaction rollback after simulated acceptance")

        monkeypatch.setattr(service, "_record_order_reports", recorded_then_rollback)
    with pytest.raises((TimeoutError, RuntimeError)):
        await service._handle_stream_message({"data": retry.model_dump_json()})
    assert len(adapter.requests) == 1
    client_id = service._build_client_order_id(retry)
    with factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == client_id))
        assert order is not None and order.status == "pending"
        assert order.payload["nfq_retry_token"] == retry.payload.metadata["nfq_retry_token"]
        assert session.scalar(select(Fill).where(Fill.order_id == order.id)) is None
    journal.poll(strategy.apply_fanout_outcome)
    assert state.webull_resting_active and state.fanout_claim_outcome == "held"
    assert state.fanout_claim_attempt_id == client_id
    slot = event.payload.metadata["fanout_slot_id"]
    assert service._nfq_holds[slot].phase == "uncertain"
    restarted, new_adapter = _integrated_service(factory, enabled=True, nfq_enabled=True)
    restarted._restore_nfq_holds()
    assert restarted._nfq_holds[slot].phase == "uncertain"
    set_quote(restarted, event, clock)
    await restarted._evaluate_nfq_holds(event.payload.symbol)
    await restarted._handle_stream_message({"data": retry.model_dump_json()})
    clock[0] = clock[0].replace(hour=20)
    await restarted._evaluate_nfq_holds()
    assert restarted._nfq_holds[slot].phase == "uncertain"
    assert not new_adapter.requests and not queued_events(restarted)
    journal.poll(strategy.apply_fanout_outcome)
    assert state.webull_resting_active and state.fanout_webull_claimed
    assert state.resting_active and state.resting_is_broker_order
    assert not [r for r in outcomes(factory) if r["reason"].startswith(GAVE_UP_PREFIX)]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_field", ["fanout_predecessor_attempt_id", "webull_mirror_generation_id"])
async def test_retry_attempt_transition_requires_current_predecessor_and_generation(lane, bad_field):
    service, _, factory, clock = lane
    event, _ = await hold_and_queue(lane)
    strategy, state, journal = consume_initial_hold(factory, event, clock)
    original_attempt = state.fanout_claim_attempt_id
    md = dict(event.payload.metadata)
    md.update(fanout_predecessor_attempt_id=original_attempt, fanout_attempt_id="stale-retry")
    md[bad_field] = "stale"
    journal.record(metadata=md, symbol=event.payload.symbol, outcome="queued", evidence_id="stale-queued")
    journal.record(metadata=md, symbol=event.payload.symbol, outcome="held_no_fresh_quote", evidence_id="stale-held")
    journal.record(metadata=md, symbol=event.payload.symbol, outcome="rejected_client_abort",
                   reason=GAVE_UP_PREFIX + "resting_window_ended", evidence_id="stale-terminal")
    journal.poll(strategy.apply_fanout_outcome)
    assert state.fanout_claim_attempt_id == original_attempt
    assert state.webull_resting_active and state.fanout_claim_outcome == "held"


@pytest.mark.asyncio
async def test_retry_risk_refusal_first_terminal_clears_only_webull_with_actual_reason(lane, monkeypatch, caplog):
    service, adapter, factory, clock = lane
    event, retry = await hold_and_queue(lane)
    strategy, state, journal = consume_initial_hold(factory, event, clock)
    reason = "controlled_existing_risk_guard_refusal"
    monkeypatch.setattr(service, "_evaluate_risk", lambda event: (False, reason))
    await service._handle_stream_message({"data": retry.model_dump_json()})
    journal.poll(strategy.apply_fanout_outcome)
    assert not state.webull_resting_active and not state.fanout_webull_claimed
    assert state.resting_active and state.resting_is_broker_order
    assert not adapter.requests and not service._nfq_holds
    terminals = [r for r in outcomes(factory) if r["outcome"] in {"dropped_risk", "rejected_client_abort"}]
    assert len(terminals) == 1
    assert reason in terminals[0]["reason"]
    assert any("decision=retired" in r.message and reason in r.message for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [0, 3])
async def test_recorded_broker_reject_feedback_honors_pa1_budget(lane, monkeypatch, attempt):
    service, _, factory, clock = lane
    row = OUTCOMES["price_aggressive_structured"][0]
    event = event_for()
    event.payload.symbol = row["symbol"]
    event.payload.metadata = dict(row["order_metadata"])
    event.payload.metadata.pop("reject_reason", None)
    # The real PA1 response is terminal only at the unchanged three-attempt cap.
    event.payload.metadata["webull_deferred_resubmit_attempt"] = str(attempt)
    event, retry = await hold_and_queue(lane, event)
    strategy, state, journal = consume_initial_hold(factory, event, clock)

    async def recorded_reject(request):
        return [outcome_report("price_aggressive_structured")]

    monkeypatch.setattr(service.broker_adapter, "submit_order", recorded_reject)
    await service._handle_stream_message({"data": retry.model_dump_json()})
    journal.poll(strategy.apply_fanout_outcome)
    assert state.resting_active and state.resting_is_broker_order
    terminals = [r for r in outcomes(factory) if r["outcome"] in {"rejected_venue", "rejected_client_abort"}]
    assert not service._nfq_holds
    if attempt < 3:
        assert state.webull_resting_active and state.fanout_webull_claimed
        assert state.fanout_claim_outcome == "held"
        assert service._webull_mirror_deferred_by_slot
        assert not terminals
        return
    assert not state.webull_resting_active and not state.fanout_webull_claimed
    assert not service._webull_mirror_deferred_by_slot
    assert len(terminals) == 1
    assert "PRICE_AGGRESSIVE" in terminals[0]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [
    "dropped_ineligible", "dropped_routing", "dropped_risk", "dropped_collision", "dropped_dedup",
    "rejected_client_abort", "rejected_venue", "cancelled", "expired",
])
@pytest.mark.parametrize("new_generation", [False, True])
async def test_first_terminal_family_is_scoped_and_cleanup_does_not_notify_twice(lane, outcome, new_generation, caplog):
    service, _, factory, clock = lane
    event, _ = await hold_and_queue(lane)
    strategy, state, journal = consume_initial_hold(factory, event, clock)
    if new_generation:
        state.webull_resting_generation_id = "new-generation-must-survive"
    reason = "controlled_" + outcome
    # Exercise the actual generic pre-submit store boundary and poll BEFORE NFQ cleanup.
    # These are client-side classification schedules, not fabricated broker responses.
    with factory() as session:
        intent = session.scalar(select(TradeIntent))
        intent.payload = {**intent.payload, "metadata": dict(event.payload.metadata)}
        service.store.record_fanout_pre_submit_outcome(
            session, intent=intent, outcome=outcome, reason=reason, broker_account_name=ACCOUNT,
        )
        session.commit()
    journal.poll(strategy.apply_fanout_outcome)
    assert state.webull_resting_active is new_generation
    assert state.fanout_webull_claimed is new_generation
    assert state.resting_active and state.resting_is_broker_order
    with factory() as session:
        service._nfq_retire(session, service._nfq_holds[event.payload.metadata["fanout_slot_id"]], "serial_pipeline_ended")
        session.commit()
    assert len([r for r in outcomes(factory) if r["reason"].startswith(RETIRED_PREFIX)]) == 1
    assert not [r for r in outcomes(factory) if r["reason"].startswith(GAVE_UP_PREFIX)]
    assert any("decision=retired" in r.message and reason in r.message for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("wall_day", [1, 2, 3])
@pytest.mark.parametrize("retry_owner", ["NFQ", "PA1"])
async def test_retry_produced_at_uses_oms_clock_not_envelope_wall_date(lane, monkeypatch, wall_day, retry_owner):
    from project_mai_tai import events

    class EnvelopeWallClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, wall_day, 14, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(events, "datetime", EnvelopeWallClock)
    service, adapter, factory, clock = lane
    event, retry = await hold_and_queue(lane)
    assert retry.produced_at == clock[0]
    if retry_owner == "PA1":
        assert service._claim_nfq_retry(retry)
        retry.payload.metadata["fanout_attempt_id"] = service._build_client_order_id(retry)
        report = outcome_report("price_aggressive_structured")
        service._observe_webull_mirror_deferred_reports(event=retry, reports=[report])
        with factory() as session:
            service._nfq_observe_reports(session, retry, [report])
            session.commit()
        await service._evaluate_webull_mirror_deferred_resubmits(event.payload.symbol)
        retry = queued_events(service)[-1]
        assert retry.produced_at == clock[0]
    await service._handle_stream_message({"data": retry.model_dump_json()})
    assert len(adapter.requests) == 1
