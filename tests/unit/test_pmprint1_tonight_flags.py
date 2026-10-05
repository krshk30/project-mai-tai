"""October 5 sequencing: recorded prices, controlled cache/bar eligibility.

The durable-ticket replay distinguishes proven clearance from unknown ownership.
No recorded row or terminal phase is changed to manufacture clearance.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
from uuid import UUID

import pytest

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal, SNAPSHOT_TYPE, old_buy_proven_clear
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy
from tests.unit.test_pmprint1_pmflip1 import STRAYS, VEEA, armed, cross, flip
from tests.unit.test_pmrest1 import bar, setup, track
from tests.unit.test_rpg1_runtime import runtime

TONIGHT = {
    "strategy_schwab_1m_v2_pm_print_ask_confirm_enabled": True,
    "strategy_schwab_1m_v2_pm_flip_wait_enabled": False,
    "strategy_schwab_1m_v2_pm_rest_reprice_enabled": False,
    "strategy_schwab_1m_v2_atr_reprice_handoff_enabled": False,
    "oms_v2_webull_mirror_fresh_price_enabled": True,
    "strategy_schwab_1m_v2_gap_hold_enabled": True,
}
TICKETS = json.loads((Path(__file__).parents[1] / "fixtures/pmprint1/rpg_off_recorded_tickets.json").read_text())["tickets"]


def tonight_armed(case=VEEA):
    result = armed(case, **TONIGHT)
    assert all(getattr(result[0].settings, key) is value for key, value in TONIGHT.items())
    return result


@pytest.mark.parametrize("stream", [True, False])
def test_s5_tonight_saiq_stray_blocked_without_taking_state(stream):
    strategy, state, clock = tonight_armed(STRAYS[0])
    before = asdict(state)
    assert cross(strategy, state, clock, 6.83, 6.24, stream=stream, bid=6.14, size=1) is None
    after = asdict(state)
    before.pop("last_quote")
    after.pop("last_quote")
    assert after == before
    assert not strategy.drain_pending_intents()
    assert not strategy.drain_webull_fanout_intents()


@pytest.mark.parametrize("stream", [True, False])
def test_s5_tonight_stream_and_rest_cross_size_both_legs_from_same_ask(stream):
    strategy, state, clock = tonight_armed()
    primary = cross(strategy, state, clock, 5.27, 5.27, stream=stream, bid=5.26, size=280)
    assert primary is not None
    mirror, = strategy.drain_webull_fanout_intents()
    for amount, leg in ((600, primary), (300, mirror)):
        assert leg.quantity == (Decimal(amount) / Decimal("5.27")).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        assert leg.metadata["pm_confirming_ask"] == "5.27"
        assert leg.metadata["pm_confirming_ask_age_ms"] == "0"
    assert cross(strategy, state, clock, 5.27, 5.27, stream=not stream) is None
    assert not strategy.drain_webull_fanout_intents()


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("enabled", [False, True], ids=["historical-off", "all-on"])
def test_s5_historical_off_and_all_on_flip_wait_keep_distinct_recorded_veea_latches(stream, enabled):
    strategy, state, clock = (armed(VEEA, **{key: True for key in TONIGHT}) if enabled else tonight_armed())
    flip(strategy, state, clock)
    assert state.resting_flip_ms == (0 if enabled else clock[0])
    assert state.pm_resting_flip_seen_ms == (clock[0] if enabled else 0)
    clock[0] = int(datetime.fromisoformat(VEEA[1].replace("Z", "+00:00")).timestamp() * 1000)
    assert (cross(strategy, state, clock, 5.27, 5.27, stream=stream) is not None) is enabled
    assert bool(strategy.drain_webull_fanout_intents()) is enabled


def test_s5_tonight_pm_rest_off_keeps_recorded_saiq_disarm_rearm():
    options = {key: value for key, value in TONIGHT.items() if key != "strategy_schwab_1m_v2_pm_rest_reprice_enabled"}
    strategy, state, clock = setup(enabled=False, **options)
    bar(state, clock[0])
    track(strategy, state, 8.3599)
    assert state.resting_active
    clock[0] = int(datetime.fromisoformat("2026-10-05T07:45:02.368-04:00").timestamp() * 1000)
    bar(state, clock[0])
    track(strategy, state, 8.2381)
    assert not state.resting_active
    clock[0] = int(datetime.fromisoformat("2026-10-05T07:46:02.617-04:00").timestamp() * 1000)
    bar(state, clock[0])
    track(strategy, state, 8.2381)
    assert state.resting_active and state.resting_level == 8.2381
    assert not strategy.drain_pending_intents()


def test_s6_catalog_matches_all_on_live_set_and_147_checks():
    catalog = json.loads((Path(__file__).parents[2] / "ops/health/expected_flags.json").read_text())
    flags = {entry["name"]: entry["expected"] for entry in catalog["flags"]}
    assert {key: flags[key] for key in TONIGHT} == {key: True for key in TONIGHT}
    numeric = json.loads((Path(__file__).parents[2] / "ops/health/expected_numeric.json").read_text())
    entries = catalog["flags"] + numeric["settings"]
    assert sum(1 + len(entry.get("also_check_services", [])) for entry in entries) == 147


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_tonight_rpg_off_clean_journal_legacy_cancel_then_next_pass(monkeypatch, slot):
    h = await runtime(monkeypatch, "schwab", slot=slot, strategy_overrides=TONIGHT)
    assert all(getattr(h.strategy.settings, key) is value for key, value in TONIGHT.items())
    assert h.strategy._gap_hold_enabled
    assert h.service.settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled
    assert h.service.settings.oms_v2_webull_mirror_fresh_price_enabled
    track = (lambda: h.strategy._cw_v2_resting_track(h.state, None)) if slot == "first" else (
        lambda: h.strategy._cw_v2_reclaim_resting_track(h.state))
    track()
    cancel, = h.strategy.drain_pending_intents()
    mirror, = h.strategy.drain_webull_direct_intents()
    assert cancel.intent_type == mirror.intent_type == "cancel"
    assert "atr_reprice" not in cancel.metadata and "atr_reprice" not in mirror.metadata
    assert not h.strategy._rpg_entry_owned(h.state)
    track()
    opening, = h.strategy.drain_pending_intents()
    assert opening.intent_type == "open"
    assert not HandoffJournal(h.factory).jobs()


@pytest.mark.asyncio
@pytest.mark.parametrize("ticket", TICKETS, ids=lambda row: row["id"])
async def test_current_rpg_off_startup_restores_proof_dependent_ticket_ownership(monkeypatch, ticket):
    h = await runtime(monkeypatch, "schwab", strategy_overrides=TONIGHT)
    h.bot.settings = h.strategy.settings.model_copy(update=TONIGHT)
    restarted = SchwabV2Strategy(h.bot.settings)
    h.bot.strategy = restarted
    job = deepcopy(ticket["payload"])
    h.clock[0] = datetime.fromtimestamp((job.get("authorization") or {}).get("at", job["created_at"]), UTC) + timedelta(seconds=60)
    monkeypatch.setattr(restarted, "_now_ms", lambda: int(h.clock[0].timestamp() * 1000))
    # Bind every session gate on the NEW restored strategy, not just _now_ms.
    for name in ("_resting_in_window", "_resting_session_is_eh", "_entry_window_closed_for_session"):
        method = getattr(restarted, name)
        monkeypatch.setattr(restarted, name, lambda now=None, fn=method: fn(now or h.clock[0]))
    state = restarted.watchlist_state(job["old"]["symbol"])
    state.atr_state, state.atr_state_age, state.atr_trail = "short", 31, 5.0
    state.fanout_segment_id = job["segment_id"]
    state.atr_short_flip_bar_ts = int(job["old"]["metadata"]["rpg_short_segment"])
    state.bars.append(OHLCVBar(restarted._now_ms() - 60_000, 4.9, 5.0, 4.8, 4.9, 50_000))
    state.last_quote = Quote(state.symbol, 4.8, 4.9, 4.85, restarted._now_ms())
    with h.factory() as session:
        session.add(DashboardSnapshot(id=UUID(ticket["id"]), snapshot_type=SNAPSHOT_TYPE, payload=job))
        session.commit()
    assert not restarted._rpg_handoffs
    assert h.service.settings.oms_v2_webull_mirror_fresh_price_enabled
    await h.bot._rpg_handoff_pass()
    assert ticket["id"] in restarted._rpg_handoffs
    proven = ticket["id"] in {
        "fbfd692d-ec1f-5a39-9e11-1a133abccc96",
        "bd6ac0b9-727c-500b-8581-aabdd95992d4",
        "a007716c-4b50-5759-a354-ddc5b8961154",
    }
    assert old_buy_proven_clear(job) is proven
    assert restarted._rpg_entry_owned(state) is not proven
    restarted._cw_v2_resting_track(state, None)
    primary = restarted.drain_pending_intents()
    mirror = restarted.drain_webull_direct_intents()
    if proven:
        assert len(primary) == len(mirror) == 1
        assert primary[0].intent_type == mirror[0].intent_type == "open"
    else:
        assert not primary and not mirror
    assert not h.adapter.opens
