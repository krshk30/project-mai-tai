"""C1 17:09 controlled source contracts, NOT IZM/oracle/native acceptance.

The waiting-order preimage is explicit, with entries held and unreadable stores.
No test manufactures a BUY placement or relaxes a permission predicate.
"""

import asyncio
from copy import deepcopy
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.market_data.keepwatch_history import (
    KeepWatchHistory, history_coverage, project_history,
)
from project_mai_tai.market_data.keepwatch_producer import SessionProducerPins
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, SchwabV2RestClient
from project_mai_tai.market_data.schwab_v2_streamer import SchwabV2Streamer
from project_mai_tai.services.keepwatch_bar_owner import BarInbox, OwnedBar
from project_mai_tai.services.keepwatch_halt_proof import (
    attested_empty_interval, attested_repaired_interval,
)
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.session_line_restore import (
    PRODUCER_PROJECTION_SOURCE, SessionCoverage, SessionLineRestoration, history_fingerprint,
)

ANCHOR = int(datetime(2026, 10, 13, 8, tzinfo=UTC).timestamp() * 1000)


def candle(index=0, *, producer="massive", close=10.0):
    return ChartBar("IZM", close, close + .05, close - .05, close,
                    20_000, ANCHOR + index * 60_000, producer=producer)


def response(massive, schwab, *, through=None, now=None):
    merged = {bar.timestamp_ms: bar for bar in massive}
    merged.update((bar.timestamp_ms, bar) for bar in schwab)
    if through is None:
        through = max(merged)
    if now is None:
        now = through + 61_000
    return KeepWatchHistory("IZM", ANCHOR, through,
        tuple(merged[stamp] for stamp in sorted(merged)),
        tuple(stamp for stamp in range(ANCHOR, through + 1, 60_000) if stamp not in merged),
        now * 1_000_000, now * 1_000_000, "a" * 64, history_fingerprint(schwab),
        tuple(bar.timestamp_ms for bar in massive), tuple(bar.timestamp_ms for bar in schwab),
        tuple(massive), tuple(schwab), history_fingerprint(massive))


@pytest.mark.parametrize("first", ["massive", "schwab"])
def test_cross_producer_keeps_first_prices_and_both_immutable_candidates(first):
    pins = SessionProducerPins("IZM", ANCHOR)
    original = candle(producer=first)
    other = replace(original, producer="schwab" if first == "massive" else "massive",
                    close=9.99, volume=1)
    before = pins.accept(original, now_ms=ANCHOR + 60_000)
    assert pins.accept(other, now_ms=ANCHOR + 60_001) == before
    assert pins.producers[ANCHOR] == first
    assert set(pins.candidates[ANCHOR]) == {"massive", "schwab"}
    other.close = 9.98
    assert pins.candidates[ANCHOR][other.producer].close == 9.99
    assert pins.current(ANCHOR).volume == original.volume


@pytest.mark.parametrize("field,value", [("close", 9.99), ("high", 11), ("low", 9)])
def test_same_producer_price_revision_is_not_pinned_to_first_value(field, value):
    pins = SessionProducerPins("IZM", ANCHOR)
    original = candle()
    pins.accept(original, now_ms=ANCHOR + 60_000)
    revised = replace(original, **{field: value})
    assert getattr(pins.accept(revised, now_ms=ANCHOR + 60_001), field) == value
    assert pins.revisions[(ANCHOR, "massive")] == 2


@pytest.mark.parametrize("mutation", ["unknown", "future", "foreign", "outside", "nonfinite"])
def test_bad_candidate_refuses_without_retiring_or_replacing_pin(mutation):
    pins = SessionProducerPins("IZM", ANCHOR)
    original = candle()
    pins.accept(original, now_ms=ANCHOR + 60_000)
    bad = {"unknown": replace(original, producer=None),
        "future": candle(1), "foreign": replace(original, symbol="CLWT"),
        "outside": replace(original, timestamp_ms=ANCHOR + 57_600_000),
        "nonfinite": replace(original, high=float("nan"))}[mutation]
    with pytest.raises(ValueError):
        pins.accept(bad, now_ms=ANCHOR + 60_001)
    assert pins.current(ANCHOR) == original
    assert len(pins.producers) == 1


def test_full_session_memory_is_960_minutes_two_producers_no_cross_session_pin():
    pins = SessionProducerPins("IZM", ANCHOR)
    now = ANCHOR + 57_600_000
    for index in range(960):
        for producer in ("massive", "schwab"):
            pins.accept(candle(index, producer=producer), now_ms=now)
    assert len(pins.producers) == len(pins.candidates) == 960
    assert sum(map(len, pins.candidates.values())) == len(pins.revisions) == 1920
    next_session = SessionProducerPins("IZM", ANCHOR + 86_400_000)
    assert not next_session.producers
    with pytest.raises(ValueError):
        next_session.accept(candle(), now_ms=ANCHOR + 86_460_000)


def test_projection_keeps_raw_candidate_proof_and_accepts_a_massive_hole():
    first, repaired = candle(), candle(1)
    last = candle(2, producer="schwab")
    raw = response((first, repaired), (replace(first, producer="schwab", close=9.99), last))
    raw_before = deepcopy(raw)
    pins = SessionProducerPins("IZM", ANCHOR)
    pins.accept(first, now_ms=ANCHOR + 60_000)
    projected = project_history(raw, pins, now_ms=last.timestamp_ms + 61_000)
    assert raw == raw_before and raw.massive_sha256 == raw_before.massive_sha256
    assert projected[0].close == first.close != raw.bars[0].close
    assert projected[1] == repaired and pins.producers[repaired.timestamp_ms] == "massive"
    assert projected[-1].producer == "schwab"
    assert len(pins.candidates[ANCHOR]) == 2


def test_incomplete_candidate_provenance_cannot_be_projected_or_mutate_pins():
    raw = response((candle(),), ())
    pins = SessionProducerPins("IZM", ANCHOR)
    with pytest.raises(ValueError):
        project_history(replace(raw, massive_bars=()), pins, now_ms=ANCHOR + 61_000)
    assert not pins.producers
    with pytest.raises(ValueError):
        project_history(replace(raw, massive_values_sha256="f" * 64), pins,
                        now_ms=ANCHOR + 61_000)
    assert not pins.producers
    with pytest.raises(ValueError):
        project_history(replace(raw, schwab_sha256="f" * 64), pins, now_ms=ANCHOR + 61_000)
    assert not pins.producers


def test_stored_rows_do_not_invent_provider_identity_or_revision():
    pins = SessionProducerPins("IZM", ANCHOR)
    original = candle()
    pins.accept(original, now_ms=ANCHOR + 61_000)
    unknown = replace(original, producer=None, close=9.99)
    assert pins.stored_bar(unknown, now_ms=ANCHOR + 61_000) == original
    assert pins.stored[ANCHOR][3] == 9.99
    assert pins.revisions[(ANCHOR, "massive")] == 1
    with pytest.raises(ValueError):
        pins.stored_bar(candle(1, producer=None), now_ms=ANCHOR + 121_000)
    assert ANCHOR + 60_000 not in pins.producers


def test_owned_bar_copy_preserves_explicit_producer_and_freezes_raw_values():
    raw = candle()
    work = OwnedBar(raw, "rest", ANCHOR, 1)
    raw.close = 9.99
    assert work.bar.producer == "massive" and work.bar.close == 10
    assert work.source == "rest"  # Transport is not the producer identity.


def test_other_producer_canonical_duplicate_keeps_inflight_identity_but_real_revision_replaces():
    pins = SessionProducerPins("IZM", ANCHOR)
    first = pins.accept(candle(), now_ms=ANCHOR + 60_000)
    inbox = BarInbox(500)
    work = OwnedBar(first, "rest", ANCHOR, 1)
    assert inbox.offer(work)
    duplicate = pins.accept(replace(first, producer="schwab", close=9.99),
                            now_ms=ANCHOR + 60_001)
    assert inbox.offer(OwnedBar(duplicate, "rest", ANCHOR, 1))
    assert inbox.pending[ANCHOR] is work
    actual_revision = pins.accept(replace(first, close=9.99), now_ms=ANCHOR + 60_002)
    revised = OwnedBar(actual_revision, "rest", ANCHOR, 1)
    assert inbox.offer(revised) and inbox.pending[ANCHOR] is revised
    inbox.complete(work)
    assert inbox.pending[ANCHOR] is revised
    inbox.complete(revised)
    assert inbox.received == 3 and inbox.accepted == inbox.consumed == 1
    assert inbox.revised == 2 and not inbox.pending and inbox.rejected == inbox.failures == 0


def test_chart_bar_legacy_constructor_equality_repr_and_asdict_are_unchanged():
    legacy = ChartBar("IZM", 10, 10.05, 9.95, 10, 20_000, ANCHOR)
    tagged = replace(legacy, producer="massive")
    assert legacy == tagged and repr(legacy) == repr(tagged)
    names = [field.name for field in fields(legacy)]
    assert names == ["symbol", "open", "high", "low", "close", "volume", "timestamp_ms"]
    assert asdict(legacy) == asdict(tagged) == dict(zip(names, (
        "IZM", 10, 10.05, 9.95, 10, 20_000, ANCHOR)))
    assert tagged.producer == replace(tagged, close=9.99).producer == "massive"
    assert "producer" not in vars(legacy)
    # Explicit metadata lives in the instance, not the legacy dataclass wire map.
    assert vars(tagged)["producer"] == "massive"


def test_real_stream_parser_attaches_native_schwab_identity_without_value_change():
    bar = SchwabV2Streamer._extract_chart_bar({"key": "IZM", "2": 10, "3": 10.05,
        "4": 9.95, "5": 10, "6": 20_000, "7": ANCHOR})
    assert bar.producer == "schwab"
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.timestamp_ms) == (
        10, 10.05, 9.95, 10, 20_000, ANCHOR)


def test_real_rest_parser_and_streamer_are_one_producer_not_two_transport_pins():
    client = SchwabV2RestClient(Settings(), on_chart_bar=AsyncMock(), on_quote=AsyncMock())
    stream = SchwabV2Streamer._extract_chart_bar({"key": "IZM", "2": 10, "3": 10.05,
        "4": 9.95, "5": 10, "6": 20_000, "7": ANCHOR})
    parsed, proof = client._parse_session_history("IZM", ANCHOR, ANCHOR,
        {"symbol": "IZM", "empty": False, "candles": [{"datetime": ANCHOR,
            "open": 10, "high": 10.05, "low": 9.95, "close": 9.99, "volume": 20_000}]},
        allow_sparse=True)
    assert parsed[0].producer == stream.producer == "schwab"
    # SHA256 of literal [["IZM",1791878400000,10.0,10.05,9.95,9.99,20000]].
    assert proof.bars_sha256 == "85773763c8f060aa5c8322938149ca52270bab91b39521cfa4ab2601bb58d007"
    pins = SessionProducerPins("IZM", ANCHOR)
    pins.accept(stream, now_ms=ANCHOR + 60_000)
    assert pins.accept(parsed[0], now_ms=ANCHOR + 60_001).close == 9.99
    assert pins.revisions[(ANCHOR, "schwab")] == 2


async def published(*, enabled=True, bars=None):
    if bars is None:
        bars = tuple(candle(index, close=10 - index * .1) for index in range(40))
    bot = SchwabV2BotService(Settings(
        strategy_schwab_1m_v2_keepwatch1_enabled=enabled,
        strategy_schwab_1m_v2_line_chart_restoration_enabled=True,
        strategy_schwab_1m_v2_atr_flip_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_false_flip_enabled=True,
    ))
    bot.strategy._now_ms = lambda: bars[-1].timestamp_ms + 61_000
    bot._watchlist = {"IZM"}  # CONTROLLED membership; no native/entry proof.
    bot._configure_flip_entry_ownership_store({})  # Actual no-factory UNKNOWN path.
    # Real unreadable-store initializer; never claim an empty readable budget.
    bot.strategy.configure_falseflip({}, readable=False)
    bot._sync_line_epochs()
    bot._emit_confirmation_evaluations = AsyncMock()
    for bar in bars:
        bot._observe_line_bar("IZM", bar)
        bot.strategy.on_observed_bar("IZM", bar, observation_phase="replay")
    ledger = bot._line_sessions["IZM"]
    raw = response(bars, ())
    ledger.attest(history_coverage(raw, tuple(ledger._bars[bar.timestamp_ms] for bar in bars),
                                   canonical=enabled))
    if enabled:
        bot._keepwatch_proven["IZM"] = raw
    assert await bot._rebuild_session_line("IZM", ledger)
    state = bot.strategy.watchlist_state("IZM")
    assert state.atr_state == "short" and bot.strategy._entries_held
    assert not bot.strategy._flip_owner_restore_readable
    # Cancellation-only preimage, NOT a claimed admission/placement/fill.
    state.resting_active = state.webull_resting_active = True
    state.resting_is_broker_order = True
    state.resting_level = state.atr_trail
    state.resting_trigger = state.atr_trail + .01
    state.resting_schwab_quantity, state.resting_webull_quantity = 100, 50
    queued = []
    actual_cancel = bot.strategy._queue_resting_cancel

    def observed(actual_state, *, reason):
        before = len(bot.strategy._pending_intents)
        mirror_before = len(bot.strategy._pending_webull_direct_intents)
        actual_cancel(actual_state, reason=reason)
        queued.extend(bot.strategy._pending_intents[before:])
        queued.extend(bot.strategy._pending_webull_direct_intents[mirror_before:])

    bot.strategy._queue_resting_cancel = observed  # One delegating witness.
    return bot, ledger, bars, raw, state, queued


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("close", 6.09), ("high", 7), ("low", 5)])
async def test_real_ingress_owner_cross_producer_is_covered_without_cancel_or_raw_loss(field, value):
    bot, ledger, bars, raw, state, queued = await published()
    previous = bot._line_published["IZM"]
    changed = replace(bars[-1], producer="schwab", **{field: value})
    bot._enqueue_keepwatch_bar("IZM", changed, "rest")
    await asyncio.wait_for(asyncio.shield(bot._keepwatch_bar_tasks["IZM"]), 3)
    inbox = bot._keepwatch_bar_inboxes["IZM"]
    assert inbox.received == inbox.accepted == inbox.covered == 1
    assert not inbox.pending and inbox.rejected == inbox.failures == 0
    assert getattr(bot._keepwatch_producer_pins["IZM"].candidates[changed.timestamp_ms]["schwab"], field) == value
    assert ledger._bars[changed.timestamp_ms].close == bars[-1].close
    assert await bot._rebuild_session_line("IZM", ledger)
    assert bot._line_published["IZM"] is previous
    assert state.resting_active and state.webull_resting_active and not queued
    assert bot.strategy._entries_held


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["close", "high_low"])
async def test_real_same_producer_revision_rebuild_still_cancels_both_legs(kind):
    bot, ledger, bars, raw, state, queued = await published()
    old = bot._line_published["IZM"]
    changes = {"close": bars[-1].close - .04} if kind == "close" else {
        "high": bars[-1].high + 3, "low": bars[-1].low - 3}
    revised = (*bars[:-1], replace(bars[-1], **changes))
    bot._observe_line_bar("IZM", revised[-1])
    fresh = response(revised, ())
    projected = project_history(fresh, ledger.producer_pins, now_ms=bot.strategy._now_ms())
    for row in projected:
        ledger.observe(row)
    ledger.attest(history_coverage(fresh, projected, canonical=True))
    assert await bot._rebuild_session_line("IZM", ledger)
    assert bot._line_published["IZM"].request.revision != old.request.revision
    assert not state.resting_active and not state.webull_resting_active
    assert len(queued) == 2 and all(draft.intent_type == "cancel"
        and draft.metadata["reason"] == "line_history_changed" for draft in queued)
    assert bot.strategy._entries_held


@pytest.mark.asyncio
async def test_old_coverage_cannot_attest_a_same_producer_price_revision():
    bot, ledger, bars, raw, state, queued = await published()
    old = bot._line_published["IZM"]
    bot._observe_line_bar("IZM", replace(bars[-1], close=bars[-1].close - .04))
    assert not await bot._rebuild_session_line("IZM", ledger)
    assert ledger.incomplete_reason == "source_values_unproven"
    assert bot._line_published["IZM"] is old and state.resting_active and not queued


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["epoch", "reset"])
async def test_price_pins_do_not_waive_actual_reset_or_epoch_cancellation(guard):
    bot, ledger, bars, raw, state, queued = await published()
    if guard == "epoch":
        ledger.epoch += 1
    else:
        state.line_restore_reset_after_ms = ANCHOR + 60_000
    assert await bot._rebuild_session_line("IZM", ledger)
    assert not state.resting_active and queued[0].metadata["reason"] == "line_history_changed"
    assert bot.strategy._entries_held


@pytest.mark.asyncio
async def test_ingress_same_producer_revision_invalidates_cas_before_owner_awaits():
    bot, ledger, bars, raw, state, queued = await published()
    before = bot._line_published["IZM"]
    # Explicit failed transport, not a positive native/provider capability.
    bot._read_keepwatch_history_owned = AsyncMock(side_effect=ValueError("CONTROLLED_UNREADABLE"))
    bot._enqueue_keepwatch_bar("IZM", replace(bars[-1], close=bars[-1].close - .04), "rest")
    assert ledger.revision != before.request.revision
    assert ledger.admit(before) is None
    await bot._stop_keepwatch_bar_owners()
    assert not bot._keepwatch_native_reads
    assert len(bot._keepwatch_bar_inboxes["IZM"].pending) == 1  # UNKNOWN, not fake drain.


@pytest.mark.asyncio
async def test_unknown_rest_producer_is_refused_not_assumed_schwab_or_native_flat():
    bot, ledger, bars, raw, state, queued = await published()
    before = bot._line_published["IZM"]
    bot._enqueue_keepwatch_bar("IZM", replace(bars[-1], producer=None), "rest")
    inbox = bot._keepwatch_bar_inboxes["IZM"]
    assert inbox.received == inbox.rejected == 1 and not inbox.accepted
    assert "IZM" in bot._keepwatch_unknown
    assert not bot._keepwatch_bar_tasks
    assert bot._line_published["IZM"] is before and bot.strategy._entries_held


@pytest.mark.asyncio
async def test_original_response_hash_is_checked_before_canonical_source_projection():
    bot, ledger, bars, raw, state, queued = await published()
    before = bot._line_published["IZM"]
    revision = ledger.revision
    native = tuple(replace(bar, producer="schwab") for bar in bars)
    bad = SessionCoverage("schwab_rest_full_session", ANCHOR,
        native[-1].timestamp_ms + 60_000, tuple(bar.timestamp_ms for bar in native),
        True, "f" * 64, prefix_complete=True)
    assert not bot._accept_line_source("IZM", ledger.epoch, native, bad)
    assert ledger.revision == revision and bot._line_published["IZM"] is before
    assert ledger.raw_producer_coverage is None


@pytest.mark.asyncio
async def test_canonical_source_is_distinct_and_original_provider_attestation_is_immutable():
    # Explicit40-row input makes the expected canonical bytes independent of
    # both production fingerprint code and a generated floating-price fixture.
    fixture = Path(__file__).parents[1] / "fixtures/keepwatch/producer_literal_history40.json"
    rows = json.loads(fixture.read_text())
    fixed = tuple(ChartBar(row[0], *row[2:], row[1], producer="massive") for row in rows)
    bot, ledger, bars, raw, state, queued = await published(bars=fixed)
    native = tuple(replace(bar, producer="schwab", close=bar.close - .01) for bar in bars)
    original = SessionCoverage("schwab_rest_full_session", ANCHOR,
        native[-1].timestamp_ms + 60_000, tuple(bar.timestamp_ms for bar in native),
        True, history_fingerprint(native), prefix_complete=True)
    original_hash = original.bars_sha256
    assert bot._accept_line_source("IZM", ledger.epoch, native, original)
    assert ledger.raw_producer_coverage is original
    assert original.source == "schwab_rest_full_session" and original.bars_sha256 == original_hash
    derived = ledger._coverage
    assert derived.source == PRODUCER_PROJECTION_SOURCE
    assert derived.raw_source == original.source and derived.raw_bars_sha256 == original_hash
    assert derived.raw_closed_ids == original.closed_ids
    # Independent SHA256 of compact literal fixture bytes, excluding its newline.
    assert derived.bars_sha256 == "39bda40c60000aad19da9d7446b22b6ce71f423f91c1b495e5b2052f17d37574"
    assert derived.bars_sha256 != original_hash
    assert ledger.prepare() is not None and bot.strategy._entries_held


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["raw_hash", "raw_source", "raw_ids", "producer_receipts"])
async def test_canonical_coverage_cannot_erase_its_raw_provenance(missing):
    bot, ledger, bars, raw, state, queued = await published()
    proof = ledger._coverage
    changes = {"raw_hash": {"raw_bars_sha256": None},
        "raw_source": {"raw_source": "UNKNOWN"},
        "raw_ids": {"raw_closed_ids": tuple(reversed(proof.raw_closed_ids))},
        "producer_receipts": {"raw_provider_values": ()}}[missing]
    ledger.attest(replace(proof, **changes))
    assert ledger.prepare() is None
    assert bot.strategy._entries_held and state.resting_active and not queued


@pytest.mark.asyncio
async def test_boot_stored_rows_without_pins_refuse_then_real_prefix_recovers_checked_state():
    bars = tuple(candle(index, close=10 - index * .1) for index in range(40))
    bot = SchwabV2BotService(Settings(strategy_schwab_1m_v2_keepwatch1_enabled=True,
        strategy_schwab_1m_v2_line_chart_restoration_enabled=True,
        strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=True))
    bot.strategy._now_ms = lambda: bars[-1].timestamp_ms + 61_000
    bot._watchlist = {"IZM"}  # Controlled observation, not entry/native permission.
    bot._sync_line_epochs()
    ledger = bot._line_sessions["IZM"]
    state = bot.strategy.watchlist_state("IZM")
    unknown_stored = tuple(replace(bar, producer=None) for bar in bars)
    raw = response(bars, ())
    # A controlled reader receipt exists, but no producer prefix was accepted yet.
    ledger.attest(history_coverage(raw, bars))

    def forbidden_connection():
        raise AssertionError("No DB connection is authorized by this controlled test")

    bot.session_factory = forbidden_connection
    bot._read_line_session_bars = lambda *args: unknown_stored  # Explicit stored-row read port.
    key = ("IZM", ledger.epoch)
    with pytest.raises(ValueError, match="stored row has no attested producer pin"):
        await bot._reconcile_line_store("IZM", ledger, state, bars[-1].timestamp_ms)
    assert key not in bot._line_db_checked and "IZM" not in bot._line_published
    assert bot.strategy._entries_held
    projected = project_history(raw, ledger.producer_pins, now_ms=bot.strategy._now_ms())
    for row in projected:
        ledger.observe(row)
        bot.strategy.on_observed_bar("IZM", row, observation_phase="replay")
    ledger.attest(history_coverage(raw, projected, canonical=True))
    assert await bot._reconcile_line_store("IZM", ledger, state, bars[-1].timestamp_ms)
    assert key in bot._line_db_checked and ledger.prepare() is not None
    assert not state.resting_active and bot.strategy._entries_held


@pytest.mark.asyncio
async def test_cross_producer_volume_does_not_change_strict_first_minute_floor():
    bot, ledger, bars, raw, state, queued = await published()
    assert bot.strategy._liquidity_floor_ok(state)
    other = replace(bars[-1], producer="schwab", volume=1)
    bot._observe_line_bar("IZM", other)
    # Feed the canonical value, exactly as the owned dispatcher does.
    bot.strategy.on_observed_bar("IZM", ledger.producer_pins.current(other.timestamp_ms),
                                 observation_phase="live")
    assert bot.strategy._liquidity_floor_ok(state)
    assert state.liquidity_floor_volume == bars[-1].volume == 20_000
    next_bar = replace(candle(40, close=6), volume=10_000)
    bot.strategy.on_observed_bar("IZM", next_bar, observation_phase="live")
    assert not bot.strategy._liquidity_floor_ok(state)  # Strict >, not >=.
    assert bot.strategy._entries_held


@pytest.mark.asyncio
async def test_retired_same_session_readd_keeps_pin_and_next_session_does_not():
    bot, ledger, bars, raw, state, queued = await published()
    pins = ledger.producer_pins
    bot._watchlist.clear()
    bot._sync_line_epochs()
    bot._watchlist.add("IZM")
    bot._sync_line_epochs()
    assert bot._line_sessions["IZM"].producer_pins is pins
    assert pins.producers[bars[-1].timestamp_ms] == "massive"
    bot.strategy._now_ms = lambda: ANCHOR + 86_460_000
    bot._sync_line_epochs()
    assert bot._line_sessions["IZM"].producer_pins is not pins
    assert not bot._line_sessions["IZM"].producer_pins.producers


@pytest.mark.asyncio
async def test_keepwatch_off_does_not_project_cross_producer_prices():
    bot, ledger, bars, raw, state, queued = await published(enabled=False)
    assert ledger.producer_pins is None
    revised = (*bars[:-1], replace(bars[-1], producer="schwab", close=bars[-1].close - .04))
    bot._observe_line_bar("IZM", revised[-1])
    ledger.attest(history_coverage(response((), revised), revised))
    assert await bot._rebuild_session_line("IZM", ledger)
    assert not state.resting_active and queued[0].metadata["reason"] == "line_history_changed"


def test_pre07_massive_seed_and_verified_empty_halt_preserve_exact_pair():
    first, resumed = candle(), candle(5)
    raw = response((first, resumed), (), now=resumed.timestamp_ms + 60_000)
    pins = SessionProducerPins("IZM", ANCHOR)
    now = raw.completed_ns // 1_000_000
    ledger = SessionLineRestoration("IZM", ANCHOR, 1, producer_pins=pins, clock=lambda: now)
    projected = project_history(raw, pins, now_ms=now)
    for bar in projected:
        ledger.observe_closed_live(bar)
    ledger.attest(history_coverage(raw, projected, canonical=True))
    assert attested_empty_interval(ledger, raw, symbol="IZM", anchor_ms=ANCHOR,
        previous_ms=first.timestamp_ms, through_ms=resumed.timestamp_ms, now_ms=now, resumed=True)
    assert ledger.prepare().spanning_pairs == ((first.timestamp_ms, resumed.timestamp_ms),)
    assert len(ledger._bars) == 2  # No invented gap candles.
    assert set(pins.producers.values()) == {"massive"}
    ledger.observe_trade(ANCHOR + 120_000)
    assert not attested_empty_interval(ledger, raw, symbol="IZM", anchor_ms=ANCHOR,
        previous_ms=first.timestamp_ms, through_ms=resumed.timestamp_ms, now_ms=now, resumed=True)


def test_real_repaired_interval_matches_either_explicit_current_response_candidate():
    bars = (candle(), candle(1), candle(2))
    now = bars[-1].timestamp_ms + 61_000
    pins = SessionProducerPins("IZM", ANCHOR)
    pins.accept(bars[1], now_ms=now)
    raw = response(bars, tuple(replace(bar, producer="schwab", close=bar.close - .01)
                               for bar in bars), now=now)
    ledger = SessionLineRestoration("IZM", ANCHOR, 1, producer_pins=pins, clock=lambda: now)
    projected = project_history(raw, pins, now_ms=now)
    for bar in projected:
        ledger.observe_closed_live(bar)
    ledger.attest(history_coverage(raw, projected, canonical=True))
    assert raw.bars[1].close != ledger._bars[bars[1].timestamp_ms].close
    assert attested_repaired_interval(ledger, raw, symbol="IZM", anchor_ms=ANCHOR,
        previous_ms=ANCHOR, through_ms=bars[-1].timestamp_ms, now_ms=now)
    absent = replace(raw, massive_bars=(), bars=raw.schwab_bars)
    assert not attested_repaired_interval(ledger, absent, symbol="IZM", anchor_ms=ANCHOR,
        previous_ms=ANCHOR, through_ms=bars[-1].timestamp_ms, now_ms=now)
