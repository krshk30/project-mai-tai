"""Native recorded admission controls; provider completeness remains a named historical gap."""
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from project_mai_tai.events import StrategyStateSnapshotEvent, StrategyStateSnapshotPayload
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, Quote
from tests.line_restore_acceptance_factory import make_line_restore_case
from tests.unit import test_orbown1_strategy_reads as reads

NATIVE = json.loads((Path(__file__).parents[1] / "fixtures/orbown1_native.json").read_text())
replay = reads.replay


def test_native_fixture_preserves_all_69_exported_settings():
    assert len(NATIVE["settings"]) == 69
    settings = reads.Settings(**NATIVE["settings"])
    for key, value in NATIVE["settings"].items():
        assert str(getattr(settings, key)).lower() == value.lower()


async def _initial_case(factory, symbol, now_ms):
    case = make_line_restore_case(symbol=symbol, now_ms=now_ms,
                                  settings_overrides=NATIVE["settings"])
    bot = case.bot
    case.schwab.broker_account_name = case.settings.strategy_schwab_1m_v2_account_name
    case.webull.broker_account_name = case.settings.strategy_schwab_1m_v2_webull_account_name
    bot.session_factory = factory
    with case.clock():
        bot._configure_fanout_identity_store()
        bot._configure_flip_entry_ownership_store({})
        await bot._configure_removed_wait_store()
        event = StrategyStateSnapshotEvent(source_service="recorded-scanner",
            produced_at=datetime.now(UTC), payload=StrategyStateSnapshotPayload(
                top_confirmed=[{"symbol": symbol}], all_confirmed=[], watchlist=[]))
        bot._apply_strategy_state_event({"data": event.model_dump_json()}, max_watchlist=10)
    return case


async def native_case(factory, point, *, stale_quote=False, thin_bar=False, real_gap=False):
    symbol = point["symbol"]
    now = int(datetime.fromisoformat(point["at"]).timestamp() * 1000)
    bars = [ChartBar(symbol, *row[1:6], row[0]) for row in NATIVE["bars"][symbol]
            if row[0] <= now // 60000 * 60000 - 60000]
    live_ids = {row[0] for row in NATIVE["bars"][symbol] if row[6] == "live"}
    if thin_bar:
        bars[-1].volume = 1
    case = await _initial_case(factory, symbol, bars[0].timestamp_ms + 62000)
    bot, strategy = case.bot, case.strategy
    episode = next(row for row in reads.FIXTURE["cases"] if row["symbol"] == symbol)
    events = [(int(datetime.fromisoformat(episode[key]).timestamp()*1000), quantity)
              for key, quantity in (("poll_at", 2), ("exit_at", 0))]
    gap_pending = real_gap
    for bar in bars[:-1]:
        while events and events[0][0] <= bar.timestamp_ms + 62000:
            event_ms, quantity = events.pop(0)
            with case.clock(event_ms):
                reads._position(factory, "orb_schwab", symbol, quantity, price=episode["entry_price"])
                assert reads._apply_maps(bot, symbol) == ({}, {})
                owner = strategy.watchlist_state(symbol)
                assert owner.flip_owner_phase == "idle" and not owner.cw_resting_taken
        gap = NATIVE["gixi_gap"]
        if gap_pending and bar.timestamp_ms + 62000 >= gap["detected_at_ms"]:
            assert case.hold(**gap)
            gap_pending = False
        await case.feed_bar(bar, now_ms=bar.timestamp_ms + 62000,
                            phase="live" if bar.timestamp_ms in live_ids else "replay")
    ledger = bot._line_sessions[symbol]
    assert not await case.rebuild()
    assert ledger.incomplete_reason == "coverage_unproven"
    with case.clock(now):
        bot._mark_warmed_from_fresh_bar(symbol, bars[-2], source="streamer")
        bot._try_complete_boot_state_restoration({symbol}, new_symbols=set())
        bot._cw_boot_hold_check()
        strategy.apply_flip_position_book(bot._fetch_flip_position_book())
        quote = Quote(symbol, *point["quote"])
        if stale_quote:
            quote.quote_time_ms = now - 10001
        strategy.on_quote(symbol, quote)
    await case.feed_bar(bars[-1], now_ms=now, phase="live")
    assert not await case.rebuild()
    return case


@pytest.mark.asyncio
@pytest.mark.parametrize("point", NATIVE["cases"], ids=lambda p: f"{p['symbol']}-{p['at']}")
async def test_recorded_native_all_on_admission(replay, point, caplog):
    from tests.unit.test_orbown1_strategy_reads import _apply_maps, _position

    bot, factory, _, _, _ = replay
    symbol = point["symbol"]
    _position(factory, "orb_schwab", symbol, 2)
    assert _apply_maps(bot, symbol) == ({}, {})
    _position(factory, "orb_schwab", symbol, 0)
    case = await native_case(factory, point)
    state = case.strategy.watchlist_state(symbol)
    assert not state.cw_resting_taken and not state.cw_reclaim_taken
    assert "[V2-FLIP-OWNER-UNKNOWN]" not in caplog.text
    assert not case.strategy._entries_held
    assert case.strategy._liquidity_floor_ok(state)
    assert case.strategy._line_readiness == case.bot._line_buy_ready
    assert case.bot.redis is None and case.bot.rest_client is None and case.bot.streamer is None
    # Existing live fallback, not provider attestation, admits this offline chronology.
    assert case.snapshot()["incomplete_reason"] == "coverage_unproven"
    assert state.line_live_fallback and not case.bot._line_published
    assert round(state.atr_trail, 4) == point["line"]
    assert state.flip_owner_phase == "resting" and state.resting_active
    assert case.strategy.line_buy_ready(symbol)
    primary, mirror = case.schwab.intents[-1], case.webull.intents[-1]
    for draft, quantity in zip((primary, mirror), point["quantities"]):
        assert draft.side == "buy" and draft.intent_type == "open"
        assert draft.quantity == quantity and draft.metadata["order_type"] == "STOP_LIMIT"
        assert draft.metadata["stop_price"] == point["trigger"]
        assert draft.metadata["cw_entry_slot"] == "first"
        assert case.bot._line_draft_allowed(draft)
    assert primary.metadata["fanout_slot_id"] == mirror.metadata["fanout_slot_id"]
    print(f"ORBOWN1-NATIVE {symbol} at={point['at']} side=buy qty={point['quantities']} "
          f"trigger={point['trigger']} waiting=STOP_LIMIT source=existing_live_fallback")


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["stale_quote", "thin_bar"])
async def test_native_all_on_market_and_liquidity_refusals_are_not_bypassed(replay, control, caplog):
    case = await native_case(replay[1], NATIVE["cases"][0], **{control: True})
    state = case.strategy.watchlist_state("GIXI")
    assert case.strategy.line_buy_ready("GIXI") and not case.strategy._entries_held
    assert not state.cw_resting_taken and state.flip_owner_phase == "idle"
    assert case.snapshot()["buy_count"] == 0 and not state.resting_active
    if control == "thin_bar":
        assert not case.strategy._liquidity_floor_ok(state)
    else:
        assert "stale" in caplog.text
    print(f"ORBOWN1-NATIVE refusal={control} buys=0")


@pytest.mark.asyncio
async def test_actual_gixi_gap_retains_named_native_retry_identity_refusal(replay, caplog):
    caplog.set_level("INFO")
    case = await native_case(replay[1], NATIVE["cases"][0], real_gap=True)
    state = case.strategy.watchlist_state("GIXI")
    assert not state.gap_hold_active and not case.strategy._entries_held
    assert round(state.atr_trail, 4) == 6.6224 and state.atr_state_age == 5
    assert state.flip_owner_phase == "idle" and not state.cw_resting_taken
    assert state.retry_one_segment_id != state.atr_short_flip_bar_ts
    assert state.retry_one_segment_id == int(NATIVE["gixi_retry_before1126"]["payload"]["segment_id"])
    assert state.atr_short_flip_bar_ts == 1791818400000
    assert case.snapshot()["buy_count"] == 0
    assert "reason=retry_segment_mismatch" in caplog.text
    print(f"ORBOWN1-NATIVE GIXI real_gap=1 resumed=1 buys=0 refusal=retry_segment_mismatch "
          f"retained_retry={state.retry_one_segment_id} current_short={state.atr_short_flip_bar_ts}")


@pytest.mark.asyncio
async def test_corrected_gixi_full_window_preserves_native_budget_and_reports_remaining_gap(replay, caplog):
    caplog.set_level("INFO")
    factory = replay[1]
    start, end_bar = 1791818460000, 1791822360000  # 11:21 decision through the 12:26 candle.
    rows = NATIVE["bars"]["GIXI"]
    case = await _initial_case(factory, "GIXI", rows[0][7])
    events = [(row[7], "bar", row) for row in rows]
    episode = next(row for row in reads.FIXTURE["cases"] if row["symbol"] == "GIXI")
    events.extend((int(datetime.fromisoformat(episode[key]).timestamp()*1000), "position", qty)
                  for key, qty in (("poll_at", 2), ("exit_at", 0)))
    gap = NATIVE["gixi_gap"]
    events.append((gap["detected_at_ms"], "gap", gap))
    quotes = {tuple(row[1:]) for row in NATIVE["corrected_window_quotes"] if row[4] is not None}
    events.extend((row[3], "quote", row) for row in quotes)
    decisions, window_drafts, restored_before_window = [], [], False
    for event_ms, kind, payload in sorted(events, key=lambda event: event[0]):
        old_counts = (len(case.schwab.intents), len(case.webull.intents))
        with case.clock(event_ms):
            state = case.strategy.watchlist_state("GIXI")
            if kind == "position":
                reads._position(factory, "orb_schwab", "GIXI", payload, price=episode["entry_price"])
                assert reads._apply_maps(case.bot, "GIXI") == ({}, {})
                assert state.flip_owner_phase == "idle" and not state.cw_resting_taken
            elif kind == "gap":
                assert case.hold(**payload)
            else:
                if not restored_before_window and event_ms >= start and state.bars:
                    last = state.bars[-1]
                    fresh = ChartBar("GIXI", last.open, last.high, last.low, last.close,
                                     last.volume, last.timestamp_ms)
                    case.bot._mark_warmed_from_fresh_bar("GIXI", fresh, source="streamer")
                    case.bot._try_complete_boot_state_restoration({"GIXI"}, new_symbols=set())
                    case.bot._cw_boot_hold_check()
                    restored_before_window = case.bot._boot_state_restoration_complete
                case.strategy.apply_flip_position_book(case.bot._fetch_flip_position_book())
                if kind == "quote":
                    bid, ask, last, stamp = payload
                    case.strategy.on_quote("GIXI", Quote("GIXI", bid or 0, ask, last, stamp))
                    await case.bot._drain_direct_strategy_intents()
                else:
                    bar = ChartBar("GIXI", *payload[1:6], payload[0])
                    await case.feed_bar(bar, now_ms=event_ms,
                                        phase="live" if payload[6] == "live" else "replay")
                    if start - 60000 <= payload[0] <= end_bar:
                        decisions.append((payload[0], state.atr_state, state.retry_one_segment_id,
                                          state.atr_short_flip_bar_ts, state.flip_owner_phase))
        if event_ms >= start:
            for sink, previous in zip((case.schwab, case.webull), old_counts):
                window_drafts.extend((event_ms, draft) for draft in sink.intents[previous:]
                                     if draft.side == "buy" and draft.intent_type == "open")
    assert restored_before_window and len(decisions) == 67
    assert not window_drafts and "[V2-FLIP-OWNER-UNKNOWN]" not in caplog.text
    assert all(row[2] == 1791809880000 for row in decisions)
    short = [row for row in decisions[5:] if row[1] == "short"]
    long = [row for row in decisions[5:] if row[1] == "long"]
    assert short and long and all(row[4] == "idle" for row in decisions)
    assert all(row[3] == 1791818400000 for row in short)
    assert all(row[3] == 0 for row in long)
    assert "reason=retry_segment_mismatch" in caplog.text
    assert "reason=non_first_producer_disabled" in caplog.text
    assert not case.strategy.watchlist_state("GIXI").cw_resting_taken
    print(f"ORBOWN1-CORRECTED-WINDOW bars={len(decisions)} quote_events={len(quotes)} "
          f"short_evaluations={len(short)} long_evaluations={len(long)} "
          "first_rest=NONE remaining_gap=retry_segment_mismatch later_reclaim=non_first_producer_disabled "
          "existing_release=12:28:02")
