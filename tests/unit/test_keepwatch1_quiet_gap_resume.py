"""C7 SOURCE controls, UNRUN. Clocks match the reported Oct29 sequence.

OHLCV/provider replies are explicitly CONTROLLED, not retained NCPL data.
No hung lock, history error, permission, completion or positive-tape fact is
invented to force a baseline failure. Main must report a baseline PASS as
NONREPRODUCER and retain the missing-real-input gate. No network/DB fixture.
"""

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.market_data.keepwatch_history import KeepWatchHistory, history_coverage
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.market_data.schwab_v2_streamer import SchwabTick
from project_mai_tai.services import schwab_1m_v2_bot as service
from project_mai_tai.strategy_core.session_line_restore import history_fingerprint
from tests.unit.test_keepwatch_central_boot_retry import controlled_bot
from tests.unit.test_keepwatch_session_producer_pin import published
from project_mai_tai.settings import Settings

ANCHOR = int(datetime(2026, 10, 29, 8, tzinfo=UTC).timestamp() * 1000)
LAST = ANCHOR + 123 * 60_000  # 10:03Z, reported last real minute.
EMPTY_END = ANCHOR + 179 * 60_000  # 10:59Z; no invented gap candles.
RESUME = ANCHOR + 180 * 60_000  # CONTROLLED first resumed minute, not raw proof.


def bar(symbol, stamp):
    # These values are deliberately not C1's quoted NCPL trade/quote prices.
    close = 10.0 - ((stamp - ANCHOR) // 60_000) * .005
    return ChartBar(symbol, close, close + .05, close - .05, close, 100,
                    stamp, producer="schwab")


def history(symbol, bars, through, now, *, anchor=ANCHOR):
    rows = tuple(bars)
    ids = tuple(row.timestamp_ms for row in rows)
    return KeepWatchHistory(symbol, anchor, through, rows,
        tuple(stamp for stamp in range(anchor, through + 1, 60_000) if stamp not in ids),
        now * 1_000_000, now * 1_000_000, "a" * 64, history_fingerprint(rows),
        (), ids, (), rows, history_fingerprint(()))


async def seeded(monkeypatch, *, symbols=("NCPL", "PCLA"), anchor=ANCHOR, last=LAST, sized_policy=False):
    bot = controlled_bot(symbols=symbols)
    supplied = bot.settings.model_dump()
    supplied["strategy_schwab_1m_v2_keepwatch_sized_trade_gap_enabled"] = sized_policy
    bot.settings = Settings(_env_file=None, **supplied)
    bot.strategy.settings = bot.settings
    bot.session_factory = None  # Real no-store path; not readable/flat proof.
    bot.tick_writer = None  # Set only by service start(); capture is off in these controls.
    bot._configure_flip_entry_ownership_store({})
    bot.strategy.configure_falseflip({}, readable=False)
    clock = [last + 61_000]
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
    for symbol in symbols:
        rows = tuple(bar(symbol, stamp) for stamp in range(anchor, last + 1, 60_000))
        for row in rows:
            bot._observe_line_bar(symbol, row)
            bot.strategy.on_observed_bar(symbol, row, observation_phase="replay")
        ledger = bot._line_sessions[symbol]
        receipt = history(symbol, rows, last, clock[0], anchor=anchor)
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
        # Boot-restoration readability is outside C7A; only the held-entry fence is asserted.
        assert bot.strategy._entries_held
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


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["clock", "logger"])
async def test_diagnostic_failure_cannot_prevent_unknown_or_real_cancel(monkeypatch, caplog, fault):
    bot, _ledger, _bars, _raw, state, queued = await published()
    caplog.set_level(logging.WARNING)
    caplog.clear()
    original_cancel = bot.strategy._queue_resting_cancel
    original_clock = bot.strategy._now_ms
    phase = {"cancel_completed": False}

    def real_cancel(actual, *, reason):
        assert actual.symbol in bot._keepwatch_unknown  # Fence precedes cancel.
        original_cancel(actual, reason=reason)  # Actual two-leg cancellation, no success stub.
        phase["cancel_completed"] = True

    def clock():
        if phase["cancel_completed"]:
            raise RuntimeError("CONTROLLED_DIAGNOSTIC_CLOCK_FAILURE")
        return original_clock()

    def broken_warning(*_args, **_kwargs):
        assert phase["cancel_completed"]  # Diagnostics follow original cancellation.
        raise RuntimeError("CONTROLLED_DIAGNOSTIC_LOGGER_FAILURE")

    with monkeypatch.context() as patch:
        patch.setattr(bot.strategy, "_queue_resting_cancel", real_cancel)
        if fault == "clock":
            patch.setattr(bot.strategy, "_now_ms", clock)
        else:
            patch.setattr(service.logger, "warning", broken_warning)
        bot._keepwatch_hold_unknown("IZM", "controlled_diagnostic_failure")
        assert phase["cancel_completed"] and queued
        assert all(draft.intent_type == "cancel" for draft in queued)
        count = len(queued)
        bot._keepwatch_hold_unknown("IZM", "controlled_diagnostic_failure")
        assert len(queued) == count  # Original cancellation dedup survives failure.
    assert "IZM" in bot._keepwatch_unknown and "IZM" in bot._keepwatch_cancel_requested
    assert bot.strategy._entries_held
    assert not unknown_warnings(caplog)
    bot._keepwatch_hold_unknown("IZM", "controlled_diagnostic_failure")
    assert len(queued) == count
    assert len(unknown_warnings(caplog)) == 1  # Failed logger was not labelled emitted.
    if fault == "clock":
        assert "age_s=UNKNOWN" in unknown_warnings(caplog)[0].getMessage()


@pytest.mark.parametrize("fault", ["clock", "logger"])
def test_mark_unknown_sets_fence_even_if_diagnostic_fails(monkeypatch, fault):
    bot = controlled_bot(symbols=("NCPL",))

    def broken(*_args, **_kwargs):
        raise RuntimeError("CONTROLLED_DIAGNOSTIC_FAILURE")

    if fault == "clock":
        monkeypatch.setattr(bot.strategy, "_now_ms", broken)
    else:
        monkeypatch.setattr(service.logger, "warning", broken)
    bot._keepwatch_mark_unknown("NCPL", "controlled_mark_failure")
    assert "NCPL" in bot._keepwatch_unknown and bot.strategy._entries_held


CASE_ROOT = Path("/Users/velkris/.codex/task-queues/analysis/OCT29-C6-ORIGINAL-CASE-RECEIPTS")
ORIGINAL_PRINT_CASES = (
    ("NCPL", "2026-10-29T10:04:00+00:00", "ncpl_trades_0602_0617_oddlot.jsonl",
     "5b8e86ef2cc3a58be6cd21be9ba11d47ae79221e46d0ec9b3a48d1aafeb5e4b1", 9),
    ("CHSN", "2026-10-28T22:25:00+00:00", "chsn_pom_1028_1815_1840ET.jsonl",
     "eb3b28a1540721aba9625cde9cb6fb3d998d724966b8048e350477489063ea3d", 4),
    ("POM", "2026-10-28T22:28:00+00:00", "chsn_pom_1028_1815_1840ET.jsonl",
     "eb3b28a1540721aba9625cde9cb6fb3d998d724966b8048e350477489063ea3d", 3),
)


def stamp(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def retained_prints(symbol, missing, filename, expected_hash, count):
    # Main executes this finite original-input loader; no export, scan or query.
    with (CASE_ROOT / filename).open("rb") as source:
        raw = source.read(2 * 1024 * 1024 + 1)
    assert len(raw) <= 2 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == expected_hash
    lines = raw.splitlines()
    assert len(lines) <= 2000 and all(len(line) <= 65536 for line in lines)
    rows = [json.loads(line) for line in lines]
    selected = [row for row in rows if row["symbol"] == symbol
                and stamp(row["event_ts"]) // 60_000 * 60_000 == missing]
    assert len(selected) == count
    assert all("37" in row["conditions"].split(",") and 0 < row["size"] < 100 for row in selected)
    return selected


def tick_from_print(row):
    # CONTROLLED Schwab carrier preserving exact retained consolidated print
    # time/price/size. This is NOT a recovered original Schwab wire payload.
    at = stamp(row["event_ts"])
    return SchwabTick("trade", "TIMESALE_EQUITY", row["symbol"], at,
        {"1": at, "2": row["price"], "3": row["size"]}, "CONTROLLED-" + str(row["id"]),
        price=float(row["price"]), size=row["size"])


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,minute,filename,digest,count", ORIGINAL_PRINT_CASES)
async def test_retained_oddlot_prints_recover_via_real_history_and_owned_bar_paths(
        monkeypatch, symbol, minute, filename, digest, count):
    """REAL print operands; provider OHLCV/coverage explicitly CONTROLLED.

    Original historical BAR qualification remains separate until C1 supplies
    its local bar receipt. Same test can expose a608's actual caller defect.
    """
    missing = stamp(minute)
    day = datetime.fromisoformat(minute).date()
    anchor = int(datetime(day.year, day.month, day.day, 8, tzinfo=UTC).timestamp() * 1000)
    prints = retained_prints(symbol, missing, filename, digest, count)
    bot, clock, sources = await seeded(monkeypatch, symbols=(symbol,), anchor=anchor,
                                      last=missing - 60_000, sized_policy=True)
    reads = []

    def read(_rest, _massive, requested, requested_anchor, through):
        assert requested == symbol and requested_anchor == anchor
        reads.append(through)
        return history(symbol, sources[symbol], through, clock[0], anchor=anchor)

    monkeypatch.setattr("project_mai_tai.market_data.keepwatch_history.read_keepwatch_history", read)
    try:
        clock[0] = max(missing + 61_000, *(stamp(row["received_at"]) + 1 for row in prints))
        for row in prints:
            await bot._handle_stream_tick(tick_from_print(row))
        # Unsized quote clock must not re-poison the same odd-lot-only minute.
        last = prints[-1]
        await bot._handle_quote(symbol, Quote(symbol, float(last["price"]), float(last["price"]),
            float(last["price"]), clock[0], trade_time_ms=stamp(last["event_ts"])))
        await bot._keepwatch_history_pass()
        resumed = missing + 60_000
        sources[symbol].append(bar(symbol, resumed))
        clock[0] = resumed + 61_000
        await bot._keepwatch_history_pass()
        owners = tuple(bot._keepwatch_bar_tasks.values())
        await asyncio.wait_for(asyncio.gather(*(asyncio.shield(task) for task in owners)), 5)
        await bot._line_restoration_pass()
        ledger = bot._line_sessions[symbol]
        request = ledger.prepare()
        print("C7_RETAINED_PRINT_WITNESS", {"symbol": symbol, "row_ids": [row["id"] for row in prints],
            "incomplete_reason": ledger.incomplete_reason, "prepared": request is not None,
            "unknown": symbol in bot._keepwatch_unknown, "reads": reads,
            "flag": getattr(bot.settings, "strategy_schwab_1m_v2_keepwatch_sized_trade_gap_enabled", "ABSENT_a608"),
            "provider_bars": "CONTROLLED_NOT_RETAINED_HISTORY"}, flush=True)
        assert request is not None and symbol not in bot._keepwatch_unknown
        assert bot._line_published[symbol].request.current_bar_ms == resumed
        assert missing not in ledger._bars  # Never manufacture the missing candle.
        assert bot.strategy._entries_held  # Boot readability is outside C7A.
    finally:
        await bot._stop_keepwatch_bar_owners()


@pytest.mark.parametrize("size,conditions,expected", [
    (1, None, False), (99, None, False), (None, None, False), (True, None, False),
    (100, None, True), (101, [], True), (100, [37], False), (1000, "12,37", False),
    (100, ["12"], True), (100, ["unknown_code"], True)])
def test_explicit_C1_bot_policy_only_positive_sized_non37_creates_barrier(size, conditions, expected):
    assert service.SchwabV2BotService._line_print_eligibility(size=size, conditions=conditions) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,event", [(False, "unsized_quote"), (True, "same_closed_positive"),
                                         (True, "forming_positive")])
async def test_OFF_legacy_read_and_ON_closed_positive_generation_race(monkeypatch, caplog, enabled, event):
    bot, clock, sources = await seeded(monkeypatch, symbols=("NCPL",), sized_policy=enabled)
    current = LAST + 60_000
    clock[0] = current + 61_000
    ledger = bot._line_sessions["NCPL"]
    previous = bot._keepwatch_proven["NCPL"]
    caplog.set_level(logging.INFO)

    def positive(at):
        return SchwabTick("trade", "TIMESALE_EQUITY", "NCPL", at, {},
                          "CONTROLLED-positive", price=10.0, size=100)

    if event == "same_closed_positive":
        await bot._handle_stream_tick(positive(current + 1_000))
    revision_before = ledger.revision

    async def read(symbol, anchor, through):
        if event == "unsized_quote":
            await bot._handle_quote(symbol, Quote(symbol, 10.0, 10.0, 10.0,
                clock[0], trade_time_ms=current + 2_000))
        else:
            at = current + 2_000 if event == "same_closed_positive" else current + 60_500
            await bot._handle_stream_tick(positive(at))
        assert ledger.revision == revision_before  # Same-minute/native future evidence is not a bar revision.
        return history(symbol, sources[symbol], through, clock[0], anchor=anchor)

    # Controlled asynchronous provider port; all history guards/CAS remain real.
    monkeypatch.setattr(bot, "_read_keepwatch_history_owned", read)
    try:
        await bot._keepwatch_history_pass()
        if event == "same_closed_positive":
            assert bot._keepwatch_proven["NCPL"] is previous
            assert "NCPL" in bot._keepwatch_unknown
            assert any("positive_trade_arrived_during_history" in record.getMessage() for record in caplog.records)
        else:
            assert bot._keepwatch_proven["NCPL"].through_ms == current
            assert "NCPL" not in bot._keepwatch_unknown
        if not enabled:
            assert not getattr(ledger, "_trade_evidence_by_minute", {})  # OFF adds no generation tracking.
        assert bot.strategy._entries_held
    finally:
        await bot._stop_keepwatch_bar_owners()
