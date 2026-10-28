"""C12259 controlled boot operands, not original LNKS/native admission proof.

No completion latch or held bit is assigned by these fixtures. Provider proof,
scanner population and warmup inputs are explicit CONTROLLED preimages; the real
completion predicate and dangerous-arm check make every resulting decision.
"""

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest

from project_mai_tai.services.keepwatch_bar_owner import BarInbox, OwnedBar
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from tests.unit.test_keepwatch_session_producer_pin import ANCHOR, candle, response


def controlled_bot(*, symbols=("IZM",), keepwatch=True, safety=True):
    bot = SchwabV2BotService(Settings(_env_file=None,
        strategy_schwab_1m_v2_keepwatch1_enabled=keepwatch,
        strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=safety,
        strategy_schwab_1m_v2_line_chart_restoration_enabled=True))
    bot.strategy._now_ms = lambda: ANCHOR + 11 * 60_000
    # Explicit input populations/readability, not historical or native facts.
    bot._watchlist = set(symbols)
    bot._boot_scanner_selected = set(symbols)
    bot._boot_exclusion_sources_readable = True
    for symbol in symbols:
        bot.strategy.watchlist_state(symbol)

    def no_database():
        raise AssertionError("central KEEPWATCH retry must never open a DB")

    bot.session_factory = no_database
    return bot


def controlled_proof(bot, symbol="IZM", *, warm=True):
    rows = tuple(replace(candle(index), symbol=symbol) for index in range(10))
    proof = replace(response(rows, ()), symbol=symbol)
    bot._keepwatch_proven[symbol] = proof
    if warm:
        # Reproduce the production history pass's existing premark; do not
        # replace _mark_warmed_from_fresh_bar or completion with a favorable stub.
        bot._rest_warmup_done.add(symbol)
    return proof


def assert_held(bot):
    bot._cw_boot_hold_check()
    assert not bot._boot_state_restoration_complete
    assert bot.strategy._entries_held


def test_late_proof_retries_real_fleet_predicate_without_second_scanner():
    bot = controlled_bot()
    assert_held(bot)
    controlled_proof(bot)
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete
    assert not bot.strategy._entries_held


@pytest.mark.parametrize("missing", ["proof", "foreign_anchor", "exclusions",
    "scanner", "selected", "warmup", "restart_replay"])
def test_incomplete_or_unreadable_inputs_cannot_complete(missing):
    bot = controlled_bot()
    proof = controlled_proof(bot)
    if missing == "proof":
        bot._keepwatch_proven.clear()
    elif missing == "foreign_anchor":
        bot._keepwatch_proven["IZM"] = replace(proof, anchor_ms=ANCHOR - 86_400_000)
    elif missing == "exclusions":
        bot._boot_exclusion_sources_readable = False
    elif missing == "scanner":
        bot._boot_scanner_selected.clear()
    elif missing == "selected":
        bot._watchlist.clear()
    elif missing == "warmup":
        bot._rest_warmup_done.clear()
    else:
        bot._restart_membership_replay = True
    assert_held(bot)


def test_partial_fleet_cannot_release_until_last_symbol_has_real_seed_operand():
    bot = controlled_bot(symbols=("IZM", "LNKS"))
    controlled_proof(bot)
    assert_held(bot)
    controlled_proof(bot, "LNKS")
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete
    assert not bot.strategy._entries_held


def test_dangerous_reconstructed_arm_still_holds_after_fleet_completion():
    bot = controlled_bot()
    controlled_proof(bot)
    state = bot.strategy.watchlist_state("IZM")
    bot.strategy._boot_ms = ANCHOR + 60_000
    state.cw_armed = True
    state.cw_arm_bar_ts = ANCHOR
    state.cw_entries_this_flip = 0
    assert any(row["dangerous"] for row in bot.strategy.cw_armed_segments())
    bot._cw_boot_hold_check()
    assert bot._boot_state_restoration_complete
    assert bot.strategy._entries_held
    assert state.cw_armed and state.cw_entries_this_flip == 0


def test_post_latch_calls_do_not_retry_seed_or_change_new_symbol_ownership(monkeypatch):
    bot = controlled_bot()
    controlled_proof(bot)
    actual = bot._try_complete_boot_state_restoration
    spy = Mock(wraps=actual)
    monkeypatch.setattr(bot, "_try_complete_boot_state_restoration", spy)
    bot._cw_boot_hold_check()
    assert spy.call_count == 1 and bot._boot_state_restoration_complete
    bot._watchlist.add("LNKS")
    state = bot.strategy.watchlist_state("LNKS")
    before = (state.cw_armed, state.cw_entries_this_flip, state.position_qty)
    for _ in range(100):
        bot._cw_boot_hold_check()
    assert spy.call_count == 1
    assert (state.cw_armed, state.cw_entries_this_flip, state.position_qty) == before
    assert "LNKS" not in bot._keepwatch_proven


@pytest.mark.parametrize("keepwatch,safety", [(False, True), (True, False), (False, False)])
def test_disabled_flags_do_not_invoke_new_completion_retry(monkeypatch, keepwatch, safety):
    bot = controlled_bot(keepwatch=keepwatch, safety=safety)
    spy = Mock(side_effect=AssertionError("disabled flag invoked new retry"))
    monkeypatch.setattr(bot, "_try_complete_boot_state_restoration", spy)
    initial = bot.strategy._entries_held
    bot._cw_boot_hold_check()
    assert not spy.called and bot.strategy._entries_held == initial
    assert not bot._boot_state_restoration_complete


@pytest.mark.asyncio
async def test_pending_owner_after_proof_before_callback_completion_must_not_release():
    """Ordering exposure: no extra APP fence is authorized by this assertion.

    This intentional adversary must stay strict if the exact central placement
    releases early. Report that result to C1; never fix it by a guard stub.
    """
    bot = controlled_bot()
    controlled_proof(bot)
    bar = candle(9)
    inbox = bot._keepwatch_bar_inboxes["IZM"] = BarInbox(500)
    work = OwnedBar(bar, "rest", ANCHOR, 1)
    assert inbox.offer(work)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def pending_callback():
        entered.set()
        await finish.wait()
        inbox.complete(work)

    task = asyncio.create_task(pending_callback())
    bot._keepwatch_bar_tasks["IZM"] = task
    await entered.wait()
    try:
        bot._cw_boot_hold_check()
        assert not bot._boot_state_restoration_complete
        assert bot.strategy._entries_held
        assert inbox.pending[bar.timestamp_ms] is work
    finally:
        finish.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["native_read", "shutdown"])
async def test_unresolved_native_read_or_shutdown_must_not_release(stage):
    bot = controlled_bot()
    controlled_proof(bot)
    finish = asyncio.Event()
    task = asyncio.create_task(finish.wait())
    bot._keepwatch_native_reads.add(task)
    if stage == "shutdown":
        bot._stop_event.set()
    try:
        bot._cw_boot_hold_check()
        assert not bot._boot_state_restoration_complete
        assert bot.strategy._entries_held
        assert not task.done()
    finally:
        finish.set()
        await task
        bot._keepwatch_native_reads.discard(task)


def test_same_minute_revision_preserves_pending_work_identity_and_hold():
    bot = controlled_bot()
    controlled_proof(bot)
    inbox = bot._keepwatch_bar_inboxes["IZM"] = BarInbox(500)
    old = OwnedBar(candle(9), "rest", ANCHOR, 1)
    revised = OwnedBar(replace(old.bar, close=9.99), "streamer", ANCHOR, 1)
    assert inbox.offer(old) and inbox.offer(revised)
    inbox.complete(old)
    assert inbox.pending[old.bar.timestamp_ms] is revised
    bot._cw_boot_hold_check()
    assert not bot._boot_state_restoration_complete
    assert bot.strategy._entries_held
    assert inbox.pending[old.bar.timestamp_ms] is revised
