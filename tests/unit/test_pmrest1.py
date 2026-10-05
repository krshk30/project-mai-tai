"""Recorded SAIQ moves/TNON ticks; adversarial interleavings are explicitly controlled."""

import inspect
import json
import logging
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, Quote, SchwabV2Strategy

RAW = json.loads((Path(__file__).parents[1] / "fixtures/pmrest1/recorded.json").read_text())
ET = ZoneInfo("America/New_York")


def ms(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def setup(*, enabled=True, symbol="SAIQ", offset=0.5, **extra):
    settings = Settings(
        _env_file=None,
        strategy_schwab_1m_v2_confirmed_window_enabled=True,
        strategy_schwab_1m_v2_cw_v2_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
        strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=True,
        strategy_schwab_1m_v2_pm_rest_reprice_enabled=enabled,
        strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct=offset,
        strategy_schwab_1m_v2_cw_v2_resting_entry_reprice_pct=0.5,
        strategy_schwab_1m_v2_atr_flip_vol_floor=10000,
        strategy_schwab_1m_v2_entry_window_end_hour_et=15,
        strategy_schwab_1m_v2_entry_window_end_minute_et=45,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
        strategy_schwab_1m_v2_entry_notional_usd=600,
        strategy_schwab_1m_v2_webull_entry_notional_usd=300,
        **extra,
    )
    strategy = SchwabV2Strategy(settings)
    strategy._entries_held = False
    session = strategy._resting_session_is_eh
    window = strategy._resting_in_window
    clock = [ms("2026-10-05T07:35:02.519-04:00")]
    strategy._now_ms = lambda: clock[0]
    strategy._resting_session_is_eh = lambda now=None: session(
        datetime.fromtimestamp(clock[0] / 1000, UTC)
    )
    strategy._resting_in_window = lambda now=None: window(
        datetime.fromtimestamp(clock[0] / 1000, UTC)
    )
    state = strategy.watchlist_state(symbol)
    return strategy, state, clock


def bar(state, before):
    rows = [
        r
        for r in RAW["bars"]
        if r["symbol"] == state.symbol and ms(r["bar_time"]) < before // 60_000 * 60_000
    ]
    r = rows[-1]
    state.bars.append(
        OHLCVBar(
            ms(r["bar_time"]),
            float(r["open_price"]),
            float(r["high_price"]),
            float(r["low_price"]),
            float(r["close_price"]),
            r["volume"],
        )
    )


def track(strategy, state, line, slot="first"):
    state.atr_state = "short"
    state.atr_trail = line
    if slot == "first":
        strategy._cw_v2_resting_track(state, {"state": "short", "trail": line, "state_age": 10})
    else:
        state.cw_armed = True
        state.cw_bars_waited = 2
        state.cw_segment_high = line
        strategy._cw_v2_reclaim_resting_track(state)


def armed(**kwargs):
    strategy, state, clock = setup(**kwargs)
    bar(state, clock[0])
    track(strategy, state, 8.3599)
    assert state.resting_active and not state.resting_is_broker_order
    assert strategy.drain_pending_intents() == []
    return strategy, state, clock


@pytest.mark.parametrize("slot", ["first", "reclaim"])
def test_p1_saiq_moves_same_bar_without_a_disarm_or_intent(slot, caplog):
    strategy, state, clock = armed()
    state.resting_slot = state.last_resting_placed_slot = slot
    with caplog.at_level(logging.INFO):
        for when, line in [
            ("2026-10-05T07:45:02.368-04:00", 8.2381),
            ("2026-10-05T07:47:02.102-04:00", 7.9699),
        ]:
            clock[0] = ms(when)
            bar(state, clock[0])
            track(strategy, state, line, slot)
            assert state.resting_active and not state.resting_is_broker_order
            assert state.resting_level == line
            assert state.resting_trigger == pytest.approx(line * 1.005)
            assert not strategy.drain_pending_intents()
            assert not strategy._pending_webull_direct_intents
    messages = [r.getMessage() for r in caplog.records]
    assert sum("V2-RESTING-EH-MOVE" in m for m in messages) == 2
    assert not any("V2-RESTING-EH-DISARM" in m for m in messages)


@pytest.mark.parametrize("stream", [False, True])
def test_p2_tnon_recorded_tick_crosses_new_trigger(stream):
    strategy, state, clock = setup(symbol="TNON", offset=0)
    clock[0] = ms("2026-09-10T07:46:02.275-04:00")
    bar(state, clock[0])
    track(strategy, state, 3.5714)
    strategy._reprice_resting(state, 3.5454)
    tick = RAW["tnon_quotes"][0]
    clock[0] = ms(tick["received_at"])
    bar(state, clock[0])
    price, ask = float(tick["last_price"]), float(tick["ask_price"])
    if stream:
        draft = strategy.on_stream_trade("TNON", price, ms(tick["event_ts"]), ask_price=ask)
    else:
        draft = strategy.on_quote(
            "TNON",
            Quote(
                "TNON",
                float(tick["bid_price"]),
                ask,
                price,
                ms(tick["event_ts"]),
                ms(tick["event_ts"]),
            ),
        )
    assert draft is not None
    assert draft.metadata["entry_price"] == "3.5454"
    assert draft.metadata["limit_price"] == "3.5631"
    assert state.resting_active and state.resting_flip_ms == clock[0]


@pytest.mark.parametrize("stream", [False, True])
def test_p3_quote_before_and_after_move_never_sees_unarmed(stream, monkeypatch):
    strategy, state, clock = armed()
    clock[0] = ms("2026-10-05T07:45:02.368-04:00")
    bar(state, clock[0])
    observed = []

    def observe(st, quote):
        observed.append((st.resting_active, st.resting_trigger))
        return None

    monkeypatch.setattr(strategy, "_eh_resting_cross_check", observe)
    # The recorded TNON tick supplies prices; only callback ordering is controlled.
    tick = RAW["tnon_quotes"][0]

    def quote():
        if stream:
            strategy.on_stream_trade(
                "SAIQ", state.resting_trigger, clock[0], ask_price=state.resting_trigger
            )
        else:
            strategy.on_quote(
                "SAIQ",
                Quote(
                    "SAIQ",
                    float(tick["bid_price"]),
                    float(tick["ask_price"]),
                    float(tick["last_price"]),
                    clock[0],
                    clock[0],
                ),
            )

    quote()
    track(strategy, state, 8.2381)
    quote()
    assert observed == [
        (True, pytest.approx(8.3599 * 1.005)),
        (True, pytest.approx(8.2381 * 1.005)),
    ]
    assert not inspect.iscoroutinefunction(strategy._reprice_resting)


def test_p4_sub_half_percent_move_is_noop(caplog):
    strategy, state, clock = armed()
    clock[0] = ms("2026-10-05T07:45:02.368-04:00")
    bar(state, clock[0])
    with caplog.at_level(logging.INFO):
        track(strategy, state, 8.3599 * 0.996)
    assert state.resting_level == 8.3599
    assert not any("EH-MOVE" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("streak", [1, 2])
def test_p5_move_preserves_thin_streak_then_third_thin_cancels(streak, monkeypatch, caplog):
    strategy, state, clock = armed()
    state.resting_below_floor_bars = streak
    strategy._reprice_resting(state, 8.2381)
    assert state.resting_below_floor_bars == streak
    monkeypatch.setattr(strategy, "_liquidity_floor_ok", lambda st: False)
    with caplog.at_level(logging.INFO):
        for _ in range(3 - streak):
            track(strategy, state, 7.9699)
    assert not state.resting_active
    assert any("reason=liquidity_floor" in r.getMessage() for r in caplog.records)


def test_p6_long_flip_grace_and_soft_rest_no_fill_unchanged(caplog):
    strategy, state, clock = armed()
    strategy._reprice_resting(state, 8.2381)
    strategy._cw_v2_resting_track(state, {"state": "long"})
    assert state.resting_active and state.resting_flip_ms == clock[0]
    clock[0] += strategy._resting_flip_grace_ms - 1
    strategy._cw_v2_resting_track(state, {"state": "long"})
    assert state.resting_active
    clock[0] += 1
    with caplog.at_level(logging.INFO):
        strategy._cw_v2_resting_track(state, {"state": "long"})
    assert not state.resting_active
    assert any("reason=flip_no_fill_soft_rest" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("hold", ["boot", "gap"])
def test_p7_move_cannot_make_a_held_rest_live(hold):
    strategy, state, clock = armed()
    if hold == "boot":
        strategy._entries_held = True
    else:
        strategy._gap_hold_enabled = True
        state.gap_hold_active = True
    strategy._reprice_resting(state, 8.2381)
    assert state.resting_active and state.resting_level == 8.3599
    assert strategy.on_quote("SAIQ", Quote("SAIQ", 8.40, 8.42, 8.41, clock[0], clock[0])) is None
    assert strategy.on_stream_trade("SAIQ", 8.41, clock[0], ask_price=8.42) is None


@pytest.mark.parametrize("reason", ["watchlist-removed", "window_closed"])
def test_p7_other_cancels_keep_their_disarm_reason(reason, caplog):
    strategy, state, clock = armed()
    strategy._reprice_resting(state, 8.2381)
    with caplog.at_level(logging.INFO):
        if reason == "watchlist-removed":
            strategy._flip_owned_first_entry_enabled = True
            state.flip_owner_phase = "resting"
            strategy.release_and_drop_symbol("SAIQ")
        else:
            clock[0] = ms("2026-10-05T16:00:02-04:00")
            track(strategy, state, 7.9699)
    assert not state.resting_active
    assert any(f"reason={reason}" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("count", [1, 2, 10])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
def test_p8_moves_preserve_all_accounting_and_rest_state(count, slot, monkeypatch):
    strategy, state, clock = armed()
    state.resting_slot = state.last_resting_placed_slot = slot
    state.resting_below_floor_bars = 2
    state.resting_flip_ms = clock[0] - 100
    state.flip_owner_phase = "resting"
    state.flip_owner_opportunity_id = 12345
    state.fanout_segment_id = 67890
    state.retry_one_segment_id = 67890
    state.retry_one_closes_in_segment = 1
    before = deepcopy(asdict(state))
    monkeypatch.setattr(
        strategy, "_queue_resting_place", lambda *a, **k: pytest.fail("new admission")
    )
    monkeypatch.setattr(strategy, "_queue_resting_cancel", lambda *a, **k: pytest.fail("disarm"))
    for i in range(count):
        strategy._reprice_resting(state, (8.2381, 7.9699)[i % 2])
    after = asdict(state)
    for field in ("resting_level", "resting_trigger"):
        before.pop(field)
        after.pop(field)
    assert before == after


def test_p9_flag_off_reproduces_saiq_one_bar_gap(caplog):
    strategy, state, clock = armed(enabled=False)
    clock[0] = ms("2026-10-05T07:45:02.368-04:00")
    bar(state, clock[0])
    with caplog.at_level(logging.INFO):
        track(strategy, state, 8.2381)
        assert not state.resting_active
        clock[0] = ms("2026-10-05T07:46:02.617-04:00")
        bar(state, clock[0])
        track(strategy, state, 8.2381)
    assert state.resting_active and state.resting_level == 8.2381
    assert any("EH-DISARM" in r.getMessage() for r in caplog.records)
    assert not any("EH-MOVE" in r.getMessage() for r in caplog.records)
    assert Settings(_env_file=None).strategy_schwab_1m_v2_pm_rest_reprice_enabled is False


@pytest.mark.parametrize("stream", [False, True])
def test_p10_cross_drafts_match_unchanged_rest_with_600_300_sizing(stream):
    moved, st, clock = setup(symbol="TNON", offset=0)
    control, original, other_clock = setup(enabled=False, symbol="TNON", offset=0)
    for strat, state, times in ((moved, st, clock), (control, original, other_clock)):
        times[0] = ms("2026-09-10T07:46:02.275-04:00")
        bar(state, times[0])
        track(strat, state, 3.5714 if strat is moved else 3.5454)
        state.fanout_segment_id = 1234
    moved._reprice_resting(st, 3.5454)
    tick = RAW["tnon_quotes"][0]
    for strat, state, times in ((moved, st, clock), (control, original, other_clock)):
        times[0] = ms(tick["received_at"])
        bar(state, times[0])

    def cross(strat, times):
        if stream:
            return strat.on_stream_trade("TNON", 3.55, ms(tick["event_ts"]), ask_price=3.55)
        return strat.on_quote("TNON", Quote("TNON", 3.54, 3.55, 3.55, ms(tick["event_ts"]), 0))

    left, right = cross(moved, clock), cross(control, other_clock)
    assert left is not None and right is not None
    assert left == right
    assert moved._pending_webull_fanout_intents == control._pending_webull_fanout_intents
    assert left.quantity == (Decimal(600) / Decimal(left.metadata["limit_price"])).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    if stream:
        # Existing #1054 stream Quote loses the ask before dollar fan-out sizing.
        # This is a characterized defect, not a passing Webull delivery claim.
        assert moved._pending_webull_fanout_intents == []
    else:
        webull = moved._pending_webull_fanout_intents[0]
        assert webull.quantity == Decimal(85)
        assert webull.metadata["entry_notional_target_usd"] == "300"


def test_rth_move_places_both_brokers_same_evaluation_without_cancel(caplog):
    strategy, state, clock = armed(strategy_schwab_1m_v2_webull_resting_mirror_enabled=True)
    clock[0] = ms("2026-10-05T09:30:02-04:00")
    state.bars[-1] = replace(state.bars[-1], timestamp_ms=clock[0] - 60_000)
    state.last_quote = Quote("SAIQ", 7.95, 7.96, 7.95, clock[0], clock[0])
    with caplog.at_level(logging.INFO):
        track(strategy, state, 7.9699)
    assert state.resting_active and state.resting_is_broker_order
    assert [d.intent_type for d in strategy.drain_pending_intents()] == ["open"]
    assert [d.intent_type for d in strategy._pending_webull_direct_intents] == ["open"]
    moves = [r.getMessage() for r in caplog.records if "EH-MOVE" in r.getMessage()]
    assert len(moves) == 1
    assert "old_line=8.3599" in moves[0] and "new_line=7.9699" in moves[0]


@pytest.mark.parametrize("streak", [1, 2])
@pytest.mark.parametrize("when", ["09:30:02", "09:31:02"])
def test_p5_rth_conversion_preserves_thin_streak_until_third_thin_cancel(
    streak, when, monkeypatch, caplog
):
    strategy, state, clock = armed(strategy_schwab_1m_v2_webull_resting_mirror_enabled=True)
    clock[0] = ms(f"2026-10-05T{when}-04:00")
    state.bars[-1] = replace(state.bars[-1], timestamp_ms=clock[0] - 60_000)
    state.last_quote = Quote("SAIQ", 7.95, 7.96, 7.95, clock[0], clock[0])
    state.resting_below_floor_bars = streak
    strategy._reprice_resting(state, 7.9699)
    assert state.resting_active and state.resting_is_broker_order
    assert state.resting_below_floor_bars == streak
    assert [d.intent_type for d in strategy.drain_pending_intents()] == ["open"]
    assert [d.intent_type for d in strategy._pending_webull_direct_intents] == ["open"]
    strategy._pending_webull_direct_intents.clear()

    monkeypatch.setattr(strategy, "_liquidity_floor_ok", lambda st: False)
    with caplog.at_level(logging.INFO):
        for expected in range(streak + 1, 4):
            clock[0] += 60_000
            state.bars[-1] = replace(state.bars[-1], timestamp_ms=clock[0] - 60_000)
            track(strategy, state, 7.9699)
            if expected < 3:
                assert state.resting_active
                assert state.resting_below_floor_bars == expected
                assert strategy.drain_pending_intents() == []
                assert strategy._pending_webull_direct_intents == []
    assert not state.resting_active
    assert any("reason=liquidity_floor" in r.getMessage() for r in caplog.records)
    assert [d.intent_type for d in strategy.drain_pending_intents()] == ["cancel"]
    assert [d.intent_type for d in strategy._pending_webull_direct_intents] == ["cancel"]


@pytest.mark.parametrize("slot", ["first", "reclaim"])
@pytest.mark.parametrize("when", ["15:45:00", "16:00:00"])
def test_p7_production_cutoff_disarms_before_reprice_helper(slot, when, monkeypatch, caplog):
    strategy, state, clock = armed()
    state.resting_slot = state.last_resting_placed_slot = slot
    state.cw_armed = True
    state.cw_bars_waited = 2
    state.cw_segment_high = state.atr_trail = 7.9699
    state.atr_state = "short"
    state.atr_session_anchor_ms = ms("2026-10-05T04:00:00-04:00")
    clock[0] = ms(f"2026-10-05T{when}-04:00")
    monkeypatch.setattr(
        strategy, "_reprice_resting", lambda *a, **k: pytest.fail("post-cutoff reprice")
    )
    with caplog.at_level(logging.INFO):
        assert strategy.on_bar(
            "SAIQ", replace(state.bars[-1], timestamp_ms=clock[0] - 60_000)
        ) is None
    assert not state.resting_active
    assert not state.cw_armed
    assert all(d.intent_type != "open" for d in strategy.drain_pending_intents())
    assert all(d.intent_type != "open" for d in strategy._pending_webull_direct_intents)
    assert any("reason=post-close-entry-disabled" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("failure", ["price", "stop_ask", "window", "admission"])
def test_failed_preparation_or_rth_admission_leaves_old_rest(failure, monkeypatch):
    strategy, state, clock = armed()
    if failure == "price":
        monkeypatch.setattr(strategy, "_resting_trigger_for_line", lambda line: float("nan"))
    else:
        clock[0] = ms("2026-10-05T09:30:02-04:00")
        state.bars[-1] = replace(state.bars[-1], timestamp_ms=clock[0] - 60_000)
        state.last_quote = Quote("SAIQ", 7.95, 7.96, 7.95, clock[0], clock[0])
        if failure == "stop_ask":
            monkeypatch.setattr(strategy, "_resting_stop_ask_allows", lambda *a, **k: False)
        elif failure == "window":
            strategy.settings.strategy_schwab_1m_v2_entry_window_start_hour_et = 10
        else:
            monkeypatch.setattr(strategy, "_strict_first_rest_admitted", lambda *a, **k: False)
    strategy._reprice_resting(state, 7.9699)
    assert state.resting_active and state.resting_level == 8.3599
    assert not state.resting_is_broker_order
    assert strategy.drain_pending_intents() == []


def test_rth_broker_reprice_still_uses_existing_cancel_path(monkeypatch):
    strategy, state, clock = armed()
    state.resting_is_broker_order = True
    calls = []
    monkeypatch.setattr(strategy, "_queue_resting_cancel", lambda *a, **k: calls.append(k))
    strategy._reprice_resting(state, 7.9699)
    assert calls == [{"reason": "reprice"}]


def test_flag_on_does_not_enable_initial_premarket_reclaim():
    strategy, state, clock = setup()
    bar(state, clock[0])
    track(strategy, state, 8.3599, "reclaim")
    assert not state.resting_active
    assert strategy.drain_pending_intents() == []


def test_configured_cutoff_still_disarms_a_moved_rest(caplog):
    strategy, state, clock = armed()
    strategy._reprice_resting(state, 8.2381)
    clock[0] = ms("2026-10-05T15:45:00-04:00")
    state.atr_session_anchor_ms = ms("2026-10-05T04:00:00-04:00")
    with caplog.at_level(logging.INFO):
        strategy._reprice_resting(state, 7.9699)
        assert state.resting_level == 8.2381
        strategy.on_bar("SAIQ", replace(state.bars[-1], timestamp_ms=clock[0]))
    assert not state.resting_active
    assert any("reason=post-close-entry-disabled" in r.getMessage() for r in caplog.records)


def test_trigger_preparation_exception_keeps_the_old_rest(monkeypatch):
    strategy, state, clock = armed()
    before = asdict(state)

    def fail(line):
        raise ValueError("trigger preparation failed")

    monkeypatch.setattr(strategy, "_resting_trigger_for_line", fail)
    strategy._reprice_resting(state, 8.2381)
    assert asdict(state) == before
