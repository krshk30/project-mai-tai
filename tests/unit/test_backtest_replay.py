"""Hermetic CI parity test for the backtest REPLAY engine (P1, entry side).

A hand-built synthetic day (bars + quotes, no DB) with a known ATR flip drives the REAL
`SchwabV2Strategy` through `backtest.replay.replay_symbol_day`, and we assert the replayed
RESTING entry matches the expected band-fill. A mutation check (widen/narrow the band, and
gap the crossing ask above the band) flips fill<->miss — pinning that the fill/miss decision
is governed by the band exactly as the honest fill model claims. A full-day 07-23 fixture is
too heavy for CI; that lives in the Deliverable-3 VPS reconciliation.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.backtest.data import Quote as TapeQuote
from project_mai_tai.backtest.data import SchwabBar
from project_mai_tai.backtest.replay import build_replay_settings, replay_symbol_day

ET = ZoneInfo("America/New_York")
SYM = "TEST"
DAY = "2026-07-23"
BASE = datetime(2026, 7, 23, 10, 0, tzinfo=ET)  # 10:00 ET — inside window, past the ORB skip

# OHLC sequence validated to drive the real ATR state machine (period 5, factor 3.5) into:
#   long warmup -> SELL flip -> established short (>=3 bars, resting placed) -> reprice ->
#   BUY flip (the fill). The resting order just before the flip rests at stop≈98.264, limit≈98.755.
_OHLC = [
    (100.0, 100.2, 99.8, 100.0),  # 0  warm flat (hl 0.4) — ATR warmup
    (100.0, 100.2, 99.8, 100.0),  # 1
    (100.0, 100.2, 99.8, 100.0),  # 2
    (100.0, 100.2, 99.8, 100.0),  # 3
    (100.0, 100.2, 99.8, 100.0),  # 4
    (100.0, 100.2, 99.8, 100.0),  # 5
    (100.0, 100.2, 99.8, 100.0),  # 6
    (100.0, 100.2, 99.8, 100.0),  # 7
    (100.0, 100.2, 99.8, 100.0),  # 8  -> state defined: long, trail 98.6
    (99.8, 99.9, 97.9, 98.0),     # 9  -> SELL flip to short
    (97.8, 97.9, 97.5, 97.6),     # 10 short age 1
    (97.4, 97.5, 97.1, 97.2),     # 11 short age 2
    (97.1, 97.2, 96.8, 96.9),     # 12 short age 3 -> RESTING PLACE (stop 99.01/limit 99.505)
    (96.9, 97.0, 96.6, 96.7),     # 13 short age 4
    (96.8, 96.9, 96.5, 96.6),     # 14 reprice (trail moved >0.5%) -> cancel
    (96.7, 96.8, 96.4, 96.5),     # 15 re-place (stop 98.2636/limit 98.7549)
    (96.7, 99.5, 96.6, 99.3),     # 16 BUY flip (the fill happens on the crossing quote below)
]
# The resting stop/limit working into the flip (bar 15 placement), from the validated run.
RESTING_STOP = 98.2636
RESTING_LIMIT = 98.7549


def _bars() -> list[SchwabBar]:
    out = []
    for i, (o, h, lo, c) in enumerate(_OHLC):
        ts_ms = int((BASE + timedelta(minutes=i)).timestamp() * 1000)
        out.append(SchwabBar(ts=ts_ms, open=o, high=h, low=lo, close=c, volume=50_000))
    return out


def _quotes(cross_ask: float) -> list[TapeQuote]:
    """One quote mid-minute for bars 9..15 (ask below the resting stop so the STOP<=ASK
    placement guard passes), then the crossing quote mid-way through the flip minute (bar 16)
    with the given ask — this is the quote the resting order fills (or gaps) against."""
    qs = []
    for i in range(9, 16):
        c = _OHLC[i][3]
        ts = BASE + timedelta(minutes=i, seconds=30)
        qs.append(TapeQuote(ts=ts, bid=c - 0.15, ask=c + 0.05, last=c))  # ask well below the stop
    cross_ts = BASE + timedelta(minutes=16, seconds=30)
    qs.append(TapeQuote(ts=cross_ts, bid=cross_ask - 0.1, ask=cross_ask, last=cross_ask))
    return qs


class _MemSource:
    """Minimal in-memory MarketDataSource with just the two methods the replay uses."""

    def __init__(self, bars, quotes):
        self._bars, self._quotes = bars, quotes

    def schwab_bars(self, symbol, start, end):
        lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        return [b for b in self._bars if lo <= b.ts < hi]

    def schwab_quotes(self, symbol, start, end):
        return [q for q in self._quotes if start <= q.ts < end]


def _run(cross_ask: float, **settings_overrides):
    settings = build_replay_settings(**settings_overrides)
    source = _MemSource(_bars(), _quotes(cross_ask))
    return replay_symbol_day(source, SYM, DAY, settings)


# ------------------------------------------------------------------ the golden entry
def test_replay_produces_expected_resting_entry() -> None:
    # Crossing ask 98.50 lands inside the band [98.264, 98.755] -> fills at the ask.
    res = _run(cross_ask=98.50)
    assert res.n_bars == len(_OHLC)
    assert len(res.entries) == 1, f"expected exactly one entry, got {res.entries} skips={res.skips}"
    e = res.entries[0]
    assert e.mode == "resting" and e.order_type == "STOP_LIMIT"
    assert e.fill_price == pytest.approx(98.50, abs=1e-6)      # fills at the in-band ask
    assert e.level == pytest.approx(RESTING_STOP, abs=1e-3)    # keyed off the ATR line
    assert res.misses == []
    # The fill is priced inside the resting band, above the stop.
    assert RESTING_STOP <= e.fill_price <= RESTING_LIMIT


def test_replay_reports_bar_and_quote_counts() -> None:
    res = _run(cross_ask=98.50)
    assert res.n_bars == 17 and res.n_quotes == 8 and res.symbol == SYM


# ------------------------------------------------------------------ coverage honesty
def test_sparse_feed_is_a_skip_not_a_silent_absence() -> None:
    source = _MemSource(_bars()[:5], _quotes(98.50))  # only 5 bars (< MIN_BARS_FOR_REPLAY)
    res = replay_symbol_day(source, SYM, DAY, build_replay_settings())
    assert res.entries == [] and len(res.skips) == 1
    assert res.skips[0].reason == "sparse_schwab_feed"


# ------------------------------------------------------------------ mutation: the band decides
def test_mutation_gap_above_band_flips_fill_to_miss() -> None:
    """Same day, but the break gaps the whole 0.5% band: crossing ask 99.00 > limit 98.755.
    The stop triggers but the limit is below market -> NO fill (honest resting miss)."""
    res = _run(cross_ask=99.00)
    assert res.entries == [], f"expected a MISS on a gap-through, got {res.entries}"
    assert len(res.misses) == 1 and res.misses[0].reason == "resting_never_filled"


def test_mutation_wider_band_recovers_the_gap_fill() -> None:
    """The SAME 99.00 gap that missed at band 0.5% now FILLS when the band is widened to 1.5%
    (limit ≈ stop*1.015 ≈ 99.74 > 99.00) — proving the band is the fill/miss threshold, and the
    replay reads the live `resting_entry_band_pct` setting (no re-implemented constant)."""
    res = _run(cross_ask=99.00, strategy_schwab_1m_v2_cw_v2_resting_entry_band_pct=1.5)
    assert len(res.entries) == 1, f"widened band should fill; got skips={res.skips} misses={res.misses}"
    assert res.entries[0].fill_price == pytest.approx(99.00, abs=1e-6)
