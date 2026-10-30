"""UNRUN retained CHOW rows + explicitly CONTROLLED private runtime plumbing.

Main executor only. No native/provider/Redis connection; all native absence is
the supplied bounded export, not a fake provider empty-list response.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, Strategy, TradeIntent
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload, TradeIntentEvent, TradeIntentPayload
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.oms import bot_control, bot_control_runtime, service as oms
from project_mai_tai.settings import Settings

from tests.unit.wbpa2_controlled_transport import ControlledProvider, ControlledRedis, ControlledSubmitReply

RAW_SHA = "7f8663cde1757ef67f051bbfa799bf58cc0a93bec57905aab1ecd95748512881"
ORIGINAL_INTENT = "1c696692-1c90-4408-b584-e7fca8919f26"
ORIGINAL_COID = "schwab_1m_v2-CHOW-open-8429b7f271cc"
ORIGINAL_ORDER = "5d842b0a-d02d-491b-892f-a17b0456c8e0"
FINAL_INTENT = "2d57adb7-28d1-424f-8667-baddc3a03a37"
SYMBOL, ACCOUNT = "CHOW", "live:orb"
SLOT = "06f8c5aa-eafc-5203-986c-c5ae4c8fa316"
SEGMENT = "1793374142931"
FIRST_CLOCK = datetime(2026, 10, 30, 15, 29, 4, tzinfo=UTC)
BOUNDARY = Decimal("5.7592")


def retained():
    path = Path(os.environ["WBPA2_CHOW_EPISODE_SOURCE"])
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == RAW_SHA, "exact reviewed CHOW export required"
    result = json.loads(raw)
    assert [len(result[key]) for key in ("intents", "orders", "order_events", "accounts")] == [7, 4, 4, 2]
    assert all(result[key] is None for key in ("fills", "virtual_positions", "account_positions"))
    return result


def event_from_intent(intent):
    # Actual wire event produced_at is absent from DB export. Its DB-created
    # clock is an explicitly disclosed reconstruction proxy, not a raw event.
    return TradeIntentEvent(event_id=UUID(intent["payload"]["event_id"]),
        source_service=intent["payload"]["source_service"], produced_at=datetime.fromisoformat(intent["created_at"]),
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=ACCOUNT,
            symbol=SYMBOL, side=intent["side"], intent_type=intent["intent_type"],
            quantity=Decimal(str(intent["quantity"])), reason=intent["reason"],
            metadata=deepcopy(intent["payload"]["metadata"])))


def original_projection(raw, intent_id=ORIGINAL_INTENT):
    intent = next(row for row in raw["intents"] if row["id"] == intent_id)
    event = event_from_intent(intent)
    order = next(row for row in raw["orders"] if row["intent_id"] == intent_id)
    audit = next(row for row in raw["order_events"] if row["order_id"] == order["id"])
    assert audit["event_source"] == "broker"
    if intent_id == ORIGINAL_INTENT:
        assert order["client_order_id"] == ORIGINAL_COID and order["id"] == ORIGINAL_ORDER
    md = audit["payload"]["metadata"]
    assert md["webull_error_code"] == "ORDER_RISK_RULE_PRICE_AGGRESSIVE" and md["webull_http_status"] == "417"
    report = ExecutionReport("rejected", order["client_order_id"], symbol=SYMBOL, side="buy", intent_type="open",
        quantity=Decimal(str(order["quantity"])), filled_quantity=Decimal(0),
        reported_at=datetime.fromisoformat(audit["event_at"]), origin="broker",
        reason=audit["payload"]["reason"], metadata=deepcopy(md))
    return event, report


def decode_model(model, row):
    columns = {attr.key: attr.columns[0] for attr in inspect(model).column_attrs}
    values = {}
    for key, value in row.items():
        attr = "metadata_json" if key == "metadata" else key
        assert attr in columns, f"unbound exported column {model.__name__}.{key}"
        column = columns[attr]
        kind = column.type.python_type
        if value is not None and kind is UUID:
            value = UUID(str(value))
        elif value is not None and kind is datetime:
            value = datetime.fromisoformat(value)
            assert value.tzinfo is not None
        elif value is not None and kind is Decimal:
            value = Decimal(str(value))
        values[attr] = deepcopy(value)
    return model(**values)


@dataclass
class Case:
    service: object
    provider: ControlledProvider
    redis: ControlledRedis
    factory: object
    engine: object
    clock: list
    raw: dict
    event: TradeIntentEvent
    report: ExecutionReport
    seeded_ids: dict


async def _make_case(tmp_path, monkeypatch, *, enabled=True, replies=(), final_slot=False):
    raw = retained()
    event, report = original_projection(raw, FINAL_INTENT if final_slot else ORIGINAL_INTENT)
    clock = [datetime(2026, 10, 30, 15, 45, 6, tzinfo=UTC) if final_slot else FIRST_CLOCK]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    # Time operands only: no session/risk/owner/permission function is replaced.
    monkeypatch.setattr(oms, "utcnow", lambda: clock[0])
    monkeypatch.setattr(bot_control, "datetime", Clock)
    monkeypatch.setattr(bot_control_runtime, "datetime", Clock)
    settings_values = dict(environment="production", redis_stream_prefix="CONTROLLED-WBPA2",
        strategy_schwab_1m_v2_enabled=True, strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_broker_provider="schwab", strategy_schwab_1m_v2_webull_account_name=ACCOUNT,
        strategy_schwab_1m_v2_country_route_enabled=True, strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True, strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_resting_buy_round_up_enabled=True, strategy_schwab_1m_v2_atr_reprice_handoff_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True, strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
        strategy_schwab_1m_v2_keepwatch1_enabled=True, strategy_schwab_1m_v2_brokerdecides1_enabled=True,
        strategy_schwab_1m_v2_wbexit2_enabled=True, oms_webull_exit3_enabled=True,
        oms_v2_webull_mirror_deferred_resubmit_enabled=enabled,
        oms_v2_webull_mirror_retained_hold_enabled=True, bot_control_enabled=True,
        oms_v2_exit_management_enabled=True)
    for key, value in settings_values.items():
        assert key in Settings.model_fields, f"unknown fixture setting {key}"
        if type(value) is bool:
            assert Settings.model_fields[key].annotation is bool
    settings = Settings(_env_file=None, **settings_values)
    assert all(getattr(settings, key) == value for key, value in settings_values.items())
    assert settings.oms_v2_webull_mirror_deferred_resubmit_enabled is enabled
    for key in ("bot_control_enabled", "strategy_schwab_1m_v2_country_route_enabled",
                "strategy_schwab_1m_v2_keepwatch1_enabled", "strategy_schwab_1m_v2_brokerdecides1_enabled",
                "strategy_schwab_1m_v2_wbexit2_enabled", "oms_webull_exit3_enabled",
                "oms_v2_webull_mirror_retained_hold_enabled"):
        assert key in Settings.model_fields and getattr(settings, key) is True
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "wbpa2.sqlite"),
        connect_args={"check_same_thread": False}, poolclass=NullPool)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    seeded = {}
    try:
        with factory.begin() as session:
            for row in raw["accounts"]:
                session.add(decode_model(BrokerAccount, row))
            # Mandatory strategy row omitted from export: CONTROLLED relational
            # plumbing only, never represented as an original source DB row.
            session.add(Strategy(id=UUID(raw["intents"][0]["strategy_id"]), code="schwab_1m_v2",
                name="CONTROLLED omitted strategy plumbing", execution_mode="live", is_enabled=True,
                metadata_json={"fixture_provenance": "CONTROLLED_NOT_EXPORTED"}))
            session.flush()
            for key, model, field in (("intents", TradeIntent, "created_at"),
                ("orders", BrokerOrder, "submitted_at"), ("order_events", BrokerOrderEvent, "event_at")):
                prefix = [row for row in raw[key] if datetime.fromisoformat(row[field]) <= clock[0]]
                seeded[key] = [row["id"] for row in prefix]
                for row in prefix:
                    session.add(decode_model(model, row))
                session.flush()
        assert [len(seeded[key]) for key in ("intents", "orders", "order_events")] == (
            [7, 4, 4] if final_slot else [1, 1, 1])
        redis = ControlledRedis()
        provider = ControlledProvider(account=ACCOUNT, symbol=SYMBOL, slot=SLOT, segment=SEGMENT,
                                      clock=lambda: clock[0], replies=replies)
        service = oms.OmsRiskService(settings=settings, redis_client=redis,
            session_factory=factory, broker_adapter=provider)
        assert service.strategy_registrations["schwab_1m_v2"].execution_mode == "live"
        redis.values[service._session_symbol_block_key(account_name=ACCOUNT, symbol=SYMBOL,
                                                      session_date="2026-10-30")] = None
        control = service._bot_control_runtime
        assert control is not None
        control.store.seed(UUID(raw["intents"][0]["strategy_id"]))
        await control.refresh()
        assert control.admission_open("schwab_1m_v2")
        # Required active segment is not in bounded export; real journal writer
        # with a disclosed CONTROLLED positive operand, not a gate lambdaTrue.
        FanoutSegmentIdentityStore(factory).record(SYMBOL, int(SEGMENT), True,
            "CONTROLLED active segment for retained original slot", now=clock[0])
        await service._refresh_drift_working_cache()
        return Case(service, provider, redis, factory, engine, clock, raw, event, report, seeded)
    except BaseException:
        engine.dispose()
        raise


async def make_case(tmp_path, monkeypatch, *, enabled=True, replies=(), final_slot=False):
    # Parent cancellation never cancels a setup DB await/to_thread child.
    task = asyncio.create_task(_make_case(tmp_path, monkeypatch, enabled=enabled, replies=replies,
                                         final_slot=final_slot))
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    case = task.result()
    if cancellation is not None:
        await close_case(case)
        raise cancellation
    return case


def state(case):
    key = (ACCOUNT, "schwab_1m_v2", SYMBOL, SEGMENT, SLOT)
    return case.service.__dict__.get("_webull_country_deferred_by_slot", {}).get(key)


def queued(case):
    return [TradeIntentEvent.model_validate_json(fields["data"]) for stream, fields, _ in case.redis.rows
            if stream.endswith("strategy-intents")]


def observe_original(case):
    with case.factory.begin() as session:
        case.service._observe_webull_mirror_deferred_reports(event=case.event,
            reports=[case.report], session=session)


def real_v2_cancel(case, *, reason="flip_no_fill"):
    """Real v2 cancel emitter at a CONTROLLED SELL boundary; not full bar replay."""
    from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
    strategy = SchwabV2Strategy(case.service.settings)
    current = strategy.watchlist_state(SYMBOL)
    md = case.event.payload.metadata
    current.fanout_segment_id = int(SEGMENT)
    current.atr_country_route = {key: value for key, value in md.items() if key.startswith("atr_country_")}
    current.resting_active = current.resting_is_broker_order = current.webull_resting_active = True
    current.resting_level = float(md["cw_flip_level"])
    current.resting_trigger = current.resting_webull_wire_stop = float(md["stop_price"])
    current.resting_webull_wire_limit = float(md["limit_price"])
    current.resting_webull_quantity = int(case.event.payload.quantity)
    current.resting_webull_generation = md["rpg_resting_generation"]
    current.webull_resting_generation_id = md["webull_mirror_generation_id"]
    current.resting_slot = "first"
    strategy._queue_resting_cancel(current, reason=reason)
    assert strategy.drain_pending_intents() == []  # selected Webull primary, no Schwab leg
    draft, = strategy.drain_webull_direct_intents()
    assert draft.metadata["fanout_slot_id"] == SLOT
    assert draft.metadata["fanout_segment_id"] == SEGMENT
    assert draft.metadata["rpg_resting_generation"] == md["rpg_resting_generation"]
    return TradeIntentEvent(source_service="schwab-1m-v2", produced_at=case.clock[0],
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=ACCOUNT,
            symbol=draft.symbol, side=draft.side, intent_type=draft.intent_type,
            quantity=draft.quantity, reason=draft.reason, metadata=draft.metadata))


def append_recorded_rows(case, through):
    """Append only completed captured rows at their recorded completion bound.

    The immutable packet holds final row state. Never seed its rejected status
    before that row's actual updated_at or its broker audit exists.
    """
    case.clock[0] = through
    with case.factory.begin() as session:
        for key, model, clock_key in (("intents", TradeIntent, "updated_at"),
            ("orders", BrokerOrder, "updated_at"), ("order_events", BrokerOrderEvent, "event_at")):
            for row in case.raw[key]:
                if row["id"] not in case.seeded_ids[key] and datetime.fromisoformat(row[clock_key]) <= through:
                    session.add(decode_model(model, row))
                    case.seeded_ids[key].append(row["id"])
            session.flush()


async def quote(case, ask=BOUNDARY, *, age=0):
    event = QuoteTickEvent(source_service="CONTROLLED-WBPA2-market",
        produced_at=case.clock[0] - timedelta(seconds=age),
        payload=QuoteTickPayload(symbol=SYMBOL, bid_price=Decimal(str(ask)) - Decimal(".01"),
                                 ask_price=Decimal(str(ask))))
    case.provider.market_tasks.add(asyncio.current_task())
    return await case.service._handle_quote_tick_event(event)


async def drain_ticks(case):
    # Finite actual scheduler tasks, not a manufactured completed callback.
    while case.service.__dict__.get("_symbol_tick_work"):
        tasks = tuple(case.service._symbol_tick_work.values())
        await asyncio.gather(*tasks)


async def serial(case, event):
    async def actor():
        case.provider.serial_task = asyncio.current_task()
        await case.service._handle_stream_message({"data": event.model_dump_json()})
    task = asyncio.create_task(actor())
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    if cancellation is not None:
        try:
            task.result()
        finally:
            raise cancellation
    return task.result()


async def close_case(case):
    # Main must reach this even after failed provider assertions/partial setup.
    async def cleanup():
        failures = []
        for operation in (case.service._shutdown_symbol_tick_work,
                          case.service._drain_webull_order_identities):
            try:
                await operation()
            except BaseException as exc:
                failures.append(exc)
        # Once tick admission is closed, these existing drain functions finish
        # all admitted work. No external Task.cancel is sent by this fixture.
        assert not case.service.__dict__.get("_symbol_tick_work")
        case.engine.dispose()
        if failures:
            raise failures[0]

    task = asyncio.create_task(cleanup())
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    if cancellation is not None:
        try:
            task.result()
        finally:
            raise cancellation
    task.result()
