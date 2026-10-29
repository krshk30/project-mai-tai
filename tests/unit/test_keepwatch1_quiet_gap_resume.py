"""C7 SOURCE controls, UNRUN. Clocks match the reported Oct29 sequence.

OHLCV/provider replies are explicitly CONTROLLED, not retained NCPL data.
No hung lock, history error, permission, completion or positive-tape fact is
invented to force a baseline failure. Main must report a baseline PASS as
NONREPRODUCER and retain the missing-real-input gate. No network/DB fixture.
"""

import asyncio
from datetime import UTC, datetime
import logging
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.market_data.keepwatch_history import KeepWatchHistory, history_coverage
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.services import schwab_1m_v2_bot as service
from project_mai_tai.strategy_core.session_line_restore import history_fingerprint
from tests.unit.test_keepwatch_central_boot_retry import controlled_bot

ANCHOR = int(datetime(2026, 10, 29, 8, tzinfo=UTC).timestamp() * 1000)
LAST = ANCHOR + 123 * 60_000  # 10:03Z, reported last real minute.
EMPTY_END = ANCHOR + 179 * 60_000  # 10:59Z; no invented gap candles.
RESUME = ANCHOR + 180 * 60_000  # CONTROLLED first resumed minute, not raw proof.


def bar(symbol, stamp):
    # These values are deliberately not C1's quoted NCPL trade/quote prices.
    close = 10.0 - ((stamp - ANCHOR) // 60_000) * .005
    return ChartBar(symbol, close, close + .05, close - .05, close, 100,
                    stamp, producer="schwab")


def history(symbol, bars, through, now):
    rows = tuple(bars)
    ids = tuple(row.timestamp_ms for row in rows)
    return KeepWatchHistory(symbol, ANCHOR, through, rows,
        tuple(stamp for stamp in range(ANCHOR, through + 1, 60_000) if stamp not in ids),
        now * 1_000_000, now * 1_000_000, "a" * 64, history_fingerprint(rows),
        (), ids, (), rows, history_fingerprint(()))


async def seeded(monkeypatch):
    bot = controlled_bot(symbols=("NCPL", "PCLA"))
    bot.session_factory = None  # Real no-store path; not readable/flat proof.
    bot._configure_flip_entry_ownership_store({})
    bot.strategy.configure_falseflip({}, readable=False)
    clock = [LAST + 61_000]
    bot.strategy._now_ms = lambda: clock[0]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            result = datetime.fromtimestamp(clock[0] / 1000, UTC)
            return result.astimezone(tz) if tz else result.replace(tzinfo=None)

    monkeypatch.setattr(service, "datetime", Clock)
    bot._emit_confirmation_evaluations = AsyncMock()  # Report-only sink.
    bot._sync_line_epochs()
    sources = {}
    for symbol in ("NCPL", "PCLA"):
        rows = tuple(bar(symbol, stamp) for stamp in range(ANCHOR, LAST + 1, 60_000))
        for row in rows:
            bot._observe_line_bar(symbol, row)
            bot.strategy.on_observed_bar(symbol, row, observation_phase="replay")
        ledger = bot._line_sessions[symbol]
        receipt = history(symbol, rows, LAST, clock[0])
        ledger.attest(history_coverage(receipt, tuple(ledger._bars.values()), canonical=True))
        bot._keepwatch_proven[symbol] = receipt  # Explicit retained-read preimage.
        assert await bot._rebuild_session_line(symbol, ledger)
        assert bot._mark_warmed_from_fresh_bar(symbol, rows[-1], source="REST")
        sources[symbol] = list(rows)
    assert bot.strategy._entries_held  # No native/owner readiness manufactured.

    async def one_pass_sleep(_stop, _seconds):
        await asyncio.sleep(0)

    monkeypatch.setattr(service, "sleep_or_stop", one_pass_sleep)
    return bot, clock, sources


def diagnostics(bot, label):
    # Read prepare() ONCE per witness; report exact reason, not a guessed cause.
    result = {}
    for symbol in ("NCPL", "PCLA"):
        ledger = bot._line_sessions[symbol]
        request = ledger.prepare()
        state = bot.strategy.watchlist_state(symbol)
        result[symbol] = {"prepared": request is not None, "incomplete_reason": ledger.incomplete_reason,
            "unknown": symbol in bot._keepwatch_unknown,
            "state_bar_ms": state.bars[-1].timestamp_ms if state.bars else None,
            "ledger_bar_ms": ledger.current_bar_ms,
            "published_bar_ms": bot._line_published[symbol].request.current_bar_ms}
    print("C7_CONTROLLED_WITNESS", label, result, flush=True)
    return result


@pytest.mark.asyncio
async def test_controlled_reported_clock_sequence_quiet_gap_then_proven_resume(monkeypatch):
    """Functional expectation; baseline outcome UNKNOWN until Main executes.

    A PASS on a608 means chronology alone is NOT the real NCPL reproducer.
    It must not be converted into fail→pass proof by forcing a lock or guard.
    """
    bot, clock, sources = await seeded(monkeypatch)
    reads = []

    def read(_rest, _massive, symbol, anchor, through):
        assert anchor == ANCHOR
        reads.append((symbol, through))
        return history(symbol, sources[symbol], through, clock[0])

    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history", read)
    try:
        # NCPL remains empty after10:03; PCLA continuously prints closed candles.
        sources["PCLA"].extend(bar("PCLA", stamp) for stamp in range(LAST + 60_000, EMPTY_END + 1, 60_000))
        clock[0] = EMPTY_END + 61_000
        await bot._keepwatch_history_pass()
        owners = tuple(bot._keepwatch_bar_tasks.values())
        await asyncio.wait_for(asyncio.gather(*(asyncio.shield(task) for task in owners)), 5)
        await bot._line_restoration_pass()
        diagnostics(bot, "empty_tail")
        assert bot.strategy.watchlist_state("NCPL").bars[-1].timestamp_ms == LAST
        assert all(not LAST < stamp <= EMPTY_END for stamp in bot._line_sessions["NCPL"]._bars)
        assert ("NCPL", EMPTY_END) in reads and ("PCLA", EMPTY_END) in reads

        for symbol in sources:
            sources[symbol].append(bar(symbol, RESUME))
        clock[0] = RESUME + 61_000
        await bot._keepwatch_history_pass()
        owners = tuple(bot._keepwatch_bar_tasks.values())
        await asyncio.wait_for(asyncio.gather(*(asyncio.shield(task) for task in owners)), 5)
        await bot._line_restoration_pass()
        observed = diagnostics(bot, "resumed")
        assert ("NCPL", RESUME) in reads and ("PCLA", RESUME) in reads
        assert all(row["prepared"] and not row["unknown"] and row["published_bar_ms"] == RESUME
                   for row in observed.values())
        assert bot.strategy._entries_held and not bot.strategy._flip_owner_restore_readable
        assert not bot._keepwatch_native_reads and not bot._keepwatch_history_scopes
    finally:
        await bot._stop_keepwatch_bar_owners()


def unknown_warnings(caplog):
    return [record for record in caplog.records if record.levelno == logging.WARNING
            and "[V2-KEEPWATCH1]" in record.getMessage() and "outcome=UNKNOWN" in record.getMessage()]


def test_preexisting_unknown_still_gets_one_visible_episode_with_truthful_age(caplog):
    """Literal current observability defect, NOT proof of the recovery cause."""
    bot = controlled_bot(symbols=("NCPL",))
    bot._keepwatch_unknown.add("NCPL")  # Exact silent-add preimage, no invented onset.
    caplog.set_level(logging.WARNING)
    bot._keepwatch_hold_unknown("NCPL", "controlled_prefix_unproven")
    bot._keepwatch_hold_unknown("NCPL", "controlled_prefix_unproven")
    records = unknown_warnings(caplog)
    assert len(records) == 1
    assert "age_s=" in records[0].getMessage()
    assert "reason=controlled_prefix_unproven" in records[0].getMessage()


def test_new_unknown_episode_after_recovery_logs_once_without_changing_cancel_dedup(caplog):
    bot = controlled_bot(symbols=("NCPL",))
    caplog.set_level(logging.WARNING)
    before = bot._keepwatch_cancel_requested.copy()
    bot._keepwatch_hold_unknown("NCPL", "first_episode")
    bot._keepwatch_hold_unknown("NCPL", "first_episode")
    bot._keepwatch_unknown.discard("NCPL")  # Explicit recovered preimage, not permission.
    bot._keepwatch_hold_unknown("NCPL", "second_episode")
    assert len(unknown_warnings(caplog)) == 2
    assert bot._keepwatch_cancel_requested == before and bot.strategy._entries_held


@pytest.mark.asyncio
async def test_real_background_failed_reads_warn_once_per_symbol_and_keep_exact_hold_reason(monkeypatch, caplog):
    bot, clock, _sources = await seeded(monkeypatch)
    caplog.set_level(logging.INFO)

    def unreadable(*_args):
        raise ValueError("CONTROLLED_PROVIDER_FAILURE_NOT_NATIVE_EVIDENCE")

    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history", unreadable)
    try:
        clock[0] = EMPTY_END + 61_000
        await bot._keepwatch_history_pass()
        clock[0] += 60_000
        await bot._keepwatch_history_pass()
        records = unknown_warnings(caplog)
        assert len(records) == 2  # NCPL + PCLA, not one repeated warning per failed read.
        assert all("age_s=" in record.getMessage() and "incomplete_reason=" in record.getMessage()
                   for record in records)
        messages = [record.getMessage() for record in caplog.records]
        assert any("reason=closed_minute_read_error_ValueError" in text for text in messages)
        assert {"NCPL", "PCLA"} <= bot._keepwatch_unknown
        assert not bot._keepwatch_native_reads and not bot._keepwatch_history_scopes
        diagnostics(bot, "failed_reads_stay_held")
    finally:
        await bot._stop_keepwatch_bar_owners()
