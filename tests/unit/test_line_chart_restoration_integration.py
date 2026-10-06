"""Recorded candles, real provider parser and service publication; no production I/O.

Provider responses are controlled. Neither stored candles nor these manifests
reconstruct historical provider completeness or broker fills.
"""

import asyncio
import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

from project_mai_tai.confirmation_exit import ConfirmationEntry
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, Quote, SchwabV2RestClient
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft
from project_mai_tai.strategy_core.session_line_restore import (
    SessionCoverage, SessionLineRestoration, build_session_line, history_fingerprint,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
RAW = json.loads((FIXTURES / "line_chart_restoration_bars.json").read_text())
CONTROLS = json.loads((FIXTURES / "line_chart_recorded_entry_controls.json").read_text())


def _ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def _bar(row):
    return ChartBar(row["symbol"], *(float(row[key]) for key in (
        "open_price", "high_price", "low_price", "close_price",
    )), int(row["volume"]), _ms(row["bar_time"]))


def _bars(symbol, through=None):
    return [_bar(row) for row in RAW["bars"] if row["symbol"] == symbol
            and (through is None or _ms(row["bar_time"]) <= through)]


def _payload(symbol, bars):
    return {"symbol": symbol, "empty": False, "candles": [
        {"datetime": bar.timestamp_ms, "open": bar.open, "high": bar.high,
         "low": bar.low, "close": bar.close, "volume": bar.volume} for bar in bars
    ]}


def _proof(ledger, bars):
    return SessionCoverage("schwab_rest_full_session", ledger.anchor_ms,
                           bars[-1].timestamp_ms + 60_000,
                           tuple(bar.timestamp_ms for bar in bars), True,
                           history_fingerprint(bars))


def _bot(symbol, current, **overrides):
    options = dict(
        strategy_schwab_1m_v2_line_chart_restoration_enabled=True,
        strategy_schwab_1m_v2_atr_flip_enabled=True,
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
    )
    options.update(overrides)
    bot = SchwabV2BotService(Settings(**options))
    bot.strategy._now_ms = lambda: current + 61_000
    bot._watchlist = {symbol}
    bot._sync_line_epochs()
    bot._emit_confirmation_evaluations = AsyncMock()
    return bot


def _ingest(bot, bars, *, live=True):
    for bar in bars:
        bot._observe_line_bar(bar.symbol, bar)
        bot.strategy.on_observed_bar(bar.symbol, bar, observation_phase="live" if live else "replay")
    symbol = bars[0].symbol
    if live:
        bot._line_live_bars[symbol] = {bar.timestamp_ms for bar in bars[-11:]}
        bot._confirmation_last_live_bar_ms[symbol] = bars[-1].timestamp_ms
    return bot._line_sessions[symbol]


@pytest.mark.parametrize("damage", ["symbol", "empty", "malformed", "duplicate", "current", "truncated"])
def test_provider_refuses_partial_or_malformed_anchored_response(damage):
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms)
    client = SchwabV2RestClient(bot.settings, on_chart_bar=AsyncMock(), on_quote=AsyncMock())
    payload = _payload("RETO", bars)
    if damage == "symbol":
        payload["symbol"] = "JAGX"
    elif damage == "empty":
        payload.pop("empty")
    elif damage == "malformed":
        payload["candles"][3].pop("high")
    elif damage == "duplicate":
        payload["candles"].append(payload["candles"][0])
    elif damage == "current":
        payload["candles"].pop()
    else:
        payload["truncated"] = True
    client._authorized_get = lambda url: payload
    with pytest.raises((ValueError, KeyError)):
        client.fetch_session_history("RETO", bot._line_sessions["RETO"].anchor_ms, bars[-1].timestamp_ms)


def test_provider_uses_exact_0400_anchor_without_cursor_or_250_bar_limit():
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms)
    ledger = bot._line_sessions["RETO"]
    client = SchwabV2RestClient(bot.settings, on_chart_bar=AsyncMock(), on_quote=AsyncMock())
    urls = []
    client._authorized_get = lambda url: urls.append(url) or _payload("RETO", bars)
    client._last_bar_timestamp_ms["RETO"] = bars[-1].timestamp_ms
    response, proof = client.fetch_session_history("RETO", ledger.anchor_ms, bars[-1].timestamp_ms)
    query = parse_qs(urlparse(urls[0]).query)
    assert query["startDate"] == [str(ledger.anchor_ms)]
    assert query["endDate"] == [str(bars[-1].timestamp_ms + 59_999)]
    assert len(response) == 255
    # Pinned directly from the retained 255-row RETO source, not the helper under test.
    assert proof.bars_sha256 == "2aacce66cc9821972334caf58c8035c5d6fb3e09a48c323acb7412333ccfa333"
    assert proof.closed_ids == tuple(bar.timestamp_ms for bar in bars)


@pytest.mark.asyncio
@pytest.mark.parametrize("minute", ["11:18", "11:21"])
async def test_service_reto_publishes_correct_math_and_confirmation_without_false_sell_or_rest(minute):
    current = _ms(f"2026-10-05T{minute}:00-04:00")
    bars = _bars("RETO", current)
    bot = _bot("RETO", current)
    ledger = _ingest(bot, bars)
    ledger.attest(_proof(ledger, bars))
    state = bot.strategy.watchlist_state("RETO")
    state.cw_resting_taken = state.cw_reclaim_taken = True
    state.fanout_webull_resting_taken = state.fanout_webull_reclaim_taken = True
    state.retry_one_closes_in_segment = 1
    state.atr_fired_in_short_seg = True
    assert not bot.strategy.line_buy_ready("RETO")
    assert await bot._rebuild_session_line("RETO", ledger)
    assert state.atr_state == "long"
    assert round(state.atr_trail, 4) == 2.0639
    assert bot._confirmation_bar_states[("RETO", current)] == "long"
    assert bot.strategy.line_buy_ready("RETO")
    assert state.cw_resting_taken and state.cw_reclaim_taken and state.atr_fired_in_short_seg
    assert state.fanout_webull_resting_taken and state.fanout_webull_reclaim_taken
    assert state.retry_one_closes_in_segment == 1
    assert not bot.strategy._pending_atr_sell_observations
    assert not bot.strategy.drain_pending_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,cut,current,late_count", [
    ("RETO", "2026-10-05T15:10:18+00:00", "2026-10-05T15:09:00+00:00", 16),
    ("JAGX", "2026-10-05T15:52:15+00:00", "2026-10-05T15:51:00+00:00", 33),
])
async def test_actual_service_rest_skip_still_records_late_history_and_admits_only_after_backfill(
    symbol, cut, current, late_count,
):
    pairs = [(row, _bar(row)) for row in RAW["bars"] if row["symbol"] == symbol
             and _ms(row["bar_time"]) <= _ms(current)]
    bars = [bar for row, bar in pairs]
    available = [bar for row, bar in pairs if _ms(row["created_at"]) <= _ms(cut)]
    late = [bar for row, bar in pairs if _ms(row["created_at"]) > _ms(cut)]
    assert len(late) == late_count
    assert all(row["source"] == "rest" for row, bar in pairs if bar in late)
    bot = _bot(symbol, _ms(current))
    ledger = _ingest(bot, available)
    ledger.attest(_proof(ledger, bars))
    assert not await bot._rebuild_session_line(symbol, ledger)
    assert not bot.strategy.line_buy_ready(symbol)
    bot._should_skip_rest_strategy_feed = lambda symbol, bar: True
    for bar in late:
        await bot._handle_bar_from_rest(symbol, bar)
    assert await bot._rebuild_session_line(symbol, ledger)
    assert ledger.current_bar_ms == _ms(current)
    assert bot.strategy.line_buy_ready(symbol)


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["epoch", "revision", "current", "coverage", "session", "gap_reset"])
async def test_inflight_worker_cannot_publish_after_any_identity_or_coverage_change(monkeypatch, fence):
    import project_mai_tai.services.schwab_1m_v2_bot as service_module

    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms)
    ledger = _ingest(bot, bars)
    ledger.attest(_proof(ledger, bars))
    started, release = threading.Event(), threading.Event()
    original = service_module.build_session_line
    callback_thread = threading.get_ident()
    workers = []

    def blocked(*args):
        workers.append(threading.get_ident())
        started.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(service_module, "build_session_line", blocked)
    task = asyncio.create_task(bot._rebuild_session_line("RETO", ledger))
    assert await asyncio.to_thread(started.wait, 5)
    if fence == "epoch":
        bot._watchlist = set()
        bot._sync_line_epochs()
        bot._watchlist = {"RETO"}
        bot._sync_line_epochs()
    elif fence == "revision":
        ledger.revision += 1
    elif fence == "current":
        ledger.current_bar_ms += 60_000
    elif fence == "coverage":
        ledger.invalidate_coverage()
    elif fence == "session":
        bot.strategy._now_ms = lambda: bars[-1].timestamp_ms + 86_400_000
    else:
        bot.strategy.watchlist_state("RETO").line_restore_reset_after_ms = bars[-1].timestamp_ms
    release.set()
    assert not await task
    assert workers == [workers[0]] and workers[0] != callback_thread
    assert not bot._line_published
    assert not bot._confirmation_bar_states
    assert not bot.strategy.line_buy_ready("RETO")


@pytest.mark.asyncio
async def test_readd_keeps_consumed_slots_but_requires_a_new_provider_epoch():
    bars = _bars("JAGX")
    bot = _bot("JAGX", bars[-1].timestamp_ms)
    ledger = _ingest(bot, bars)
    ledger.attest(_proof(ledger, bars))
    assert await bot._rebuild_session_line("JAGX", ledger)
    state = bot.strategy.watchlist_state("JAGX")
    state.cw_resting_taken = state.cw_reclaim_taken = True
    state.retry_one_closes_in_segment = 1
    bot._watchlist = set()
    bot._sync_line_epochs()
    bot.strategy.release_and_drop_symbol("JAGX")
    bot._watchlist = {"JAGX"}
    bot._sync_line_epochs()
    assert not bot.strategy.line_buy_ready("JAGX")
    assert bot.strategy.watchlist_state("JAGX") is state
    assert state.cw_resting_taken and state.cw_reclaim_taken and state.retry_one_closes_in_segment == 1
    new = _ingest(bot, bars)
    assert new.epoch != ledger.epoch
    assert not await bot._rebuild_session_line("JAGX", new)
    new.attest(_proof(new, bars))
    assert await bot._rebuild_session_line("JAGX", new)


@pytest.mark.asyncio
async def test_restart_never_inherits_admission_from_the_previous_process():
    bars = _bars("RETO")
    old = _bot("RETO", bars[-1].timestamp_ms)
    ledger = _ingest(old, bars)
    ledger.attest(_proof(ledger, bars))
    assert await old._rebuild_session_line("RETO", ledger)
    restarted = _bot("RETO", bars[-1].timestamp_ms)
    new = _ingest(restarted, bars)
    assert new.epoch != ledger.epoch
    assert not restarted.strategy.line_buy_ready("RETO")
    assert not await restarted._rebuild_session_line("RETO", new)
    new.attest(_proof(new, bars))
    assert await restarted._rebuild_session_line("RETO", new)
    assert not restarted.strategy._pending_atr_sell_observations


@pytest.mark.asyncio
async def test_real_provider_service_pass_publishes_full_history_off_callback(monkeypatch):
    bars = _bars("RETO", _ms("2026-10-05T11:21:00-04:00"))
    bot = _bot("RETO", bars[-1].timestamp_ms)
    _ingest(bot, bars[:-1])
    client = SchwabV2RestClient(bot.settings, on_chart_bar=bot._handle_bar_from_rest,
                                on_quote=AsyncMock())
    provider_threads = []

    def get(url):
        provider_threads.append(threading.get_ident())
        return _payload("RETO", bars)

    client._authorized_get = get
    client._session_request = bot._line_source_request
    client._on_session_history = bot._accept_line_source
    client._on_session_failure = bot._line_source_failure
    client.set_desired_symbols({"RETO"})
    bot.rest_client = client
    bot._persist_bar = lambda *args: None
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    await client._bar_loop_pass(15)
    await bot._line_restoration_pass()
    assert provider_threads[0] != threading.get_ident()
    assert len(bot._line_published["RETO"].request.bars) == len(bars)
    assert bot.strategy.line_buy_ready("RETO")
    assert round(bot.strategy.watchlist_state("RETO").atr_trail, 4) == 2.0639
    client._authorized_get = lambda url: {"symbol": "RETO", "empty": True, "candles": []}
    await client._bar_loop_pass(15)
    assert not bot.strategy.line_buy_ready("RETO")


def test_later_1127_recorded_line_has_a_distinct_cutoff_from_1118_and_1121():
    bars = _bars("RETO")
    assert bars[-1].timestamp_ms == _ms("2026-10-05T11:27:00-04:00")
    bot = _bot("RETO", bars[-1].timestamp_ms)
    ledger = _ingest(bot, bars)
    ledger.attest(_proof(ledger, bars))
    snapshot = build_session_line(ledger.prepare(), 5, 3.5)
    assert round(dict(snapshot.indicator)["atr_trail"], 4) == 2.2300


@pytest.mark.asyncio
async def test_sixteen_symbol_source_and_rebuild_cycles_do_not_wait_for_persistence(monkeypatch):
    bars = _bars("RETO", _ms("2026-10-05T11:21:00-04:00"))
    symbols = {f"LOAD{index:02}" for index in range(16)}
    bot = _bot("RETO", bars[-1].timestamp_ms)
    bot._watchlist = symbols
    bot._sync_line_epochs()
    release = asyncio.Event()
    callback_started = set()

    async def slow_callback(symbol, bar):
        callback_started.add(symbol)
        await release.wait()
        await bot._handle_bar_from_rest(symbol, bar)

    client = SchwabV2RestClient(bot.settings, on_chart_bar=slow_callback, on_quote=AsyncMock())
    active, maximum = [0], [0]
    lock = threading.Lock()

    def fetch(symbol, anchor, current):
        with lock:
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
        # Sixteen controlled copies of recorded RETO prices; network delay is
        # simulated. This is a capacity measurement, not sixteen market sessions.
        time.sleep(0.01)
        cloned = [replace(bar, symbol=symbol) for bar in bars]
        ledger = bot._line_sessions[symbol]
        with lock:
            active[0] -= 1
        return cloned, _proof(ledger, cloned)

    client.fetch_session_history = fetch
    client._session_request = bot._line_source_request
    client._on_session_history = bot._accept_line_source
    client._on_session_failure = bot._line_source_failure
    client.set_desired_symbols(symbols)
    bot._persist_bar = lambda *args: None
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    started = time.perf_counter()
    poll = asyncio.create_task(client._bar_loop_pass(15))
    worker = asyncio.create_task(bot._line_restoration_loop())
    try:
        async with asyncio.timeout(5):
            while not all(bot.strategy.line_buy_ready(symbol) for symbol in symbols):
                await asyncio.sleep(0.005)
        elapsed = time.perf_counter() - started
        assert callback_started == symbols and maximum[0] <= 4
        assert not release.is_set() and not poll.done()
        assert all(round(bot.strategy.watchlist_state(symbol).atr_trail, 4) == 2.0639 for symbol in symbols)
        print(f"16-symbol controlled source-to-admission: {elapsed:.3f}s; max_fetch_concurrency={maximum[0]}")
        release.set()
        await poll
    finally:
        release.set()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_real_late_rest_callback_wakes_worker_and_publishes_without_replaying_history():
    current = _ms("2026-10-05T11:21:00-04:00")
    pairs = [(row, _bar(row)) for row in RAW["bars"] if row["symbol"] == "RETO"
             and _ms(row["bar_time"]) <= current]
    available = [bar for row, bar in pairs if not (
        row["source"] == "rest" and "2026-10-05T14:49" <= row["bar_time"][:16].replace(" ", "T") <= "2026-10-05T15:04"
    )]
    late = [bar for row, bar in pairs if bar not in available]
    assert len(late) == 16
    bot = _bot("RETO", current)
    ledger = _ingest(bot, available)
    ledger.attest(_proof(ledger, [bar for row, bar in pairs]))
    bot._should_skip_rest_strategy_feed = lambda symbol, bar: True
    worker = asyncio.create_task(bot._line_restoration_loop())
    try:
        await bot._handle_bar_from_rest("RETO", late[0])
        await asyncio.sleep(0.01)
        assert not bot.strategy.line_buy_ready("RETO")
        started = time.perf_counter()
        for bar in late[1:]:
            await bot._handle_bar_from_rest("RETO", bar)
        async with asyncio.timeout(5):
            while not bot.strategy.line_buy_ready("RETO"):
                await asyncio.sleep(0.005)
        elapsed = time.perf_counter() - started
        print(f"RETO actual late-REST callback-to-publication: {elapsed:.3f}s")
        assert round(bot.strategy.watchlist_state("RETO").atr_trail, 4) == 2.0639
        assert not bot.strategy._pending_atr_sell_observations
        assert not bot.strategy.drain_pending_intents()
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_unchanged_adjacent_recorded_sell_is_delivered_once_and_boot_hold_survives():
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms)
    ledger = _ingest(bot, bars)
    ledger.attest(_proof(ledger, bars))
    rows = build_session_line(ledger.prepare(), 5, 3.5).confirmation
    states = dict(rows)
    index = next(i for i, bar in enumerate(bars) if i > 10
                 and states.get(bars[i - 1].timestamp_ms) == "long"
                 and states.get(bar.timestamp_ms) == "short"
                 and bars[i - 1].timestamp_ms + 60_000 == bar.timestamp_ms)
    bot = _bot("RETO", bars[index].timestamp_ms)
    bot._drain_atr_sell_observations = AsyncMock()
    ledger = _ingest(bot, bars[:index])
    ledger.attest(_proof(ledger, bars[:index]))
    assert await bot._rebuild_session_line("RETO", ledger)
    assert not bot.strategy._pending_atr_sell_observations
    held_before = bot.strategy._entries_held
    _ingest(bot, bars[index:index + 1])
    assert not bot.strategy.line_buy_ready("RETO")
    ledger.attest(_proof(ledger, bars[:index + 1]))
    assert await bot._rebuild_session_line("RETO", ledger)
    assert len(bot.strategy._pending_atr_sell_observations) == 1
    assert bot.strategy._entries_held == held_before
    assert await bot._rebuild_session_line("RETO", ledger)
    assert len(bot.strategy._pending_atr_sell_observations) == 1


@pytest.mark.asyncio
async def test_revision_of_current_recorded_bar_repairs_math_without_replaying_exit():
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms)
    ledger = _ingest(bot, bars)
    ledger.attest(_proof(ledger, bars))
    assert await bot._rebuild_session_line("RETO", ledger)
    old_version = bot._line_version("RETO")
    # The previous recorded minute's prices are a controlled correction scenario,
    # not a claim that this revision actually occurred in the provider response.
    old, current = bars[-2:]
    revised = ChartBar("RETO", old.open, old.high, old.low, old.close,
                       old.volume, current.timestamp_ms)
    bot._observe_line_bar("RETO", revised)
    assert not bot.strategy.line_buy_ready("RETO")
    corrected = bars[:-1] + [revised]
    ledger.attest(_proof(ledger, corrected))
    assert await bot._rebuild_session_line("RETO", ledger)
    assert bot._line_version("RETO") != old_version
    assert bot.strategy.watchlist_state("RETO").bars[-1].close == revised.close
    assert not bot.strategy._pending_atr_sell_observations


@pytest.mark.asyncio
async def test_gap_hold_rebuild_uses_existing_reset_and_clean_ten_bar_wait():
    from tests.unit.test_schwab_1m_v2_gap_hold import _chart_bar, MIDDAY_MS

    bars = [_chart_bar(MIDDAY_MS + index * 60_000, 2.5) for index in range(12)]
    bot = _bot("BENF", bars[-1].timestamp_ms, strategy_schwab_1m_v2_gap_hold_enabled=True)
    ledger = _ingest(bot, bars[:1])
    assert bot.strategy.begin_gap_hold(
        "BENF", detected_at_ms=MIDDAY_MS + 60_000,
        last_bar_age_s=91, last_print_age_s=1,
    )
    _ingest(bot, bars[2:11])
    state = bot.strategy.watchlist_state("BENF")
    assert state.gap_hold_active and state.gap_hold_contiguous_bars == 9
    ledger.attest(_proof(ledger, bars[:1] + bars[2:11]))
    assert await bot._rebuild_session_line("BENF", ledger)
    assert state.gap_hold_active and not bot.strategy.line_buy_ready("BENF")
    _ingest(bot, bars[11:])
    ledger.attest(_proof(ledger, bars[:1] + bars[2:]))
    assert not state.gap_hold_active and state.gap_hold_contiguous_bars == 10
    assert await bot._rebuild_session_line("BENF", ledger)
    assert bot._line_published["BENF"].snapshot.reset_after_ms == MIDDAY_MS + 60_000
    assert bot.strategy.line_buy_ready("BENF")


@pytest.mark.asyncio
async def test_anchored_source_current_callbacks_advance_gap_hold_without_double_counting():
    from tests.unit.test_schwab_1m_v2_gap_hold import _chart_bar, MIDDAY_MS

    bars = [_chart_bar(MIDDAY_MS + index * 60_000, 2.5) for index in range(12)]
    bot = _bot("BENF", bars[-1].timestamp_ms, strategy_schwab_1m_v2_gap_hold_enabled=True)
    ledger = _ingest(bot, bars[:1])
    bot._rest_warmup_done.add("BENF")
    assert bot.strategy.begin_gap_hold(
        "BENF", detected_at_ms=MIDDAY_MS + 60_000, last_bar_age_s=91, last_print_age_s=1,
    )
    for end in range(3, 13):
        response = bars[:1] + bars[2:end]
        assert bot._accept_line_source("BENF", ledger.epoch, response, _proof(ledger, response))
    state = bot.strategy.watchlist_state("BENF")
    assert state.gap_hold_contiguous_bars == 10 and not state.gap_hold_active
    assert bot._accept_line_source("BENF", ledger.epoch, response, _proof(ledger, response))
    assert state.gap_hold_contiguous_bars == 10
    assert await bot._rebuild_session_line("BENF", ledger)
    assert bot.strategy.line_buy_ready("BENF")


def test_rpg_authorization_refuses_replacement_without_current_line():
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms)
    _ingest(bot, bars)
    job = {"old": {"symbol": "RETO", "broker_account_name": "live:test"},
           "slot": "first", "segment_id": 1, "phase": "expired"}
    auth = bot.strategy.rpg_handoff_authorization("test", job)
    assert auth["verdict"] == "wait" and auth["reason"] == "session_line_unproven"


@pytest.mark.asyncio
async def test_direct_drains_reject_stale_opens_and_deliver_cancellations():
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms)
    ledger = _ingest(bot, bars)
    ledger.attest(_proof(ledger, bars))
    assert await bot._rebuild_session_line("RETO", ledger)
    version = bot._line_version("RETO")
    drafts = [TradeIntentDraft("RETO", "buy", kind, Decimal(1), "ATR Flip",
                              {"line_restore_version": version}) for kind in ("open", "cancel")]
    bot.strategy._pending_intents.extend(drafts)
    bot.strategy._pending_webull_direct_intents.extend(drafts)
    ledger.invalidate_coverage()
    bot.intent_emitter = type("Emitter", (), {"emit": AsyncMock()})()
    bot.webull_intent_emitter = type("Emitter", (), {"emit": AsyncMock()})()
    await bot._drain_direct_strategy_intents()
    assert bot.intent_emitter.emit.call_args.args[0].intent_type == "cancel"
    assert bot.webull_intent_emitter.emit.call_args.args[0].intent_type == "cancel"
    assert bot.intent_emitter.emit.call_count == bot.webull_intent_emitter.emit.call_count == 1


def test_incomplete_line_blocks_both_quote_crosses_reclaim_rest_and_reprice():
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms)
    _ingest(bot, bars)
    strategy = bot.strategy
    state = strategy.watchlist_state("RETO")
    state.resting_active = True
    state.resting_level = state.resting_trigger = 2.0
    quote = Quote("RETO", 2.1, 2.2, 2.2, strategy._now_ms())
    assert strategy.on_quote("RETO", quote) is None
    assert strategy._eh_resting_cross_check(state, quote) is None
    assert strategy._cw_v2_quote(state, quote) is None
    strategy._fanout_rth_resting_cross(state, quote)
    strategy._cw_v2_reclaim_resting_track(state)
    strategy._reprice_resting(state, 1.9)
    strategy._queue_resting_place(state, 1.9)
    assert not strategy.drain_pending_intents()
    assert not strategy.drain_webull_fanout_intents()
    assert state.resting_level == 2.0


@pytest.mark.asyncio
async def test_confirmation_corrects_only_pending_targets_and_does_not_repeat_issued_exit():
    bars = _bars("RETO", _ms("2026-10-05T11:18:00-04:00"))
    bot = _bot("RETO", bars[-1].timestamp_ms)
    ledger = _ingest(bot, bars)
    ledger.attest(_proof(ledger, bars))
    current = bars[-1].timestamp_ms
    entry = ConfirmationEntry(uuid4(), uuid4(), "fill", "order", "live:test", "RETO",
                              datetime.fromtimestamp((current - 60_000) / 1000, UTC), current,
                              1, None, datetime(1970, 1, 1, tzinfo=UTC))
    bot._confirmation_exit.add(entry)
    bot._confirmation_bar_states[("RETO", current)] = "short"
    assert await bot._rebuild_session_line("RETO", ledger)
    evaluations = [item for call in bot._emit_confirmation_evaluations.call_args_list
                   for item in call.args[0]]
    assert len(evaluations) == 1
    assert evaluations[0].atr_state == "long" and not evaluations[0].should_exit
    ledger.invalidate_coverage()
    ledger.attest(_proof(ledger, bars))
    assert await bot._rebuild_session_line("RETO", ledger)
    evaluations = [item for call in bot._emit_confirmation_evaluations.call_args_list
                   for item in call.args[0]]
    assert len(evaluations) == 1


@pytest.mark.parametrize("control", CONTROLS["controls"], ids=lambda value: f'{value["symbol"]}-{value["timestamp_ms"]}')
def test_thirteen_recorded_entry_math_controls(control):
    day = control["probe_at"][:10]
    bars = sorted([_bar(row) for row in CONTROLS["bars"][day]
                   if row["symbol"] == control["symbol"]
                   and _ms(row["bar_time"]) <= control["timestamp_ms"]], key=lambda bar: bar.timestamp_ms)
    assert bars and all("source" in row and "created_at" in row for row in CONTROLS["bars"][day])
    bot = _bot(control["symbol"], control["timestamp_ms"])
    ledger = SessionLineRestoration(control["symbol"], bot._line_sessions[control["symbol"]].anchor_ms, 1)
    for bar in bars:
        ledger.observe(bar)
    ledger.attest(_proof(ledger, bars))
    snapshot = build_session_line(ledger.prepare(), 5, 3.5)
    math = dict(snapshot.indicator)
    assert math["atr_state"] == control["state"]
    assert round(math["atr_trail"], 4) == round(control["trail"], 4)
