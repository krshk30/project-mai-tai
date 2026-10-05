"""Production entry points, recorded strict reads, explicitly simulated placements.

Clock/strategy/ledger setup and later races are controlled scenarios. Readback
bodies retain their recorded identities; simulated acceptance is not venue data.
"""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import Base, BrokerOrder, DashboardSnapshot, TradeIntent, Fill, OmsManagedPosition
from project_mai_tai.broker_adapters.atr_buy_readback import AtrBuyReadback
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import service as oms
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from tests.unit.test_oms_webull_mirror_deferred_resubmit import _integrated_service
from tests.unit.test_rpg1_buy_readback import recorded


def _session_factory():
    # Worker readers must not share/rollback the serial writer's DBAPI transaction.
    directory = TemporaryDirectory(prefix="rpg-runtime-")
    engine = create_engine("sqlite+pysqlite:///" + str(Path(directory.name) / "runtime.sqlite"),
        connect_args={"check_same_thread": False}, poolclass=NullPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    factory._rpg_test_directory = directory
    return factory


class RecordedReadbackSimulator:
    def __init__(self, old, body, decode):
        self.old, self.body, self.decode = old, body, decode
        self.cancels, self.opens, self.reads = [], [], []
        self.protection = []
        self.override = None
        self.refusal = None
        self.refusal_metadata = {}

    async def submit_order(self, request):
        if request.intent_type == "cancel":
            self.cancels.append(request)
            return []  # A simulated lost ACK is never cancellation proof.
        (self.opens if request.side == "buy" else self.protection).append(request)
        if self.refusal:
            return [ExecutionReport("rejected", request.client_order_id, symbol=request.symbol,
                side="buy", intent_type="open", quantity=request.quantity,
                origin="broker", reason="SIMULATOR " + self.refusal, metadata=self.refusal_metadata)]
        return [ExecutionReport("accepted", request.client_order_id,
            broker_order_id="simulated-replacement", symbol=request.symbol, side="buy",
            intent_type="open", quantity=request.quantity, metadata=dict(request.metadata),
            origin="broker", reason="SIMULATOR acceptance, not a recorded venue answer")]

    async def read_atr_resting_buy_after_cancel(self, request):
        self.reads.append(request)
        return self.override or self.decode(request, self.body)


async def runtime(monkeypatch, broker, *, filled=False, quantity=None, slot="first", amod=False, notional=0,
                  strategy_overrides=None):
    factory = _session_factory()
    service, _ = _integrated_service(factory, enabled=True, nfq_enabled=bool(
        strategy_overrides and strategy_overrides.get("oms_v2_webull_mirror_fresh_price_enabled")))
    old, body, decode = recorded(broker, filled=filled, amod_1356=amod and broker == "schwab")
    if quantity is not None:
        old = replace(old, quantity=Decimal(quantity))  # Explicit controlled dollar-size race.
    clock = [datetime(2026, 10, 2, 17, 56, 2, 899000, tzinfo=UTC)]
    monkeypatch.setattr(oms, "utcnow", lambda: clock[0])
    monkeypatch.setattr(oms, "_is_regular_market_session", lambda now=None: True)
    monkeypatch.setattr(oms.OmsRiskService, "_market_is_fillable", lambda self, now=None: True)
    service.settings = service.settings.model_copy(update={
        "strategy_schwab_1m_v2_account_name": "live:schwab_1m_v2",
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_atr_reprice_handoff_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
        "strategy_schwab_1m_v2_flip_owned_first_entry_enabled": False,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_reprice_pct": 0.5,
        "strategy_schwab_1m_v2_cw_v2_reactive_entry_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct": 0.5,
        "oms_v2_exit_management_enabled": True,
        "oms_v2_exit_close_on_fill_enabled": True,
        "strategy_schwab_1m_v2_entry_notional_usd": notional if broker == "schwab" else 0,
        "strategy_schwab_1m_v2_webull_entry_notional_usd": notional if broker == "webull" else 0,
    })
    strategy_settings = service.settings.model_copy(update=strategy_overrides) if strategy_overrides else service.settings
    strategy = SchwabV2Strategy(strategy_settings)
    monkeypatch.setattr(strategy, "_now_ms", lambda: int(clock[0].timestamp() * 1000))
    for name in ("_resting_in_window", "_resting_session_is_eh", "_entry_window_closed_for_session"):
        method = getattr(strategy, name)
        monkeypatch.setattr(strategy, name, lambda now=None, fn=method: fn(now or clock[0]))
    state = strategy.watchlist_state(old.symbol)
    state.atr_state, state.atr_state_age, state.atr_trail = "short", 31, 3.05
    state.fanout_segment_id = state.atr_short_flip_bar_ts = int(clock[0].timestamp() * 1000) - 31 * 60000
    state.bars.append(OHLCVBar(strategy._now_ms() - 60000, 2.9, 3., 2.9, 2.95, 40151))
    state.last_quote = Quote(old.symbol, 2.94, 2.95, 2.95, strategy._now_ms())
    if amod:
        # Recorded probe/cancel, /var/log/project-mai-tai/schwab-1m-v2.log
        # lines 37261-37262; captured v3-all-retained-v2.ndjson on 2026-10-02.
        # Open and bid were NOT in the ATR probe: those are controlled replay inputs.
        state.atr_trail, state.atr_state_age = 3.220137, 49
        if "fanout_segment_id" in old.metadata:
            state.fanout_segment_id = state.atr_short_flip_bar_ts = int(old.metadata["fanout_segment_id"])
        state.bars.clear()
        state.bars.append(OHLCVBar(1790963700000, 3.135, 3.14, 3.13, 3.135, 23522))
        state.last_quote = Quote(old.symbol, 3.13, 3.14, 3.135, strategy._now_ms())
    if slot == "reclaim":
        state.atr_state, state.cw_armed, state.cw_bars_waited, state.cw_segment_high = "long", True, 3, 3.05
    strategy._queue_resting_place(state, 3.2441 if amod else 3.10, slot=slot)
    primary, = strategy.drain_pending_intents()
    mirror, = strategy.drain_webull_direct_intents()
    draft = primary if broker == "schwab" else mirror
    md = {**draft.metadata, **old.metadata}
    old = replace(old, metadata=md)
    # Controlled initial ledger, bound to the recorded old parent's exact identity.
    opening = TradeIntentEvent(source_service="schwab-1m-v2", produced_at=clock[0],
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=old.broker_account_name,
            symbol=old.symbol, side="buy", intent_type="open", quantity=old.quantity,
            reason=draft.reason, metadata=md))
    with factory() as session:
        st = service.store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        account = service.store.ensure_broker_account(session, old.broker_account_name,
                                                      provider="simulated", environment="test")
        intent = service.store.create_trade_intent(session, strategy=st, broker_account=account, event=opening)
        service.store.get_or_create_order(session, intent=intent, strategy_id=st.id,
            broker_account_id=account.id, client_order_id=old.client_order_id,
            broker_order_id=md["broker_order_id"], symbol=old.symbol, side="buy", quantity=old.quantity,
            metadata=md, status="accepted", order_type="STOP_LIMIT", time_in_force="day")
        session.commit()
    adapter = RecordedReadbackSimulator(old, body, decode)
    service.broker_adapter = adapter
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot.settings, bot.session_factory, bot.redis = strategy, service.settings, factory, service.redis
    return SimpleNamespace(service=service, strategy=strategy, state=state, clock=clock,
                           adapter=adapter, bot=bot, factory=factory, old=old, opening=opening)


async def begin(h, broker):
    if h.state.resting_slot == "reclaim":
        h.strategy._cw_v2_reclaim_resting_track(h.state)
    else:
        h.strategy._cw_v2_resting_track(h.state, None)
    primary, mirror = h.strategy.drain_pending_intents(), h.strategy.drain_webull_direct_intents()
    drafts = primary if broker == "schwab" else mirror
    cancel, = drafts
    event = TradeIntentEvent(source_service="schwab-1m-v2", produced_at=h.clock[0],
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=h.old.broker_account_name,
            symbol=cancel.symbol, side="buy", intent_type="cancel", quantity=cancel.quantity,
            reason=cancel.reason, metadata=cancel.metadata))
    await h.service._handle_stream_message({"data": event.model_dump_json()})
    return HandoffJournal(h.factory).jobs()[0][0], event


async def feedback(h):
    await h.bot._rpg_handoff_pass()
    rows, h.service.redis.entries = h.service.redis.entries, []
    for _, data in rows:
        if data.get("event_type") == "atr_reprice_tick":
            await h.service._handle_stream_message({"data": json.dumps(data)})


def tick_clock(h, seconds=1):
    h.clock[0] += timedelta(seconds=seconds)
    h.state.last_quote = replace(h.state.last_quote, quote_time_ms=h.strategy._now_ms())


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_recorded_cancel_clear_uses_v2_callback_and_serial_lane_without_next_bar(monkeypatch, broker):
    h = await runtime(monkeypatch, broker)
    h.strategy._cw_v2_resting_track(h.state, None)
    drafts = h.strategy.drain_pending_intents() if broker == "schwab" else h.strategy.drain_webull_direct_intents()
    cancel, = drafts
    assert cancel.metadata["atr_reprice"] == "true"
    event = TradeIntentEvent(source_service="schwab-1m-v2", produced_at=h.clock[0],
        payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=h.old.broker_account_name,
            symbol=cancel.symbol, side="buy", intent_type="cancel", quantity=cancel.quantity,
            reason=cancel.reason, metadata=cancel.metadata))
    await h.service._handle_stream_message({"data": event.model_dump_json()})
    assert len(h.adapter.cancels) == len(h.adapter.reads) == 1
    assert h.adapter.opens == []
    await h.bot._rpg_handoff_pass()
    ticks = [data for _, data in h.service.redis.entries if data.get("event_type") == "atr_reprice_tick"]
    assert ticks
    for tick in ticks * 2:
        await h.service._handle_stream_message({"data": json.dumps(tick)})
    assert len(h.adapter.opens) == 1
    assert h.adapter.opens[0].metadata["stop_price"] != h.opening.payload.metadata["stop_price"]
    with h.factory() as session:
        orders = list(session.scalars(select(BrokerOrder)))
        assert len(orders) == 2 and orders[0].status == "cancelled"
        assert session.get(TradeIntent, orders[1].intent_id).intent_type == "open"
    token = ticks[0]["token"]
    assert HandoffJournal(h.factory).read(UUID(token))["phase"] == "placed"


@pytest.mark.asyncio
@pytest.mark.parametrize("broker,quantity,filled", [("schwab", 197, 31), ("webull", 98, 17)])
async def test_controlled_partial_race_accounts_original_buy_and_protects_actual_position(monkeypatch, broker, quantity, filled):
    h = await runtime(monkeypatch, broker, quantity=quantity)
    h.adapter.override = AtrBuyReadback("fills", "CONTROLLED partial race", Decimal(filled), Decimal("3.05"), False)
    token, event = await begin(h, broker)
    journal = HandoffJournal(h.factory)
    assert journal.read(token)["no_rebuy"]
    assert journal.read(token)["phase"] == "fills_waiting"
    tick_clock(h)
    h.adapter.override = replace(h.adapter.override, terminal_cancel=True)
    await h.service._handle_stream_message({"data": event.model_dump_json()})
    await feedback(h)
    assert journal.read(token)["phase"] == "filled"
    with h.factory() as session:
        fills = list(session.scalars(select(Fill)))
        positions = list(session.scalars(select(OmsManagedPosition)))
        assert len(fills) == 1 and fills[0].quantity == filled
        assert len(positions) == 1 and positions[0].current_quantity == filled
        assert positions[0].status == "open"
        assert session.get(BrokerOrder, fills[0].order_id).status == "cancelled"
        assert session.get(TradeIntent, session.get(BrokerOrder, fills[0].order_id).intent_id).intent_type == "open"
    assert (h.old.broker_account_name, h.old.symbol) in h.service._managed_v2_symbols
    assert not h.adapter.opens and h.state.cw_resting_taken


@pytest.mark.asyncio
async def test_recorded_webull_full_fill_is_original_buy_not_replacement(monkeypatch):
    h = await runtime(monkeypatch, "webull", filled=True)
    token, _ = await begin(h, "webull")
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "filled"
    with h.factory() as session:
        fill = session.scalar(select(Fill))
        assert fill.quantity == 1 and fill.price == Decimal("3.41")
        assert session.scalar(select(OmsManagedPosition)).current_quantity == 1
    assert not h.adapter.opens


@pytest.mark.asyncio
@pytest.mark.parametrize("filled", [False, True])
async def test_webull_client_only_acceptance_binds_recorded_exact_readback(monkeypatch, filled):
    h = await runtime(monkeypatch, "webull", filled=filled)
    # Controlled local acceptance timing; recorded detail body is unchanged.
    with h.factory() as session:
        order = session.scalar(select(BrokerOrder))
        order.broker_order_id = None
        order.payload = {k: v for k, v in order.payload.items() if k != "broker_order_id"}
        session.commit()
    token, event = await begin(h, "webull")
    assert len(h.adapter.cancels) == len(h.adapter.reads) == 1
    assert h.adapter.cancels[0].client_order_id == h.old.client_order_id
    await feedback(h)
    await h.service._handle_stream_message({"data": event.model_dump_json()})
    await feedback(h)
    job = HandoffJournal(h.factory).read(token)
    assert job["old"]["metadata"]["broker_order_id"] == h.old.metadata["broker_order_id"]
    assert job["phase"] == ("filled" if filled else "placed")
    assert len(h.adapter.opens) == (0 if filled else 1)
    with h.factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == h.old.client_order_id))
        assert order.broker_order_id == h.old.metadata["broker_order_id"]
        assert order.status == ("filled" if filled else "cancelled")


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_unknown_is_spaced_bounded_and_restart_cannot_duplicate_cancel(monkeypatch, broker):
    h = await runtime(monkeypatch, broker)
    h.adapter.override = AtrBuyReadback("unknown", "CONTROLLED missing order")
    token, event = await begin(h, broker)
    for _ in range(4):
        await h.service._handle_stream_message({"data": event.model_dump_json()})
    assert len(h.adapter.reads) == len(h.adapter.cancels) == 1
    h.service.__dict__.pop("_atr_reprice_controller")  # Runtime restart over durable journal.
    for _ in range(31):
        tick_clock(h)
        await h.service._handle_stream_message({"data": event.model_dump_json()})
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "held_unknown" and job["reads"] == 30
    assert len(h.adapter.reads) == 30 and len(h.adapter.cancels) == 1 and not h.adapter.opens
    await feedback(h)
    assert h.strategy._rpg_entry_owned(h.state)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["buy_flip", "segment", "slot", "position", "liquidity", "1545", "1600", "stale_bar", "disabled"])
async def test_current_gate_ends_cleared_candidate_without_fallback(monkeypatch, gate):
    h = await runtime(monkeypatch, "schwab")
    token, _ = await begin(h, "schwab")
    if gate == "buy_flip":
        h.state.atr_state = "long"
    elif gate == "segment":
        h.state.fanout_segment_id += 60000
    elif gate == "slot":
        h.state.cw_resting_taken = True
    elif gate == "position":
        h.state.position_qty_held = 1
    elif gate == "liquidity":
        h.state.bars[-1] = replace(h.state.bars[-1], volume=0)
    elif gate in {"1545", "1600"}:
        h.service.settings.strategy_schwab_1m_v2_entry_window_end_hour_et = 15 if gate == "1545" else 16
        h.service.settings.strategy_schwab_1m_v2_entry_window_end_minute_et = 45 if gate == "1545" else 0
        h.clock[0] = h.clock[0].replace(hour=19 if gate == "1545" else 20, minute=45 if gate == "1545" else 0)
    elif gate == "stale_bar":
        tick_clock(h, 180)
    else:
        h.strategy._resting_entry_enabled = False
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "expired"
    assert not h.adapter.opens


@pytest.mark.asyncio
async def test_first_stale_quote_wait_resumes_without_bar_and_reclaim_armed_long_is_allowed(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    token, _ = await begin(h, "schwab")
    h.state.last_quote = replace(h.state.last_quote, quote_time_ms=h.strategy._now_ms() - 17000)
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "clear"
    tick_clock(h)
    await feedback(h)
    assert len(h.adapter.opens) == 1
    h = await runtime(monkeypatch, "schwab", slot="reclaim")
    token, _ = await begin(h, "schwab")
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "placed"
    assert h.adapter.opens[0].metadata["cw_entry_slot"] == "reclaim"


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_explicit_simulated_broker_refusal_releases_only_proven_clear_leg(monkeypatch, broker):
    h = await runtime(monkeypatch, broker)
    h.adapter.refusal = "stop must exceed ask"
    token, _ = await begin(h, broker)
    await feedback(h)
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "refused"
    assert not h.state.resting_active and len(h.adapter.opens) == 1
    h.strategy._cw_v2_resting_track(h.state, None)
    drafts = h.strategy.drain_pending_intents() if broker == "schwab" else h.strategy.drain_webull_direct_intents()
    assert len(drafts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["clear", "submitting", "submit_unknown"])
async def test_restart_clear_resumes_but_uncertain_submit_never_replays(monkeypatch, phase):
    h = await runtime(monkeypatch, "schwab")
    token, _ = await begin(h, "schwab")
    journal = HandoffJournal(h.factory)
    job = journal.read(token)
    journal.change(token, job["revision"], phase=phase)
    h.service.__dict__.pop("_atr_reprice_controller")
    await feedback(h)
    await h.service._rpg_advance(token)
    assert len(h.adapter.cancels) == 1
    assert len(h.adapter.opens) == (1 if phase == "clear" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_until_flip", [False, True])
async def test_amod_1356_recorded_state_replay_and_1357_flip_boundary(monkeypatch, blocked_until_flip):
    h = await runtime(monkeypatch, "schwab", amod=True)
    if blocked_until_flip:
        h.adapter.override = AtrBuyReadback("unknown", "CONTROLLED delayed cancellation")
    token, _ = await begin(h, "schwab")
    if blocked_until_flip:
        # Recorded next probe, same capture line 37313. Not a broker execution claim.
        h.clock[0] = datetime(2026, 10, 2, 17, 57, 2, 350000, tzinfo=UTC)
        h.state.atr_state, h.state.atr_state_age, h.state.atr_trail = "long", 0, 3.099460
        h.state.bars.append(OHLCVBar(1790963760000, 3.135, 3.3099, 3.1312, 3.29, 283979))
        h.adapter.override = None
        await h.service._rpg_advance(token)
    await feedback(h)
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == ("expired" if blocked_until_flip else "placed")
    if not blocked_until_flip:
        assert h.adapter.opens[0].metadata["stop_price"] == "3.2362"
        assert h.clock[0].minute == 56  # Replacement without consuming the 13:57 bar.
    else:
        assert not h.adapter.opens


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [1.001, 6537.101])
async def test_final_wire_guard_rechecks_authorization_age_and_1545(monkeypatch, age):
    h = await runtime(monkeypatch, "schwab")
    h.service.settings.strategy_schwab_1m_v2_entry_window_end_hour_et = 15
    h.service.settings.strategy_schwab_1m_v2_entry_window_end_minute_et = 45
    token, _ = await begin(h, "schwab")
    original = h.service._finalize_v2_entry_quantity

    def delayed(event, intent):
        result = original(event, intent)
        h.clock[0] += timedelta(seconds=age)  # Controlled await/processing delay before final wire.
        return result

    monkeypatch.setattr(h.service, "_finalize_v2_entry_quantity", delayed)
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "refused"
    assert not h.adapter.opens


@pytest.mark.asyncio
async def test_callback_gate_revocation_wins_even_within_authorization_second(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    token, _ = await begin(h, "schwab")
    await h.bot._rpg_handoff_pass()  # Queues a ready authorization.
    h.state.atr_state = "long"
    await feedback(h)  # A fresh decision revokes it before queued delivery.
    assert not h.adapter.opens
    assert HandoffJournal(h.factory).read(token)["phase"] == "expired"


@pytest.mark.asyncio
async def test_wire_sees_durable_claim_and_restart_after_success_does_not_place_twice(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    token, event = await begin(h, "schwab")
    submit = h.adapter.submit_order

    async def inspect_claim(request):
        if request.side == "buy" and request.intent_type == "open":
            job = HandoffJournal(h.factory).read(token)
            assert job["phase"] == "submitting"
            assert job["replacement"]["client_order_id"] == request.client_order_id
        return await submit(request)

    monkeypatch.setattr(h.adapter, "submit_order", inspect_claim)
    await feedback(h)
    h.service.__dict__.pop("_atr_reprice_controller")
    for _ in range(3):
        await h.service._handle_stream_message({"data": event.model_dump_json()})
        await feedback(h)
    assert len(h.adapter.opens) == 1
    assert h.state.resting_active


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy", ["schwab", "webull"])
async def test_one_unproven_leg_does_not_block_other_legs_confirmed_replacement(monkeypatch, healthy):
    h = await runtime(monkeypatch, healthy)
    # Only the healthy parent is seeded: the other is explicitly unproven,
    # not invented zero-fill venue evidence. Both cancels are actual v2 emissions.
    h.strategy._cw_v2_resting_track(h.state, None)
    primary, = h.strategy.drain_pending_intents()
    mirror, = h.strategy.drain_webull_direct_intents()
    for account, draft in [("live:orb", mirror), ("live:schwab_1m_v2", primary)]:
        event = TradeIntentEvent(source_service="schwab-1m-v2", produced_at=h.clock[0],
            payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=account,
                symbol=draft.symbol, side="buy", intent_type="cancel", quantity=draft.quantity,
                reason=draft.reason, metadata=draft.metadata))
        await h.service._handle_stream_message({"data": event.model_dump_json()})
    await feedback(h)
    phases = {job["old"]["broker_account_name"]: job["phase"] for _, job in HandoffJournal(h.factory).jobs()}
    other = "live:orb" if healthy == "schwab" else "live:schwab_1m_v2"
    assert phases == {other: "held_unknown", h.old.broker_account_name: "placed"}
    assert len(h.adapter.opens) == 1 and h.adapter.opens[0].broker_account_name == h.old.broker_account_name
    await feedback(h)
    h.state.atr_trail = 2.99
    h.strategy._cw_v2_resting_track(h.state, None)
    primary = h.strategy.drain_pending_intents()
    mirror = h.strategy.drain_webull_direct_intents()
    cancel, = primary if healthy == "schwab" else mirror
    assert cancel.metadata["atr_reprice"] == "true"  # Healthy leg still managed.
    assert not (mirror if healthy == "schwab" else primary)


@pytest.mark.asyncio
async def test_current_schwab_ineligible_cache_still_refuses_serial_replacement(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    h.service.settings = h.service.settings.model_copy(update={"strategy_schwab_1m_v2_broker_provider": "schwab"})
    token, _ = await begin(h, "schwab")
    with h.factory() as session:
        order = session.scalar(select(BrokerOrder))
        h.service.store.record_schwab_ineligible_entry(session,
            broker_account_id=order.broker_account_id, symbol=h.state.symbol,
            session_date=h.service._current_session_day(), first_seen_at=h.clock[0],
            reason_text="CONTROLLED cached ineligibility, not a recorded refusal")
        session.commit()
    await feedback(h)
    assert not h.adapter.opens
    assert HandoffJournal(h.factory).read(token)["phase"] == "refused"
    assert any(data.get("payload", {}).get("reason") == "schwab_ineligible_cached"
               for _, data in h.service.redis.entries)


@pytest.mark.asyncio
async def test_virtual_only_position_cannot_orphan_replacement_and_latest_trail_wins(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    h.strategy.update_position(h.state.symbol, 2, held_qty=0)
    token, _ = await begin(h, "schwab")
    h.state.atr_trail = 3.00
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "placed"
    assert h.adapter.opens[0].metadata["stop_price"] == "3.0150"


@pytest.mark.asyncio
@pytest.mark.parametrize("broker,notional", [("schwab", 600), ("webull", 300)])
async def test_replacement_uses_current_dollar_size_and_preserves_economic_slot(monkeypatch, broker, notional):
    h = await runtime(monkeypatch, broker, notional=notional)
    token, _ = await begin(h, broker)
    await feedback(h)
    request, = h.adapter.opens
    assert request.quantity > 1
    assert request.quantity * Decimal(request.metadata["limit_price"]) <= notional + Decimal("1")
    for key in ("fanout_segment_id", "fanout_slot_id", "cw_entry_slot", "cw_entry_n"):
        assert request.metadata[key] == h.opening.payload.metadata[key]
    assert HandoffJournal(h.factory).read(token)["phase"] == "placed"


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["restore", "retry_unknown", "retry_exhausted", "owner_bound", "reclaim_consumed"])
async def test_real_current_owner_retry_and_slot_cap_gates_revalidated(monkeypatch, gate):
    h = await runtime(monkeypatch, "schwab", slot="reclaim" if gate == "reclaim_consumed" else "first")
    token, _ = await begin(h, "schwab")
    if gate == "reclaim_consumed":
        h.state.cw_reclaim_taken = True
    else:
        h.strategy._flip_owned_first_entry_enabled = True
        h.strategy._flip_owner_restore_readable = True
        h.strategy._retry_one_enabled = True
        h.state.retry_one_budget_readable = True
        h.state.retry_one_segment_id = h.state.atr_short_flip_bar_ts
        h.state.flip_owner_evidence_readable = True
        h.state.flip_owner_evidence_at_ms = h.strategy._now_ms()
        if gate == "restore":
            h.strategy._flip_owner_restore_readable = False
        elif gate == "retry_unknown":
            h.state.retry_one_budget_readable = False
        elif gate == "retry_exhausted":
            h.state.retry_one_closes_in_segment = 1 + h.strategy._retry_one_max_retries
        else:
            h.state.flip_owner_phase = "bound"
    await feedback(h)
    assert not h.adapter.opens
    assert HandoffJournal(h.factory).read(token)["phase"] in {"clear", "expired"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["filled", "cancelled"])
async def test_v2_restart_uses_committed_replacement_status_not_stale_placed_latch(monkeypatch, status):
    h = await runtime(monkeypatch, "schwab")
    token, _ = await begin(h, "schwab")
    await feedback(h)
    request, = h.adapter.opens
    with h.factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
        order.status = status  # Controlled committed broker-sync outcome; no venue claim.
        session.commit()
    h.strategy._rpg_handoffs.clear()
    h.strategy._rpg_feedback_applied.clear()  # Restart's empty feedback cache.
    await feedback(h)
    assert not h.state.resting_active
    assert h.state.cw_resting_taken is (status == "filled")
    assert HandoffJournal(h.factory).read(token)["phase"] == ("filled" if status == "filled" else "refused")
    assert len(h.adapter.opens) == 1


@pytest.mark.asyncio
async def test_actual_runtime_read_timeout_retains_exact_old_buy(monkeypatch):
    h = await runtime(monkeypatch, "schwab")

    async def slow(request):
        h.adapter.reads.append(request)
        await asyncio.Future()  # Controlled unresponsive transport, not a venue answer.

    monkeypatch.setattr(h.adapter, "read_atr_resting_buy_after_cancel", slow)
    token, _ = await begin(h, "schwab")
    job = HandoffJournal(h.factory).read(token)
    assert job["phase"] == "waiting" and job["reason"] == "readback_raised:TimeoutError"
    assert job["reads"] == 1 and not h.adapter.opens


REFUSAL_TEXTS = json.loads((Path(__file__).parents[1] / "fixtures/rpg1_recorded_refusal_texts.json").read_text())["records"]


@pytest.mark.asyncio
@pytest.mark.parametrize("record", REFUSAL_TEXTS, ids=lambda row: row.get("symbol", "schwab-excerpt"))
async def test_recorded_refusal_text_projection_ends_or_bounds_price_retry(monkeypatch, record):
    h = await runtime(monkeypatch, record["broker"])
    h.adapter.refusal = record["reason"]
    price = "PRICE_AGGRESSIVE" in record["reason"]
    if price:
        # The retained DB row has text only; the adapter's structured code is a
        # controlled projection, not passed off as a newly recorded response.
        h.adapter.refusal_metadata = {"webull_error_code": "ORDER_RISK_RULE_PRICE_AGGRESSIVE"}
    token, _ = await begin(h, record["broker"])
    journal = HandoffJournal(h.factory)
    await feedback(h)
    assert journal.read(token)["phase"] == ("price_wait" if price else "refused")
    for _ in range(5):
        tick_clock(h)
        await feedback(h)
    assert journal.read(token)["phase"] == "refused"
    assert len(h.adapter.opens) <= 1 + h.service._WEBULL_MIRROR_RESUBMIT_MAX_ATTEMPTS
    assert not h.state.resting_active


@pytest.mark.asyncio
async def test_background_sync_cannot_bypass_owned_strict_read_budget_or_fill_decoder(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    h.adapter.override = AtrBuyReadback("unknown", "CONTROLLED pending cancel")
    token, _ = await begin(h, "schwab")
    generic = []

    async def forbidden(request):
        generic.append(request)
        return None

    monkeypatch.setattr(h.adapter, "fetch_order_update", forbidden, raising=False)
    await h.service.sync_broker_orders(account_names=[h.old.broker_account_name])
    assert generic == []
    assert HandoffJournal(h.factory).read(token)["reads"] == 1
    assert not h.adapter.opens


@pytest.mark.asyncio
async def test_inflight_generic_read_yields_to_new_strict_handoff(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    h.adapter.override = AtrBuyReadback("unknown", "CONTROLLED pending cancel")

    async def raced_read(request):
        await begin(h, "schwab")
        # Explicit fault injection: shared cancellation decoding must not account
        # the recorded cancellation activity's quantity as an actual fill.
        return ExecutionReport("filled", request.client_order_id,
            broker_order_id=h.old.metadata["broker_order_id"], symbol=request.symbol,
            side="buy", intent_type="open", quantity=request.quantity,
            filled_quantity=request.quantity, fill_price=Decimal("3.05"), origin="broker")

    monkeypatch.setattr(h.adapter, "fetch_order_update", raced_read, raising=False)
    await h.service.sync_broker_orders(account_names=[h.old.broker_account_name])
    with h.factory() as session:
        assert session.scalar(select(Fill)) is None
        assert session.scalar(select(BrokerOrder)).status == "accepted"
    assert HandoffJournal(h.factory).jobs()[0][1]["reads"] == 1
    assert not h.adapter.opens


@pytest.mark.asyncio
async def test_v2_restart_waits_for_current_watch_state_before_reauthorizing(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    token, _ = await begin(h, "schwab")
    h.strategy._symbol_states.pop(h.state.symbol)
    await feedback(h)
    assert HandoffJournal(h.factory).read(token)["phase"] == "clear"
    assert not h.adapter.opens
    h.strategy._symbol_states[h.state.symbol] = h.state
    await feedback(h)
    assert len(h.adapter.opens) == 1


@pytest.mark.asyncio
async def test_retained_session_object_cannot_hide_current_authorization_revocation(monkeypatch):
    h = await runtime(monkeypatch, "schwab")
    token, _ = await begin(h, "schwab")
    journal = HandoffJournal(h.factory)
    with h.factory() as session:
        retained = session.get(DashboardSnapshot, token)
        prior = journal.read(token, session=session)
        journal.change(token, prior["revision"], authorization={"verdict": "expired", "reason": "CONTROLLED revocation"})
        assert journal.read(token, session=session)["authorization"]["verdict"] == "expired"
        assert retained.payload["revision"] > prior["revision"]
