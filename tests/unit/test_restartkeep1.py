"""Recorded episode inputs with explicit controlled broker/scanner gaps; no live I/O."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill, Strategy, TradeIntent,
)

from project_mai_tai.events import StrategyStateSnapshotEvent, StrategyStateSnapshotPayload
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from project_mai_tai.strategy_core.session_line_restore import build_session_line
from project_mai_tai.v2_restart_rest_store import RestartRestStore, checkpoint_state
from tests.unit.test_v2_flip_entry_ownership_store import _factory
from tests.unit.test_v2_flip_owned_first_entry import (
    _book, _leg, _strategy, PRIMARY, WEBULL,
)
from tests.unit.test_line_chart_restoration_integration import _bot, _ingest, _proof


def ms(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


# SELL/boot identities and available levels come from the frozen S census.
# Owner IDs, missing levels, candles and broker/scanner receipts are explicit
# scenario controls, not full historical tape/fill or continuity evidence.
CASES = [
    ("TCRT", "2026-10-13T13:07:48-04:00", 1791910740000, 3.5771, False),
    ("JBDI", "2026-10-13T13:07:48-04:00", 1791911100000, 6.4, False),
    ("FLYE", "2026-10-13T13:50:42-04:00", 1791911400000, 3.6, False),
    ("BKYI", "2026-10-13T13:50:42-04:00", 1791913440000, 3.0632, False),
    ("PFAI", "2026-10-07T13:45:27-04:00", 1791394440000, 3.7793, False),
    ("AIXI", "2026-10-08T08:30:14-04:00", 1791461820000, 2.1057, True),
    ("JZ", "2026-10-13T13:07:48-04:00", 1791910500000, 1.6184, False),
]


def case_bot(case, *, active=True):
    symbol, boot, sell, line, software = case
    strategy, clock, _, _ = _strategy(dual=True, retry_one=True)
    clock[0] = ms(boot)
    strategy._boot_ms = clock[0]
    strategy._cw_armed_segment_safety_enabled = True
    strategy._eh_resting_enabled = software
    strategy._retry_one_max_retries = 0
    strategy.settings.strategy_schwab_1m_v2_keep_rest_after_buy_enabled = True
    state = strategy.watchlist_state(symbol)
    state.atr_state = "short"
    state.atr_short_flip_bar_ts = state.retry_one_segment_id = sell
    state.restart_last_live_sell_ms = sell
    state.atr_state_age = 5
    state.atr_trail = line
    state.bars.append(OHLCVBar(clock[0] - 60_000, line-.2, line-.1, line-.3, line-.2, 10000))
    _book(strategy, clock, symbol)
    if active:
        strategy._resting_session_is_eh = lambda now=None: software
        strategy._queue_resting_place(state, line)
        strategy.drain_pending_intents()
        strategy.drain_webull_direct_intents()
    saved = checkpoint_state(state)
    state.resting_active = state.webull_resting_active = False
    state.resting_level = state.resting_trigger = 0
    state.fanout_segment_id = 0
    bot = object.__new__(SchwabV2BotService)
    bot.strategy = strategy
    bot._watch_start_ms = {symbol: sell - 60_000}
    bot._restart_continuing = {symbol: {"watch_ms": sell-60_000, "state": saved}}
    bot._restart_rest_applied = set()
    bot._restart_rest_legs = {symbol: (
        {"software_dispatched": False} if software else {PRIMARY: "owned", WEBULL: "owned"})}
    return bot, state, saved, clock


@pytest.mark.parametrize("case", CASES, ids=[row[0] for row in CASES])
def test_recorded_episodes_restore_rest_management_without_minting_slot(case):
    bot, state, saved, _ = case_bot(case, active=case[0] != "JBDI")
    bot._cap_reconstructed_segment(state.symbol, stage="db-seed")
    assert not state.cw_resting_taken and not state.cw_reclaim_taken
    assert state.resting_active == (case[0] != "JBDI")
    assert state.fanout_segment_id == saved["opportunity_id"]
    assert not bot.strategy.drain_pending_intents()
    assert not bot.strategy.drain_webull_direct_intents()


def test_jbdi_no_rest_until_minimum_age_then_original_sell_can_place():
    bot, state, _, clock = case_bot(CASES[1], active=False)
    bot._cap_reconstructed_segment("JBDI", stage="db-seed")
    state.atr_state_age = 1
    bot.strategy._cw_v2_resting_track(state, None)
    assert not state.resting_active
    clock[0] += 120_000
    state.bars.append(OHLCVBar(clock[0]-60_000, 6, 6.1, 5.9, 6, 10000))
    state.atr_state_age = 3
    _book(bot.strategy, clock, "JBDI")
    bot.strategy._cw_v2_resting_track(state, None)
    assert state.resting_active
    assert len(bot.strategy.drain_pending_intents()) == 1
    assert len(bot.strategy.drain_webull_direct_intents()) == 1


def test_bkyi_frozen_case_restores_cancel_reprice_and_fill_attribution():
    bot, state, saved, clock = case_bot(CASES[3])
    bot._cap_reconstructed_segment("BKYI", stage="db-seed")
    bot.strategy._cw_v2_resting_track(state, {"state": "short", "trail": 3.0342, "state_age": 14})
    # Default 1% stable-rest threshold is unchanged: this 0.95% move is not due yet.
    assert state.resting_active
    assert not bot.strategy.drain_pending_intents()
    bot.strategy._cw_v2_resting_track(state, {"state": "short", "trail": 3.02, "state_age": 15})
    assert len(bot.strategy.drain_pending_intents()) == 1
    assert len(bot.strategy.drain_webull_direct_intents()) == 1
    assert state.flip_owner_opportunity_id == saved["opportunity_id"]
    assert state.retry_one_closes_in_segment == 0
    # Separate explicit fill control: late rows bind to the original opportunity.
    bot.strategy._note_flip_owner_fill(state, account_name=PRIMARY, reason="controlled_native_fill")
    bot.strategy._note_flip_owner_fill(state, account_name=WEBULL, reason="controlled_native_fill")
    _book(bot.strategy, clock, "BKYI",
          _leg(PRIMARY, "primary-row", entered_ms=clock[0]),
          _leg(WEBULL, "mirror-row", entered_ms=clock[0]))
    bot.strategy._bind_flip_owner_on_buy_flip(state, flip_bar_ts=clock[0])
    assert state.flip_owner_phase == "bound"
    assert state.flip_owner_position_ids == {PRIMARY: "primary-row", WEBULL: "mirror-row"}
    assert state.flip_owner_opportunity_id == saved["opportunity_id"]


@pytest.mark.parametrize("case", [CASES[0], CASES[2], CASES[3]])
def test_restored_unfilled_buy_freezes_then_sell_cancels_both_original_legs(case):
    bot, state, saved, clock = case_bot(case)
    bot._cap_reconstructed_segment(state.symbol, stage="db-seed")
    state.atr_state = "long"
    bot.strategy._cw_v2_resting_track(state, {"state": "long"})
    assert state.resting_buy_frozen
    clock[0] += 120_000
    state.bars.append(OHLCVBar(clock[0]-60_000, 3, 3.1, 2.9, 3, 10000))
    state.atr_short_flip_bar_ts = state.bars[-1].timestamp_ms
    _book(bot.strategy, clock, state.symbol)
    bot.strategy._cw_v2_track(state, {"flip": "SELL", "state": "short", "observation_phase": "live"})
    primary = bot.strategy.drain_pending_intents()
    mirror = bot.strategy.drain_webull_direct_intents()
    assert len(primary) == len(mirror) == 1
    assert primary[0].intent_type == mirror[0].intent_type == "cancel"
    assert mirror[0].metadata["fanout_segment_id"] == str(saved["opportunity_id"])
    assert primary[0].metadata["rpg_resting_generation"] == saved["rest"]["resting_schwab_generation"]


@pytest.mark.parametrize("reason", ["filled", "unknown", "wrong_episode"])
def test_no_reactivation_of_filled_unknown_or_retired_owner(reason):
    bot, state, saved, _ = case_bot(CASES[3])
    if reason == "filled":
        state.flip_owner_fill_accounts.add(PRIMARY)
    elif reason == "unknown":
        state.flip_owner_phase = "unknown"
    else:
        state.flip_owner_opportunity_id += 1
    assert not bot.strategy.restore_restart_rest(state, saved, bot._restart_rest_legs["BKYI"])
    assert not state.resting_active


def test_consumed_real_slot_and_retry_budget_never_refunded_by_checkpoint():
    bot, state, saved, _ = case_bot(CASES[3])
    state.cw_resting_taken = state.cw_reclaim_taken = True
    state.retry_one_closes_in_segment = 1
    assert bot.strategy.restore_restart_rest(state, saved, bot._restart_rest_legs["BKYI"])
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert state.retry_one_closes_in_segment == 1
    assert bot.strategy._retry_one_max_retries == 0


@pytest.mark.parametrize("symbol", ["SCNI", "FCHL"])
def test_genuine_readd_still_caps_original_sell(symbol):
    bot, state, _, clock = case_bot((symbol, *CASES[0][1:]))
    bot._restart_continuing.clear()
    bot._watch_start_ms[symbol] = clock[0]
    bot._cap_reconstructed_segment(symbol, stage="readd")
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert not state.resting_active


def test_unobserved_new_sell_during_downtime_is_not_continuous_episode():
    bot, state, _, _ = case_bot(CASES[0])
    state.atr_short_flip_bar_ts += 60_000
    bot._cap_reconstructed_segment("TCRT", stage="db-seed")
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert not state.resting_active


def checkpoint():
    bot, state, _, clock = case_bot(CASES[3])
    return {
        "session_anchor_ms": ms("2026-10-13T04:00:00-04:00"),
        "cursor": "1791913800000-0",
        "symbols": {"BKYI": {"watch_ms": state.atr_short_flip_bar_ts-60_000,
                             "state": checkpoint_state(state)}},
    }, datetime.fromtimestamp(clock[0]/1000, UTC)


def test_store_same_session_roundtrip_and_nextday_zero_previous_session_gates():
    store = RestartRestStore(_factory())
    payload, now = checkpoint()
    store.record(payload, now=now)
    assert store.restore(now=now+timedelta(seconds=1))["symbols"] == payload["symbols"]
    assert store.restore(now=now+timedelta(days=1)) is None
    assert store.restore(now=now.replace(hour=8, minute=0)+timedelta(days=1)) is None


def test_store_latest_removal_tombstone_and_write_session_fence():
    store = RestartRestStore(_factory())
    payload, now = checkpoint()
    store.record(payload, now=now)
    empty = {**payload, "symbols": {}}
    store.record(empty, now=now+timedelta(seconds=1))
    assert store.restore(now=now+timedelta(seconds=2))["symbols"] == {}
    with pytest.raises(ValueError, match="session"):
        store.record(payload, now=now+timedelta(days=1))


@pytest.mark.asyncio
async def test_first_valid_line_publication_keeps_owned_rest_but_later_correction_cancels():
    start = ms("2026-10-13T07:00:00-04:00")
    bars = [ChartBar("FLYE", p, p+.02, p-.02, p, 10000, start+i*60_000)
            for i, p in enumerate([2+i*.03 for i in range(30)] +
                                   [2.9-i*.03 for i in range(30)])]
    bot = _bot("FLYE", bars[-1].timestamp_ms,
               strategy_schwab_1m_v2_account_name=PRIMARY,
               strategy_schwab_1m_v2_webull_account_name=WEBULL)
    ledger = _ingest(bot, bars, live=False)
    ledger.attest(_proof(ledger, bars))
    state = bot.strategy.watchlist_state("FLYE")
    result = build_session_line(ledger.prepare(), bot.strategy._atr_period, bot.strategy._atr_factor)
    sell = dict(result.indicator)["atr_short_flip_bar_ts"]
    assert sell > start
    state.atr_short_flip_bar_ts = sell
    state.restart_last_live_sell_ms = sell
    state.resting_active = state.webull_resting_active = True
    state.resting_level = 3.6
    state.resting_trigger = 3.62
    state.resting_schwab_quantity = 1
    state.resting_webull_quantity = 1
    saved = checkpoint_state(state)
    bot.strategy._boot_ms = bot.strategy._now_ms()
    bot._restart_continuing = {"FLYE": {"watch_ms": start, "state": saved}}
    bot._restart_rest_legs = {"FLYE": {PRIMARY: "owned", WEBULL: "owned"}}
    cancel = Mock(wraps=bot.strategy._queue_resting_cancel)
    bot.strategy._queue_resting_cancel = cancel
    assert await bot._rebuild_session_line("FLYE", ledger)
    cancel.assert_not_called()
    assert state.resting_active and state.webull_resting_active
    # An actual later correction must still revoke the old line.
    corrected = bars[-2]
    ledger.observe(ChartBar("FLYE", corrected.open, corrected.high+.01,
                           corrected.low, corrected.close, corrected.volume, corrected.timestamp_ms))
    revised = tuple(ledger._bars.values()) if isinstance(ledger._bars, dict) else None
    assert revised is not None
    ledger.attest(_proof(ledger, sorted(revised, key=lambda b: b.timestamp_ms)))
    assert await bot._rebuild_session_line("FLYE", ledger)
    assert cancel.call_args.kwargs["reason"] == "line_history_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("gap", ["same", "readd", "trimmed", "unreadable", "malformed", "gone"])
async def test_retained_scanner_cursor_proves_membership_gap_not_latest_presence(monkeypatch, gap):
    bot = SchwabV2BotService(Settings())
    bot.strategy._now_ms = lambda: ms(CASES[3][1])
    bot._restart_checkpoint, _ = checkpoint()
    cursor = bot._restart_checkpoint["cursor"]
    def event(symbols):
        return {"data": StrategyStateSnapshotEvent(
            source_service="strategy-engine",
            payload=StrategyStateSnapshotPayload(top_confirmed=[
                {"symbol": sym, "confirmed_at": "13:00:00", "price": 3} for sym in symbols]),
        ).model_dump_json()}
    latest_id = "1791913800100-0"
    first = (cursor, event(["BKYI"]))
    latest = (latest_id, event(["BKYI"]))
    history = [first, latest]
    if gap == "readd":
        history.insert(1, ("1791913800050-0", event([])))
    elif gap == "trimmed":
        history.pop(0)
    elif gap == "malformed":
        history.insert(1, ("1791913800050-0", {"data": "not a scanner snapshot"}))
    elif gap == "gone":
        history[0] = (cursor, event(["BKYI", "GONE"]))
    bot.redis = Mock(xrevrange=AsyncMock(return_value=[latest]),
                     xrange=AsyncMock(return_value=history))
    if gap == "unreadable":
        bot.redis.xrange.side_effect = RuntimeError("controlled unreadable retained stream")
    bot.rest_client = Mock()
    bot._strategy_state_event_is_current = lambda event: True
    bot._prepare_atr_massive_seeds = AsyncMock()
    bot._protected_symbols = lambda: set()
    bot._schwab_ineligible_symbols = lambda: set()
    bot._try_complete_boot_state_restoration = Mock()
    bot._log_atr_massive_seed_census = Mock()
    bot._sync_gateway_subscription = AsyncMock()
    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.run_resilient_loop", AsyncMock())
    await bot._scanner_consumer_loop()
    assert ("BKYI" in bot._restart_continuing) == (gap in {"same", "gone"})
    expected = bot._restart_checkpoint["symbols"]["BKYI"]["watch_ms"] if gap in {"same", "gone"} else bot.strategy._now_ms()
    assert bot._watch_start_for("BKYI") == expected
    assert all(set(call.args[0]) <= {"BKYI"}
               for call in bot._prepare_atr_massive_seeds.call_args_list)
    assert all(set(call.args[0]) <= {"BKYI"}
               for call in bot.rest_client.set_desired_symbols.call_args_list)


@pytest.mark.asyncio
async def test_checkpoint_excludes_exit_only_symbols_and_deduplicates_writes():
    bot = SchwabV2BotService(Settings())
    bot.strategy._now_ms = lambda: ms(CASES[3][1])
    bot._watchlist = {"BKYI", "EXITONLY"}
    bot._entry_watch_symbols = {"BKYI"}
    bot._watch_start_ms = {sym: bot.strategy._now_ms() for sym in bot._watchlist}
    bot._restart_scanner_ready = True
    bot._strategy_state_last_id = "1791913800000-0"
    bot._restart_rest_store = Mock()
    bot._persist_restart_checkpoint()
    bot._persist_restart_checkpoint()
    assert await bot._restart_checkpoint_writer.drain()
    assert bot._restart_rest_store.record.call_count == 1
    assert set(bot._restart_rest_store.record.call_args.args[0]["symbols"]) == {"BKYI"}


@pytest.mark.parametrize("order_state", [
    "accepted", "cancelled", "cached_cancel_only", "partial_fill", "wrong_generation", "absent",
])
def test_exact_cached_native_order_generation_and_fill_fences(order_state):
    bot, state, saved, clock = case_bot(CASES[3])
    factory = _factory()
    BrokerOrderEvent.__table__.create(factory.kw["bind"])
    bot.session_factory = factory
    now = datetime.fromtimestamp(clock[0]/1000, UTC)
    with factory() as session:
        strategy = Strategy(code="schwab_1m_v2", name="v2")
        account = BrokerAccount(name=PRIMARY, provider="schwab", environment="live")
        session.add_all([strategy, account])
        session.flush()
        intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
            symbol="BKYI", side="buy", intent_type="open", quantity=Decimal("194"),
            reason="controlled native original episode", created_at=now,
            payload={"metadata": {"fanout_segment_id": str(saved["opportunity_id"]),
                "rpg_resting_generation": ("foreign" if order_state == "wrong_generation"
                    else saved["rest"]["resting_schwab_generation"])}})
        session.add(intent)
        session.flush()
        if order_state != "absent":
            order = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id,
                broker_account_id=account.id, client_order_id="controlled-order",
                symbol="BKYI", side="buy", order_type="STOP_LIMIT", time_in_force="day",
                quantity=Decimal("194"), status="cancelled" if order_state in {
                    "cancelled", "cached_cancel_only"} else "accepted",
                payload={})
            session.add(order)
            session.flush()
            if order_state == "cancelled":
                session.add(BrokerOrderEvent(order_id=order.id, event_type="cancelled",
                    event_at=now-timedelta(minutes=1), event_source="broker",
                    payload={"metadata": {}}))
            if order_state == "partial_fill":
                session.add(Fill(order_id=order.id, strategy_id=strategy.id,
                    broker_account_id=account.id, symbol="BKYI", side="buy",
                    quantity=Decimal("1"), price=Decimal("3.08"), filled_at=now, payload={}))
        session.commit()
    payload, _ = checkpoint()
    payload["symbols"]["BKYI"]["state"] = saved
    legs = bot._read_restart_rest_legs(payload)["BKYI"]
    expected = {"accepted": "owned", "cancelled": "terminal", "partial_fill": "filled"}.get(order_state)
    assert legs.get(PRIMARY) == expected


def test_terminal_both_rest_legs_allow_same_owner_nextbar_not_a_new_slot():
    bot, state, saved, clock = case_bot(CASES[0])
    assert bot.strategy.restore_restart_rest(state, saved, {PRIMARY: "terminal", WEBULL: "terminal"})
    assert not state.resting_active
    assert state.flip_owner_opportunity_id == saved["opportunity_id"]
    _book(bot.strategy, clock, state.symbol)
    bot.strategy._cw_v2_resting_track(state, None)
    drafts = bot.strategy.drain_pending_intents()
    assert len(drafts) == 1
    assert drafts[0].metadata["fanout_segment_id"] == str(saved["opportunity_id"])


def test_dispatched_software_rest_cannot_be_rearmed_from_older_checkpoint():
    bot, state, saved, _ = case_bot(CASES[5])
    assert not bot.strategy.restore_restart_rest(state, saved, {"software_dispatched": True})
    assert not state.resting_active


def test_unknown_cancel_gap_blocks_new_rest_even_after_fresh_empty_position_poll():
    bot, state, saved, clock = case_bot(CASES[0])
    assert not bot.strategy.restore_restart_rest(state, saved, {})
    _book(bot.strategy, clock, state.symbol)
    assert state.restart_rest_evidence_pending
    bot.strategy._cw_v2_resting_track(state, None)
    assert not bot.strategy.drain_pending_intents()
    assert not bot.strategy.drain_webull_direct_intents()
    opportunity = state.flip_owner_opportunity_id
    bot.strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == opportunity
    assert state.restart_rest_evidence_pending
    assert len(bot.strategy.drain_pending_intents()) == 1
    assert len(bot.strategy.drain_webull_direct_intents()) == 1


def test_mathematical_seed_sell_is_not_persisted_as_live_observation():
    bot, state, _, _ = case_bot(CASES[1], active=False)
    state.restart_last_live_sell_ms = 0
    assert checkpoint_state(state)["sell_ms"] == 0
    bot.strategy._reset_atr_indicator_state(state, ms("2026-10-14T04:00:00-04:00"))
    assert state.restart_last_live_sell_ms == 0


def test_uncertain_cancel_owner_remains_held_across_another_own_restart():
    bot, state, saved, clock = case_bot(CASES[0])
    assert not bot.strategy.restore_restart_rest(state, saved, {})
    _book(bot.strategy, clock, state.symbol)
    bot.strategy._end_flip_owner_on_sell(state)
    pending = checkpoint_state(state)
    assert pending["cancel_pending"]
    state.restart_rest_evidence_pending = False
    state.restart_rest_cancel_fields.clear()
    assert not bot.strategy.restore_restart_rest(state, pending, {})
    assert state.restart_rest_evidence_pending and state.flip_owner_phase == "unknown"


def test_warmup_sell_after_process_start_is_still_unobserved_not_live_provenance():
    bot, state, _, clock = case_bot(CASES[1], active=False)
    state.atr_short_flip_bar_ts = bot.strategy._boot_ms + 1000
    clock[0] += 60_000
    bot._cap_reconstructed_segment(state.symbol, stage="rest-warmup")
    assert state.cw_resting_taken and state.cw_reclaim_taken


def test_distinct_native_leg_prices_quantities_and_generations_survive_recovery():
    bot, state, saved, _ = case_bot(CASES[2])
    rest = saved["rest"]
    rest.update(resting_schwab_wire_stop=3.62, resting_webull_wire_stop=3.76,
                resting_schwab_wire_limit=3.64, resting_webull_wire_limit=3.79,
                resting_schwab_quantity=165, resting_webull_quantity=79,
                resting_schwab_generation="primary-generation",
                resting_webull_generation="mirror-generation")
    bot.strategy.settings.strategy_schwab_1m_v2_resting_buy_round_up_enabled = True
    assert bot.strategy.restore_restart_rest(state, saved, {PRIMARY: "owned", WEBULL: "owned"})
    assert bot.strategy._active_resting_trigger(state) == 3.62
    assert bot.strategy._active_resting_trigger(state, leg="webull") == 3.76
    assert state.resting_schwab_quantity == 165 and state.resting_webull_quantity == 79
    assert state.resting_schwab_generation == "primary-generation"
    assert state.resting_webull_generation == "mirror-generation"


@pytest.mark.asyncio
async def test_pending_native_terminal_proof_reconciles_off_quote_path(monkeypatch):
    bot, state, saved, _ = case_bot(CASES[0])
    assert not bot.strategy.restore_restart_rest(state, saved, {})
    bot.session_factory = object()
    bot._read_restart_rest_legs = Mock(return_value={"TCRT": {PRIMARY: "terminal", WEBULL: "terminal"}})
    await bot._reconcile_pending_restart_rest()
    assert not state.restart_rest_evidence_pending and not state.resting_active
    assert state.flip_owner_opportunity_id == saved["opportunity_id"]
    assert bot._read_restart_rest_legs.call_count == 1


def test_new_owner_older_checkpoint_is_not_permission_for_duplicate_first_rest():
    bot, state, saved, clock = case_bot(CASES[3])
    state.flip_owner_opportunity_id += 1
    current = state.flip_owner_opportunity_id
    assert not bot.strategy.restore_restart_rest(state, saved, bot._restart_rest_legs["BKYI"])
    _book(bot.strategy, clock, state.symbol)
    assert state.restart_rest_evidence_pending
    bot.strategy._cw_v2_resting_track(state, None)
    assert not bot.strategy.drain_pending_intents()
    assert not bot.strategy.drain_webull_direct_intents()
    bot.strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_opportunity_id == current
    assert state.flip_owner_phase == "unknown"
    assert len(bot.strategy.drain_pending_intents()) == 1
    assert len(bot.strategy.drain_webull_direct_intents()) == 1


def test_one_terminal_leg_recovers_only_that_account_under_same_opportunity():
    bot, state, saved, clock = case_bot(CASES[0])
    assert bot.strategy.restore_restart_rest(state, saved, {PRIMARY: "terminal", WEBULL: "owned"})
    assert state.resting_active and state.webull_resting_active
    generation = state.resting_webull_generation
    _book(bot.strategy, clock, state.symbol)
    bot.strategy._cw_v2_resting_track(state, None)
    primary = bot.strategy.drain_pending_intents()
    assert len(primary) == 1 and primary[0].intent_type == "open"
    assert primary[0].metadata["fanout_segment_id"] == str(saved["opportunity_id"])
    assert not bot.strategy.drain_webull_direct_intents()
    assert state.resting_webull_generation == generation
