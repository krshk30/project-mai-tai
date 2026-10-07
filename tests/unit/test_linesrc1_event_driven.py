"""[codex] Event-only source tests; prices and empty envelopes are retained records.

Provider completeness and callback clocks are controlled, not historical receipts.
"""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from project_mai_tai.events import StrategyStateSnapshotEvent, StrategyStateSnapshotPayload
from project_mai_tai.confirmation_exit import ConfirmationEntry
from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy, TradeIntentDraft
from project_mai_tai.strategy_core.session_line_restore import build_session_line
from tests.line_restore_acceptance_factory import RecordingEmitter
from tests.unit.test_line_chart_restoration_integration import _bars, _bot, _ingest, _ms, _payload
from tests.unit.test_linesrc1_anchored_session_poll import OWN_EMPTY_ROWS


def _scanner(bot, symbols, *, identity="09:01:00", price=2):
    event = StrategyStateSnapshotEvent(
        source_service="strategy-engine",
        payload=StrategyStateSnapshotPayload(top_confirmed=[
            {"symbol": sym, "confirmed_at": identity, "price": price} for sym in symbols
        ]),
    )
    return bot._line_scanner_transition(event, set(symbols))


async def _seed(symbol="RETO", clock="2026-10-05T11:18:00-04:00", *, database=False):
    bars = _bars(symbol, _ms(clock))
    bot = _bot(symbol, bars[-1].timestamp_ms)
    now = [bars[-1].timestamp_ms + 61_000]
    bot.strategy._now_ms = lambda: now[0]
    # The retained source has sixteen later-created rows, removed here to
    # control an actual incomplete local prefix before the single event GET.
    available = [bar for bar in bars if not (
        _ms("2026-10-05T10:49:00-04:00") <= bar.timestamp_ms
        <= _ms("2026-10-05T11:04:00-04:00"))]
    _ingest(bot, available, live=False)
    bot._rest_warmup_done.add(symbol)
    bot._persist_bar = lambda *args: None
    bot.rest_client = SchwabV2RestClient(bot.settings, on_chart_bar=bot._handle_bar_from_rest,
                                      on_quote=AsyncMock())
    if database:
        bot.session_factory = object()
        bot._read_line_session_bars = Mock(return_value=[])
    bot.rest_client._authorized_get = Mock(return_value=_payload(symbol, bars))
    bot._request_line_repairs(_scanner(bot, {symbol}))
    assert not bot._line_source_pending
    # Initial membership is not a re-add. The real first closed-source callback
    # authorizes the one bootstrap GET, even though the local deque is populated.
    await bot._handle_bar_from_streamer(symbol, bars[-1])
    assert symbol in bot._line_source_pending
    await bot._line_source_events_pass()
    await bot._line_restoration_pass()
    assert bot.rest_client._authorized_get.call_count == 1
    assert bot._line_buy_ready(symbol)
    return bot, now, bars


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["streamer", "rest", "c3"])
async def test_one_real_parser_fetch_then_live_callbacks_advance_immutable_seed_math_and_ready(source, monkeypatch):
    bot, now, prefix = await _seed(database=True)
    ledger = bot._line_sessions["RETO"]
    seed = ledger._coverage
    original = (seed.closed_ids, seed.end_ms, seed.bars_sha256)
    assert bot._read_line_session_bars.call_count == 1
    suffix = [bar for bar in _bars("RETO") if bar.timestamp_ms > prefix[-1].timestamp_ms]
    assert len(suffix) == 9
    bot.rest_client.fetch_session_history = Mock(side_effect=AssertionError("second full GET"))
    calculations = []
    original_update = SchwabV2Strategy._update_atr_state

    def counted(engine, state, bar, **kwargs):
        calculations.append(bar.timestamp_ms)
        return original_update(engine, state, bar, **kwargs)

    monkeypatch.setattr(SchwabV2Strategy, "_update_atr_state", counted)
    for bar in suffix:
        now[0] = bar.timestamp_ms + 61_000
        old_version = bot._line_version("RETO")
        if source == "streamer":
            await bot._handle_bar_from_streamer("RETO", bar)
        else:
            if source == "c3":
                # Strategy already received the stream candle. REST's C3 skip
                # must still observe its source and wake the line worker.
                bot.strategy.on_observed_bar("RETO", bar, observation_phase="live")
                bot._should_skip_rest_strategy_feed = lambda *args: True
            await bot._handle_bar_from_rest("RETO", bar)
        assert not bot._line_buy_ready("RETO")
        calculations.clear()
        await bot._line_restoration_pass()
        assert calculations == [bar.timestamp_ms]
        assert ledger._coverage is seed
        assert (seed.closed_ids, seed.end_ms, seed.bars_sha256) == original
        assert bot._line_published["RETO"].request.current_bar_ms == bar.timestamp_ms
        assert bot._line_buy_ready("RETO") and bot._line_version("RETO") != old_version
        oracle = build_session_line(ledger.prepare(), bot.strategy._atr_period, bot.strategy._atr_factor)
        assert bot._line_published["RETO"].snapshot == oracle
        assert bot._confirmation_bar_states[("RETO", bar.timestamp_ms)] in {"long", "short"}
    bot.rest_client.fetch_session_history.assert_not_called()
    assert bot.rest_client._authorized_get.call_count == 1 and not bot._line_source_pending
    assert bot._read_line_session_bars.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["BIYA", "MI", "MTEN", "SXTC"])
async def test_recorded_empty_event_is_once_waiting_without_callback_retry(symbol):
    current = _ms("2026-10-07T07:00:00-04:00")
    bot = _bot(symbol, current)
    now = [current + 61_000]
    bot.strategy._now_ms = lambda: now[0]
    bot.rest_client = SchwabV2RestClient(bot.settings, on_chart_bar=AsyncMock(), on_quote=AsyncMock())
    bot.rest_client._authorized_get = Mock(return_value=OWN_EMPTY_ROWS[symbol]["raw_marketdata_response"])
    bot._request_line_repairs(_scanner(bot, {symbol}))
    assert not bot._line_source_pending
    # Controlled removal/re-confirmation of the unknown local prefix, not an
    # invented observed candle for these measured empty names.
    _scanner(bot, set())
    bot._request_line_repairs(_scanner(bot, {symbol}))
    await bot._line_source_events_pass()
    ledger = bot._line_sessions[symbol]
    assert ledger._coverage is None and not ledger._bars
    assert bot.rest_client._line_source_states[symbol][0] == "waiting"
    assert bot._line_source_waiting[symbol] == ledger.epoch
    # There are no measured price bars for these four names. Repeated scanner
    # publications/worker wakeups are controls, not fabricated live candles.
    for minute in range(1, 12):
        now[0] = current + (minute + 1) * 60_000 + 1_000
        bot._request_line_repairs(_scanner(bot, {symbol}, price=minute))
        bot._line_source_event.set()
        await bot._line_source_events_pass()
        assert not bot._line_buy_ready(symbol)
    assert bot.rest_client._authorized_get.call_count == 1
    assert not bot._line_source_pending and not bot._line_published


@pytest.mark.asyncio
@pytest.mark.parametrize("minute", ["07:00:00", "07:02:00", "07:10:00"])
async def test_first_actual_closed_bar_triggers_startup_once_even_quiet_names(minute):
    # Counterfactual time shift of a retained RETO candle, not a raw morning bar.
    target = _ms(f"2026-10-07T{minute}-04:00")
    bot = _bot("RETO", _ms("2026-10-07T06:58:00-04:00"))
    now = [_ms("2026-10-07T07:00:00-04:00")]
    bot.strategy._now_ms = lambda: now[0]
    bar = replace(_bars("RETO")[0], timestamp_ms=target)
    bot._observe_line_bar("RETO", bar, source_callback=True)
    assert not bot._line_source_pending
    now[0] = target + 61_000
    bot._observe_line_bar("RETO", bar, source_callback=True)
    assert len(bot._line_source_pending) == 1
    identity = bot._line_source_identity["RETO"]
    bot._observe_line_bar("RETO", bar, source_callback=True)
    next_bar = replace(bar, timestamp_ms=target + 60_000)
    now[0] += 60_000
    bot._observe_line_bar("RETO", next_bar, source_callback=True)
    assert bot._line_source_identity["RETO"] == identity


@pytest.mark.asyncio
async def test_repeat_publications_quotes_and_certified_sparse_gaps_do_not_create_events():
    bot, _, _ = await _seed()
    ledger = bot._line_sessions["RETO"]
    budget = set(bot._line_source_budget)
    for price in range(4):
        bot._request_line_repairs(_scanner(bot, {"RETO"}, price=price))
    assert bot._line_source_budget == budget and not bot._line_source_pending
    # A certified sparse prefix is not a newly unrecovered re-add hole.
    bot._line_scanner_symbols.clear()
    ledger.gap_pairs = Mock(return_value=((ledger.anchor_ms, ledger.anchor_ms + 120_000),))
    bot._request_line_repairs(_scanner(bot, {"RETO"}))
    assert ledger.prepare() is not None and not bot._line_source_pending


@pytest.mark.asyncio
async def test_nohole_retired_readd_reuses_receipt_but_old_endpoint_waits_for_same_unused_event():
    bot, now, bars = await _seed()
    old = bot._line_sessions["RETO"]
    seed = old._coverage
    _scanner(bot, set())
    bot._watchlist.clear()
    bot._sync_line_epochs()
    now[0] += 120_000
    bot._watchlist.add("RETO")
    bot._sync_line_epochs()
    new = bot._line_sessions["RETO"]
    assert new.epoch != old.epoch and new._coverage == seed
    bot._request_line_repairs(_scanner(bot, {"RETO"}))
    assert not bot._line_buy_ready("RETO") and not bot._line_source_pending
    # First received bar proves a tail hole. Use the unspent re-add event;
    # ordinary callback gaps without that event never authorize a request.
    next_bar = _bars("RETO", bars[-1].timestamp_ms + 120_000)[-1]
    await bot._handle_bar_from_streamer("RETO", next_bar)
    assert len(bot._line_source_pending) == 1
    assert bot._line_source_pending["RETO"][-1] == "reconfirmed_hole"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["tail_gap", "tail_tape", "prefix_value", "prefix_id", "db_value"])
async def test_unknown_tail_and_prefix_conflicts_revoke_without_new_source_authority(damage):
    bot, now, bars = await _seed()
    ledger = bot._line_sessions["RETO"]
    seed = ledger._coverage
    if damage.startswith("tail"):
        bar = _bars("RETO", bars[-1].timestamp_ms + 120_000)[-1]
        now[0] = bar.timestamp_ms + 61_000
        await bot._handle_bar_from_streamer("RETO", bar)
        if damage == "tail_tape":
            ledger.observe_trade(bar.timestamp_ms - 60_000)
    else:
        bar = bars[20]
        if damage == "prefix_id":
            # Counterfactual added prefix ID just before the retained first bar.
            bar = replace(bars[0], timestamp_ms=bars[0].timestamp_ms - 60_000)
        else:
            bar = replace(bar, volume=bar.volume + 1)
        if damage == "db_value":
            bot.session_factory = object()
            bot._read_line_session_bars = lambda *args: [bar]
        else:
            bot._observe_line_bar("RETO", bar, source_callback=True)
    bot._line_dirty.add("RETO")
    await bot._line_restoration_pass()
    assert ledger._coverage is seed and ledger.prepare() is None
    assert not bot._line_buy_ready("RETO") and not bot._line_source_pending
    assert bot.rest_client._authorized_get.call_count == 1


@pytest.mark.asyncio
async def test_stale_event_readd_response_cannot_attest_new_epoch():
    bot, now, bars = await _seed()
    _scanner(bot, set())
    bot._watchlist.clear()
    bot._sync_line_epochs()
    bot._watchlist.add("RETO")
    bot._sync_line_epochs()
    ledger = bot._line_sessions["RETO"]
    ledger.invalidate_coverage()
    bot._request_line_repairs(_scanner(bot, {"RETO"}))
    started, release = asyncio.Event(), asyncio.Event()
    real = asyncio.to_thread

    async def blocked(fn, *args):
        started.set()
        await release.wait()
        return await real(fn, *args)

    from unittest.mock import patch
    with patch("project_mai_tai.services.schwab_1m_v2_bot.asyncio.to_thread", blocked):
        task = asyncio.create_task(bot._line_source_events_pass())
        await started.wait()
        _scanner(bot, set())
        _scanner(bot, {"RETO"})
        release.set()
        await task
    assert ledger._coverage is None and not bot._line_buy_ready("RETO")


@pytest.mark.asyncio
async def test_rest_incremental_fallback_not_shortcircuited_by_history_hook():
    bot, _, bars = await _seed()
    client = bot.rest_client
    client._session_request = Mock(side_effect=AssertionError("periodic anchored request"))
    client._fetch_recent_closed_bars = Mock(return_value=[bars[-1]])
    client._on_chart_bar = AsyncMock()
    client.set_desired_symbols({"RETO"})
    await client._bar_loop_pass(0)
    client._fetch_recent_closed_bars.assert_called_once_with("RETO", 0)
    client._on_chart_bar.assert_awaited_once()
    client._session_request.assert_not_called()


def test_flag_off_has_no_event_or_callback_provenance_side_effect():
    bot = _bot("RETO", _bars("RETO")[-1].timestamp_ms,
               strategy_schwab_1m_v2_line_chart_restoration_enabled=False)
    assert not _scanner(bot, {"RETO"})
    assert not bot._queue_line_source_event("RETO", "reconfirmed_hole")
    bot._observe_line_bar("RETO", _bars("RETO")[-1], source_callback=True)
    assert not bot._line_sessions and not bot._line_source_pending


@pytest.mark.asyncio
async def test_ten_later_closed_bars_cannot_certify_unknown_tail_even_without_tape():
    bot, now, bars = await _seed(clock="2026-10-05T11:10:00-04:00")
    ledger = bot._line_sessions["RETO"]
    seed = ledger._coverage
    tail = [bar for bar in _bars("RETO") if bar.timestamp_ms > bars[-1].timestamp_ms + 60_000]
    assert len(tail) > 10
    for bar in tail:
        now[0] = bar.timestamp_ms + 61_000
        await bot._handle_bar_from_streamer("RETO", bar)
        await bot._line_restoration_pass()
        assert ledger.prepare() is None and ledger.incomplete_reason == "live_tail_unproven"
        assert not bot._line_buy_ready("RETO") and not bot._line_source_pending
    assert ledger._coverage is seed and bot.rest_client._authorized_get.call_count == 1


@pytest.mark.asyncio
async def test_prefix_changed_then_reverted_still_requires_new_qualifying_event():
    bot, _, bars = await _seed()
    ledger = bot._line_sessions["RETO"]
    bot._observe_line_bar("RETO", replace(bars[20], volume=bars[20].volume + 1), source_callback=True)
    bot._observe_line_bar("RETO", bars[20], source_callback=True)
    await bot._line_restoration_pass()
    assert ledger.prepare() is None and ledger.incomplete_reason == "seed_invalidated"
    assert not bot._line_buy_ready("RETO") and not bot._line_source_pending
    bot._request_line_repairs(_scanner(bot, {"RETO"}, identity="09:02:00"))
    await bot._line_source_events_pass()
    await bot._line_restoration_pass()
    assert bot._line_buy_ready("RETO") and bot.rest_client._authorized_get.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["none", "value", "db_tail"])
async def test_db_fill_only_repairs_provider_listed_original_values_not_new_tail(damage):
    bot, now, bars = await _seed()
    ledger = bot._line_sessions["RETO"]
    seed = ledger._coverage
    # Controlled local missing-row state; the provider receipt is never edited.
    ledger._bars.pop(bars[20].timestamp_ms)
    ledger.revision += 1
    missing = replace(bars[20], volume=bars[20].volume + 1) if damage == "value" else bars[20]
    stored = [missing]
    if damage == "db_tail":
        tail = _bars("RETO", bars[-1].timestamp_ms + 60_000)[-1]
        now[0] = tail.timestamp_ms + 61_000
        bot._observe_line_bar("RETO", tail)
        bot.strategy.on_observed_bar("RETO", tail, observation_phase="replay")
        stored.append(tail)
    bot.session_factory = object()
    bot._read_line_session_bars = Mock(return_value=stored)
    bot._line_reconcile_pending.add("RETO")
    bot._line_dirty.add("RETO")
    await bot._line_restoration_pass()
    assert ledger._coverage is seed and bot.rest_client._authorized_get.call_count == 1
    assert bot._line_buy_ready("RETO") is (damage == "none")
    assert not bot._line_source_pending


@pytest.mark.asyncio
async def test_stale_endpoint_does_not_allow_quote_buy_without_new_bar():
    bot, now, _ = await _seed()
    now[0] += 120_000
    assert not bot._line_buy_ready("RETO") and not bot._line_source_pending


@pytest.mark.asyncio
async def test_unchanged_full_snapshot_identity_does_not_fetch_on_publication_or_quote_updates():
    bot, _, _ = await _seed()
    bot._strategy_state_event_is_current = lambda event: True  # Controlled recorded session.
    bot._try_complete_boot_state_restoration = Mock()
    for day, price in [(6, 3), (7, 4), (8, 5)]:
        event = StrategyStateSnapshotEvent(
            source_service="strategy-engine", produced_at=datetime(2026, 10, day, tzinfo=UTC),
            payload=StrategyStateSnapshotPayload(top_confirmed=[
                {"symbol": "RETO", "confirmed_at": "09:01:00", "price": price}
            ]),
        )
        bot._apply_strategy_state_event({"data": event.model_dump_json()}, max_watchlist=25)
    assert not bot._line_source_pending and bot.rest_client._authorized_get.call_count == 1


@pytest.mark.asyncio
async def test_source_waiting_keeps_quote_callback_protective_close_dispatch_both_accounts():
    bot, _, bars = await _seed()
    bot._line_source_waiting["RETO"] = bot._line_sessions["RETO"].epoch
    assert not bot._line_buy_ready("RETO")
    bot.intent_emitter, bot.webull_intent_emitter = RecordingEmitter(), RecordingEmitter()
    primary = TradeIntentDraft("RETO", "sell", "close", Decimal(1), "protective exit")
    mirror = TradeIntentDraft("RETO", "sell", "close", Decimal(1), "protective exit")
    buy = TradeIntentDraft("RETO", "buy", "open", Decimal(1), "ATR Flip")
    bot.strategy._pending_intents.extend([primary, buy])
    bot.strategy._pending_webull_direct_intents.append(mirror)
    # Control the upstream trigger, not source-waiting quote/drain/emit handling.
    bot.strategy.on_quote = Mock(return_value=None)
    bot._gap_hold_enabled = False
    close = bars[-1].close
    await bot._handle_quote("RETO", Quote("RETO", close, close, close, bot.strategy._now_ms()))
    assert bot.intent_emitter.intents == [primary]
    assert bot.webull_intent_emitter.intents == [mirror]


@pytest.mark.asyncio
async def test_confirmation_exact_once_on_live_append_and_held_target_expires_unanswerable(caplog):
    bot, now, bars = await _seed()
    target = bars[-1].timestamp_ms + 60_000
    entry = ConfirmationEntry(uuid4(), uuid4(), "fill", "order", "live:schwab_1m_v2", "RETO",
                              datetime.fromtimestamp((target - 60_000) / 1000, UTC), target,
                              1, None, datetime(1970, 1, 1, tzinfo=UTC))
    bot._confirmation_exit.add(entry)
    live = _bars("RETO", target)[-1]
    now[0] = target + 61_000
    await bot._handle_bar_from_streamer("RETO", live)
    await bot._line_restoration_pass()
    evaluations = [item for call in bot._emit_confirmation_evaluations.call_args_list
                   for item in call.args[0] if item.entry == entry]
    assert len(evaluations) == 1 and evaluations[0].bar_start_ms == target
    held = replace(entry, order_id=uuid4(), fill_id=uuid4(), evaluation_bar_start_ms=target + 60_000)
    bot._confirmation_exit.add(held)
    bot._observe_line_bar("RETO", replace(bars[20], volume=bars[20].volume + 1), source_callback=True)
    later = _bars("RETO", target + 120_000)[-1]
    now[0] = later.timestamp_ms + 61_000
    await bot._handle_bar_from_streamer("RETO", later)
    await bot._line_restoration_pass()
    assert not bot._confirmation_exit._pending
    assert any("reason=line_unproven" in row.message and str(held.fill_id) in row.message
               for row in caplog.records)
    assert not any(item.entry == held for call in bot._emit_confirmation_evaluations.call_args_list
                   for item in call.args[0])
