"""B2 original FLYE bars/cancels; broker freshness and acceptance are replay controls."""
from __future__ import annotations

import json
import inspect
import textwrap
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import get_ident
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, Strategy, TradeIntent,
)
from project_mai_tai.events import TradeIntentEvent
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, Quote
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.positions_read_receipt import record_receipts
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar, SchwabV2Config, SchwabV2IntentEmitter, SchwabV2Strategy,
)
from project_mai_tai.v2_flip_entry_ownership import (
    FlipEntryOwnershipRecord, FlipPositionBook, FlipPositionLeg,
)
from project_mai_tai.v2_removed_wait import RemovedWait

DATA = json.loads((Path(__file__).parents[1] / "fixtures/fresh_sell_flye_20261008.json").read_text())
FLYE = DATA["owner"]
PRIMARY, MIRROR = "live:schwab_1m_v2", "live:orb"


def instant(ms):
    return datetime.fromtimestamp(ms / 1000, UTC)


def setup(monkeypatch, *, pm=False, purpose="retry_exhausted"):
    strategy = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_atr_flip_enabled=True,
        strategy_schwab_1m_v2_atr_only_mode=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=pm,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_retry_one_enabled=True,
        strategy_schwab_1m_v2_retry_one_max_retries=0,
        strategy_schwab_1m_v2_keep_rest_after_buy_enabled=True,
        strategy_schwab_1m_v2_slotclear_fresh_flip_enabled=True,
        strategy_schwab_1m_v2_slotclear_fresh_sell_enabled=True,
        strategy_schwab_1m_v2_account_name=PRIMARY,
        strategy_schwab_1m_v2_webull_account_name=MIRROR,
        strategy_schwab_1m_v2_atr_reprice_handoff_enabled=False,
    ), config=SchwabV2Config(macd_warmup_settling_bars=0))
    clock = [FLYE["fresh_sell_poll_ms"]]
    strategy._now_ms = lambda: clock[0]
    strategy._boot_ms = FLYE["retry_segment_id"] - 60_000
    record = FlipEntryOwnershipRecord("FLYE", FLYE["opportunity_id"], "bound",
        FLYE["flip_bar_ts"], FLYE["provisional_started_ms"], tuple(FLYE["fill_accounts"]),
        FLYE["position_ids"], FLYE["position_entry_ms"], FLYE["retry_segment_id"], 0)
    strategy.configure_fanout_identity_persistence(lambda *_: None)
    strategy.configure_flip_entry_ownership(lambda *_: None, restored={"FLYE": record},
        active_segments={"FLYE": record.opportunity_id}, retry_budget_persist=lambda *_: None,
        restored_retry_budgets={"FLYE": (record.retry_segment_id, 1)})
    request = RemovedWait("FLYE", record.opportunity_id, FLYE["cancel_token"],
        FLYE["cancel_request_ms"], (PRIMARY, MIRROR), purpose)
    writes = []
    strategy.configure_removed_wait(lambda r, active: writes.append((r, active)),
                                    restored={"FLYE": request}, readable=True)
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    state = strategy.watchlist_state("FLYE")
    state.retry_one_watch_start_ms = strategy._boot_ms
    state.cw_resting_taken = state.cw_reclaim_taken = True
    state.bars.append(OHLCVBar(FLYE["fresh_sell_bar_ms"], 2.325, 2.3499, 2.27, 2.285, 149257))
    state.atr_short_flip_bar_ts = FLYE["fresh_sell_bar_ms"]
    strategy.apply_flip_position_book(FlipPositionBook(clock[0], True, {}))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        own = Strategy(code="schwab_1m_v2", name="B2 replay")
        foreign = Strategy(code="orb_schwab", name="shared Schwab ORB control")
        accounts = [BrokerAccount(name=name, provider=provider, environment="test")
                    for name, provider in ((PRIMARY, "schwab"), (MIRROR, "webull"))]
        session.add_all([own, foreign, *accounts])
        session.flush()
        for row in DATA["cancels"]:
            payload = json.loads(json.dumps(row["payload"]))
            payload["metadata"]["reason"] = ("false_flip_restore" if purpose == "false_flip_restore"
                                              else "retry_budget_exhausted")
            account = next(a for a in accounts if a.name == row["account"])
            session.add(TradeIntent(id=UUID(row["id"]), strategy_id=own.id, broker_account_id=account.id,
                symbol="FLYE", side="buy", intent_type="cancel", quantity=1, reason="recorded cancellation",
                status=row["status"], payload=payload, created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"])))
        session.commit()

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0] / 1000, tz or UTC)

    monkeypatch.setattr(bot_module, "datetime", ClockDatetime)
    bot = SchwabV2BotService.__new__(SchwabV2BotService)
    bot.settings, bot.strategy, bot.session_factory = strategy.settings, strategy, factory
    return bot, state, clock, request, writes, engine


def refresh(bot, clock, *, age=0):
    with bot.session_factory() as session:
        # Controlled complete broker reads: the historical receipts were overwritten, not retained.
        record_receipts(session, [(a, instant(clock[0] - age), 0) for a in (PRIMARY, MIRROR)],
                        recorded_at=instant(clock[0]))
        session.commit()
    bot.strategy.apply_flip_position_book(FlipPositionBook(clock[0], True, {}))


def sell(bot, state, phase="live"):
    bot.strategy._cw_v2_track(state, {"flip": "SELL", "observation_phase": phase})


@pytest.mark.asyncio
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("purpose", ["retry_exhausted", "false_flip_restore"])
async def test_original_no_id_cancel_receipts_release_only_on_fresh_sell(monkeypatch, pm, purpose):
    bot, state, clock, request, writes, engine = setup(monkeypatch, pm=pm, purpose=purpose)
    refresh(bot, clock)
    await bot._fresh_sell_owner_release_poll()
    assert bot.strategy._fresh_sell_release_evidence["FLYE"].clear
    assert state.flip_owner_phase == "bound" and bot.strategy._removed_wait_gate_closed("FLYE")
    sell(bot, state)
    assert state.flip_owner_phase == "idle" and not bot.strategy._removed_wait_requests
    assert state.fanout_last_retired_opportunity_id == request.opportunity_id
    assert writes == [(request, False)]
    assert state.retry_one_segment_id == FLYE["fresh_sell_bar_ms"]
    assert not bot.strategy.drain_pending_intents() and not bot.strategy.drain_webull_direct_intents()
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
@pytest.mark.parametrize("fault", ["position", "order", "orb", "intent", "cancel", "settle", "receipt",
                                  "token", "missing", "stale", "unreadable", "flag", "replay", "owner"])
async def test_blocking_controls(monkeypatch, caplog, pm, account, fault):
    bot, state, clock, request, writes, engine = setup(monkeypatch, pm=pm)
    refresh(bot, clock, age=15_001 if fault == "receipt" else 0)
    with bot.session_factory() as session:
        acct = session.scalar(select(BrokerAccount).where(BrokerAccount.name == account))
        own = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        foreign = session.scalar(select(Strategy).where(Strategy.code == "orb_schwab"))
        if fault == "position":
            session.add(AccountPosition(broker_account_id=acct.id, symbol="FLYE", quantity=1))
        elif fault in {"order", "orb"}:
            session.add(BrokerOrder(strategy_id=foreign.id if fault == "orb" else own.id,
                broker_account_id=acct.id, symbol="FLYE", side="buy", status="accepted",
                client_order_id="CONTROL-working", order_type="limit", time_in_force="day", quantity=1))
        elif fault == "intent":
            session.add(TradeIntent(strategy_id=foreign.id, broker_account_id=acct.id, symbol="FLYE",
                side="buy", intent_type="open", quantity=1, reason="control", status="pending"))
        elif fault in {"cancel", "settle", "token", "missing"}:
            cancel = session.scalar(select(TradeIntent).where(TradeIntent.broker_account_id == acct.id))
            if fault == "cancel":
                cancel.status = "submitted"
            elif fault == "settle":
                cancel.updated_at = instant(clock[0] - 14_999)
            elif fault == "token":
                cancel.payload = {**cancel.payload, "metadata": {**cancel.payload["metadata"],
                                                                  "clearwait_removal_token": "wrong"}}
            else:
                session.delete(cancel)
        session.commit()
    await bot._fresh_sell_owner_release_poll()
    if fault in {"position", "order", "orb", "intent", "cancel", "settle", "receipt", "token", "missing"}:
        assert not bot.strategy._fresh_sell_release_evidence["FLYE"].clear
    if fault == "stale":
        clock[0] += 15_001
    elif fault == "unreadable":
        state.flip_owner_evidence_readable = False
    elif fault == "flag":
        bot.settings.strategy_schwab_1m_v2_fresh_sell_owner_release_enabled = False
    elif fault == "owner":
        state.fanout_segment_id = request.opportunity_id + 1
    with caplog.at_level("INFO"):
        sell(bot, state, phase="replay" if fault == "replay" else "live")
    assert "[V2-FRESH-SELL-OWNER-HELD]" in caplog.text
    assert state.flip_owner_phase == "bound" and bot.strategy._removed_wait_requests == {"FLYE": request}
    assert writes == [] and not bot.strategy._strict_first_rest_admitted(state, slot="first")
    engine.dispose()


@pytest.mark.asyncio
async def test_poll_reads_off_loop_and_bar_has_no_database_reads(monkeypatch):
    bot, state, clock, _, _, engine = setup(monkeypatch)
    refresh(bot, clock)
    original = bot._fetch_fresh_sell_release_evidence
    threads = []
    def measured(requests):
        threads.append(get_ident())
        return original(requests)
    bot._fetch_fresh_sell_release_evidence = measured
    await bot._fresh_sell_owner_release_poll()
    assert threads and threads[0] != get_ident()
    bot.session_factory = lambda: pytest.fail("database read on bar")
    sell(bot, state)
    assert state.flip_owner_phase == "idle"
    engine.dispose()


@pytest.mark.asyncio
async def test_settled_receipt_alone_never_reenters_original_segment(monkeypatch):
    bot, state, clock, request, _, engine = setup(monkeypatch)
    refresh(bot, clock)
    await bot._fresh_sell_owner_release_poll()
    state.atr_short_flip_bar_ts = FLYE["retry_segment_id"]
    state.bars[-1] = replace(state.bars[-1], timestamp_ms=FLYE["retry_segment_id"])
    sell(bot, state)
    assert state.flip_owner_phase == "bound" and bot.strategy._removed_wait_requests == {"FLYE": request}
    assert not bot.strategy._strict_first_rest_admitted(state, slot="first")
    engine.dispose()


@pytest.mark.asyncio
async def test_original_flye_recorded_bar_runtime_reaches_1517_buy(monkeypatch):
    bot, state, clock, request, _, engine = setup(monkeypatch)
    state.bars.clear()
    state.atr_short_flip_bar_ts = 0
    bot.strategy._entries_held = True
    drafts = []
    flips = []
    for row in DATA["bars"]:
        ts = int(datetime.fromisoformat(row[0]).timestamp() * 1000)
        if ts >= FLYE["next_buy_flip_bar_ms"]:
            break
        clock[0] = ts + 62_626
        replay = ts < FLYE["fresh_sell_bar_ms"]
        bot.strategy._entries_held = replay
        refresh(bot, clock)
        await bot._fresh_sell_owner_release_poll()
        before = state.atr_state
        bot._strategy_on_bar("FLYE", ChartBar("FLYE", *(float(n) for n in row[1:5]), int(row[5]), ts),
                             observation_phase="replay" if replay else "live")
        if state.atr_state != before:
            flips.append((ts, state.atr_state))
        drafts.extend(bot.strategy.drain_pending_intents())
        bot.strategy.drain_webull_direct_intents()
    assert not bot.strategy._removed_wait_requests, (state.flip_owner_phase, flips[-5:])
    assert state.fanout_last_retired_opportunity_id == request.opportunity_id
    assert (FLYE["fresh_sell_bar_ms"], "short") in flips
    assert state.atr_state == "short"
    assert any(d.intent_type == "open" for d in drafts), [(d.intent_type, d.reason) for d in drafts]
    # Feed the last working entry from the real bar manager through the normal emitter and OMS.
    # Broker acceptance/fill are controlled; the cross prices/times are original retained quotes.
    draft = next(d for d in reversed(drafts) if d.intent_type == "open")
    assert int(draft.metadata["fanout_segment_id"]) > request.opportunity_id
    recorded_quote = next(q for q in DATA["quotes_1517"] if q[4] and float(q[4]) >= 2.33)
    quote_at = datetime.fromisoformat(recorded_quote[0])
    received_at = datetime.fromisoformat(recorded_quote[1])
    clock[0] = int(received_at.timestamp() * 1000)
    last = Decimal(recorded_quote[4])
    emitted = []

    class Redis:
        async def xadd(self, _stream, fields, **_kwargs):
            emitted.append(json.loads(fields["data"]))
            return "test"

    class Broker:
        async def submit_order(self, order):
            assert order.symbol == "FLYE" and order.side == "buy"
            assert last >= Decimal(str(order.metadata["stop_price"]))
            return [ExecutionReport(event_type=kind, client_order_id=order.client_order_id,
                broker_order_id="CONTROL-FLYE-1517", symbol=order.symbol, side=order.side,
                intent_type=order.intent_type, quantity=order.quantity,
                filled_quantity=order.quantity if kind == "filled" else Decimal(0),
                fill_price=last if kind == "filled" else None, metadata=dict(order.metadata),
                reported_at=quote_at) for kind in ("accepted", "filled")]

    redis = Redis()
    emitter = SchwabV2IntentEmitter(bot.settings, redis, PRIMARY)
    assert await bot._emit_removal_tracked(emitter, draft)
    event = TradeIntentEvent.model_validate(emitted[-1])
    settings = bot.settings.model_copy(update={"oms_adapter": "simulated"})
    oms = OmsRiskService(settings=settings, redis_client=redis,
                         session_factory=bot.session_factory, broker_adapter=Broker())
    oms._market_is_fillable = lambda now=None: True
    results = await oms.process_trade_intent(event)
    assert [r.payload.status for r in results] == ["accepted", "filled"]
    with bot.session_factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == results[0].payload.client_order_id))
        assert order.status == "filled" and order.symbol == "FLYE"
    bot.strategy.update_position("FLYE", int(draft.quantity), held_qty=int(draft.quantity))
    bot.strategy.apply_flip_position_book(FlipPositionBook(clock[0], True,
        {"FLYE": (FlipPositionLeg(PRIMARY, "CONTROL-new-row", clock[0], int(draft.quantity)),)}))
    bot.strategy.on_quote("FLYE", Quote("FLYE", float(recorded_quote[2]), float(recorded_quote[3]),
                                       float(last), int(quote_at.timestamp() * 1000)))
    clock[0] = FLYE["next_buy_flip_poll_ms"]
    refresh_at = bot.strategy._symbol_states["FLYE"].flip_owner_evidence_at_ms
    bot.strategy.apply_flip_position_book(FlipPositionBook(clock[0], True,
        {"FLYE": (FlipPositionLeg(PRIMARY, "CONTROL-new-row", refresh_at, int(draft.quantity)),)}))
    row = next(row for row in DATA["bars"] if int(datetime.fromisoformat(row[0]).timestamp() * 1000)
               == FLYE["next_buy_flip_bar_ms"])
    bot._strategy_on_bar("FLYE", ChartBar("FLYE", *(float(n) for n in row[1:5]), int(row[5]),
                                         FLYE["next_buy_flip_bar_ms"]), observation_phase="live")
    assert state.atr_state == "long" and state.flip_owner_phase == "bound"
    assert state.atr_trail == pytest.approx(FLYE["next_buy_flip_trail"], abs=0.000001)
    assert not bot.strategy._strict_first_rest_admitted(state, slot="first")
    engine.dispose()


@pytest.mark.asyncio
async def test_restart_requires_new_poll_and_fresh_live_boundary(monkeypatch):
    bot, state, clock, request, _, engine = setup(monkeypatch)
    refresh(bot, clock)
    await bot._fresh_sell_owner_release_poll()
    bot.strategy._fresh_sell_release_evidence.clear()  # evidence is never restart-restored
    sell(bot, state)
    assert bot.strategy._removed_wait_requests == {"FLYE": request} and state.flip_owner_phase == "bound"
    await bot._fresh_sell_owner_release_poll()
    sell(bot, state)
    assert state.flip_owner_phase == "idle"
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["identity", "owner", "request"])
async def test_persistence_failure_preserves_entry_barrier(monkeypatch, store):
    bot, state, clock, request, _, engine = setup(monkeypatch)
    refresh(bot, clock)
    await bot._fresh_sell_owner_release_poll()
    def fail(*_args):
        raise RuntimeError("controlled store failure")
    setattr(bot.strategy, {"identity": "_fanout_identity_persist", "owner": "_flip_owner_persist",
                           "request": "_removed_wait_persist"}[store], fail)
    sell(bot, state)
    assert bot.strategy._removed_wait_requests == {"FLYE": request}
    assert not bot.strategy._strict_first_rest_admitted(state, slot="first")
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
@pytest.mark.parametrize("status", ["cancelled", "canceled"])
async def test_exact_15_second_own_cancel_boundary(monkeypatch, account, status):
    bot, state, clock, _, _, engine = setup(monkeypatch)
    refresh(bot, clock)
    with bot.session_factory() as session:
        acct = session.scalar(select(BrokerAccount).where(BrokerAccount.name == account))
        cancel = session.scalar(select(TradeIntent).where(TradeIntent.broker_account_id == acct.id))
        cancel.status = status
        cancel.updated_at = instant(clock[0] - 15_000)
        session.commit()
    await bot._fresh_sell_owner_release_poll()
    sell(bot, state)
    assert state.flip_owner_phase == "idle"
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,before,after", [
    ("orb", 'BrokerOrder.side == "buy", BrokerOrder.status.notin_(terminal)',
     'BrokerOrder.side == "buy", BrokerOrder.strategy_id == session.scalar(select(Strategy.id).where(Strategy.code == STRATEGY_CODE)), BrokerOrder.status.notin_(terminal)'),
    ("settle", 'updated > int(cutoff.timestamp() * 1000)', 'False'),
    ("receipt", 'not 0 <= observed - read_at <= FLIP_OWNER_EVIDENCE_MAX_AGE_MS', 'False'),
])
async def test_safety_controls_kill_mutations(monkeypatch, caplog, fault, before, after):
    original = SchwabV2BotService._fetch_fresh_sell_release_evidence
    source = textwrap.dedent(inspect.getsource(original))
    assert before in source
    namespace = dict(bot_module.__dict__)
    class ClockProxy:
        now = staticmethod(lambda tz=None: bot_module.datetime.now(tz))
        fromtimestamp = staticmethod(datetime.fromtimestamp)
    namespace["datetime"] = ClockProxy
    exec(compile(source.replace(before, after), "<B2-safety-mutant>", "exec"), namespace)
    monkeypatch.setattr(SchwabV2BotService, original.__name__, namespace[original.__name__])
    with pytest.raises(AssertionError):
        await test_blocking_controls(monkeypatch, caplog, False, PRIMARY, fault)


def test_default_on_flag_and_v2_catalog_owner():
    name = "strategy_schwab_1m_v2_fresh_sell_owner_release_enabled"
    assert getattr(Settings(), name) is True
    catalog = json.loads((Path(__file__).parents[2] / "ops/health/expected_flags.json").read_text())
    entries = [e for e in catalog["flags"] if e["name"] == name]
    assert len(entries) == 1 and entries[0]["expected"] is True
    assert entries[0]["owning_service"] == "schwab-1m-v2"


@pytest.mark.asyncio
async def test_original_flye_acceptance_kills_flag_off_mutation(monkeypatch):
    original = setup
    def disabled(*args, **kwargs):
        result = original(*args, **kwargs)
        result[0].settings.strategy_schwab_1m_v2_fresh_sell_owner_release_enabled = False
        return result
    monkeypatch.setitem(test_original_flye_recorded_bar_runtime_reaches_1517_buy.__globals__, "setup", disabled)
    with pytest.raises(AssertionError):
        await test_original_flye_recorded_bar_runtime_reaches_1517_buy(monkeypatch)
