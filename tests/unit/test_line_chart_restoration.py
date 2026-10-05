"""Recorded-bar admission fences; provider coverage responses are controlled.

These tests do not claim historical full-session REST response coverage. The
service adapter and mathematical publication are not wired by this primitive.
"""

import asyncio
import json
import threading
from datetime import datetime
from pathlib import Path

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.strategy_core.schwab_1m_v2 import session_start_ts_ms
from project_mai_tai.strategy_core.session_line_restore import (
    SessionCoverage, SessionLineRestoration, history_fingerprint,
)

RAW = json.loads((Path(__file__).parents[1] / "fixtures/line_chart_restoration_bars.json").read_text())


def _ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def _bars(symbol):
    return [(row, ChartBar(
        symbol, float(row["open_price"]), float(row["high_price"]),
        float(row["low_price"]), float(row["close_price"]),
        row["volume"], _ms(row["bar_time"]),
    )) for row in RAW["bars"] if row["symbol"] == symbol]


def _ledger(symbol="RETO", epoch=1):
    pairs = _bars(symbol)
    ledger = SessionLineRestoration(symbol, session_start_ts_ms(pairs[0][1].timestamp_ms), epoch)
    for _, bar in pairs:
        ledger.observe(bar)
    ledger.attest(SessionCoverage(
        "schwab_rest_full_session", ledger.anchor_ms, ledger.current_bar_ms + 60_000,
        tuple(bar.timestamp_ms for _, bar in pairs), True,
        history_fingerprint(bar for _, bar in pairs),
    ))
    return ledger


def test_recorded_reto_late_111019_bars_are_required_before_admission():
    pairs = [(row, bar) for row, bar in _bars("RETO")
             if bar.timestamp_ms <= _ms("2026-10-05T15:09:00+00:00")]
    ledger = SessionLineRestoration("RETO", session_start_ts_ms(pairs[0][1].timestamp_ms), 1)
    late = []
    for row, bar in pairs:
        if _ms(row["created_at"]) > _ms("2026-10-05T15:10:18+00:00"):
            late.append(bar)
        else:
            ledger.observe(bar)
    assert len(late) == 16
    ledger.attest(SessionCoverage(
        "schwab_rest_full_session", ledger.anchor_ms, ledger.current_bar_ms + 60_000,
        tuple(bar.timestamp_ms for _, bar in pairs), True,
        history_fingerprint(bar for _, bar in pairs),
    ))
    assert ledger.prepare() is None
    assert ledger.incomplete_reason == "history_missing_or_conflicting"
    for bar in late:
        ledger.observe(bar)
    prepared = ledger.prepare()
    assert prepared is not None
    assert tuple(bar.timestamp_ms for bar in prepared.bars) == tuple(bar.timestamp_ms for _, bar in pairs)


def test_recorded_jagx_readd_34_minute_hole_blocks_until_122012_backfill():
    pairs = [(row, bar) for row, bar in _bars("JAGX")
             if bar.timestamp_ms <= _ms("2026-10-05T15:51:00+00:00")]
    ledger = SessionLineRestoration("JAGX", session_start_ts_ms(pairs[0][1].timestamp_ms), 2)
    late = []
    for row, bar in pairs:
        if _ms(row["created_at"]) > _ms("2026-10-05T15:52:15+00:00"):
            late.append(bar)
        else:
            ledger.observe(bar)
    assert len(late) == 33
    assert late[0].timestamp_ms == _ms("2026-10-05T15:18:00+00:00")
    assert late[-1].timestamp_ms == _ms("2026-10-05T15:50:00+00:00")
    ledger.attest(SessionCoverage(
        "schwab_rest_full_session", ledger.anchor_ms, ledger.current_bar_ms + 60_000,
        tuple(bar.timestamp_ms for _, bar in pairs), True,
        history_fingerprint(bar for _, bar in pairs),
    ))
    assert ledger.prepare() is None
    assert ledger.incomplete_reason == "history_missing_or_conflicting"
    assert all(_ms(row["created_at"]) == _ms("2026-10-05T16:20:12.521834+00:00")
               for row, bar in pairs if bar in late)
    for bar in late:
        ledger.observe(bar)
    assert ledger.prepare() is not None


@pytest.mark.parametrize("source,complete,start_shift,end_shift", [
    ("database_read", True, 0, 0),
    ("schwab_rest_full_session", False, 0, 0),
    ("schwab_rest_full_session", True, 60_000, 0),
    ("schwab_rest_full_session", True, 0, -60_000),
])
def test_source_window_and_current_closed_bar_must_be_proven(source, complete, start_shift, end_shift):
    ledger = _ledger()
    proof = ledger._coverage
    ledger.attest(SessionCoverage(source, proof.start_ms + start_shift,
                                  proof.end_ms + end_shift, proof.closed_ids, complete,
                                  proof.bars_sha256))
    assert ledger.prepare() is None


def test_recorded_reto_session_history_is_not_truncated_to_250_bars():
    ledger = _ledger()
    assert len(ledger.prepare().bars) == 255
    assert ledger.prepare().bars[0].timestamp_ms == _bars("RETO")[0][1].timestamp_ms


def test_readd_epoch_refuses_an_earlier_episode_worker_result():
    old = _ledger("JAGX", epoch=1)
    result = asyncio.run(old.rebuild(lambda request: {"bars": len(request.bars)}))
    new = _ledger("JAGX", epoch=2)
    assert new.admit(result) is None
    assert new.incomplete_reason == "stale_rebuild"


def test_worker_is_off_callback_and_a_late_revision_invalidates_its_result():
    ledger = _ledger()
    main_thread = threading.get_ident()
    worker_threads = []

    def build(request):
        worker_threads.append(threading.get_ident())
        return tuple((bar.timestamp_ms, bar.close) for bar in request.bars)

    result = asyncio.run(ledger.rebuild(build))
    assert worker_threads and worker_threads[0] != main_thread
    # A second recorded candle set changes source coverage while the old job
    # is in flight. No invented prices or trading callbacks are involved.
    proof = ledger._coverage
    ledger.attest(SessionCoverage(proof.source, proof.start_ms - 60_000,
                                  proof.end_ms, proof.closed_ids, proof.complete,
                                  proof.bars_sha256))
    assert ledger.admit(result) is None


def test_provider_chart_objects_cannot_mutate_frozen_worker_input():
    ledger = _ledger()
    _, mutable = _bars("RETO")[0]
    ledger.observe(mutable)
    request = ledger.prepare()
    original_close = request.bars[0].close
    mutable.close = 999
    assert request.bars[0].close == original_close
    assert ledger.prepare() == request


def test_recorded_ids_without_matching_source_values_cannot_admit_a_line():
    ledger = _ledger()
    proof = ledger._coverage
    other = _ledger("JAGX")
    ledger.attest(SessionCoverage(proof.source, proof.start_ms, proof.end_ms,
                                  proof.closed_ids, True, other._coverage.bars_sha256))
    assert ledger.prepare() is None
    assert ledger.incomplete_reason == "source_values_unproven"


def test_recorded_reto_full_history_worker_replay_has_no_1117_sell():
    ledger = _ledger("RETO")

    def rebuild(request):
        return compute_atr_trail([
            Bar(bar.timestamp_ms, bar.open, bar.high, bar.low, bar.close, bar.volume)
            for bar in request.bars
        ])

    result = asyncio.run(ledger.rebuild(rebuild))
    rows = ledger.admit(result)
    assert len(rows) == 255
    by_time = {row["et"]: row for row in rows}
    for minute in ("11:17", "11:18", "11:21"):
        assert by_time[minute]["state"] == "long"
        assert by_time[minute]["trail"] == 2.0639
        assert by_time[minute]["flip"] is None
