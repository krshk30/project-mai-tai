"""Real F emitter/poll controls; legacy gaps remain UNKNOWN, never fake book proof."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, update

from project_mai_tai.broker_adapters import cancel_terminal as broker
from project_mai_tai.db.models import BrokerAccount, TradeIntent
from project_mai_tai.events import TradeIntentEvent
from project_mai_tai.fanout_segment_store import current_session_anchor
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2IntentEmitter, TradeIntentDraft
from tests.integration.test_cancel_terminal_runtime import sdk as controlled_sdk
from tests.unit.test_clearwait1_session_rollover import PRIMARY, RAW, WEBULL, ms, recorded_request, seed, service, strategy
from tests.unit.test_clearwait1_session_rollover import db as rollover_db
from tests.unit.test_clearwait1_unbound import CONFIGURED_IDS, NOW, controlled_routing

db, sdk = rollover_db, controlled_sdk


def runtime(database, monkeypatch, *, req=None, side=None, venue=None):
    req = req or recorded_request("DKI")
    strat = strategy(req)
    strat._now_ms = lambda: ms(NOW)
    strat._removed_wait_persist = database[0].record
    bot = service(strat, database[0])
    bot._removed_wait_roll_anchor = current_session_anchor(NOW)
    bot._removed_wait_adapter = controlled_routing()
    calls = []
    monkeypatch.setattr(broker, "now_ms", lambda: strat._now_ms())
    with database[1]() as session:
        session.execute(update(BrokerAccount).values(external_account_id=None))
        session.commit()
    schwab = bot._removed_wait_adapter._adapter_for_account(PRIMARY)
    async def no_schwab_book(*args):
        pytest.fail("F must not acquire a Schwab book")
    schwab._authorized_request_json = no_schwab_book
    webull = bot._removed_wait_adapter._adapter_for_account(WEBULL)
    def response(request):
        assert request.values == {"account_id": CONFIGURED_IDS[WEBULL], "page_size": 100}
        calls.append((WEBULL, request.values))
        orders = [] if venue != WEBULL else [{"client_order_id": "controlled-foreign",
            "order_id": "controlled-broker", "symbol": req.symbol, "status": "working", "side": side}]
        return SimpleNamespace(status_code=200, body={"hasNext": False, "orders": orders})
    webull._get_client = lambda: SimpleNamespace(_auto_retry=False, get_response=response)
    webull._body = lambda r: r.body
    webull._query_budget = SimpleNamespace(claim=lambda *_a, **_kw: None)
    return bot, strat, req, calls


class RedisReceipt:
    """Actual emitted envelope; explicitly controlled terminal OMS persistence."""
    def __init__(self, database, now):
        self.database, self.now = database, now
        self.events, self.pause = [], False
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def xadd(self, _stream, fields, **_kw):
        envelope = TradeIntentEvent.model_validate_json(fields["data"])
        self.events.append(envelope)
        if self.pause:
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), 2)
        self.persist(envelope)
        return "controlled-redis-id"

    def persist(self, envelope):
        _, sessions, ids, strategy_id = self.database
        p = envelope.payload
        with sessions() as session:
            session.add(TradeIntent(strategy_id=strategy_id, broker_account_id=ids[p.broker_account_name],
                symbol=p.symbol, side=p.side, intent_type=p.intent_type, quantity=p.quantity,
                reason=p.reason, status="rejected", created_at=self.now(), updated_at=self.now(),
                payload={"event_id": str(envelope.event_id), "source_service": envelope.source_service,
                    "metadata": p.metadata, "refusal_origin": "skipped_before_submit",
                    "refusal_code": "cancel_target_not_found"}))
            session.commit()


def emitters(bot, database):
    redis = RedisReceipt(database, lambda: datetime.fromtimestamp(bot.strategy._now_ms() / 1000, UTC))
    bot.intent_emitter = SchwabV2IntentEmitter(bot.settings, redis, broker_account_name=PRIMARY)
    bot.webull_intent_emitter = SchwabV2IntentEmitter(bot.settings, redis, broker_account_name=WEBULL)
    return redis


async def barriers(bot, strat, req):
    strat._queue_removed_wait_barriers(strat.watchlist_state(req.symbol), req)
    await asyncio.wait_for(bot._drain_direct_strategy_intents(), 2)


async def poll(bot):
    await asyncio.wait_for(bot._removed_wait_poll(), 2)
    task = getattr(bot, "_removed_wait_book_task", None)
    if task is not None:
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_untagged_cancel_real_drain_published_before_db_keeps(db, sdk, monkeypatch):
    bot, strat, req, calls = runtime(db, monkeypatch)
    seed(db, req, receipts=False)
    redis = emitters(bot, db)
    await barriers(bot, strat, req)
    redis.pause = True
    generic = TradeIntentDraft("DKI", "buy", "cancel", Decimal(100), "untagged protection", {})
    strat._pending_intents.append(generic)
    task = asyncio.create_task(bot._drain_direct_strategy_intents())
    try:
        await asyncio.wait_for(redis.entered.wait(), 2)
        await bot._removed_wait_poll()
        assert not calls and db[0].restore() == {"DKI": req}
        assert not await bot._emit_removal_tracked(bot.intent_emitter, replace(generic, intent_type="open"))
    finally:
        redis.release.set()
        await asyncio.wait_for(task, 2)
    await poll(bot)
    assert calls == [] and db[0].restore() == {"DKI": req} and len(redis.events) == 3


@pytest.mark.asyncio
async def test_missing_db_event_after_emit_returns_is_not_absence(db, sdk, monkeypatch):
    bot, strat, req, calls = runtime(db, monkeypatch)
    seed(db, req, receipts=False)
    redis = emitters(bot, db)
    await barriers(bot, strat, req)
    persist = redis.persist
    redis.persist = lambda _event: None
    draft = TradeIntentDraft("DKI", "buy", "cancel", Decimal(100), "untagged protection", {})
    await bot._emit_removal_tracked(bot.intent_emitter, draft)
    missing = redis.events[-1]
    redis.persist = persist
    await bot._emit_removal_tracked(bot.intent_emitter, draft)
    assert not db[0].request_publication_closed(req,
        last_publications={PRIMARY: tuple(bot._clearwait_last_publications["DKI", PRIMARY])}, now=NOW)
    await poll(bot)
    assert not calls and db[0].restore() == {"DKI": req}
    persist(missing)
    await poll(bot)
    assert not calls and db[0].restore() == {"DKI": req}


@pytest.mark.asyncio
async def test_recorded_dki_boot_zero_one_two_receipts_all_legacy_unknown(db, sdk, monkeypatch):
    bot, strat, req, calls = runtime(db, monkeypatch)
    seed(db, req, receipts=False)
    retained = [r for r in RAW["intents"] if r["symbol"] == "DKI"
        and r["payload"].get("metadata", {}).get("clearwait_removal_token") == req.token]
    assert len(retained) == 2
    for index in range(3):
        if index:
            raw = retained[index - 1]
            with db[1]() as session:
                session.add(TradeIntent(strategy_id=db[3], broker_account_id=db[2][raw["account"]],
                    symbol="DKI", side=raw["side"], intent_type=raw["intent_type"], quantity=100,
                    reason="recorded DKI", status=raw["status"], payload=raw["payload"],
                    created_at=datetime.fromisoformat(raw["created_at"]), updated_at=datetime.fromisoformat(raw["updated_at"])))
                session.commit()
        strat._pending_intents.clear()
        strat._pending_webull_direct_intents.clear()
        await bot._configure_removed_wait_store()
        assert db[0].restore() == {"DKI": req} and not calls


@pytest.mark.asyncio
async def test_stalled_offloop_proof_quote_memory_only_unrelated_cancel_immediate(db, sdk, monkeypatch):
    from project_mai_tai.services.schwab_1m_v2_bot import Quote
    bot, strat, req, calls = runtime(db, monkeypatch)
    seed(db, req, receipts=False)
    redis = emitters(bot, db)
    await barriers(bot, strat, req)
    entered, release = threading.Event(), threading.Event()
    original = db[0].retire_unbound
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(db[0], "retire_unbound", paused)
    bot._last_tick_at, bot._last_quote_at_ms, bot._last_quote_by_symbol = {}, {}, {}
    bot._gap_hold_enabled = bot._line_restoration_enabled = False
    bot._observe_line_trade = bot._observe_halt_from_quote = lambda *_: None
    bot._emit_webull_fanout_legs = bot._maybe_emit = AsyncMock()
    task = asyncio.create_task(poll(bot))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 2.5)
        quote_thread = threading.get_ident()
        def no_quote_sql(*_args):
            if threading.get_ident() == quote_thread:
                pytest.fail("quote performed proof SQL")
        engine = db[1].kw["bind"]
        event.listen(engine, "before_cursor_execute", no_quote_sql)
        try:
            await asyncio.wait_for(bot._handle_quote("DKI", Quote("DKI", 4.78, 4.79, 4.7871, ms(NOW))), .25)
        finally:
            event.remove(engine, "before_cursor_execute", no_quote_sql)
        draft = TradeIntentDraft("DKI", "buy", "open", Decimal(100), "delayed eligibility", {})
        bot._line_draft_allowed = lambda *_: True
        assert not await bot._emit_removal_tracked(bot.intent_emitter, draft)
        assert await asyncio.wait_for(bot._emit_removal_tracked(bot.intent_emitter,
            replace(draft, symbol="AIXI", intent_type="cancel")), .25)
        assert len(redis.events) == 3 and not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", [PRIMARY, WEBULL])
async def test_active_rest_cancel_and_state_disarm_not_delayed_by_proof(db, sdk, monkeypatch, venue):
    bot, strat, _, calls = runtime(db, monkeypatch)
    strat._removed_wait_requests.clear()
    state = strat.watchlist_state("DKI")
    state.cw_armed = True
    if venue == PRIMARY:
        state.resting_active = state.resting_is_broker_order = True
    else:
        state.webull_resting_active = True
    strat.release_and_drop_symbol("DKI")
    assert not state.cw_armed and not state.resting_active and not state.webull_resting_active
    assert strat._pending_intents and strat._pending_webull_direct_intents
    redis = emitters(bot, db)
    await asyncio.wait_for(bot._drain_direct_strategy_intents(), .25)
    assert redis.events and not calls


@pytest.mark.asyncio
async def test_fresh_raise_has_no_broker_get_before_cancel_feedback(db, sdk, monkeypatch):
    bot, strat, _, calls = runtime(db, monkeypatch)
    strat._removed_wait_requests.clear()
    state = strat.watchlist_state("DKI")
    state.cw_armed = True
    assert state.resting_is_broker_order and not state.resting_active
    strat.release_and_drop_symbol("DKI")
    req = strat._removed_wait_requests["DKI"]
    await poll(bot)
    assert not calls and db[0].restore() == {"DKI": req}
    assert strat._pending_intents and strat._pending_webull_direct_intents
    redis = emitters(bot, db)
    await asyncio.wait_for(bot._drain_direct_strategy_intents(), 2)
    assert len(redis.events) == 2
    await poll(bot)
    assert not calls and db[0].restore() == {"DKI": req}


@pytest.mark.asyncio
async def test_completed_known_row_sync_is_not_crash_orphan_discovery(db):
    from project_mai_tai.oms.service import OmsRiskService
    from project_mai_tai.settings import Settings
    adapter = SimpleNamespace(fetch_order_update=AsyncMock(), list_open_orders=AsyncMock(
        return_value=[{"symbol": "DKI", "status": "working"}]))
    oms = OmsRiskService(Settings(_env_file=None, oms_adapter="simulated"), session_factory=db[1],
        redis_client=SimpleNamespace(xadd=AsyncMock()), broker_adapter=adapter)
    result = await asyncio.wait_for(oms.sync_broker_orders(account_names=[PRIMARY, WEBULL]), 2)
    assert result == {"orders": 0, "terminal_orders": 0}
    adapter.fetch_order_update.assert_not_awaited()
    adapter.list_open_orders.assert_not_awaited()
