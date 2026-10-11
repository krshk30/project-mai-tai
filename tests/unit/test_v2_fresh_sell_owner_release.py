"""B2 original FLYE bars/cancels; broker freshness and acceptance are replay controls."""
from __future__ import annotations

import asyncio
import json
import inspect
import textwrap
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, get_ident
from time import perf_counter
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, DashboardSnapshot, Strategy, TradeIntent,
)
from project_mai_tai.events import TradeIntentEvent
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, Quote
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.positions_read_receipt import record_receipts
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.strategy_core import schwab_1m_v2 as strategy_module
from project_mai_tai import v2_removed_wait as wait_module
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar, SchwabV2Config, SchwabV2IntentEmitter, SchwabV2Strategy,
)
from project_mai_tai.v2_flip_entry_ownership import (
    FlipEntryOwnershipRecord, FlipPositionBook, FlipPositionClose, FlipPositionLeg,
)
from project_mai_tai.v2_removed_wait import RemovedWait, RemovedWaitStore
from project_mai_tai.strategy_core.session_line_restore import SessionCoverage, history_fingerprint
from tests.line_restore_acceptance_factory import make_line_restore_case

DATA = json.loads((Path(__file__).parents[1] / "fixtures/fresh_sell_flye_20261008.json").read_text())
FLYE = DATA["owner"]
PRIMARY, MIRROR = "live:schwab_1m_v2", "live:orb"


def instant(ms):
    return datetime.fromtimestamp(ms / 1000, UTC)


def setup(monkeypatch, *, pm=False, purpose="retry_exhausted", all_on=False, request_present=True):
    shift = -6 * 60 * 60_000 if pm else 0  # Explicit PM time-shift control, not original event time.
    settings = Settings(
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
    )
    if all_on:
        # Every observed process override, not just ON flag names or replay defaults.
        settings = Settings(**{**{k: getattr(settings, k) for k in Settings.model_fields},
                               **DATA["proc_settings"]})
        expected = Settings(**DATA["proc_settings"])
        assert len(DATA["proc_settings"]) == 62
        assert sum(k.endswith(("_enabled", "_atr_only_mode", "_retry_one_max_retries"))
                   for k in DATA["proc_settings"]) == 40
        assert all(getattr(settings, k) == getattr(expected, k) for k in DATA["proc_settings"])
        assert settings.strategy_schwab_1m_v2_enabled and settings.strategy_schwab_1m_v2_atr_only_mode
        assert all(getattr(settings, f"strategy_schwab_1m_v2_{name}_enabled") for name in (
            "line_chart_restoration", "gap_hold", "false_flip", "slotclear_fresh_flip",
            "slotclear_fresh_sell", "fresh_sell_owner_release"))
        assert (settings.strategy_schwab_1m_v2_account_name,
                settings.strategy_schwab_1m_v2_webull_account_name) == (PRIMARY, MIRROR)
    strategy = SchwabV2Strategy(settings, config=(
        SchwabV2Config() if all_on else SchwabV2Config(macd_warmup_settling_bars=0)))
    if all_on:
        strategy._line_readiness = lambda symbol: symbol == "FLYE"
        strategy.configure_falseflip({}, readable=True)
    clock = [FLYE["fresh_sell_poll_ms"] + shift]
    strategy._now_ms = lambda: clock[0]
    strategy._boot_ms = FLYE["retry_segment_id"] + shift - 60_000
    record = FlipEntryOwnershipRecord("FLYE", FLYE["opportunity_id"], "bound",
        FLYE["flip_bar_ts"] + shift, FLYE["provisional_started_ms"] + shift, tuple(FLYE["fill_accounts"]),
        FLYE["position_ids"], {k: v + shift for k, v in FLYE["position_entry_ms"].items()},
        FLYE["retry_segment_id"] + shift, 0)
    strategy.configure_fanout_identity_persistence(lambda *_: None)
    strategy.configure_flip_entry_ownership(lambda *_: None, restored={"FLYE": record},
        active_segments={"FLYE": record.opportunity_id}, retry_budget_persist=lambda *_: None,
        restored_retry_budgets={"FLYE": (record.retry_segment_id, 1)})
    request = RemovedWait("FLYE", record.opportunity_id, FLYE["cancel_token"],
        FLYE["cancel_request_ms"] + shift, (PRIMARY, MIRROR), purpose)
    writes = []
    strategy.configure_removed_wait(lambda r, active: writes.append((r, active)),
                                    restored={"FLYE": request} if request_present else {}, readable=True)
    strategy.drain_pending_intents()
    strategy.drain_webull_direct_intents()
    state = strategy.watchlist_state("FLYE")
    state.retry_one_watch_start_ms = strategy._boot_ms
    state.cw_resting_taken = state.cw_reclaim_taken = True
    state.bars.append(OHLCVBar(FLYE["fresh_sell_bar_ms"] + shift, 2.325, 2.3499, 2.27, 2.285, 149257))
    state.atr_short_flip_bar_ts = FLYE["fresh_sell_bar_ms"] + shift
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
        for row in DATA["cancels"] if request_present else ():
            payload = json.loads(json.dumps(row["payload"]))
            payload["metadata"]["reason"] = ("false_flip_restore" if purpose == "false_flip_restore"
                                              else "retry_budget_exhausted")
            account = next(a for a in accounts if a.name == row["account"])
            session.add(TradeIntent(id=UUID(row["id"]), strategy_id=own.id, broker_account_id=account.id,
                symbol="FLYE", side="buy", intent_type="cancel", quantity=1, reason="recorded cancellation",
                status=row["status"], payload=payload,
                created_at=datetime.fromisoformat(row["created_at"]) + timedelta(milliseconds=shift),
                updated_at=datetime.fromisoformat(row["updated_at"]) + timedelta(milliseconds=shift)))
        session.commit()

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0] / 1000, tz or UTC)

    for module in (bot_module, strategy_module, wait_module):
        monkeypatch.setattr(module, "datetime", ClockDatetime)
    if all_on:
        case = make_line_restore_case(symbol="FLYE", now_ms=clock[0],
                                      settings_overrides={k: getattr(settings, k) for k in Settings.model_fields})
        bot = case.bot
        bot._persist_bar = lambda *_: None  # External recording, never a source-admission proof.
        strategy._line_readiness = bot._line_buy_ready
        strategy._line_version_reader = bot._line_version
    else:
        bot = SchwabV2BotService.__new__(SchwabV2BotService)
    bot.settings, bot.strategy, bot.session_factory = strategy.settings, strategy, factory
    bot._removed_wait_store = RemovedWaitStore(factory)
    if request_present:
        bot._removed_wait_store.record(request, True)
    with factory() as session:
        session.add(DashboardSnapshot(snapshot_type="v2_flip_entry_ownership",
                    payload=record.as_payload(active=True, reason="recorded_owner_replay_control")))
        session.commit()
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


async def commit_and_later_bar(bot, state, clock):
    await bot._removed_wait_poll()
    clock[0] += 60_000
    state.bars.append(replace(state.bars[-1], timestamp_ms=state.bars[-1].timestamp_ms + 60_000))
    refresh(bot, clock)
    await bot._fresh_sell_owner_release_poll()
    bot.strategy._cw_v2_track(state, None)


async def recorded_bar(bot, row, phase):
    ts = int(datetime.fromisoformat(row[0]).timestamp() * 1000)
    bar = ChartBar("FLYE", *(float(n) for n in row[1:5]), int(row[5]), ts)
    await bot._handle_bar("FLYE", bar, observation_phase=phase)
    ledger = bot._line_sessions["FLYE"]
    bars = tuple(ledger._bars[key] for key in sorted(ledger._bars))
    # Historical provider coverage was not retained. This is a labelled external control.
    ledger.attest(SessionCoverage("schwab_rest_full_session", ledger.anchor_ms, ts + 60_000,
                  tuple(b.timestamp_ms for b in bars), True, history_fingerprint(bars),
                  prefix_complete=True))
    rebuilt = await bot._rebuild_session_line("FLYE", ledger)
    assert rebuilt or (phase == "replay" and ledger.incomplete_reason == "indicator_unseeded"), (
        ts, ledger.incomplete_reason)


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
    assert state.flip_owner_phase == "bound"
    await commit_and_later_bar(bot, state, clock)
    assert state.flip_owner_phase == "idle" and not bot.strategy._removed_wait_requests
    assert state.fanout_last_retired_opportunity_id == request.opportunity_id
    assert writes == []  # The new atomic store path never calls the synchronous callback.
    assert bot._removed_wait_store.restore() == {}
    assert state.retry_one_segment_id == FLYE["fresh_sell_bar_ms"] - (6 * 60 * 60_000 if pm else 0)
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
    clock[0] += 60_000
    state.bars.append(replace(state.bars[-1], timestamp_ms=state.bars[-1].timestamp_ms + 60_000))
    state.atr_state, state.atr_short_flip_bar_ts = "long", 0
    bot.strategy._cw_v2_track(state, {"flip": "BUY", "observation_phase": "live", "flip_level": 2.3})
    assert state.fresh_sell_owner_release_segment_ms == 0
    assert bot.strategy._removed_wait_requests == {"FLYE": request}
    assert not bot.strategy._strict_first_rest_admitted(state, slot="first")
    assert not bot.strategy.drain_pending_intents()
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
    bot.session_factory = bot._removed_wait_store.session_factory
    await bot._removed_wait_poll()
    clock[0] += 60_000
    state.bars.append(replace(state.bars[-1], timestamp_ms=state.bars[-1].timestamp_ms + 60_000))
    refresh(bot, clock)
    await bot._fresh_sell_owner_release_poll()
    bot.session_factory = lambda: pytest.fail("database access on release bar")
    for name in ("_removed_wait_persist", "_flip_owner_persist", "_fanout_identity_persist"):
        setattr(bot.strategy, name, lambda *_: pytest.fail("synchronous persistence on release bar"))
    bot.strategy._cw_v2_track(state, None)
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
async def test_one_trade_in_new_sell_segment_cannot_release_again(monkeypatch):
    bot, state, clock, old_request, _, engine = setup(monkeypatch)
    refresh(bot, clock)
    await bot._fresh_sell_owner_release_poll()
    sell(bot, state)
    await commit_and_later_bar(bot, state, clock)
    assert state.flip_owner_phase == "idle"
    assert not bot.strategy.__dict__.get("_removed_wait_committed_clears", {})
    new_opportunity = bot.strategy._ensure_flip_owner_opportunity(state)
    assert new_opportunity > old_request.opportunity_id
    state.flip_owner_phase = "provisional"  # Controlled first trade closes before the next BUY.
    state.flip_owner_retry_segment_id = state.retry_one_segment_id
    state.flip_owner_retry_closes_at_place = 0
    assert not bot.strategy._retry_one_release_or_hold(state, exit_reason="CW_TARGET")
    assert state.retry_one_closes_in_segment == 1 and state.flip_owner_phase == "consumed"
    clock[0] += 60_000
    state.bars.append(replace(state.bars[-1], timestamp_ms=state.bars[-1].timestamp_ms + 60_000))
    refresh(bot, clock)
    await bot._fresh_sell_owner_release_poll()
    bot.strategy._cw_v2_track(state, None)
    assert state.fresh_sell_owner_release_segment_ms == 0
    assert state.flip_owner_phase == "consumed" and state.flip_owner_opportunity_id == new_opportunity
    assert state.fanout_last_retired_opportunity_id == old_request.opportunity_id
    assert bot.strategy._flip_owner_counts["release_succeeded"] == 1
    assert not bot.strategy._strict_first_rest_admitted(state, slot="first")
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred_cancel,late_request", [(False, False), (True, False), (False, True)])
async def test_original_flye_recorded_bar_runtime_reaches_1517_buy(monkeypatch, deferred_cancel,
                                                                late_request):
    bot, state, clock, request, _, engine = setup(monkeypatch, all_on=True,
                                                request_present=not late_request)
    state.bars.clear()
    state.atr_short_flip_bar_ts = 0
    bot.strategy._entries_held = True
    drafts = []
    flips = []
    released_at = 0
    emitted = []
    class Redis:
        async def xadd(self, _stream, fields, **_kwargs):
            emitted.append((clock[0], json.loads(fields["data"])))
            return "test"
    redis = Redis()
    wire = SchwabV2IntentEmitter(bot.settings, redis, PRIMARY)
    record_emit = bot.intent_emitter.emit
    async def recorded_emit(draft):
        await record_emit(draft)
        await wire.emit(draft)
    bot.intent_emitter.emit = recorded_emit
    if deferred_cancel:
        with bot.session_factory() as session:
            for cancel in session.scalars(select(TradeIntent)):
                cancel.updated_at = instant(FLYE["fresh_sell_poll_ms"] - 10_000)
            session.commit()
    for row in DATA["bars"]:
        ts = int(datetime.fromisoformat(row[0]).timestamp() * 1000)
        if ts >= FLYE["next_buy_flip_bar_ms"]:
            break
        clock[0] = ts + 62_626
        replay = ts < FLYE["fresh_sell_bar_ms"]
        bot.strategy._entries_held = replay
        refresh(bot, clock)
        if late_request and ts == FLYE["fresh_sell_bar_ms"]:
            leg = FlipPositionLeg(PRIMARY, FLYE["position_ids"][PRIMARY],
                                  FLYE["position_entry_ms"][PRIMARY], 280)
            bot.strategy.update_position("FLYE", 280, held_qty=280)
            bot.strategy.apply_flip_position_book(FlipPositionBook(clock[0], True, {"FLYE": (leg,)}))
            assert not bot.strategy._removed_wait_requests
        await bot._fresh_sell_owner_release_poll()
        await bot._removed_wait_poll()
        before = state.atr_state
        await recorded_bar(bot, row, "replay" if replay else "live")
        if not bot.strategy._removed_wait_requests and not released_at and ts > FLYE["fresh_sell_bar_ms"]:
            released_at = ts
        if ts == FLYE["fresh_sell_bar_ms"]:
            if late_request:
                assert state.flip_owner_phase == "awaiting_close"
                assert state.position_qty_held == 280 and state.flip_owner_open_positions
                assert state.fresh_sell_owner_release_segment_ms == ts
                assert not bot.strategy._removed_wait_requests
                assert not bot.strategy._strict_first_rest_admitted(state, slot="first")
                assert any(o.bar_time_ms == ts for o in bot.intent_emitter.atr_sell_observations)
                bot.strategy._cancel_retry_leftovers(state)
                assert not bot.strategy._removed_wait_requests  # Holding still owns its position.
            else:
                assert bot.strategy._removed_wait_requests == {"FLYE": request}
        if state.atr_state != before:
            flips.append((ts, state.atr_state))
        drafts.extend(bot.strategy.drain_pending_intents())
        bot.strategy.drain_webull_direct_intents()
        # The production 5-second poll commits a staged clear between minute bars.
        clock[0] += 5_000
        if late_request and ts == FLYE["fresh_sell_bar_ms"]:
            # Controlled post-SELL close/cancel responses, not historical claims.
            # Existing publisher creates the request; no pre-seeded request or invented token.
            bot.strategy.update_position("FLYE", 0, held_qty=0)
            state.flip_owner_open_positions.clear()
            bot.strategy._removed_wait_persist = bot._removed_wait_store.record
            bot.strategy._cancel_retry_leftovers(state)
            request = bot.strategy._removed_wait_requests["FLYE"]
            assert request.requested_at_ms == clock[0] and request.opportunity_id == FLYE["opportunity_id"]
            with bot.session_factory() as session:
                own = session.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
                for row in DATA["cancels"]:
                    acct = session.scalar(select(BrokerAccount).where(BrokerAccount.name == row["account"]))
                    metadata = {**row["payload"]["metadata"], "clearwait_removal_token": request.token}
                    session.add(TradeIntent(strategy_id=own.id, broker_account_id=acct.id, symbol="FLYE",
                        side="buy", intent_type="cancel", quantity=1, reason="controlled post-SELL close",
                        status="cancelled", created_at=instant(clock[0]), updated_at=instant(clock[0]),
                        payload={**row["payload"], "metadata": metadata}))
                session.commit()
            close = FlipPositionClose(PRIMARY, FLYE["position_ids"][PRIMARY], "ATR_FLIP")
            bot.strategy.apply_flip_position_book(FlipPositionBook(clock[0], True, {},
                                                  closes_by_symbol={"FLYE": (close,)}))
            assert state.flip_owner_phase == "awaiting_close" and not state.flip_owner_open_positions
        refresh(bot, clock)
        await bot._fresh_sell_owner_release_poll()
        if late_request and ts == FLYE["fresh_sell_bar_ms"]:
            assert not bot.strategy._fresh_sell_release_evidence["FLYE"].clear
            assert not bot.strategy._fresh_sell_release_owner(state, None)
            assert bot.strategy._removed_wait_requests == {"FLYE": request}
        await bot._removed_wait_poll()
    drafts.extend(bot.intent_emitter.intents)
    assert not bot.strategy._removed_wait_requests, (state.flip_owner_phase, flips[-5:])
    assert state.fanout_last_retired_opportunity_id == request.opportunity_id
    assert (FLYE["fresh_sell_bar_ms"], "short") in flips
    assert state.atr_state == "short"
    assert released_at > FLYE["fresh_sell_bar_ms"]
    assert any(d.intent_type == "open" for d in drafts), [(d.intent_type, d.reason) for d in drafts]
    # Feed the last working entry from the real bar manager through the normal emitter and OMS.
    # Broker acceptance/fill are controlled; the cross prices/times are original retained quotes.
    draft = next(d for d in reversed(drafts) if d.intent_type == "open")
    assert int(draft.metadata["fanout_segment_id"]) > request.opportunity_id
    # This order passed the real service gate when first placed, before the recorded cross.
    waiting_at, event_row = next(
        (at, row) for at, row in reversed(emitted) if row["payload"]["intent_type"] == "open")
    event = TradeIntentEvent.model_validate(event_row)
    stop, limit = (Decimal(event.payload.metadata[k]) for k in ("stop_price", "limit_price"))
    recorded_quote = next(q for q in DATA["quotes_1517"] if q[4] and stop <= Decimal(q[4]) <= limit)
    quote_at = datetime.fromisoformat(recorded_quote[0])
    received_at = datetime.fromisoformat(recorded_quote[1])
    clock[0] = int(received_at.timestamp() * 1000)
    last = Decimal(recorded_quote[4])
    class Broker:
        async def submit_order(self, order):
            assert order.symbol == "FLYE" and order.side == "buy"
            assert order.broker_account_name == PRIMARY
            assert Decimal(order.metadata["stop_price"]) <= last <= Decimal(order.metadata["limit_price"])
            return [ExecutionReport(event_type=kind, client_order_id=order.client_order_id,
                broker_order_id="CONTROL-FLYE-1517", symbol=order.symbol, side=order.side,
                intent_type=order.intent_type, quantity=order.quantity,
                filled_quantity=order.quantity if kind == "filled" else Decimal(0),
                fill_price=last if kind == "filled" else None, metadata=dict(order.metadata),
                reported_at=quote_at) for kind in ("accepted", "filled")]

    assert waiting_at < clock[0] < FLYE["next_buy_flip_poll_ms"]
    assert received_at.astimezone(strategy_module.EASTERN_TZ).strftime("%H:%M") == "15:17"
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
    await recorded_bar(bot, row, "live")
    assert state.atr_state == "long" and state.flip_owner_phase == "bound"
    assert state.atr_trail == pytest.approx(FLYE["next_buy_flip_trail"], abs=0.000001)
    assert not bot.strategy._strict_first_rest_admitted(state, slot="first")
    def et(ms):
        return instant(ms).astimezone(strategy_module.EASTERN_TZ).strftime("%H:%M:%S.%f")
    print(f"FLYE_CONTROLLED deferred_cancel={deferred_cancel} late_request={late_request} "
          f"release_bar={et(released_at)} waiting_buy={et(waiting_at)} "
          f"account={event.payload.broker_account_name} "
          f"stop={event.payload.metadata['stop_price']} limit={event.payload.metadata['limit_price']} "
          f"accepted_filled={quote_at.astimezone(strategy_module.EASTERN_TZ).strftime('%H:%M:%S.%f')} "
          f"observed={received_at.astimezone(strategy_module.EASTERN_TZ).strftime('%H:%M:%S.%f')} price={last} "
          "proc_fields=62 tracked_fields=40 provider_coverage/freshness/broker=CONTROLLED "
          "LINE/GAP/FALSE/SLOTx2/ATR_ONLY=ON old_segment=consumed same_segment_reentry=blocked")
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
    await commit_and_later_bar(bot, state, clock)
    assert state.flip_owner_phase == "idle"
    engine.dispose()


@pytest.mark.asyncio
async def test_persistence_failure_preserves_entry_barrier(monkeypatch):
    bot, state, clock, request, _, engine = setup(monkeypatch)
    refresh(bot, clock)
    await bot._fresh_sell_owner_release_poll()
    def fail(*_args):
        raise RuntimeError("controlled store failure")
    bot._removed_wait_store.commit_clear = fail
    sell(bot, state)
    await commit_and_later_bar(bot, state, clock)
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
    await commit_and_later_bar(bot, state, clock)
    assert state.flip_owner_phase == "idle"
    engine.dispose()


@pytest.mark.asyncio
async def test_pending_slow_commit_does_not_block_bar_or_release_at_buy(monkeypatch):
    bot, state, clock, request, _, engine = setup(monkeypatch)
    refresh(bot, clock)
    await bot._fresh_sell_owner_release_poll()
    sell(bot, state)
    started, finish = Event(), Event()
    commit = bot._removed_wait_store.commit_clear
    def slow(plan, valid):
        started.set()
        assert finish.wait(2)
        commit(plan, valid)
    bot._removed_wait_store.commit_clear = slow
    task = asyncio.create_task(bot._removed_wait_poll())
    try:
        async def wait_for_commit():
            while not started.is_set():
                assert not task.done(), "off-loop retirement not staged; L1 API dependency required"
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait_for_commit(), 2)
        before = perf_counter()
        bot.strategy._cw_v2_track(state, None)
        elapsed = perf_counter() - before
        assert elapsed < .05 and not task.done()
        assert state.flip_owner_phase == "bound" and bot.strategy._removed_wait_gate_closed("FLYE")
        state.atr_state, state.atr_short_flip_bar_ts = "long", 0
        bot.strategy._cw_v2_track(state, {"flip": "BUY", "observation_phase": "live", "flip_level": 2.3})
    finally:
        finish.set()
        await task
    assert bot._removed_wait_store.restore() == {"FLYE": request}
    assert bot.strategy._removed_wait_requests == {"FLYE": request}
    assert not bot.strategy._strict_first_rest_admitted(state, slot="first")
    engine.dispose()


@pytest.mark.asyncio
async def test_recorded_deferred_release_kills_sell_bar_only_mutation(monkeypatch):
    original = SchwabV2Strategy._cw_v2_track
    source = textwrap.dedent(inspect.getsource(original))
    before = 'elif state.fresh_sell_owner_release_segment_ms and flip != "SELL":'
    assert before in source
    namespace = dict(strategy_module.__dict__)
    exec(compile(source.replace(before, "elif False:"), "<B2-deferred-mutant>", "exec"), namespace)
    monkeypatch.setattr(SchwabV2Strategy, original.__name__, namespace[original.__name__])
    with pytest.raises(AssertionError):
        await test_original_flye_recorded_bar_runtime_reaches_1517_buy(monkeypatch, True, False)


@pytest.mark.asyncio
async def test_post_sell_request_kills_request_dependent_latch_mutation(monkeypatch):
    original = SchwabV2Strategy._end_flip_owner_on_sell
    source = textwrap.dedent(inspect.getsource(original))
    before = "        state.fresh_sell_owner_release_segment_ms = bar_ms"
    assert before in source
    namespace = dict(strategy_module.__dict__)
    exec(compile(source.replace(before, "        if state.symbol in self._removed_wait_requests:\n"
                                       "    " + before), "<B2-late-request-mutant>", "exec"), namespace)
    monkeypatch.setattr(SchwabV2Strategy, original.__name__, namespace[original.__name__])
    with pytest.raises(AssertionError):
        await test_original_flye_recorded_bar_runtime_reaches_1517_buy(monkeypatch, False, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [PRIMARY, MIRROR])
async def test_original_flye_unclear_at_1517_buy_stays_owned(monkeypatch, account):
    bot, state, clock, request, _, engine = setup(monkeypatch, all_on=True)
    with bot.session_factory() as session:
        acct = session.scalar(select(BrokerAccount).where(BrokerAccount.name == account))
        cancel = session.scalar(select(TradeIntent).where(TradeIntent.broker_account_id == acct.id))
        cancel.status = "submitted"
        session.commit()
    state.bars.clear()
    state.atr_short_flip_bar_ts = 0
    for row in DATA["bars"]:
        ts = int(datetime.fromisoformat(row[0]).timestamp() * 1000)
        if ts > FLYE["next_buy_flip_bar_ms"]:
            break
        clock[0] = ts + 62_626
        replay = ts < FLYE["fresh_sell_bar_ms"]
        bot.strategy._entries_held = replay
        refresh(bot, clock)
        await bot._fresh_sell_owner_release_poll()
        await bot._removed_wait_poll()
        await recorded_bar(bot, row, "replay" if replay else "live")
    assert state.atr_state == "long" and state.fresh_sell_owner_release_segment_ms == 0
    assert bot.strategy._removed_wait_requests == {"FLYE": request}
    assert state.flip_owner_opportunity_id == request.opportunity_id
    assert not bot.strategy._strict_first_rest_admitted(state, slot="first")
    assert not any(d.intent_type == "open" for d in bot.intent_emitter.intents + bot.webull_intent_emitter.intents)
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,before,after", [
    ("orb", 'BrokerOrder.side == "buy",',
     'BrokerOrder.side == "buy", BrokerOrder.strategy_id == session.scalar(select(Strategy.id).where(Strategy.code == STRATEGY_CODE)),'),
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
    assert callable(getattr(SchwabV2Strategy, "stage_removed_wait_clear", None))
    original = setup
    def disabled(*args, **kwargs):
        result = original(*args, **kwargs)
        result[0].settings.strategy_schwab_1m_v2_fresh_sell_owner_release_enabled = False
        return result
    monkeypatch.setitem(test_original_flye_recorded_bar_runtime_reaches_1517_buy.__globals__, "setup", disabled)
    with pytest.raises(AssertionError):
        await test_original_flye_recorded_bar_runtime_reaches_1517_buy(monkeypatch, False, False)
