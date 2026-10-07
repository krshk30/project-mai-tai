"""Recorded Oct 7 candles/probes; ownership/quote prerequisites are controlled, not live fills."""
from datetime import UTC, datetime
import json
from pathlib import Path
import re

import pytest

from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from tests.unit.test_v2_flip_owned_first_entry import _book, _leg, _strategy, PRIMARY, WEBULL


RECEIPT = Path(__file__).parents[2] / "docs/review-artifacts/slotclear1/SELL_EVIDENCE_2026-10-07.json"
EVIDENCE = json.loads(RECEIPT.read_text())
WATCH = 1791395139968
BOOT = 1791395127187
SELL = 1791397500000


def recorded_probes(symbol, *, after="2026-10-07 18:26:00"):
    rows = {datetime.fromisoformat(row["bar_time"]).timestamp() * 1000: row
            for row in EVIDENCE["bars"][symbol]}
    for row in EVIDENCE["logs"][symbol]:
        text = row["text"]
        if "[V2-ATR-PROBE]" not in text or text[:19] < after:
            continue
        fields = dict(re.findall(r"(\w+)=([^ ]+)", text))
        bar_ms = int(fields["ts_ms"])
        if bar_ms not in rows:
            continue  # Historical warmup probes outside the bounded DB window are not invented bars.
        db = rows[bar_ms]
        bar = OHLCVBar(bar_ms, *(float(db[key]) for key in
                               ("open_price", "high_price", "low_price", "close_price")), db["volume"])
        assert bar.close == float(fields["close"]) and bar.volume == int(fields["vol"])
        now = int(datetime.strptime(text[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC).timestamp() * 1000)
        yield now, bar, {"flip": None if fields["flip"] == "none" else fields["flip"],
                         "state": fields["state"], "trail": float(fields["trail"]),
                         "state_age": int(fields["age"]), "observation_phase": "live"}


def seeded(*, enabled=True, retry=True, symbol="SXTC", partial=False):
    strategy, clock, identities, owners = _strategy(dual=True, retry_one=retry)
    strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_sell_enabled = enabled
    strategy._atr_rearm_timeout_secs = 1_000_000_000  # Keep the controlled in-flight claim live.
    # BUY exception stays independent and OFF in this SELL proof.
    strategy._boot_ms = BOOT
    clock[0] = WATCH
    state = strategy.watchlist_state(symbol)
    state.retry_one_watch_start_ms = WATCH
    state.atr_state = "long"
    state.cw_armed = True
    state.cw_arm_bar_ts = 1791386040000
    state.cw_resting_taken = partial
    state.retry_one_segment_id = 1791383580000
    state.retry_one_closes_in_segment = 1
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot._watch_start_ms = strategy, {symbol: WATCH}
    strategy._cw_armed_segment_safety_enabled = True
    bot._cap_reconstructed_segment(symbol, stage="db-seed")
    return strategy, state, clock, identities, owners, bot


def observe(strategy, state, clock, probe, *, book=True):
    now, bar, signal = probe
    clock[0] = now
    if not state.bars or state.bars[-1].timestamp_ms != bar.timestamp_ms:
        state.bars.append(bar)
    state.atr_state, state.atr_trail, state.atr_state_age = signal["state"], signal["trail"], signal["state_age"]
    if signal["flip"] == "SELL":
        state.atr_short_flip_bar_ts = bar.timestamp_ms
    if book:
        _book(strategy, clock, state.symbol)
    strategy._cw_v2_track(state, signal)
    strategy._cw_v2_resting_track(state, signal)


def test_bounded_recorded_evidence_counts_and_suppression_window():
    assert len(EVIDENCE["bars"]["SXTC"]) == 60
    assert len(EVIDENCE["bars"]["DKI"]) == 12
    suppressed = [r for r in EVIDENCE["logs"]["SXTC"] if "[V2-RESTING-SUPPRESSED-BAR]" in r["text"]]
    assert len(suppressed) == 16
    assert suppressed[0]["text"].startswith("2026-10-07 18:29:01,119")
    assert suppressed[-1]["text"].startswith("2026-10-07 18:44:02,259")
    assert list(recorded_probes("SXTC"))[0][2]["flip"] == "SELL"


@pytest.mark.parametrize("retry", [True, False])
@pytest.mark.parametrize("partial", [True, False])
def test_recorded_sxtc_sell_releases_reconstruction_then_first_rest_at_age_three_once(retry, partial):
    strategy, state, clock, _, _, _ = seeded(retry=retry, partial=partial)
    for index, probe in enumerate(list(recorded_probes("SXTC"))[:4]):
        observe(strategy, state, clock, probe)
        primary = strategy.drain_pending_intents()
        mirror = strategy.drain_webull_direct_intents()
        if index < 3:
            assert not primary and not mirror
            assert not state.cw_resting_taken and not state.cw_reclaim_taken
        else:
            assert len(primary) == len(mirror) == 1
            assert primary[0].metadata["cw_entry_slot"] == "first"
            assert primary[0].metadata["fanout_slot_id"] == mirror[0].metadata["fanout_slot_id"]
            observe(strategy, state, clock, probe)
            assert strategy.drain_pending_intents() == []
            assert strategy.drain_webull_direct_intents() == []
    assert state.resting_active and state.slotclear_fresh_buy_bar_ms == 0
    if retry:
        assert state.retry_one_segment_id == SELL and state.retry_one_closes_in_segment == 0


def test_reconstructed_claim_without_old_cap_only_marker_is_not_erased_when_flag_off():
    strategy, state, clock, _, _, _ = seeded(enabled=False, partial=True)
    assert state.cw_seed_cap_watch_start_ms == 0
    for probe in list(recorded_probes("SXTC"))[:4]:
        observe(strategy, state, clock, probe)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert not strategy.drain_pending_intents() and not state.resting_active


@pytest.mark.parametrize("blocker", ["held_primary", "held_mirror", "held_qty", "union_qty", "working",
                                    "mirror_working", "emit", "filled_claim", "owner_id", "owner_rows",
                                    "stale_book", "unreadable", "restore", "boot", "gap", "line", "removed"])
def test_fresh_sell_never_releases_held_working_unknown_or_unready_claims(blocker):
    strategy, state, clock, _, _, _ = seeded(partial=True)
    probe = list(recorded_probes("SXTC"))[0]
    clock[0] = probe[0]
    _book(strategy, clock, "SXTC")
    if blocker in {"held_primary", "held_mirror"}:
        _book(strategy, clock, "SXTC", _leg(PRIMARY if blocker == "held_primary" else WEBULL, "owned", entered_ms=WATCH))
    elif blocker == "held_qty":
        state.position_qty_held = 2
    elif blocker == "union_qty":
        state.position_qty = 2
    elif blocker == "working":
        state.resting_active = True
    elif blocker == "mirror_working":
        state.webull_resting_active = True
    elif blocker == "emit":
        state.cw_v2_emit_claimed, state.cw_v2_emit_ms = True, probe[0]
    elif blocker == "filled_claim":
        state.fanout_claim_outcome = "filled"
    elif blocker == "owner_id":
        state.flip_owner_opportunity_id = WATCH
    elif blocker == "owner_rows":
        state.flip_owner_position_ids = {PRIMARY: "owned"}
    elif blocker == "stale_book":
        state.flip_owner_evidence_at_ms = WATCH
    elif blocker == "unreadable":
        state.flip_owner_evidence_readable = False
    elif blocker == "restore":
        strategy._flip_owner_restore_readable = False
    elif blocker == "boot":
        strategy._entries_held = True
    elif blocker == "gap":
        strategy.gap_hold_active = lambda symbol: True
    elif blocker == "line":
        strategy.line_buy_ready = lambda symbol: False
    elif blocker == "removed":
        strategy._removed_wait_enabled = True
        strategy._removed_scanner_symbols.add("SXTC")
    observe(strategy, state, clock, probe, book=False)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert not any(d.intent_type == "open" for d in strategy.drain_pending_intents())


@pytest.mark.parametrize("kind", ["replay_signal", "replay_handler", "pre_watch", "cold_boot", "stale", "wrong_flip_bar"])
def test_sell_requires_live_completed_bar_after_watch_and_boot(kind):
    strategy, state, clock, _, _, _ = seeded(partial=True)
    now, bar, signal = list(recorded_probes("SXTC"))[0]
    signal = dict(signal)
    if kind == "replay_signal":
        signal["observation_phase"] = "replay"
    elif kind == "replay_handler":
        strategy._bar_observation_phase = "replay"
    elif kind == "pre_watch":
        state.slotclear_reconstructed_watch_start_ms = bar.timestamp_ms + 60001
    elif kind == "cold_boot":
        strategy._boot_ms = bar.timestamp_ms + 60001
    elif kind == "stale":
        now += 600000
    if kind == "wrong_flip_bar":
        state.bars.append(bar)
        state.atr_short_flip_bar_ts = bar.timestamp_ms - 60000
        clock[0] = now
        _book(strategy, clock, "SXTC")
        strategy._cw_v2_track(state, signal)
    else:
        observe(strategy, state, clock, (now, bar, signal))
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert not state.resting_active


def test_recorded_dki_no_fresh_flip_retains_reconstructed_short_cap():
    strategy, state, clock, _, _, bot = seeded(symbol="DKI", partial=True)
    state.cw_armed = False
    state.atr_state, state.atr_short_flip_bar_ts = "short", 1791395940000
    bot._watch_start_ms["DKI"] = 1791397292982
    bot._cap_reconstructed_segment("DKI", stage="db-seed")
    probes = list(recorded_probes("DKI", after="2026-10-07 18:21:00"))
    # 12 DB candles; the last one's close is outside the requested log window.
    assert len(probes) == 11 and all(p[2]["flip"] is None for p in probes)
    for probe in probes:
        observe(strategy, state, clock, probe)
    assert state.cw_resting_taken and state.cw_reclaim_taken
    assert strategy.drain_pending_intents() == []


def test_keeprest_sell_cancels_existing_order_without_granting_another_slot():
    strategy, state, clock, _, _, _ = seeded(partial=True)
    strategy.settings.strategy_schwab_1m_v2_keep_rest_after_buy_enabled = True
    state.resting_active = state.resting_buy_frozen = True
    observe(strategy, state, clock, list(recorded_probes("SXTC"))[0])
    intents = strategy.drain_pending_intents()
    assert len(intents) == 1 and intents[0].intent_type == "cancel"
    assert intents[0].metadata["reason"] == "keep_rest_sell"
    assert state.cw_resting_taken and state.cw_reclaim_taken


def test_fresh_sell_has_its_own_default_off_switch():
    assert Settings().strategy_schwab_1m_v2_slotclear_fresh_sell_enabled is False


@pytest.mark.parametrize("max_age_ms", [120000, 300000])
def test_delayed_duplicate_recorded_sell_cannot_retire_the_new_first_rest_owner(max_age_ms):
    strategy, state, clock, _, owners, _ = seeded(partial=True)
    strategy._resting_max_bar_age_ms = max_age_ms  # Freshness-setting counterfactual, same recorded candles.
    probes = list(recorded_probes("SXTC"))[:4]
    for probe in probes:
        observe(strategy, state, clock, probe)
    first = strategy.drain_pending_intents()
    assert len(first) == 1
    strategy.drain_webull_direct_intents()
    opportunity = state.flip_owner_opportunity_id
    writes = len(owners)
    state.bars.append(probes[0][1])  # Duplicate of a recorded bar, not a fabricated candle.
    state.atr_short_flip_bar_ts = SELL
    strategy._cw_v2_track(state, probes[0][2])
    assert state.resting_active and state.flip_owner_opportunity_id == opportunity
    assert len(owners) == writes
    assert strategy.drain_pending_intents() == []
    assert strategy.drain_webull_direct_intents() == []


def test_fresh_sell_close_boundary_does_not_depend_on_buy_exception_or_retry_on():
    strategy, state, clock, _, _, _ = seeded(partial=True)
    # Boundary counterfactual: the recorded SELL candle straddles a later re-add/restart.
    boundary = SELL + 30000
    state.slotclear_reconstructed_watch_start_ms = state.retry_one_watch_start_ms = boundary
    strategy._boot_ms = boundary
    assert not strategy.settings.strategy_schwab_1m_v2_slotclear_fresh_flip_enabled
    observe(strategy, state, clock, list(recorded_probes("SXTC"))[0])
    assert not state.cw_resting_taken and not state.cw_reclaim_taken
    assert state.retry_one_segment_id == SELL and state.retry_one_closes_in_segment == 0


def test_recorded_restart_owner_recovery_and_second_cap_keep_sell_provenance():
    strategy, state, clock, _, owners, bot = seeded(partial=True)
    # The log proves UNKNOWN + this restored opportunity, then proven-empty retirement.
    # In-flight order/row transport is controlled; no historical fills are inferred.
    state.flip_owner_phase = "unknown"
    state.flip_owner_opportunity_id = state.fanout_segment_id = 1791380522895
    state.flip_owner_first_rest_placed = True
    clock[0] = 1791395140486
    bot._cap_reconstructed_segment("SXTC", stage="db-seed")
    clock[0] = 1791395146122
    _book(strategy, clock, "SXTC", terminal_unfilled_opportunities=frozenset({1791380522895}))
    assert state.flip_owner_phase == "idle"
    assert owners[-1][2] == "proven_empty_first_rest_opportunity"
    clock[0] = 1791395147650
    bot._cap_reconstructed_segment("SXTC", stage="streamer-warmup")
    assert state.slotclear_reconstructed_watch_start_ms == WATCH
    observe(strategy, state, clock, list(recorded_probes("SXTC"))[0])
    assert not state.cw_resting_taken and not state.cw_reclaim_taken


def test_actual_entry_invalidates_reconstruction_provenance_before_any_later_sell():
    strategy, state, clock, _, _, _ = seeded(partial=True)
    for probe in list(recorded_probes("SXTC"))[:4]:
        observe(strategy, state, clock, probe)
    assert state.slotclear_reconstructed_watch_start_ms == 0
    strategy.update_position("SXTC", 2, held_qty=2)
    assert state.slotclear_reconstructed_watch_start_ms == 0


@pytest.mark.parametrize("enabled", [False, True])
def test_recorded_sxtc_entire_sell_suppression_window_with_flag_pair(enabled):
    strategy, state, clock, _, _, _ = seeded(enabled=enabled, partial=True)
    primary, mirror, first_age = [], [], None
    probes = list(recorded_probes("SXTC"))
    assert len(probes) == 19
    for probe in probes:
        observe(strategy, state, clock, probe)
        drafts = strategy.drain_pending_intents()
        mirrored = strategy.drain_webull_direct_intents()
        if first_age is None and any(d.intent_type == "open" for d in drafts):
            first_age = probe[2]["state_age"]
        primary.extend(drafts)
        mirror.extend(mirrored)
    opened = [d for d in primary if d.intent_type == "open"]
    mirror_opened = [d for d in mirror if d.intent_type == "open"]
    if enabled:
        assert first_age == 3 and state.cw_resting_suppressed_bars == 0
        assert opened and mirror_opened
        assert len({d.metadata["fanout_segment_id"] for d in opened + mirror_opened}) == 1
        assert all(d.metadata["cw_entry_slot"] == "first" for d in opened + mirror_opened)
    else:
        assert not opened and not mirror_opened
        assert state.cw_resting_suppressed_bars == 16
    print(json.dumps({"case": "SXTC recorded 14:26-14:44 ET", "sell_flag": enabled,
                      "probe_count": len(probes), "first_arm_age": first_age,
                      "primary_open_drafts": len(opened), "mirror_open_drafts": len(mirror_opened),
                      "primary_cancel_drafts": sum(d.intent_type == "cancel" for d in primary),
                      "mirror_cancel_drafts": sum(d.intent_type == "cancel" for d in mirror),
                      "suppressed_bars": state.cw_resting_suppressed_bars,
                      "boundary": "controlled fresh empty owner book, window/liquidity/stop-ask; no fills or quotes invented"}))
