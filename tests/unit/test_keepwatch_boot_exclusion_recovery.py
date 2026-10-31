"""C11229 CONTROLLED recovery inputs, not original AHT economics/native proof.

Real history/ledger/rebuild/warmup methods remain in the path. Only the provider
transport, clock, report-only readout and local state publication are controlled.
No test assigns a completion latch or clears a safety/ownership predicate.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.services import schwab_1m_v2_bot as service
from project_mai_tai.services.keepwatch_bar_owner import BarInbox, OwnedBar
from project_mai_tai.settings import Settings
from tests.unit.test_keepwatch_central_boot_retry import controlled_bot
from tests.unit.test_keepwatch_session_producer_pin import ANCHOR, candle, response


async def repaired(monkeypatch, *, symbols=("AHT",)):
    # This control exercises unreadable flip ownership, so enable that feature
    # at construction rather than relying on a former enabled default.
    bot = service.SchwabV2BotService(Settings(_env_file=None,
        strategy_schwab_1m_v2_keepwatch1_enabled=True,
        strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=True,
        strategy_schwab_1m_v2_line_chart_restoration_enabled=True,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True))
    bot._watchlist = set(symbols)
    bot._boot_scanner_selected = set(symbols)
    bot._boot_exclusion_sources_readable = True
    for symbol in symbols:
        bot.strategy.watchlist_state(symbol)
    bot.session_factory = None  # Real constructor no-store mode, not readable/flat evidence.
    bot._configure_flip_entry_ownership_store({})  # Real no-factory UNKNOWN initialization.
    bot.strategy.configure_falseflip({}, readable=False)
    wall, monotonic = [ANCHOR + 2 * 60_000], [0.0]
    bot.strategy._now_ms = lambda: wall[0]
    bot._keepwatch_boot_retry_clock = lambda: monotonic[0]

    class ControlledClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromtimestamp(wall[0] // 1000, UTC)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(service, "datetime", ControlledClock)
    bot._keepwatch.update(ANCHOR, set(symbols), wall[0],
                         {symbol: "short" for symbol in symbols}, retain_entries=True)
    bot._sync_line_epochs()
    bot._keepwatch_exclude_boot_symbol("AHT", ANCHOR, "consecutive_deferral_bound", 60.0)
    bot._cw_boot_hold_check()
    assert not bot._boot_state_restoration_complete and bot.strategy._entries_held
    assert "AHT" in bot._keepwatch_unknown and not bot._keepwatch_entry_allowed("AHT")

    # Model later actual provider completion, without fabricating an original
    # AHT historical response, original permission, native account or quantity.
    wall[0] = ANCHOR + 40 * 60_000 + 1000
    bot._emit_confirmation_evaluations = AsyncMock()  # Report-only control.
    histories = {}
    for symbol in symbols:
        bars = tuple(replace(candle(index, close=10 - index * .1), symbol=symbol)
                     for index in range(40))
        for bar in bars:
            bot._observe_line_bar(symbol, bar)
            bot.strategy.on_observed_bar(symbol, bar, observation_phase="replay")
        ledger = bot._line_sessions[symbol]
        raw = replace(response(bars, (), now=wall[0]), symbol=symbol)
        histories[symbol] = raw
        from project_mai_tai.market_data.keepwatch_history import history_coverage
        ledger.attest(history_coverage(raw, tuple(ledger._bars[bar.timestamp_ms] for bar in bars), canonical=True))
        bot._keepwatch_proven[symbol] = raw  # Explicit provider result preimage.
        assert await bot._rebuild_session_line(symbol, ledger)
        assert bot._mark_warmed_from_fresh_bar(symbol, bars[-1], source="REST")

    async def one_pass(_stop, _interval):
        await asyncio.sleep(0)

    def actual_transport(_client, _seed, symbol, _anchor, _current):
        return histories[symbol]

    monkeypatch.setattr(service, "sleep_or_stop", one_pass)
    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history", actual_transport)
    # Actual background read/native scope/empty-tail completion retires only
    # source-owned UNKNOWN. The new timer does not perform that repair itself.
    await bot._keepwatch_history_pass()
    assert "AHT" not in bot._keepwatch_unknown
    assert bot._keepwatch_boot_recovery_reason("AHT", ANCHOR) is None
    assert "AHT" in bot._keepwatch_boot_excluded_symbols()
    assert not bot._keepwatch_entry_allowed("AHT")
    assert not bot._keepwatch_native_reads and not bot._keepwatch_history_scopes
    return bot, wall, monotonic, histories


class LocalState:
    def __init__(self):
        self.events = []

    async def xadd(self, _stream, fields, **_kwargs):
        self.events.append(json.loads(fields["data"]))
        return "CONTROLLED_LOCAL_STATE_NOT_ORDER"


@pytest.mark.asyncio
async def test_timer_clear_uses_real_repaired_prefix_then_original_fleet_and_dangerous_check(monkeypatch, caplog):
    bot, wall, clock, histories = await repaired(monkeypatch)
    bot.redis = LocalState()
    monkeypatch.setattr(bot, "_fetch_reportable_state", lambda: {
        "positions": [], "pending_open": [], "pending_close": [], "daily_pnl": 0, "closed_today": []})
    clock[0] = 59.999
    await bot._publish_bot_state()
    assert "AHT" in bot._keepwatch_boot_excluded_symbols() and bot.strategy._entries_held
    with caplog.at_level(logging.INFO):
        clock[0] = 60.0
        await bot._publish_bot_state()
    assert not bot._keepwatch_boot_excluded_symbols()
    assert bot._boot_state_restoration_complete and not bot.strategy._entries_held
    assert bot._keepwatch_entry_allowed("AHT")  # Membership gate ONLY, not BUY/native permission.
    assert "[V2-BOOT-EXCLUDE-RETRY] symbol=AHT" in caplog.text
    assert "[V2-BOOT-EXCLUDE-CLEAR] symbol=AHT" in caplog.text
    assert bot.redis.events[-1]["payload"]["data_health"]["boot_retry"]["excluded"] == {}
    assert not bot.strategy._flip_owner_restore_readable  # No forged native/owner readiness.


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_history", "stale", "future_response", "foreign_anchor",
    "epoch", "revision", "prefix", "unreadable_exclusion", "unknown", "quarantine", "warmup", "missing_publication"])
async def test_unproven_or_stale_recovery_stays_excluded(monkeypatch, caplog, fault):
    bot, wall, clock, histories = await repaired(monkeypatch)
    ledger = bot._line_sessions["AHT"]
    if fault == "missing_history":
        bot._keepwatch_proven.pop("AHT")
    elif fault == "stale":
        wall[0] += 180_001
    elif fault == "future_response":
        bot._keepwatch_proven["AHT"] = replace(histories["AHT"], completed_ns=(wall[0] + 1) * 1_000_000)
    elif fault == "foreign_anchor":
        bot._keepwatch_proven["AHT"] = replace(histories["AHT"], anchor_ms=ANCHOR - 86_400_000)
    elif fault == "epoch":
        ledger.epoch += 1
    elif fault == "revision":
        ledger.revision += 1
    elif fault == "prefix":
        ledger._bars.pop(ANCHOR)  # Explicit corrupt/incomplete retained prefix input.
    elif fault == "unreadable_exclusion":
        bot._boot_exclusion_sources_readable = False
    elif fault == "unknown":
        bot._keepwatch_unknown.add("AHT")
    elif fault == "quarantine":
        bot._keepwatch_bar_quarantine["AHT"] = ANCHOR
    elif fault == "warmup":
        bot._rest_warmup_done.clear()
    else:
        bot._line_published.pop("AHT")
    clock[0] = 60.0
    with caplog.at_level(logging.WARNING):
        bot._retry_keepwatch_boot_exclusions()
    assert "AHT" in bot._keepwatch_boot_excluded_symbols()
    assert not bot._keepwatch_entry_allowed("AHT") and bot.strategy._entries_held
    assert not bot._boot_state_restoration_complete
    assert bot._keepwatch_boot_exclusions["AHT"]["retry_count"] == 1
    assert "[V2-BOOT-EXCLUDE-RETRY]" in caplog.text and "[V2-BOOT-EXCLUDE-CLEAR]" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["pending", "native", "unscoped_native", "history"])
async def test_timer_never_clears_over_pending_owner_or_unjoined_reads(monkeypatch, fault):
    bot, wall, clock, histories = await repaired(monkeypatch)
    ledger = bot._line_sessions["AHT"]
    native = object()  # Controlled opaque identity, not a running task.
    if fault == "pending":
        inbox = bot._keepwatch_bar_inboxes["AHT"] = BarInbox(500)
        bar = replace(candle(39), symbol="AHT")
        work = OwnedBar(bar, "rest", ANCHOR, ledger.epoch)
        assert inbox.offer(work)
    elif fault in {"native", "unscoped_native"}:
        bot._keepwatch_native_reads.add(native)
        if fault == "native":
            bot._keepwatch_native_read_scopes[native] = ("AHT", ANCHOR, ledger.epoch)
    else:
        bot._keepwatch_history_scopes[native] = ("AHT", ANCHOR, ledger.epoch)
    clock[0] = 60.0
    bot._retry_keepwatch_boot_exclusions()
    assert "AHT" in bot._keepwatch_boot_excluded_symbols() and not bot._keepwatch_entry_allowed("AHT")
    if fault == "pending":
        assert inbox.pending[bar.timestamp_ms] is work and inbox.consumed == 0
    elif fault in {"native", "unscoped_native"}:
        assert native in bot._keepwatch_native_reads
    else:
        assert native in bot._keepwatch_history_scopes


@pytest.mark.asyncio
async def test_clear_after_fleet_latch_does_not_relatch_or_touch_economic_state(monkeypatch):
    bot, wall, clock, histories = await repaired(monkeypatch, symbols=("AHT", "IZM"))
    bot._cw_boot_hold_check()  # Healthy IZM can complete the original non-vacuous fleet predicate.
    assert bot._boot_state_restoration_complete and not bot.strategy._entries_held
    assert not bot._keepwatch_entry_allowed("AHT")
    state = bot.strategy.watchlist_state("AHT")
    before = (state.flip_owner_opportunity_id, state.flip_owner_phase, state.position_qty,
              state.cw_entries_this_flip, state.fanout_segment_id)
    clock[0] = 60.0
    bot._retry_keepwatch_boot_exclusions()
    assert "AHT" not in bot._keepwatch_boot_excluded_symbols()
    assert bot._boot_state_restoration_complete
    assert before == (state.flip_owner_opportunity_id, state.flip_owner_phase, state.position_qty,
                      state.cw_entries_this_flip, state.fanout_segment_id)
    assert not bot.strategy._flip_owner_restore_readable


@pytest.mark.asyncio
async def test_dangerous_reconstructed_arm_is_not_released_by_exclusion_clear(monkeypatch):
    bot, wall, clock, histories = await repaired(monkeypatch)
    state = bot.strategy.watchlist_state("AHT")
    bot.strategy._boot_ms = ANCHOR + 60_000
    state.cw_armed, state.cw_arm_bar_ts, state.cw_entries_this_flip = True, ANCHOR, 0
    clock[0] = 60.0
    bot._retry_keepwatch_boot_exclusions()
    bot._cw_boot_hold_check()
    assert not bot._keepwatch_boot_excluded_symbols() and bot._boot_state_restoration_complete
    assert bot.strategy._entries_held and state.cw_entries_this_flip == 0


def test_one_minute_then_five_minute_schedule_and_failed_retry_logs(caplog):
    bot = controlled_bot(symbols=("AHT",))
    clock, wall = [0.0], [ANCHOR + 5 * 3_600_000 + 59 * 60_000]  # 09:59 ET.
    bot.strategy._now_ms = lambda: wall[0]
    bot._keepwatch_boot_retry_clock = lambda: clock[0]
    bot._keepwatch_exclude_boot_symbol("AHT", ANCHOR, "pending_work_stale", 0.0)
    with caplog.at_level(logging.WARNING):
        clock[0] = 59.999
        bot._retry_keepwatch_boot_exclusions()
        assert bot._keepwatch_boot_exclusions["AHT"]["retry_count"] == 0
        clock[0], wall[0] = 60.0, ANCHOR + 6 * 3_600_000
        bot._retry_keepwatch_boot_exclusions()
        assert bot._keepwatch_boot_exclusions["AHT"]["retry_count"] == 0
        clock[0], wall[0] = 300.0, ANCHOR + 6 * 3_600_000 + 4 * 60_000
        bot._retry_keepwatch_boot_exclusions()
        assert bot._keepwatch_boot_exclusions["AHT"]["retry_count"] == 1
        for _ in range(100):
            bot._retry_keepwatch_boot_exclusions()
        assert bot._keepwatch_boot_exclusions["AHT"]["retry_count"] == 1
        clock[0] = 599.999
        bot._retry_keepwatch_boot_exclusions()
        assert bot._keepwatch_boot_exclusions["AHT"]["retry_count"] == 1
        clock[0] = 600.0
        bot._retry_keepwatch_boot_exclusions()
    assert bot._keepwatch_boot_exclusions["AHT"]["retry_count"] == 2
    assert caplog.text.count("[V2-BOOT-EXCLUDE-RETRY]") == 2
    assert "interval_seconds=300" in caplog.text
    assert "[V2-BOOT-EXCLUDE-CLEAR]" not in caplog.text


@pytest.mark.parametrize("fault", ["keepwatch_off", "safety_off", "restart", "shutdown", "not_accepting", "old_anchor"])
def test_disabled_or_retired_context_has_no_retry_side_effects(fault):
    bot = controlled_bot(symbols=("AHT",), keepwatch=fault != "keepwatch_off", safety=fault != "safety_off")
    clock = [0.0]
    bot._keepwatch_boot_retry_clock = lambda: clock[0]
    bot._keepwatch_exclude_boot_symbol("AHT", ANCHOR, "pending_work_stale", 0.0)
    if fault == "restart":
        bot._restart_membership_replay = True
    elif fault == "shutdown":
        bot._stop_event.set()
    elif fault == "not_accepting":
        bot._keepwatch_bar_accepting = False
    elif fault == "old_anchor":
        bot.strategy._now_ms = lambda: ANCHOR + 86_400_000 + 120_000
    before = dict(bot._keepwatch_boot_exclusions["AHT"])
    clock[0] = 1000.0
    bot._retry_keepwatch_boot_exclusions()
    assert bot._keepwatch_boot_exclusions["AHT"] == before
    assert "AHT" in bot._keepwatch_unknown


def test_per_bar_completion_calls_cannot_spend_exclusion_retry_schedule():
    bot = controlled_bot(symbols=("AHT",))
    clock = [0.0]
    bot._keepwatch_boot_retry_clock = lambda: clock[0]
    bot._keepwatch_exclude_boot_symbol("AHT", ANCHOR, "pending_work_stale", 0.0)
    clock[0] = 1000.0
    for _ in range(100):
        bot._try_complete_boot_state_restoration(bot._watchlist, new_symbols=set())
    assert bot._keepwatch_boot_exclusions["AHT"]["retry_count"] == 0


@pytest.mark.asyncio
async def test_timer_proof_revalidation_adds_no_provider_or_database_operation(monkeypatch):
    bot, wall, clock, histories = await repaired(monkeypatch)

    def forbidden_io(*_args, **_kwargs):
        raise AssertionError("timer revalidation must use only existing admitted memory proof")

    monkeypatch.setattr(bot, "_read_keepwatch_history_owned", forbidden_io)
    monkeypatch.setattr(bot, "_read_line_session_bars", forbidden_io)
    # The isolated constructor has no live REST client; keep an explicit
    # forbidden transport port rather than dereferencing None.
    monkeypatch.setattr(bot, "rest_client", SimpleNamespace(fetch_session_history=forbidden_io))
    clock[0] = 60.0
    bot._retry_keepwatch_boot_exclusions()
    assert not bot._keepwatch_boot_excluded_symbols()
    assert not bot._keepwatch_native_reads and not bot._keepwatch_history_scopes
