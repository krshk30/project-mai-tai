"""Real paper detection -> handoff -> canonical OMS -> real actor offer.

This proves the ingress contract, NOT actor execution, durable native ownership,
fills or a production incident. No permission-return mock is used. Unexpected
execution/persistence calls fail rather than manufacturing a successful BUY.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest

from project_mai_tai.buy_price_floor import buy_intent_allowed, buy_order_allowed
from project_mai_tai.events import TradeIntentEvent
from project_mai_tai.momentum_live.actor import ActorPorts, LiveActor
from project_mai_tai.momentum_live.admission import EpisodeLedger
from project_mai_tai.momentum_live.execution import ExecutionPorts, LiveEntryExecutor
from project_mai_tai.momentum_live.handoff import LiveHandoff
from project_mai_tai.momentum_live_sec import CountryRouter
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


NOW = datetime(2026, 10, 22, 14, tzinfo=UTC)
OBSERVED = int(NOW.timestamp()*1_000_000_000)


async def forbidden_io(*args, **kwargs):
    raise AssertionError("PRICE1 ingress witness reached execution/persistence")


async def real_handoff_event(price="1.20"):
    engine = MomentumPaperEngine(prior_closes={"SCNI": Decimal(".80")},
        condition_version="CONTROLLED", coverage_started_ms=OBSERVED//1_000_000-30_000,
        strategy_windows={"momentum_30s_warn": 30})
    end = OBSERVED//1_000_000-1000
    records = engine.detect_from_bucket(strategy_code="momentum_30s_warn", symbol="SCNI",
        reference_price=Decimal(".80"), reference_start_ms=end-1000, reference_end_ms=end,
        signal_price=Decimal(price), signal_start_ms=end, signal_end_ms=end+1000,
        observed_decision_ns=OBSERVED, aggregate_received_utc_ns=OBSERVED,
        aggregate_published_utc_ns=OBSERVED)
    records = engine.seal_warning_decisions(records, observed_decision_ns=OBSERVED)
    assert len(records) == 1 and records[0].payload["prior_close"] == "0.80"
    writes = []

    async def xadd(stream, fields, **kwargs):
        writes.append((stream, dict(fields)))
        return "controlled-1"

    settings = Settings(_env_file=None, momentum_live_enabled=True)
    router = CountryRouter(SimpleNamespace())  # Actual unknown-SEC route policy, no lookup.
    handoff = LiveHandoff(SimpleNamespace(xadd=xadd), settings, route_for=router.route_for)
    handoff.ledger = EpisodeLedger.restore([])  # Explicit controlled EMPTY owner snapshot.
    assert handoff.offer(records[0], day_volume=50_000, now_ns=OBSERVED) == "offered"
    worker = asyncio.create_task(handoff._emit())
    try:
        await asyncio.wait_for(handoff.queue.join(), timeout=1)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert len(writes) == 1
    event = TradeIntentEvent.model_validate_json(writes[0][1]["data"])
    return settings, router, event


async def ingress(settings, router):
    book = EpisodeLedger.restore([])
    clock, quote = lambda: NOW, lambda _: None
    entry = LiveEntryExecutor(book, ExecutionPorts(clock, quote, router.route_for,
        forbidden_io, forbidden_io, forbidden_io, forbidden_io, forbidden_io,
        forbidden_io, forbidden_io), schwab_account="live:schwab_1m_v2", webull_account="live:orb")
    actor = LiveActor(book, entry, ActorPorts(lambda: settings.momentum_live_enabled,
        lambda: OBSERVED, forbidden_io, forbidden_io, forbidden_io, forbidden_io,
        forbidden_io, quote), state_lock=asyncio.Lock())
    await actor.restore_before_serving()
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings, service.logger = settings, logging.getLogger("price1-real-handoff")
    service._momentum_live_actor = actor
    service._run_db = forbidden_io
    return service, actor


@pytest.mark.asyncio
@pytest.mark.parametrize("price", ["1.00", "1.20", "5.25"])
async def test_real_producer_reaches_canonical_guard_and_actual_actor_offer_once(price):
    settings, router, event = await real_handoff_event(price)
    raw = json.loads(event.payload.metadata["momlive_signal"])
    assert "signal_price" not in event.payload.metadata
    assert raw["signal_price"] == price
    assert event.source_service == "momentum-paper"
    assert str(event.event_id) == event.payload.metadata["episode_id"]
    assert raw["source_id"].startswith("momentum_30s_warn:SCNI:")
    assert raw["observed_ns"] == OBSERVED and event.produced_at == NOW
    assert buy_order_allowed(event.payload) and buy_intent_allowed(event)
    before = event.model_dump_json()
    service, actor = await ingress(settings, router)
    assert await service.process_trade_intent(event) == []
    assert actor.inbox.qsize() == 1 and actor.inbox.get_nowait() is event
    assert event.model_dump_json() == before and not actor.book.episodes


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_price", "missing_nested", "conflict_top", "conflict_nested",
    "sub1_both", "padded_cap_not_anchor", "symbol", "episode", "source_service",
    "source_id", "observed_time", "future_interval", "duplicate_price", "bool_price"])
async def test_real_envelope_mutations_refuse_before_actor_or_generic_io(fault):
    settings, router, original = await real_handoff_event()
    event = original.model_copy(deep=True)
    md = event.payload.metadata
    raw = json.loads(md["momlive_signal"])
    if fault == "missing_price":
        md.pop("signal_price", None)
        raw.pop("signal_price")
    elif fault == "missing_nested":
        md.pop("momlive_signal")
    elif fault == "conflict_top":
        md["signal_price"] = "1.21"
    elif fault == "conflict_nested":
        md["signal_price"] = "1.20"
        raw["signal_price"] = ".9975"
    elif fault == "sub1_both":
        md["signal_price"] = raw["signal_price"] = ".9975"
    elif fault == "padded_cap_not_anchor":
        md.pop("signal_price", None)
        md.update(limit_price="1.20", reference_price="1.20", ask="1.20")
        raw["signal_price"] = ".9975"
    elif fault == "symbol":
        raw["symbol"] = "OTHER"
    elif fault == "episode":
        md["episode_id"] = str(uuid4())
    elif fault == "source_service":
        event = event.model_copy(update={"source_service": "other-producer"})
    elif fault == "source_id":
        raw["source_id"] = ""
    elif fault == "observed_time":
        event = event.model_copy(update={"produced_at": NOW+timedelta(seconds=1)})
    elif fault == "future_interval":
        raw["interval_end_ms"] = OBSERVED//1_000_000+1
    elif fault == "bool_price":
        raw["signal_price"] = True
    if "momlive_signal" in md:
        md["momlive_signal"] = json.dumps(raw)
        if fault == "duplicate_price":
            md["momlive_signal"] = md["momlive_signal"][:-1]+', "signal_price":"1.20"}'
    # model_copy reuses the deep-copied payload: no favorable top-level injection.
    assert not buy_intent_allowed(event)
    service, actor = await ingress(settings, router)
    assert await service.process_trade_intent(event) == []
    assert actor.inbox.empty() and not actor.book.episodes


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_top", [False, True])
async def test_original_nested_only_handoff_reaches_actual_actor_without_rewriting_event(duplicate_top):
    settings, router, original = await real_handoff_event("5.25")
    event = original.model_copy(deep=True)
    assert "signal_price" not in event.payload.metadata
    if duplicate_top:
        event.payload.metadata["signal_price"] = "5.25"
    before = event.model_dump_json()
    assert buy_order_allowed(event.payload) and buy_intent_allowed(event)
    service, actor = await ingress(settings, router)
    assert await service.process_trade_intent(event) == []
    assert actor.inbox.qsize() == 1 and actor.inbox.get_nowait() is event
    assert event.model_dump_json() == before and not actor.book.episodes


@pytest.mark.asyncio
async def test_missing_raw_price_logs_only_key_names_and_refuses(caplog):
    _, _, event = await real_handoff_event()
    event.payload.metadata.pop("signal_price", None)
    raw = json.loads(event.payload.metadata["momlive_signal"])
    raw.pop("signal_price")
    event.payload.metadata["momlive_signal"] = json.dumps(raw)
    with caplog.at_level(logging.INFO, logger="price1"):
        assert not buy_intent_allowed(event)
    assert "[PRICE1-NO-PRICE]" in caplog.text
    assert "payload_keys=" in caplog.text and "metadata_keys=" in caplog.text
    assert event.payload.metadata["momlive_signal"] not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("side,kind", [("sell", "close"), ("buy", "cancel")])
async def test_owned_exit_and_cancel_do_not_require_entry_price_envelope(side, kind):
    settings, router, event = await real_handoff_event()
    event = event.model_copy(update={"payload": event.payload.model_copy(update={
        "side": side, "intent_type": kind, "metadata": {}})})
    assert buy_intent_allowed(event)
    service, actor = await ingress(settings, router)
    assert await service.process_trade_intent(event) == []
    assert actor.inbox.qsize() == 1 and actor.inbox.get_nowait() is event
