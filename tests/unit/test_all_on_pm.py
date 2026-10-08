"""ALL-ON candidate proof: retained prices, controlled clocks/transport, no live IO."""

import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import sys

import pytest

from project_mai_tai.market_data import schwab_v2_rest_client as rest_module
from project_mai_tai.market_data.schwab_v2_rest_client import Quote, SchwabV2RestClient
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy, session_start_ts_ms
from tests.unit import test_pmrest1 as pmrest
from tests.unit import test_rpg1_runtime_nfq as nfq
from tests.unit.test_rpg1_runtime import begin, feedback, runtime, tick_clock
from tests.unit import test_schwab_1m_v2_eh_stream_cross as stream_helpers

# The retained unit helper uses pytest's unit-directory import spelling.
sys.modules.setdefault("test_schwab_1m_v2_eh_stream_cross", stream_helpers)
from tests.unit.test_pmprint1_pmflip1 import STRAYS, VEEA, armed, cross, flip, ms  # noqa: E402
from tests.unit import test_pmprint1_pmflip1 as pmprint  # noqa: E402

_bot, _send_levelone = stream_helpers._bot, stream_helpers._send_levelone

ALL_ON = {
    "strategy_schwab_1m_v2_slotclear_fresh_flip_enabled": True,
    "strategy_schwab_1m_v2_slotclear_fresh_sell_enabled": True,
    "oms_v2_eh_fresh_price_enabled": True,
    "strategy_schwab_1m_v2_pm_print_ask_confirm_enabled": True,
    "strategy_schwab_1m_v2_pm_flip_wait_enabled": True,
    "strategy_schwab_1m_v2_pm_rest_reprice_enabled": True,
    "strategy_schwab_1m_v2_atr_reprice_handoff_enabled": True,
    "oms_v2_webull_mirror_fresh_price_enabled": True,
    "strategy_schwab_1m_v2_gap_hold_enabled": True,
    "strategy_schwab_1m_v2_gap_line_carry_enabled": True,
    "strategy_schwab_1m_v2_resting_buy_round_up_enabled": True,
    "strategy_schwab_1m_v2_line_chart_restoration_enabled": True,
}
CLRO = ("CLRO", "2026-09-28T11:24:59.992Z", 5.559, 5.5689, 5.55)


class CompletedLineControl:
    """Controlled worker output for the legacy, explicitly seeded ATR fixtures.

    This is not a history attestation or a mathematical reconstruction. The
    acceptance factory/runner exercises those boundaries without this control.
    Here only the external completed-line prerequisite is supplied; strategy
    admission and the service's draft-version check remain real.
    """

    def __init__(self, strategy):
        self.strategy = strategy
        self.complete = True

    def ready(self, symbol):
        state = self.strategy._symbol_states.get(symbol)
        return bool(self.complete and state is not None and state.bars
                    and state.atr_state in {"short", "long"}
                    and state.atr_trail is not None and state.atr_trail > 0
                    and not self.strategy.gap_hold_active(symbol))

    def version(self, symbol):
        state = self.strategy._symbol_states.get(symbol)
        return f"controlled-completed-line:{symbol}:{state.bars[-1].timestamp_ms}" if state and state.bars else ""


@pytest.fixture(autouse=True)
def completed_seeded_line(monkeypatch):
    original = SchwabV2Strategy.__init__

    def init(strategy, *args, **kwargs):
        original(strategy, *args, **kwargs)
        if strategy._line_restoration_enabled:
            control = CompletedLineControl(strategy)
            strategy._composition_line = control
            strategy._line_readiness = control.ready
            strategy._line_version_reader = control.version

    monkeypatch.setattr(SchwabV2Strategy, "__init__", init)


def assert_all_on(settings):
    assert len(ALL_ON) == 12
    assert all(getattr(settings, key) is True for key in ALL_ON)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["stream", "five_second_rest"])
@pytest.mark.parametrize("case", [STRAYS[0], VEEA, CLRO], ids=["SAIQ", "VEEA", "CLRO"])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_all_on_recorded_service_cross_same_ask_both_legs(monkeypatch, route, case, slot):
    strategy, state, clock = armed(case, **ALL_ON)
    state.resting_slot = slot  # Existing software reclaim control, not new PM admission.
    assert_all_on(strategy.settings)
    bot, written, _ = _bot(monkeypatch, **ALL_ON)
    bot.strategy, bot.settings = strategy, strategy.settings
    bot._watchlist = {state.symbol}
    # Supply the same controlled worker output to both gates, not a waiver of
    # _line_draft_allowed (which must still compare the captured version).
    bot._line_buy_ready = strategy._composition_line.ready
    bot._line_version = strategy._composition_line.version
    assert bot._gap_hold_enabled and strategy._gap_hold_enabled
    dispatched = []

    class DecisionClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0] / 1000, UTC).astimezone(tz or UTC)

    monkeypatch.setattr(bot_module, "datetime", DecisionClock)

    class CaptureEmitter:
        async def emit(self, draft):
            dispatched.append(draft)

    bot.intent_emitter = CaptureEmitter()
    bot.webull_intent_emitter = CaptureEmitter()
    monkeypatch.setattr(bot, "_maybe_emit", bot_module.SchwabV2BotService._maybe_emit.__get__(bot))
    monkeypatch.setattr(bot, "_emit_webull_fanout_legs",
                        bot_module.SchwabV2BotService._emit_webull_fanout_legs.__get__(bot))
    if case == VEEA and slot == "first":
        # Recorded 08:32 completed-bar flip processed at 08:33:02.504;
        # the known signal is supplied, not claimed as a fresh ATR reconstruction.
        clock[0] = ms("2026-10-05T12:33:02.504Z")
        state.bars[-1].timestamp_ms = ms("2026-10-05T12:31:00Z")
        state.atr_session_anchor_ms = session_start_ts_ms(clock[0])
        signal = {"state": "long", "flip": "BUY", "trail": 5.035190,
                  "state_age": 0, "flip_level": 5.235129, "touch": False}

        def recorded_flip(st, completed_bar):
            assert completed_bar.timestamp_ms == ms("2026-10-05T12:32:00Z")
            st.atr_state, st.atr_state_age, st.atr_trail = "long", 0, 5.035190
            st.atr_short_flip_bar_ts = 0
            return signal

        monkeypatch.setattr(strategy, "_update_atr_state", recorded_flip)
        # H/L/C/volume are retained; the unretained open is a controlled input.
        bar = OHLCVBar(ms("2026-10-05T12:32:00Z"), 5.2, 5.2569, 5.2, 5.2404, 27895)
        assert strategy.on_observed_bar("VEEA", bar, observation_phase="live") is None
        # With Restoration ON, the callback advances the bar but only the
        # worker's publication may evaluate the supplied current-bar signal.
        recorded_flip(state, bar)
        assert strategy._evaluate_completed_bar(
            state, is_new_bar=True, restored=True, restored_signal=signal,
        ) is None
        assert state.pm_resting_flip_seen_ms and not state.resting_flip_ms
        clock[0] = ms(VEEA[1])
    before = deepcopy(asdict(state))
    bid, size = (6.14, 1) if case == STRAYS[0] else ((5.52, 2) if case == CLRO else (5.26, 280))
    if route == "stream":
        await _send_levelone(bot, {"0": state.symbol, "1": bid, "2": case[4],
                                  "3": case[3], "9": size, "35": clock[0]}, clock[0])
        if bot._eh_stream_emit_tasks:
            await asyncio.gather(*tuple(bot._eh_stream_emit_tasks))
        assert written
    else:
        async def no_bar(*args):
            pytest.fail("quote poll must not request a bar")

        client = SchwabV2RestClient(strategy.settings, on_chart_bar=no_bar, on_quote=bot._handle_quote)
        client.set_desired_symbols({state.symbol})
        monkeypatch.setattr(client, "_fetch_quotes", lambda symbols: [
            Quote(state.symbol, bid, case[4], case[3], clock[0], clock[0])])
        sleeps = []
        original_sleep = asyncio.sleep

        async def controlled_sleep(seconds):
            sleeps.append(seconds)
            await original_sleep(0)

        monkeypatch.setattr(rest_module.asyncio, "sleep", controlled_sleep)
        assert strategy.settings.strategy_schwab_1m_v2_quote_poll_interval_seconds == 5
        await client._quote_loop_pass(5.0)
        assert sleeps == [5.0]
    if case != VEEA:
        assert not dispatched and not strategy.drain_webull_fanout_intents()
        after = asdict(state)
        before.pop("last_quote")
        after.pop("last_quote")
        assert after == before  # Stray/ask-below-trigger cannot consume any slot/latch.
        return
    assert len(dispatched) == 2
    assert dispatched[1].metadata["fanout_leg"] == "webull"
    assert dispatched[0].metadata.get("fanout_leg", "schwab") == "schwab"
    for amount, leg in zip((600, 300), dispatched, strict=True):
        assert leg.quantity == (Decimal(amount) / Decimal("5.27")).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        assert leg.metadata["pm_confirming_ask"] == leg.metadata["limit_price"] == "5.27"
        assert leg.metadata["pm_confirming_ask_age_ms"] == "0"
    assert state.resting_flip_ms == clock[0]
    assert cross(strategy, state, clock, 5.27, 5.27, stream=route != "stream") is None
    assert not strategy.drain_webull_fanout_intents()


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
def test_all_eight_on_incomplete_line_blocks_both_cross_paths_without_consuming_state(stream, slot):
    strategy, state, clock = armed(VEEA, **ALL_ON)
    state.resting_slot = slot
    strategy._composition_line.complete = False
    before = deepcopy(asdict(state))
    assert cross(strategy, state, clock, 5.27, 5.27, stream=stream) is None
    after = asdict(state)
    # A quote may refresh the cache; it cannot consume an economic slot.
    before.pop("last_quote")
    after.pop("last_quote")
    assert after == before
    assert not strategy.drain_pending_intents()
    assert not strategy.drain_webull_fanout_intents()


def test_all_eight_on_service_refuses_draft_after_completed_line_is_revoked(monkeypatch):
    strategy, state, clock = armed(VEEA, **ALL_ON)
    draft = cross(strategy, state, clock, 5.27, 5.27)
    assert draft is not None
    bot, _, _ = _bot(monkeypatch, **ALL_ON)
    bot.strategy = strategy
    bot._line_buy_ready = strategy._composition_line.ready
    bot._line_version = strategy._composition_line.version
    assert bot._line_draft_allowed(draft)
    captured = draft.metadata["line_restore_version"]
    state.bars[-1].timestamp_ms += 60_000
    assert bot._line_version(state.symbol) != captured
    assert not bot._line_draft_allowed(draft)
    state.bars[-1].timestamp_ms -= 60_000
    strategy._composition_line.complete = False
    assert not bot._line_draft_allowed(draft)


@pytest.mark.parametrize("slot", ["first", "reclaim"])
def test_all_on_pmrest_software_move_owns_no_handoff_preserves_accounting(slot):
    options = {key: value for key, value in ALL_ON.items() if key != "strategy_schwab_1m_v2_pm_rest_reprice_enabled"}
    strategy, state, clock = pmrest.armed(**options)
    assert_all_on(strategy.settings)
    state.resting_slot = state.last_resting_placed_slot = slot
    state.fanout_segment_id = 12345
    state.retry_one_segment_id, state.retry_one_closes_in_segment = 12345, 1
    state.cw_resting_taken = slot == "reclaim"
    before = asdict(state)
    for when, line in (("2026-10-05T07:45:02.368-04:00", 8.2381),
                       ("2026-10-05T07:47:02.102-04:00", 7.9699)):
        clock[0] = pmrest.ms(when)
        pmrest.bar(state, clock[0])
        pmrest.track(strategy, state, line, slot)
        assert state.resting_active and not state.resting_is_broker_order
        assert state.resting_level == line
        # ROUNDUP1: trigger is ceilinged to the cent
        assert state.resting_trigger == (8.28 if line == 8.2381 else 8.01)
        assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()
        assert not strategy._rpg_handoffs
    for field in ("fanout_segment_id", "retry_one_segment_id", "retry_one_closes_in_segment",
                  "cw_resting_taken", "cw_reclaim_taken", "resting_flip_ms"):
        assert asdict(state)[field] == before[field]


@pytest.mark.parametrize("gate", ["boot", "gap"])
@pytest.mark.parametrize("stream", [True, False])
def test_all_on_held_rest_cannot_move_or_cross(gate, stream):
    strategy, state, clock = armed(**ALL_ON)
    flip(strategy, state, clock)
    if gate == "boot":
        strategy._entries_held = True
    else:
        state.gap_hold_active = True
    before = (state.resting_level, state.resting_trigger, state.resting_flip_ms)
    strategy._reprice_resting(state, state.resting_level * 0.99)
    assert cross(strategy, state, clock, 5.27, 5.27, stream=stream) is None
    assert (state.resting_level, state.resting_trigger, state.resting_flip_ms) == before
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_fanout_intents()


def test_all_on_new_pm_reclaim_remains_disallowed():
    strategy, state, clock = armed(**ALL_ON)
    state.resting_active = False
    state.cw_armed, state.cw_bars_waited, state.cw_segment_high = True, 2, 5.2
    strategy._cw_v2_reclaim_resting_track(state)
    assert not state.resting_active
    assert not strategy.drain_pending_intents() and not strategy.drain_webull_direct_intents()


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
@pytest.mark.parametrize("slot", ["first", "reclaim"])
async def test_all_on_rth_broker_reprice_owned_by_handoff(monkeypatch, broker, slot):
    h = await runtime(monkeypatch, broker, slot=slot, strategy_overrides=ALL_ON)
    h.service.settings = h.service.settings.model_copy(update=ALL_ON)
    assert_all_on(h.strategy.settings)
    assert_all_on(h.service.settings)
    if broker == "webull":
        nfq.nfq_enable(h)
    original_drain = h.strategy.drain_pending_intents

    def observed_cancel_drain():
        drafts = original_drain()
        for draft in drafts:
            if draft.intent_type == "cancel":
                assert draft.metadata.get("atr_reprice") == "true"
                assert draft.metadata.get("cw_entry_slot") == slot
        return drafts

    monkeypatch.setattr(h.strategy, "drain_pending_intents", observed_cancel_drain)
    token, _ = await begin(h, broker)
    journal = HandoffJournal(h.factory)
    assert journal.read(token)["phase"] == "clear"
    assert len(h.adapter.cancels) == len(h.adapter.reads) == 1
    assert not h.adapter.opens
    await feedback(h)
    if broker == "webull":
        assert journal.read(token)["phase"] == "price_wait"
        queued = await nfq.queue_price(h)
        await h.service._handle_stream_message({"data": queued.model_dump_json()})
        assert not h.adapter.opens
        tick_clock(h)
        await feedback(h)
    assert journal.read(token)["phase"] == "placed"
    assert len(h.adapter.opens) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["cancel_clear", "no_wire", "buy_flip", "1545"])
async def test_all_on_existing_nfq_handoff_regressions(monkeypatch, scenario):
    async def all_on_runtime(mp, broker):
        h = await runtime(mp, broker, strategy_overrides=ALL_ON)
        h.service.settings = h.service.settings.model_copy(update=ALL_ON)
        assert_all_on(h.strategy.settings)
        assert_all_on(h.service.settings)
        return h

    monkeypatch.setattr(nfq, "runtime", all_on_runtime)
    if scenario == "cancel_clear":
        await nfq.test_recorded_cancel_read_then_nfq_hold_reauthorizes_once_and_old_token_cannot_wire(monkeypatch)
    elif scenario == "no_wire":
        await nfq.test_v2_reprice_of_unsubmitted_nfq_generation_never_invents_broker_cancel(monkeypatch)
    else:
        await nfq.test_nfq_queued_price_wait_cannot_resurrect_after_current_gate_ends(monkeypatch, scenario)


@pytest.mark.parametrize("when", ["09:30:02", "09:31:02"])
def test_all_on_rth_conversion_preserves_original_thin_cancel(monkeypatch, caplog, when):
    original = pmrest.setup
    options = {key: value for key, value in ALL_ON.items() if key != "strategy_schwab_1m_v2_pm_rest_reprice_enabled"}
    monkeypatch.setattr(pmrest, "setup", lambda **kw: original(**options, **kw))
    pmrest.test_p5_rth_conversion_preserves_thin_streak_until_third_thin_cancel(2, when, monkeypatch, caplog)


def test_all_on_catalog_audit_committed_live_set_requires_both_consumers():
    assert len(ALL_ON) == 12
    root = Path(__file__).parents[2]
    flags = json.loads((root / "ops/health/expected_flags.json").read_text())["flags"]
    numeric = json.loads((root / "ops/health/expected_numeric.json").read_text())["settings"]
    by_name = {entry["name"]: entry for entry in flags}
    # Keep the all-on behavior fixture; the installed RPG1 ruling is OFF.
    live_set = {**ALL_ON, "strategy_schwab_1m_v2_atr_reprice_handoff_enabled": False}
    mismatches = {key for key, expected in live_set.items()
                  if by_name[key]["expected"] is not expected}
    assert mismatches == set()
    assert sum(1 + len(entry.get("also_check_services", [])) for entry in flags) == 142
    assert by_name["oms_v2_webull_mirror_retained_hold_enabled"]["expected"] is True
    assert by_name["strategy_schwab_1m_v2_resting_buy_round_up_enabled"]["expected"] is True
    assert sum(1 + len(entry.get("also_check_services", [])) for entry in numeric) == 8
    for key in ALL_ON:
        assert by_name[key]["owning_service"] == ("oms" if key.startswith("oms_") else "schwab-1m-v2")
        if key.endswith(("pm_print_cross_enabled", "pm_flip_wait_enabled",
                         "pm_rest_reprice_enabled", "atr_reprice_handoff_enabled")):
            assert by_name[key]["also_check_services"] == ["oms"]


@pytest.mark.parametrize("scenario", ["no_fill_takedown", "sell_flip", "rth_flip"])
def test_all_on_flip_wait_takedown_and_rth_latch_regressions(monkeypatch, caplog, scenario):
    original = pmprint.armed
    monkeypatch.setattr(pmprint, "armed", lambda **kw: original(**{**ALL_ON, **kw}))
    if scenario == "no_fill_takedown":
        pmprint.test_ft3_flip_without_cross_keeps_existing_grace_and_next_bar_takedown(caplog)
    elif scenario == "sell_flip":
        pmprint.test_ft5_sell_flip_ends_wait_before_a_later_print()
    else:
        pmprint.test_flags_default_off_and_rth_broker_flip_retains_legacy_latch()


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["schwab", "webull"])
async def test_all_on_gap_hold_withholds_proven_clear_handoff_without_replacement(monkeypatch, broker):
    h = await runtime(monkeypatch, broker, strategy_overrides=ALL_ON)
    h.service.settings = h.service.settings.model_copy(update=ALL_ON)
    token, _ = await begin(h, broker)
    assert HandoffJournal(h.factory).read(token)["phase"] == "clear"
    h.state.gap_hold_active = True
    await feedback(h)
    job = HandoffJournal(h.factory).read(token)
    # Restoration first revokes line admission. A proven-clear ticket remains
    # clear/waiting, rather than being authorized against incomplete mathematics.
    assert job["phase"] == "clear"
    decision = h.strategy.rpg_handoff_authorization(token, job)
    assert decision["verdict"] == "wait"
    assert decision["reason"] == "session_line_unproven"
    assert not h.adapter.opens


def test_all_on_process_catalog_checker_refuses_three_dark_values_without_live_io():
    from ops.health.expected_flags_check import ServiceEnvironment, audit, load_catalog, load_numeric_catalog

    root = Path(__file__).parents[2]
    entries = load_catalog(root / "ops/health/expected_flags.json") + load_numeric_catalog(
        root / "ops/health/expected_numeric.json")
    # Controlled obsolete policy: do not depend on the live catalog staying OFF.
    dark = {"strategy_schwab_1m_v2_pm_flip_wait_enabled",
            "strategy_schwab_1m_v2_pm_rest_reprice_enabled",
            "strategy_schwab_1m_v2_atr_reprice_handoff_enabled"}
    entries = [{**entry, "expected": False} if entry["name"] in dark else entry
               for entry in entries]
    environments = {}
    for entry in entries:
        for service in [entry["owning_service"], *entry.get("also_check_services", [])]:
            value = ALL_ON.get(entry["name"], entry["expected"])
            environments.setdefault(service, {})["MAI_TAI_" + entry["name"].upper()] = str(value).lower()
    readers = []

    def controlled_proc(service):
        readers.append(service)
        return ServiceEnvironment(101, environments[service], frozenset(), None)

    rc, lines = audit(entries, environment_reader=controlled_proc)
    assert rc == 1
    assert lines[-1] == "Final call: REAL FAILURE; checked=150/150 mismatches=6 unknown=0"
    assert len(readers) == len(set(readers)) == 8
    assert len([line for line in lines if line.startswith("REAL FAILURE")]) == 6
