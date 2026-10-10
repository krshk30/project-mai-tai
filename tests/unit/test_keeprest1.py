"""Recorded bot probes/levels; clocks, fills and executable asks are controls."""

import json
import logging
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from tests.unit.test_all_on_pm import ALL_ON, CompletedLineControl
from tests.unit.test_pmprint1_pmflip1 import armed, cross, ms

FLAG = "strategy_schwab_1m_v2_keep_rest_after_buy_enabled"
COMPOSED = {**ALL_ON, FLAG: True}
RAW = json.loads((Path(__file__).parents[1] / "fixtures/keeprest1/recorded.json").read_text())
CASES = RAW["cases"]
IDS = [f"{c['symbol']}-{c['cancel_et']}" for c in CASES]


def seeded(case=None, *, enabled=True, all_on=False, wait=True):
    case = case or CASES[-1]
    price = float(case["historical_logged_level"])
    strategy, state, clock = armed(
        (case["symbol"], case["cancel_et"], price, price, price),
        wait=wait, **(COMPOSED if all_on else {FLAG: enabled}),
    )
    if all_on:
        control = CompletedLineControl(strategy)
        strategy._composition_line = control
        strategy._line_readiness = control.ready
        strategy._line_version_reader = control.version
    # Legacy EH logs do not expose the raw line separately. That missing field
    # is not reconstructed from a modern offset: seed a controlled raw line.
    if case["recorded_line"] is not None:
        state.resting_level = case["recorded_line"]
    state.resting_trigger = price
    state.resting_is_broker_order = "RESTING-CANCEL]" in (case["cancel_log"] or {}).get("text", "")
    state.bars[-1] = OHLCVBar(case["probe_bar_ms"], price, price, price,
                            case["probe_close"], case["probe_volume"])
    return strategy, state, clock


def track(strategy, state, *, atr_state="long", flip=None):
    state.atr_state = atr_state
    state.atr_trail = max(state.resting_level / 2, 0.01)
    strategy._cw_v2_resting_track(state, {"state": atr_state, "trail": state.atr_trail,
                                         "flip": flip, "state_age": 6})


def next_bar(state, clock, *, volume=25_000):
    clock[0] += 60_000
    price = state.resting_trigger or 1
    state.bars.append(OHLCVBar(clock[0] // 60_000 * 60_000 - 60_000,
                              price, price, price, price, volume))


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_recorded_bot_shape_waiting_buy_or_taken_cross_is_not_relabelled(case):
    strategy, state, clock = seeded(case)
    prior = case["exact_level_cross_before_cancel"]
    if prior:
        state.resting_flip_ms = ms(prior["at_utc"].replace(" ", "T"))
    before = state.resting_level, state.resting_trigger
    track(strategy, state, atr_state=case["observed_state"])
    if prior:
        # TOPS/IMCC/SAIQ are SHORT controls; IMCC/WHLR/MI can be LONG
        # after a taken cross. Neither is a waiting BUY saved by KEEPREST1.
        assert not state.resting_buy_frozen
        assert not state.resting_active
    elif case["observed_state"] == "long":
        assert state.resting_buy_frozen and state.resting_active
        assert (state.resting_level, state.resting_trigger) == before
        assert state.resting_flip_ms == 0
    assert not any(d.intent_type == "open" for d in strategy.drain_pending_intents())
    assert not strategy.drain_webull_fanout_intents()


def test_population_and_later_reach_denominator_are_explicit():
    assert len(CASES) == 18
    assert sum(c["classification"] == "human_authorized_chart_flip_case" for c in CASES) == 17
    assert sum(c["observed_state"] == "long" for c in CASES) == 15
    assert sum(c["exact_level_cross_before_cancel"] is not None for c in CASES) == 6
    assert sum(c["first_later_bar_high_reach"] is not None for c in CASES) == 14
    assert RAW["read_bounds"]["coverage_complete"]
    assert RAW["read_bounds"]["bytes_scanned"] <= 128 * 1024 * 1024
    for c in CASES:
        reach = c["first_later_bar_high_reach"]
        if reach:
            assert ms(reach["bar_time"].replace(" ", "T")) > ms(c["cancel_et"])
            assert Decimal(reach["high_price"]) >= Decimal(c["historical_logged_level"])


@pytest.mark.parametrize("rth", [False, True])
@pytest.mark.parametrize("wait", [False, True])
def test_waiting_buy_keeps_exact_line_for_hours_without_new_timer(rth, wait, caplog):
    strategy, state, clock = seeded(wait=wait)
    clock[0] = ms("2026-10-06T14:00:02Z" if rth else "2026-10-06T11:00:02Z")
    state.resting_is_broker_order = rth
    state.bars[-1].timestamp_ms = clock[0] - 60_000
    before = state.resting_level, state.resting_trigger
    with caplog.at_level(logging.INFO):
        track(strategy, state, flip="BUY")
        for _ in range(90):
            next_bar(state, clock)
            track(strategy, state)
    assert state.resting_active and state.resting_buy_frozen
    assert (state.resting_level, state.resting_trigger) == before
    assert state.resting_flip_ms == 0
    assert len([r for r in caplog.records if "V2-KEEP-REST-BUY" in r.message]) == 1
    assert not strategy.drain_pending_intents()
    assert not strategy.drain_webull_fanout_intents()


@pytest.mark.parametrize("rth", [False, True])
def test_sell_cancels_one_owned_waiting_order_and_mirror(rth):
    strategy, state, clock = seeded()
    state.resting_is_broker_order = rth
    state.webull_resting_active = rth
    track(strategy, state, flip="BUY")
    strategy._cw_v2_track(state, {"state": "short", "flip": "SELL"})
    assert not state.resting_active and not state.resting_buy_frozen
    assert state.resting_frozen_floor_bar_ms == 0
    primary = strategy.drain_pending_intents()
    mirror = strategy.drain_webull_direct_intents()
    assert len(primary) == len(mirror) == int(rth)
    assert all(d.intent_type == "cancel" and d.metadata["reason"] == "keep_rest_sell"
               for d in primary + mirror)


@pytest.mark.parametrize("cleanup", ["fill", "fill_after_owner_clear", "window", "gap", "session"])
def test_waiting_freeze_canonical_cleanup(cleanup):
    strategy, state, clock = seeded()
    track(strategy, state, flip="BUY")
    if cleanup in {"fill", "fill_after_owner_clear"}:
        if cleanup == "fill_after_owner_clear":
            state.resting_active = False  # Per-leg feedback can clear the active view first.
        state.position_qty_held = 5
        track(strategy, state)
    elif cleanup == "window":
        clock[0] = ms("2026-10-06T20:00:00Z")
        track(strategy, state)
    elif cleanup == "gap":
        strategy.gap_hold_active = lambda _symbol: True
        track(strategy, state)
    else:
        strategy._apply_session_anchor_reset(state, state.atr_session_anchor_ms + 86_400_000)
    assert not state.resting_active and not state.resting_buy_frozen
    assert state.resting_frozen_floor_bar_ms == 0


def test_three_completed_thin_bars_cancel_not_three_evaluations():
    strategy, state, clock = seeded()
    track(strategy, state, flip="BUY")
    for count in (1, 2, 3):
        next_bar(state, clock, volume=0)
        for _ in range(5):
            track(strategy, state)
        assert state.resting_active == (count < 3)
        if count < 3:
            assert state.resting_below_floor_bars == count
    assert not state.resting_buy_frozen


def test_liquid_bar_resets_thin_streak_and_reclaim_is_untouched():
    strategy, state, clock = seeded()
    track(strategy, state, flip="BUY")
    next_bar(state, clock, volume=0)
    track(strategy, state)
    assert state.resting_below_floor_bars == 1
    next_bar(state, clock)
    track(strategy, state)
    assert state.resting_below_floor_bars == 0
    state.resting_slot = "reclaim"
    before = deepcopy(asdict(state))
    track(strategy, state)
    assert asdict(state) == before


@pytest.mark.parametrize("stream", [False, True])
def test_all_on_later_olox_price_proxy_crosses_once_with_sizing_and_grace(stream):
    strategy, state, clock = seeded(all_on=True)
    assert len(COMPOSED) == 13 and all(getattr(strategy.settings, f) for f in COMPOSED)
    track(strategy, state, flip="BUY")
    case = CASES[-1]
    clock[0] = ms(case["first_later_bar_high_reach"]["bar_time"].replace(" ", "T")) + 1000
    state.bars[-1].timestamp_ms = clock[0] - 61_000
    # A retained high supplies only the price proxy. Fresh simultaneous print
    # and ask are controls, NOT claimed as a retained executable quote.
    price = float(case["first_later_bar_high_reach"]["high_price"])
    ask = state.resting_trigger  # Controlled executable ask; no historical ask attestation.
    if stream:
        assert cross(strategy, state, clock, price, price, stream=True) is None
        assert state.resting_flip_ms == 0  # The existing upper band still refuses.
    primary = cross(strategy, state, clock, price, ask, stream=stream)
    assert primary is not None
    mirror, = strategy.drain_webull_fanout_intents()
    latch = state.resting_flip_ms
    for amount, draft in ((600, primary), (300, mirror)):
        assert draft.quantity == (Decimal(amount) / Decimal(str(ask))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    for _ in range(5):
        assert cross(strategy, state, clock, price, ask, stream=stream) is None
    assert not strategy.drain_webull_fanout_intents()
    clock[0] += 1
    track(strategy, state)
    assert state.resting_flip_ms == latch
    clock[0] += strategy._resting_flip_grace_ms
    track(strategy, state)
    assert not state.resting_active and not state.resting_buy_frozen


@pytest.mark.parametrize("rth", [False, True])
def test_off_restores_legacy_chart_flip_expiry(rth):
    strategy, state, clock = seeded(enabled=False)
    clock[0] = ms("2026-10-06T14:00:02Z" if rth else "2026-10-06T12:00:02Z")
    state.resting_is_broker_order = rth
    track(strategy, state, flip="BUY")
    assert not state.resting_buy_frozen
    assert bool(state.resting_flip_ms) == rth
    clock[0] += strategy._resting_flip_grace_ms
    track(strategy, state)
    assert not state.resting_active


def test_default_off_catalog_on_one_owner():
    assert getattr(Settings(_env_file=None), FLAG) is False
    catalog = json.loads((Path(__file__).parents[2] / "ops/health/expected_flags.json").read_text())
    flag, = [f for f in catalog["flags"] if f["name"] == FLAG]
    assert flag["expected"] is True and flag["owning_service"] == "schwab-1m-v2"
    assert not flag.get("also_check_services")


@pytest.mark.parametrize("pm", [False, True])
@pytest.mark.parametrize("reason", ["CW_HARD_STOP", "CONFIRMATION_EXIT"])
def test_max_zero_one_closed_trade_stays_consumed_in_both_sessions(pm, reason):
    from tests.unit import test_v2_retry_one as retry

    strategy, clock, writes = retry._strategy(max_retries=0)
    setattr(strategy.settings, FLAG, True)
    strategy._eh_resting_enabled = pm
    strategy._resting_session_is_eh = lambda now=None: pm
    retry._sell_segment(strategy, clock, "KEEPZERO")
    state = strategy.watchlist_state("KEEPZERO")
    retry._book(strategy, clock, "KEEPZERO")
    strategy._queue_resting_place(state, 3.859)
    primary = strategy.drain_pending_intents()
    assert len(primary) == int(not pm)
    assert state.resting_active
    strategy._cw_v2_track(state, {"flip": "BUY", "state": "long"})
    state.atr_state = "long"
    track(strategy, state)
    assert state.resting_buy_frozen and state.flip_owner_phase == "awaiting_fill"
    retry._fill_primary(strategy, clock, "KEEPZERO", "controlled-keep-row")
    track(strategy, state)
    assert not state.resting_buy_frozen
    retry._close_primary(strategy, clock, "KEEPZERO", "controlled-keep-row", reason)
    # A confirmed BUY is already bound for this segment, not a pre-flip
    # retry-budget release. Closing it cannot grant another first entry.
    assert state.flip_owner_phase == "bound"
    assert state.retry_one_closes_in_segment == 0
    assert [count for _, _, count in writes] == [0]
    strategy._queue_resting_place(state, 3.80)
    assert not state.resting_active and not strategy.drain_pending_intents()


def test_unknown_dispatch_remains_owned_and_cannot_rearm_after_cross_grace():
    from tests.unit import test_v2_retry_one as retry

    strategy, clock, _ = retry._strategy(max_retries=0)
    setattr(strategy.settings, FLAG, True)
    state, opportunity = retry._place_first(strategy, clock, "KEEPUNKNOWN")
    track(strategy, state, flip="BUY")
    assert state.resting_buy_frozen
    state.resting_flip_ms = clock[0]
    strategy._set_flip_owner_unknown(state, reason="controlled_unknown_dispatch")
    clock[0] += strategy._resting_flip_grace_ms
    track(strategy, state)
    assert not state.resting_active and not state.resting_buy_frozen
    assert all(d.intent_type == "cancel" for d in strategy.drain_pending_intents())
    strategy._queue_resting_place(state, 3.80)
    assert not state.resting_active and not strategy.drain_pending_intents()
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == opportunity


def test_all_on_effective_catalog_zero_with_controlled_process_environments():
    from ops.health.expected_flags_check import ServiceEnvironment, audit, load_catalog, load_numeric_catalog

    root = Path(__file__).parents[2]
    entries = load_catalog(root / "ops/health/expected_flags.json") + load_numeric_catalog(
        root / "ops/health/expected_numeric.json")
    environments = {}
    for entry in entries:
        for service in [entry["owning_service"], *entry.get("also_check_services", [])]:
            environments.setdefault(service, {})["MAI_TAI_" + entry["name"].upper()] = str(entry["expected"]).lower()
    settings = Settings(_env_file=None, **COMPOSED)
    assert len(COMPOSED) == 13 and all(getattr(settings, key) for key in COMPOSED)
    rc, lines = audit(entries, environment_reader=lambda service: ServiceEnvironment(
        101, environments[service], frozenset(), None))
    assert rc == 0
    assert lines[-1] == "Final call: PASS; checked=155/155 mismatches=0 unknown=0"
    assert len(environments) == 9  # ALERTS1 adds the reconciler


@pytest.mark.parametrize("pm", [False, True])
def test_max_zero_preflip_close_spends_one_budget_and_refuses_rebuy(pm):
    from tests.unit import test_v2_retry_one as retry

    strategy, clock, writes = retry._strategy(max_retries=0)
    setattr(strategy.settings, FLAG, True)
    strategy._eh_resting_enabled = pm
    strategy._resting_session_is_eh = lambda now=None: pm
    retry._sell_segment(strategy, clock, "PREZERO")
    state = strategy.watchlist_state("PREZERO")
    retry._book(strategy, clock, "PREZERO")
    strategy._queue_resting_place(state, 3.859)
    strategy.drain_pending_intents()
    retry._fill_primary(strategy, clock, "PREZERO", "preflip-row")
    retry._close_primary(strategy, clock, "PREZERO", "preflip-row", "CW_HARD_STOP")
    assert state.flip_owner_phase == "consumed"
    assert state.retry_one_closes_in_segment == 1
    assert [count for _, _, count in writes] == [0, 1]
    strategy._queue_resting_place(state, 3.80)
    assert not strategy.drain_pending_intents()
