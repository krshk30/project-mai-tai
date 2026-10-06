"""R6 disposition on retained PMI/JAGX candles, with explicit source attestations."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.strategy_core.session_line_restore import (
    SessionCoverage, SessionLineRestoration, build_session_line, history_fingerprint,
)
from project_mai_tai.strategy_core.schwab_1m_v2 import session_start_ts_ms
from tests.line_restore_acceptance_factory import make_line_restore_case

FIXTURES = Path(__file__).parents[1] / "fixtures"
RAW = json.loads((FIXTURES / "line_restore_r6_pmi_20261002.json").read_text())
PMI = [dict(zip(RAW["columns"], row), symbol="PMI") for row in RAW["rows"]]
JAGX = [row for row in json.loads((FIXTURES / "line_chart_restoration_bars.json").read_text())["bars"]
        if row["symbol"] == "JAGX"]


def ms(row):
    return int(datetime.fromisoformat(row["bar_time"]).timestamp() * 1000)


def attest(ledger, bars):
    ledger.attest(SessionCoverage("schwab_rest_full_session", ledger.anchor_ms,
                                 bars[-1].timestamp_ms + 60_000,
                                 tuple(bar.timestamp_ms for bar in bars), True,
                                 history_fingerprint(bars), prefix_complete=True))


def populated(rows):
    bars = [make_line_restore_case(symbol=rows[0]["symbol"], now_ms=ms(rows[-1]) + 61_000).bar(row)
            for row in rows]
    ledger = SessionLineRestoration(bars[0].symbol, session_start_ts_ms(bars[0].timestamp_ms), 1)
    for bar in bars:
        ledger.observe(bar)
    attest(ledger, bars)
    return ledger, bars


@pytest.mark.parametrize("count", range(11, 24))
def test_recorded_pmi_authorized_spanning_matches_oracle_state_and_trail(count):
    ledger, bars = populated(PMI[:count])
    request = ledger.prepare()
    assert request.spanning_pairs
    indicator = dict(build_session_line(request, 5, 3.5).indicator)
    oracle = compute_atr_trail([Bar(b.timestamp_ms, b.open, b.high, b.low, b.close, b.volume)
                               for b in bars])[-1]
    assert indicator["atr_state"] == oracle["state"]
    assert round(indicator["atr_trail"], 4) == oracle["trail"]


def test_complete_provider_read_not_candle_count_authorizes_r6():
    ledger, _ = populated(PMI)
    ledger.attest(replace(ledger._coverage, complete=False))
    assert ledger.prepare() is None
    assert ledger.incomplete_reason == "coverage_unproven"
    ledger.attest(replace(ledger._coverage, complete=True, prefix_complete=False))
    assert ledger.prepare() is None
    assert ledger.incomplete_reason == "session_prefix_unproven"


def test_recorded_jagx_withheld_traded_candle_cannot_be_spanned_as_unfillable():
    # Positive volume in this retained candle proves trading in the withheld
    # minute. This is a controlled incomplete-provider response, not invented tape.
    assert int(JAGX[3]["volume"]) > 0
    rows = JAGX[:3] + JAGX[4:]
    ledger, _ = populated(rows)
    left, right = ms(JAGX[2]), ms(JAGX[4])
    ledger.mark_traded_pair((left, right))
    assert ledger.prepare() is None
    assert ledger.incomplete_reason == "traded_gap_unrecovered"
    ledger.observe(make_line_restore_case(symbol="JAGX", now_ms=ms(JAGX[-1]) + 61_000).bar(JAGX[3]))
    bars = [make_line_restore_case(symbol="JAGX", now_ms=ms(JAGX[-1]) + 61_000).bar(r) for r in JAGX]
    attest(ledger, bars)
    assert ledger.prepare() is not None  # Other complete-source sparse pairs are R6.


@pytest.mark.asyncio
async def test_recorded_pmi_eleven_arrivals_do_not_release_ten_clean_live_bar_hold():
    event = int(datetime.fromisoformat(RAW["event_utc"]).timestamp() * 1000)
    case = make_line_restore_case(symbol="PMI", now_ms=event)
    pre = [r for r in PMI if int(datetime.fromisoformat(r["created_at"]).timestamp() * 1000) <= event
           and ms(r) + 60_000 <= event]
    for row in pre:
        await case.feed_bar(row)
    for row in PMI[len(pre):]:
        await case.feed_bar(row, now_ms=max(ms(row) + 61_000, event + 1), source="streamer")
    ledger = case.bot._line_sessions["PMI"]
    attest(ledger, [case.bar(row) for row in PMI])
    assert await case.rebuild()
    snapshot = case.snapshot()
    assert snapshot["state"] == "short" and round(snapshot["trail"], 4) == 5.8432
    assert not snapshot["entry_allowed"] and snapshot["buy_count"] == 0


@pytest.mark.asyncio
async def test_recorded_jagx_sparse_session_requires_ten_consecutive_live_closes():
    assert all(ms(b) - ms(a) == 60_000 for a, b in zip(JAGX[-10:], JAGX[-9:]))
    case = make_line_restore_case(symbol="JAGX", now_ms=ms(JAGX[-10]) + 61_000)
    for row in JAGX[:-10]:
        await case.feed_bar(row)
    for index, row in enumerate(JAGX[-10:], 1):
        await case.feed_bar(row, now_ms=ms(row) + 61_000, source="streamer")
        ledger = case.bot._line_sessions["JAGX"]
        bars = [case.bar(r) for r in JAGX[:-10 + index]] if index < 10 else [case.bar(r) for r in JAGX]
        attest(ledger, bars)
        assert await case.rebuild()
        assert case.snapshot()["entry_allowed"] == (index == 10)
        assert case.snapshot()["buy_count"] == 0


@pytest.mark.asyncio
async def test_math_deadline_fails_closed_without_admitting_a_late_worker(monkeypatch):
    ledger, _ = populated(PMI)
    real_wait = asyncio.wait_for

    async def timeout(awaitable, timeout):
        assert timeout == 3.0
        return await real_wait(awaitable, 0.001)

    import time
    monkeypatch.setattr(asyncio, "wait_for", timeout)
    assert await ledger.rebuild(lambda request: time.sleep(.02)) is None
    assert ledger.incomplete_reason == "rebuild_timeout"
