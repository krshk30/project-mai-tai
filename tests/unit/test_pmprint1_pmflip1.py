"""Recorded prices; guard/cache timing and callback interleavings are controlled.

The 14-row census is a price-proxy replay, NOT a decision-cache/fill replay.
Sources: Codex pmprint1-own-history/originals/veea JSON, pulled 2026-10-05.
"""

import asyncio
import logging
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP

import pytest

from test_schwab_1m_v2_eh_stream_cross import _bot, _send_levelone

from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy

PRINT_FLAG = "strategy_schwab_1m_v2_pm_print_ask_confirm_enabled"
FLIP_FLAG = "strategy_schwab_1m_v2_pm_flip_wait_enabled"

# symbol, actual decision timestamp UTC, trigger, logged last, original route ask
# (stream rows use captured raw ask). Exact cache timing is UNMEASURED.
REAL = [
    ("YMAT", "2026-09-09T11:45:41.519Z", 1.9975, 2.0, 2.01),
    ("TNON", "2026-09-10T11:48:06.506Z", 3.5454, 3.56, 3.56),
    ("SCNI", "2026-09-14T12:48:01.570Z", 2.3302, 2.34, 2.34),
    ("MYSZ", "2026-09-15T12:53:09.771Z", 2.4399, 2.44, 2.44),
    ("DAIC", "2026-09-17T12:50:46.048Z", 3.9849, 3.989, 3.99),
    ("IMCC", "2026-09-18T12:56:23.884Z", 3.1195, 3.13, 3.15),
    ("GLND", "2026-09-21T13:22:49.623Z", 3.0417, 3.05, 3.05),
    ("TOPS", "2026-09-22T12:05:39.730Z", 1.3453, 1.3498, 1.35),
    ("DCOY", "2026-09-23T12:23:15.261Z", 4.3148, 4.3169, 4.32),
    ("WHLR", "2026-09-23T13:22:44.740Z", 4.9325, 4.94, 4.94),
    ("LGHL", "2026-09-30T12:23:59.250Z", 7.0149, 7.02, 7.04),
    ("MEDS", "2026-10-01T11:57:53.874Z", 4.6438, 4.6489, 4.66),
    ("NXL", "2026-10-01T13:16:49.828Z", 7.1412, 7.16, 7.16),
    ("AMOD", "2026-10-02T12:32:34.930Z", 2.5693, 2.57, 2.58),
]
STRAYS = [
    ("SAIQ", "2026-10-05T12:10:35.281Z", 6.7928, 6.83, 6.24, 6.14, 1),
    ("TOPS", "2026-09-22T13:28:44.274Z", 1.3490, 1.39, 1.30, 1.28, None),
    ("QNME", "2026-09-22T12:54:46.223Z", 1.4363, 1.55, 1.41, None, 1),
    ("WHLR", "2026-09-23T13:14:35.011Z", 4.9325, 6.98, None, None, 0),
    ("TOPS", "2026-09-22T13:28:46.120Z", 1.3490, 1.37, 1.29, 1.28, 1),
    ("TOPS", "2026-09-22T13:29:16.922Z", 1.3490, 1.38, 1.29, None, 1),
]
VEEA = ("VEEA", "2026-10-05T12:33:04.184Z", 5.2613, 5.27, 5.27)


def ms(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def armed(case=VEEA, *, confirm=True, wait=True, **extra):
    symbol, at, trigger, px, ask = case[:5]
    options = {
        PRINT_FLAG: confirm, FLIP_FLAG: wait,
        "strategy_schwab_1m_v2_pm_rest_reprice_enabled": True,
        "strategy_schwab_1m_v2_confirmed_window_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled": True,
        "strategy_schwab_1m_v2_dual_broker_fanout_enabled": True,
        "strategy_schwab_1m_v2_cw_v2_resting_trigger_offset_pct": 0.5,
        "strategy_schwab_1m_v2_account_name": "live:schwab_1m_v2",
        "strategy_schwab_1m_v2_webull_account_name": "live:orb",
        "strategy_schwab_1m_v2_entry_notional_usd": 600,
        "strategy_schwab_1m_v2_webull_entry_notional_usd": 300,
    }
    options.update(extra)
    strategy = SchwabV2Strategy(Settings(_env_file=None, **options))
    clock = [ms(at)]
    strategy._now_ms = lambda: clock[0]
    session, window = strategy._resting_session_is_eh, strategy._resting_in_window
    strategy._resting_session_is_eh = lambda now=None: session(datetime.fromtimestamp(clock[0] / 1000, UTC))
    strategy._resting_in_window = lambda now=None: window(datetime.fromtimestamp(clock[0] / 1000, UTC))
    strategy._entries_held = False
    state = strategy.watchlist_state(symbol)
    state.resting_active = True
    state.resting_is_broker_order = False
    state.resting_level = trigger / 1.005
    state.resting_trigger = trigger
    state.atr_state = "short"
    state.atr_trail = state.resting_level
    state.atr_state_age = 6
    # Freshness/flatness are controlled preconditions, not reconstructed historical bars.
    state.bars.append(OHLCVBar(clock[0] // 60000 * 60000 - 60000, px, px, px, px, 17480))
    return strategy, state, clock


def cross(strategy, state, clock, px, ask, *, stream=True, bid=0, size=None):
    if stream:
        return strategy.on_stream_trade(state.symbol, px, clock[0], ask_price=ask,
                                        bid_price=bid, print_size=size, ask_age_ms=0)
    return strategy.on_quote(state.symbol, Quote(state.symbol, bid or 0, ask or 0, px, clock[0]))


@pytest.mark.parametrize("case", STRAYS, ids=[f"{r[0]}-{r[1]}" for r in STRAYS])
@pytest.mark.parametrize("stream", [True, False])
def test_t1_t2_t3_t9_recorded_stray_cannot_take_state(case, stream, caplog):
    strategy, state, clock = armed(case)
    before = deepcopy(asdict(state))
    with caplog.at_level(logging.INFO):
        assert cross(strategy, state, clock, case[3], case[4], stream=stream, bid=case[5], size=case[6]) is None
    after = asdict(state)
    # on_quote owns its quote cache; no entry/owner/slot/latch field may change.
    before.pop("last_quote")
    after.pop("last_quote")
    assert after == before
    assert strategy.drain_webull_fanout_intents() == []
    assert "V2-PM-CROSS-BLOCK" in caplog.text
    for label in ("print=", "size=", "bid=", "ask=", "ask_source=", "ask_age_ms=", "trigger=", "reason="):
        assert label in caplog.text


@pytest.mark.parametrize("case", REAL, ids=[r[0] for r in REAL])
def test_t4_fourteen_first_leg_price_proxies_emit_both_sized_legs(case):
    strategy, state, clock = armed(case)
    stream = case[0] in {"MEDS", "NXL", "AMOD"}
    draft = cross(strategy, state, clock, case[3], case[4], stream=stream)
    assert draft is not None
    legs = strategy.drain_webull_fanout_intents()
    assert len(legs) == 1
    for amount, leg in ((600, draft), (300, legs[0])):
        assert leg.quantity == (Decimal(amount) / Decimal(str(case[4]))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        assert Decimal(leg.metadata["pm_confirming_ask"]) == Decimal(str(case[4]))
        assert leg.metadata["pm_confirming_ask_age_ms"] == "0"


def test_t5_clro_recorded_555_ask_blocks_second_print():
    strategy, state, clock = armed(("CLRO", "2026-09-28T11:24:59.992Z", 5.559, 5.5689, 5.55))
    assert cross(strategy, state, clock, 5.5689, 5.55, bid=5.52, size=2) is None
    assert state.resting_flip_ms == 0 and state.resting_active


@pytest.mark.parametrize("bad_age", [10001, -1])
def test_t6_rest_stale_or_future_ask_blocks_then_fresh_quote_fires(bad_age):
    strategy, state, clock = armed()
    q = Quote("VEEA", 5.26, 5.27, 5.27, clock[0] - bad_age)
    assert strategy.on_quote("VEEA", q) is None
    assert state.resting_flip_ms == 0
    assert cross(strategy, state, clock, 5.27, 5.27, stream=False) is not None


def test_t6_no_ask_never_uses_a_midpoint_or_burns_latch():
    strategy, state, clock = armed()
    assert cross(strategy, state, clock, 5.27, None) is None
    assert strategy.on_quote("VEEA", Quote("VEEA", 5.26, 5.27, 0, clock[0])) is None
    assert state.resting_flip_ms == 0
    assert cross(strategy, state, clock, 5.27, 5.27) is not None


def test_t7_routing_uses_same_confirming_ask_not_rest_cache(monkeypatch):
    strategy, state, clock = armed()
    draft = cross(strategy, state, clock, 5.27, 5.27)
    legs = strategy.drain_webull_fanout_intents()
    bot = object.__new__(SchwabV2BotService)
    bot.strategy = strategy
    bot._eh_stream_ask_max_age_ms = 10000
    bot._last_quote_by_symbol = {"VEEA": Quote("VEEA", 5.0, 5.1, 5.1, clock[0])}
    for amount, leg in ((600, draft), (300, legs[0])):
        assert bot._apply_extended_hours_routing(leg, datetime.fromtimestamp(clock[0] / 1000, UTC))
        assert leg.metadata["limit_price"] == "5.27"
        assert leg.quantity == (Decimal(amount) / Decimal("5.27")).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    clock[0] += 10001
    assert not bot._apply_extended_hours_routing(draft, datetime.fromtimestamp(clock[0] / 1000, UTC))


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("tonight", [False, True], ids=["both_cards", "tonight"])
def test_s1_confirmed_cross_decided_before_open_cannot_route_after_0930(stream, tonight):
    from tests.unit.test_pmprint1_tonight_flags import TONIGHT
    strategy, state, clock = armed(**TONIGHT) if tonight else armed()
    # Recorded VEEA prices; callback timing is a controlled boundary interleaving.
    clock[0] = ms("2026-10-05T13:29:59Z")
    state.bars[-1].timestamp_ms = ms("2026-10-05T13:29:00Z")
    primary = cross(strategy, state, clock, 5.27, 5.27, stream=stream)
    mirror, = strategy.drain_webull_fanout_intents()
    assert primary is not None
    latch = state.resting_flip_ms
    bot = object.__new__(SchwabV2BotService)
    bot.strategy, bot._eh_stream_ask_max_age_ms = strategy, 10000
    bot._last_quote_by_symbol = {"VEEA": Quote("VEEA", 5.26, 5.27, 5.27, clock[0])}
    routed_at = datetime.fromisoformat("2026-10-05T13:30:00+00:00")
    for leg in (primary, mirror):
        assert not bot._apply_extended_hours_routing(leg, routed_at)
    assert state.resting_flip_ms == latch  # No ambiguous dispatch retry/second buy.


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("tonight", [False, True], ids=["both_cards", "tonight"])
def test_s2_software_rest_at_0930_does_not_cross_or_take_state(stream, tonight):
    from tests.unit.test_pmprint1_tonight_flags import TONIGHT
    strategy, state, clock = armed(**TONIGHT) if tonight else armed()
    clock[0] = ms("2026-10-05T13:30:00Z")
    state.bars[-1].timestamp_ms = ms("2026-10-05T13:29:00Z")
    quote = Quote("VEEA", 5.26, 5.27, 5.27, clock[0])
    assert strategy._eh_resting_cross_check(state, quote) is None
    assert cross(strategy, state, clock, 5.27, 5.27, stream=stream) is None
    assert state.resting_active and state.resting_flip_ms == 0
    legs = strategy.drain_webull_fanout_intents()
    if stream:
        assert not legs
    else:
        # M19 is deliberately unchanged: on_quote's existing RTH detector can
        # queue its Webull leg although this software rest did not convert yet.
        legacy, legacy_state, legacy_clock = armed(confirm=False, wait=False)
        legacy_clock[0] = clock[0]
        legacy_state.bars[-1].timestamp_ms = state.bars[-1].timestamp_ms
        assert cross(legacy, legacy_state, legacy_clock, 5.27, 5.27, stream=False) is None
        old_leg, = legacy.drain_webull_fanout_intents()
        leg, = legs
        assert (leg.symbol, leg.intent_type, leg.quantity, leg.metadata["fanout_source"]) == (
            old_leg.symbol, old_leg.intent_type, old_leg.quantity, old_leg.metadata["fanout_source"])


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("gate", ["not_in_window", "configured_cutoff"])
def test_s3_public_cross_respects_window_before_taking_latch(monkeypatch, stream, gate):
    strategy, state, clock = armed(
        strategy_schwab_1m_v2_entry_window_end_hour_et=8,
        strategy_schwab_1m_v2_entry_window_end_minute_et=30,
    )
    end = strategy._entry_window_closed_for_session
    monkeypatch.setattr(strategy, "_entry_window_closed_for_session",
                        lambda now=None: end(datetime.fromtimestamp(clock[0] / 1000, UTC)))
    if gate == "not_in_window":
        clock[0] = ms("2026-10-05T10:59:59Z")
        state.bars[-1].timestamp_ms = ms("2026-10-05T10:59:00Z")
    assert cross(strategy, state, clock, 5.27, 5.27, stream=stream) is None
    assert state.resting_active and state.resting_flip_ms == 0
    assert not strategy.drain_webull_fanout_intents()


@pytest.mark.parametrize("feature", ["pm_print_ask_confirm", "pm_flip_wait"])
def test_s2_feature_scope_ends_at_0930_and_excludes_broker_rest(feature):
    strategy, state, clock = armed()
    clock[0] = ms("2026-10-05T13:29:59Z")
    assert strategy._pm_rest_feature(state, feature)
    state.resting_is_broker_order = True
    assert not strategy._pm_rest_feature(state, feature)
    state.resting_is_broker_order = False
    clock[0] = ms("2026-10-05T13:30:00Z")
    assert not strategy._pm_rest_feature(state, feature)
    clock[0] = ms("2026-10-05T20:00:00Z")
    assert not strategy._pm_rest_feature(state, feature)


@pytest.mark.asyncio
async def test_t7_schwab_policy_reject_still_dispatches_webull(monkeypatch):
    strategy, state, clock = armed()
    draft = cross(strategy, state, clock, 5.27, 5.27)
    legs = strategy.drain_webull_fanout_intents()
    bot = object.__new__(SchwabV2BotService)
    bot.strategy = strategy
    calls = []

    async def reject_primary(d):
        calls.append(("schwab", d))
        return "Opening transactions for this security must be placed with a broker"

    async def send_webull(*, legs):
        calls.extend(("webull", leg) for leg in legs)

    monkeypatch.setattr(bot, "_maybe_emit", reject_primary)
    monkeypatch.setattr(bot, "_emit_webull_fanout_legs", send_webull)
    await bot._emit_eh_stream_draft(draft, legs)
    assert [account for account, _ in calls] == ["schwab", "webull"]
    assert calls[1][1].quantity == Decimal(57)


def test_t7_unsizable_webull_leg_is_error_and_counted(monkeypatch, caplog):
    strategy, state, clock = armed()
    original = strategy._sized_open
    monkeypatch.setattr(strategy, "_sized_open", lambda *a, **k: None if k["leg"] == "webull" else original(*a, **k))
    with caplog.at_level(logging.ERROR):
        assert cross(strategy, state, clock, 5.27, 5.27) is not None
    assert strategy._pm_unsized_legs == {"webull": 1}
    assert "V2-PM-LEG-SIZE-REFUSED" in caplog.text


def test_t8_lghl_stream_cap_skip_then_recorded_rest_cross():
    strategy, state, clock = armed(REAL[10])
    clock[0] = ms("2026-09-30T12:23:58.103Z")
    assert cross(strategy, state, clock, 7.07, 7.07) is None
    assert state.resting_flip_ms == 0
    clock[0] = ms("2026-09-30T12:23:59.250Z")
    assert cross(strategy, state, clock, 7.02, 7.04, stream=False) is not None


def test_t10_flag_off_reproduces_saiq_false_buy_and_zero_ask_webull(caplog):
    strategy, state, clock = armed(STRAYS[0], confirm=False)
    with caplog.at_level(logging.WARNING):
        assert cross(strategy, state, clock, 6.83, 6.24) is not None
    assert strategy.drain_webull_fanout_intents() == []
    assert "price=0.0" in caplog.text


def flip(strategy, state, clock):
    clock[0] = ms("2026-10-05T12:33:02.504Z")
    state.atr_state = "long"
    strategy._cw_v2_resting_track(state, {"state": "long", "flip": "BUY"})


def test_ft1_veea_flip_then_1680ms_late_print_emits_both_legs():
    strategy, state, clock = armed()
    flip(strategy, state, clock)
    assert state.pm_resting_flip_seen_ms == clock[0] and state.resting_flip_ms == 0
    clock[0] = ms(VEEA[1])
    assert cross(strategy, state, clock, 5.27, 5.27, bid=5.26, size=280) is not None
    assert len(strategy.drain_webull_fanout_intents()) == 1
    assert state.resting_flip_ms == clock[0]


def test_ft1_recorded_veea_print_after_30s_still_watches_until_bar_takedown():
    strategy, state, clock = armed()
    flip(strategy, state, clock)
    # Own rows 6794048 / 3243244: recorded print and last quote before it.
    clock[0] = ms("2026-10-05T12:33:33.821Z")
    draft = strategy.on_stream_trade("VEEA", 5.2691, clock[0], ask_price=5.27,
                                     bid_price=5.26, print_size=1, ask_age_ms=1130)
    assert draft is not None
    assert len(strategy.drain_webull_fanout_intents()) == 1


@pytest.mark.parametrize("first_stream", [True, False])
def test_ft2_ft7_cross_then_flip_or_other_path_cannot_double_emit(first_stream):
    strategy, state, clock = armed()
    assert cross(strategy, state, clock, 5.27, 5.27, stream=first_stream) is not None
    latch = state.resting_flip_ms
    strategy._cw_v2_resting_track(state, {"state": "long", "flip": "BUY"})
    assert state.resting_flip_ms == latch
    assert cross(strategy, state, clock, 5.27, 5.27, stream=not first_stream) is None
    assert len(strategy.drain_webull_fanout_intents()) == 1


def test_ft3_flip_without_cross_keeps_existing_grace_and_next_bar_takedown(caplog):
    strategy, state, clock = armed()
    flip(strategy, state, clock)
    clock[0] += strategy._resting_flip_grace_ms - 1
    strategy._cw_v2_resting_track(state, {"state": "long"})
    assert state.resting_active and state.resting_flip_ms == 0
    clock[0] = ms("2026-10-05T12:34:02.972Z")
    with caplog.at_level(logging.INFO):
        strategy._cw_v2_resting_track(state, {"state": "long"})
    assert not state.resting_active
    assert "reason=flip_no_fill_soft_rest" in caplog.text
    assert cross(strategy, state, clock, 5.27, 5.27) is None


def test_ft4_weto_real_tick_645330ms_after_takedown_never_reenters():
    # Own market_trade_ticks row6233046: first post-takedown trigger-reaching tick.
    case = ("WETO", "2026-09-24T13:23:00.624Z", 2.0778, 2.085, 2.09)
    strategy, state, clock = armed(case)
    strategy._cw_v2_resting_track(state, {"state": "long", "flip": "BUY"})
    clock[0] = ms("2026-09-24T13:24:00.624Z")
    strategy._cw_v2_resting_track(state, {"state": "long"})
    assert not state.resting_active
    clock[0] = ms("2026-09-24T13:34:45.954Z")
    assert cross(strategy, state, clock, 2.085, 2.09, bid=2.08) is None
    assert strategy.drain_webull_fanout_intents() == []


def test_ft5_sell_flip_ends_wait_before_a_later_print():
    strategy, state, clock = armed()
    flip(strategy, state, clock)
    strategy._cw_v2_track(state, {"flip": "SELL", "state": "short"})
    assert not state.resting_active and state.pm_resting_flip_seen_ms == 0
    assert cross(strategy, state, clock, 5.27, 5.27) is None


@pytest.mark.parametrize("guard", ["boot", "gap", "cutoff", "removed", "filled"])
def test_ft6_existing_guards_still_block_during_wait(guard):
    strategy, state, clock = armed(strategy_schwab_1m_v2_gap_hold_enabled=True)
    flip(strategy, state, clock)
    if guard == "boot":
        strategy._entries_held = True
    elif guard == "gap":
        state.gap_hold_active = True
    elif guard == "cutoff":
        clock[0] = ms("2026-10-05T19:45:00Z")
    elif guard == "removed":
        strategy.release_and_drop_symbol(state.symbol)
    else:
        state.position_qty = state.position_qty_held = 114
    assert cross(strategy, state, clock, 5.27, 5.27) is None
    assert strategy.drain_webull_fanout_intents() == []


def test_ft8_flag_off_reproduces_veea_flip_latch_miss():
    strategy, state, clock = armed(wait=False)
    flip(strategy, state, clock)
    assert state.resting_flip_ms == clock[0]
    clock[0] = ms(VEEA[1])
    assert cross(strategy, state, clock, 5.27, 5.27) is None


@pytest.mark.parametrize("case", [VEEA, STRAYS[0]])
def test_t11_ft9_three_flags_move_then_flip_then_recorded_print(case):
    strategy, state, clock = armed(case)
    strategy._reprice_resting(state, state.resting_level * 1.006)
    assert state.resting_active
    # Restore the recorded level through the same PMREST helper, not a cancel/re-arm.
    strategy._reprice_resting(state, case[2] / 1.005)
    strategy._cw_v2_resting_track(state, {"state": "long", "flip": "BUY"})
    draft = cross(strategy, state, clock, case[3], case[4])
    assert (draft is not None) is (case[0] == "VEEA")
    assert len(strategy.drain_webull_fanout_intents()) == int(case[0] == "VEEA")


def test_flags_default_off_and_rth_broker_flip_retains_legacy_latch():
    settings = Settings(_env_file=None)
    assert not getattr(settings, PRINT_FLAG) and not getattr(settings, FLIP_FLAG)
    strategy, state, clock = armed()
    clock[0] = ms("2026-10-05T14:00:00Z")
    state.resting_is_broker_order = True
    strategy._cw_v2_resting_track(state, {"state": "long"})
    assert state.resting_flip_ms == clock[0] and state.pm_resting_flip_seen_ms == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ask_age", [10000, 10001])
async def test_t6_real_service_stream_cache_existing_ten_second_bound(monkeypatch, ask_age):
    strategy, state, clock = armed()
    bot, written, emitted = _bot(monkeypatch, **{PRINT_FLAG: True, FLIP_FLAG: True})
    bot.strategy = strategy
    bot._eh_stream_ask_by_symbol["VEEA"] = (5.27, clock[0] - ask_age)
    # Actual VEEA row6793965, omitting field2 to exercise the unchanged-ask cache.
    await _send_levelone(bot, {"0": "VEEA", "1": 5.26, "3": 5.27, "9": 280,
                              "35": clock[0]}, clock[0])
    await asyncio.sleep(0)
    assert len(emitted) == int(ask_age == 10000)
    assert written
    if emitted:
        assert emitted[0].metadata["pm_confirming_ask_age_ms"] == str(ask_age)
        assert emitted[0].metadata["pm_confirming_ask_source"] == "stream_ask_cache"
    else:
        assert state.resting_flip_ms == 0


@pytest.mark.asyncio
async def test_t1_service_raw_saiq_print_records_bid_size_and_ask_age(monkeypatch, caplog):
    strategy, state, clock = armed(STRAYS[0])
    bot, written, emitted = _bot(monkeypatch, **{PRINT_FLAG: True})
    bot.strategy = strategy
    with caplog.at_level(logging.INFO):
        await _send_levelone(bot, {"0": "SAIQ", "1": 6.14, "2": 6.24, "3": 6.83,
                                  "9": 1, "35": clock[0]}, clock[0])
    await asyncio.sleep(0)
    assert len(written) == 2 and not emitted and not state.resting_flip_ms
    assert "size=1 bid=6.14 ask=6.24 ask_source=stream_ask_cache ask_age_ms=0" in caplog.text


@pytest.mark.asyncio
async def test_mi_recorded_ask_past_band_still_abandoned_by_oms(monkeypatch):
    from test_oms_v2_eh_resting_entry import _oms, _set_quote, _stored_order, _v2_open
    import project_mai_tai.oms.service as oms_module

    monkeypatch.setattr(oms_module, "_extended_hours_session", lambda _now=None: "AM")
    # Actual MI quote3246088 at09:15:20.309; strategy trigger2.7288/cap2.7424.
    strategy, state, clock = armed(("MI", "2026-10-05T13:15:20.309Z", 2.7288, 2.749, 2.75))
    draft = cross(strategy, state, clock, 2.749, 2.75, stream=False)
    assert draft is not None  # no new upper cap on the five-second path
    service = _oms(strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=True)
    _set_quote(service, "MI", bid=2.73, ask=2.75)
    events = await service.process_trade_intent(_v2_open(draft.metadata, symbol="MI"))
    assert events[-1].payload.reason == "ASK_PAST_BAND"
    assert _stored_order(service) is None


@pytest.mark.asyncio
async def test_mi_working_limit_still_quote_drift_cancelled():
    from sqlalchemy import select
    from test_oms_risk_service import FakeRedis, FakeWorkingOrderRefreshBrokerAdapter, build_test_session_factory, _noop_sync_broker_state
    from project_mai_tai.db.models import BrokerOrder
    from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload, TradeIntentEvent, TradeIntentPayload
    from project_mai_tai.oms.service import OmsRiskService

    factory = build_test_session_factory()
    adapter = FakeWorkingOrderRefreshBrokerAdapter(ask_price=2.74, bid_price=2.73)
    service = OmsRiskService(settings=Settings(_env_file=None, redis_stream_prefix="test", oms_adapter="simulated",
                                             oms_quote_drift_cancel_tolerance_cents=1.0,
                                             strategy_schwab_1m_v2_entry_notional_usd=0),
                             redis_client=FakeRedis(), session_factory=factory, broker_adapter=adapter)
    service.sync_broker_state = _noop_sync_broker_state
    await service.process_trade_intent(TradeIntentEvent(source_service="schwab-1m-v2", payload=TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name="paper:schwab_1m_v2", symbol="MI", side="buy",
        quantity=Decimal(219), intent_type="open", reason="schwab_1m_v2 ATR Flip CW-v2-resting",
        metadata={"order_type": "limit", "limit_price": "2.74", "reference_price": "2.74",
                  "eh_resting": "true", "resting_entry": "true", "resting_level": "2.7288",
                  "resting_band_pct": "0.5", "entry_price": "2.7288"})))
    # Actual next capture3246089: ask2.79; controlled working-order precondition.
    await service._handle_stream_message({"data": QuoteTickEvent(source_service="market-data", payload=QuoteTickPayload(
        symbol="MI", bid_price=Decimal("2.75"), ask_price=Decimal("2.79"))).model_dump_json()})
    with factory() as session:
        order = session.scalar(select(BrokerOrder).where(BrokerOrder.symbol == "MI"))
        assert order.status == "cancelled"
        assert order.payload["abandon_reason_code"] == "QUOTE_DRIFT_CANCEL"
    assert [r.intent_type for r in adapter.submit_requests] == ["open", "cancel"]
