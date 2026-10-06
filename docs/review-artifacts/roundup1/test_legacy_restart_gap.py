"""Controlled no-ticket startup replay, also exercised by discovery unit tests.

Retained SCKT/PFSA prices are replayed as controlled accepted, unfilled orders.
That setup is not a claim about their historical status at an actual restart.
Handoff metadata is removed; persisted attempt/resting generation is retained.
The segment is taken from the record, not reconstructed from the replay clock.
The recorded Webull event had unknown origin: this controlled fixture explicitly
supplies broker origin, exact IDs and matching slot metadata, not historical proof.
The real bot run/startup/first-position-pass execute against a local test book;
Only network/heartbeat publication and unrelated background loops are stubbed.
"""
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from project_mai_tai.db.models import Base, BrokerOrderEvent, DashboardSnapshot
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.atr_reprice_handoff import SNAPSHOT_TYPE
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.strategy_core import schwab_1m_v2 as strategy_module
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar

sys.path.insert(0, str(Path(__file__).parents[3] / "tests/unit"))
from tests.unit.test_roundup1 import FLAG, ROWS, SCKT, strategy_for


class Transport:
    def __init__(self, *args, **kwargs):
        self.connected = False
        self.messages = []

    async def run(self):
        await asyncio.Event().wait()

    async def stop(self):
        pass

    async def aclose(self):
        pass

    async def xadd(self, stream, fields, **kwargs):
        self.messages.append((stream, fields))
        return "1-0"

    def max_consecutive_empty(self):
        return 0


@pytest.fixture
def local_book():
    # A shared memory DB with independent reader connections, never StaticPool.
    engine = create_engine(
        f"sqlite+pysqlite:///file:roundup-restart-{uuid4()}?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False}, poolclass=NullPool,
    )
    keeper = engine.connect()
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        keeper.close()
        engine.dispose()


async def restart(monkeypatch, factory, leg, slot, *, accepted_proof=True, status="accepted",
                  enabled=True, mutate_book=None, startup_observer=None, state_hook=None):
    row = SCKT if leg == "webull" else next(
        r for r in ROWS if r["symbol"] == "PFSA" and r["payload"].get("entry_price") == "3.7736"
    )
    clock = datetime.fromisoformat(row["submitted_at"]).astimezone(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.astimezone(tz) if tz else clock.replace(tzinfo=None)

    monkeypatch.setattr(bot_module, "datetime", Clock)
    monkeypatch.setattr(strategy_module, "datetime", Clock)
    transport = Transport()
    monkeypatch.setattr(bot_module, "Redis", SimpleNamespace(from_url=lambda *args, **kwargs: transport))
    class EntryTransport(Transport):
        async def run(self):
            if startup_observer:
                startup_observer(bot, state)
            await super().run()

    monkeypatch.setattr(bot_module, "SchwabV2RestClient", EntryTransport)
    monkeypatch.setattr(bot_module, "SchwabV2Streamer", Transport)
    template, _ = strategy_for(row)
    settings = template.settings.model_copy(update={
        FLAG: enabled,
        "strategy_schwab_1m_v2_enabled": True,
        "strategy_schwab_1m_v2_tick_capture_enabled": False,
        "strategy_schwab_1m_v2_streamer_enabled": False,
        "market_data_subscription_startup_enabled": True,
        "oms_v2_exit_management_enabled": True,
    })
    md = {key: value for key, value in deepcopy(row["payload"]).items()
          if not key.startswith("rpg_") or key == "rpg_resting_generation"}
    md["cw_entry_slot"] = slot  # Controlled first/reclaim permutation of retained prices.
    assert not md.get("rpg_handoff_token")
    store = OmsStore()
    with factory() as session:
        strategy = store.ensure_strategy(session, "schwab_1m_v2", name="v2")
        account = store.ensure_broker_account(session, row["account"], provider=leg, environment="test")
        other_account = settings.strategy_schwab_1m_v2_webull_account_name if leg == "schwab" else settings.strategy_schwab_1m_v2_account_name
        store.ensure_broker_account(session, other_account,
            provider="webull" if leg == "schwab" else "schwab", environment="test")
        event = TradeIntentEvent(source_service="local-legacy-restart-fixture", produced_at=clock,
            payload=TradeIntentPayload(strategy_code="schwab_1m_v2", broker_account_name=row["account"],
                symbol=row["symbol"], side="buy", intent_type="open", quantity=Decimal(row["quantity"]),
                reason="ATR Flip", metadata=md))
        intent = store.create_trade_intent(session, strategy=strategy, broker_account=account, event=event)
        intent.status = "submitted"
        order = store.get_or_create_order(session, intent=intent, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id=row["client_order_id"],
            broker_order_id=row["broker_order_id"], symbol=row["symbol"], side="buy",
            quantity=Decimal(row["quantity"]), metadata=md, status=status,
            order_type="STOP_LIMIT", time_in_force="day")
        if leg == "webull" and accepted_proof:
            capture = json.loads((Path(__file__).parents[3] / "tests/fixtures/roundup1/orders_179.json").read_text())
            accepted = next(e for e in capture["queries"]["sckt_events"] if e["event_type"] == "accepted")
            payload = deepcopy(accepted["payload"])
            payload["client_order_id"] = row["client_order_id"]
            payload["broker_order_id"] = row["broker_order_id"]
            payload["metadata"] = {**{key: value for key, value in payload["metadata"].items()
                                      if not key.startswith("rpg_")}, **md}
            session.add(BrokerOrderEvent(order_id=order.id, event_type="accepted", event_source="broker",
                event_at=clock, payload=payload))
        elif not accepted_proof:
            order.payload = {key: value for key, value in md.items() if key not in {"stop_price", "limit_price"}}
        if mutate_book:
            mutate_book(session, order, intent, md, row, clock)
        session.commit()
    bot = bot_module.SchwabV2BotService(settings, session_factory=factory)
    state = bot.strategy.watchlist_state(row["symbol"])
    bot._watchlist = {row["symbol"]}
    state.fanout_segment_id = int(md["fanout_segment_id"])
    state.atr_short_flip_bar_ts = int(row["payload"].get("rpg_short_segment") or md["fanout_segment_id"])
    state.atr_trail = float(md["cw_flip_level"])
    state.atr_state, state.atr_state_age = "short", 31
    state.bars.append(OHLCVBar(int(clock.timestamp() * 1000) - 60_000, 1, 1, 1, 1, 100_000))
    if slot == "reclaim":
        state.atr_state, state.cw_armed, state.cw_bars_waited, state.cw_segment_high = "long", True, 3, state.atr_trail
    if state_hook:
        state_hook(state)
    completed = []
    errors = []

    async def idle():
        await bot._stop_event.wait()

    async def heartbeat(*args):
        pass

    async def position_once():
        try:
            await bot._position_poll_pass()
            completed.append(True)
        except Exception as exc:
            errors.append(exc)
        finally:
            bot._stop_event.set()

    for name in ("_heartbeat_loop", "_state_publish_loop", "_scanner_consumer_loop",
                 "_task_liveness_loop", "_fanout_outcome_loop", "_rpg_handoff_loop"):
        monkeypatch.setattr(bot, name, idle)
    monkeypatch.setattr(bot, "_position_poll_loop", position_once)
    monkeypatch.setattr(bot, "_publish_heartbeat", heartbeat)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *args: None)
    await asyncio.wait_for(bot.run(), 5)
    assert not errors, errors
    assert completed == [True]
    with factory() as session:
        assert not session.scalars(select(DashboardSnapshot).where(
            DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE)).all()
    assert not bot.strategy._rpg_handoffs
    assert getattr(bot.settings, FLAG) is enabled
    return bot, state, row


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_no_ticket_startup_preserves_accepted_old_wire(monkeypatch, local_book, leg, slot):
    bot, state, row = await restart(monkeypatch, local_book, leg, slot)
    expected = (1.06, 1.07) if leg == "webull" else (3.77, 3.79)
    actual = (bot.strategy._active_resting_trigger(state, leg=leg),
              bot.strategy._active_resting_cap(state, leg=leg))
    assert actual == expected, ("legacy wire was not restored", leg, slot, actual, expected)
    assert state.resting_active and state.resting_is_broker_order
    placed = state.resting_webull_quantity if leg == "webull" else state.resting_schwab_quantity
    assert placed == int(Decimal(row["quantity"]))
    assert not bot.strategy.drain_pending_intents()
    assert not bot.strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["schwab", "webull"])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_no_ticket_unproven_wire_blocks_duplicate_rest(monkeypatch, local_book, leg, slot):
    bot, state, _ = await restart(monkeypatch, local_book, leg, slot, accepted_proof=False, status="submitted")
    if slot == "first":
        bot.strategy._cw_v2_resting_track(state, None)
    else:
        bot.strategy._cw_v2_reclaim_resting_track(state)
    primary = bot.strategy.drain_pending_intents()
    mirror = bot.strategy.drain_webull_direct_intents()
    assert not (primary if leg == "schwab" else mirror), "unproven owned legacy leg allowed a duplicate draft"
    assert len(mirror if leg == "schwab" else primary) == 1, "proved-clear independent leg was blocked"
