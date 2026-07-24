"""Unit tests for the daily sheet: the feed classifier (every v2-qualified name appears with a
reason — no silent absence; cases are the real 07-06/07-07 names) AND the P4 rewire that makes
`render_v2_sheet` drive the REPLAY engine (backtest/replay.py) instead of the retired `simulate_v2`,
while preserving that coverage-honesty (traded / SKIP-no-feed / no-signal)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.daily_sheet import classify_v2_feed, render_v2_sheet
from project_mai_tai.backtest.data import Quote, SchwabBar, Trade

ET = ZoneInfo("America/New_York")


def test_full_feed_backtestable():          # CLRO 07-07
    label, ok = classify_v2_feed(335, 6539)
    assert ok is True and label.startswith("full")


def test_sparse_feed_backtestable():        # TDTH 07-06
    label, ok = classify_v2_feed(95, 1709)
    assert ok is True and label.startswith("SPARSE")


def test_skip_no_bars():                    # BYAH 07-06
    label, ok = classify_v2_feed(0, 5)
    assert ok is False and "no Schwab bars" in label


def test_skip_no_ticks():                   # TDTH 07-07
    label, ok = classify_v2_feed(70, 0)
    assert ok is False and "no Schwab ticks" in label


def test_skip_insufficient_bars():          # TDIC 07-06 (7 bars)
    label, ok = classify_v2_feed(7, 200)
    assert ok is False and "insufficient" in label


# ---------------------------------------------------------------- P4: the sheet drives the replay
# The SAME validated ATR OHLC the replay golden uses (period 5, factor 3.5): long warmup -> SELL
# flip -> established short (resting placed/repriced) -> BUY flip = the resting fill.
_BASE = datetime(2026, 7, 23, 10, 0, tzinfo=ET)  # 10:00 ET — RTH, past the ORB skip
_OHLC = [
    (100.0, 100.2, 99.8, 100.0)] * 9 + [
    (99.8, 99.9, 97.9, 98.0),     # 9  SELL flip -> short
    (97.8, 97.9, 97.5, 97.6),     # 10
    (97.4, 97.5, 97.1, 97.2),     # 11
    (97.1, 97.2, 96.8, 96.9),     # 12 resting place
    (96.9, 97.0, 96.6, 96.7),     # 13
    (96.8, 96.9, 96.5, 96.6),     # 14 reprice
    (96.7, 96.8, 96.4, 96.5),     # 15 re-place (stop 98.2636 / limit 98.7549)
    (96.7, 99.5, 96.6, 99.3),     # 16 BUY flip -> the fill
]


def _good_bars():
    return [SchwabBar(ts=int((_BASE + timedelta(minutes=i)).timestamp() * 1000),
                      open=o, high=h, low=lo, close=c, volume=50_000)
            for i, (o, h, lo, c) in enumerate(_OHLC)]


def _good_quotes():
    qs = [Quote(ts=_BASE + timedelta(minutes=i, seconds=30),
                bid=_OHLC[i][3] - 0.15, ask=_OHLC[i][3] + 0.05, last=_OHLC[i][3])
          for i in range(9, 16)]
    qs.append(Quote(ts=_BASE + timedelta(minutes=16, seconds=30), bid=98.40, ask=98.50, last=98.50))
    return qs


def _good_tape():
    # a +2% target print (98.2636*1.02 ~ 100.23) after the ~10:16 fill -> a resolved static-OCO trade
    return [Trade(ts=_BASE + timedelta(minutes=25), price=101.0, size=100)]


class _SheetSource:
    """In-memory MarketDataSource exposing exactly what render_v2_sheet + replay need: the qualified
    universe plus per-symbol Schwab bars/quotes and the trade tape."""

    def __init__(self, per_symbol):
        self._d = per_symbol  # {sym: (bars, quotes, trades)}

    def v2_qualified_symbols(self, start, end):
        return sorted(self._d)

    def schwab_bars(self, sym, start, end):
        lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        return [b for b in self._d.get(sym, ([], [], []))[0] if lo <= b.ts < hi]

    def schwab_quotes(self, sym, start, end):
        return [q for q in self._d.get(sym, ([], [], []))[1] if start <= q.ts < end]

    def trades(self, sym, start, end):
        return [t for t in self._d.get(sym, ([], [], []))[2] if start <= t.ts < end]


def test_render_v2_sheet_is_coverage_honest_via_replay():
    """Every qualified name appears with a REASON: a replayed TRADE, a feed SKIP, or a no-signal
    note — the CLRO no-silent-absence contract, now served by the replay engine (P4)."""
    flat_bars = [SchwabBar(ts=int((_BASE + timedelta(minutes=i)).timestamp() * 1000),
                           open=50.0, high=50.1, low=49.9, close=50.0, volume=50_000)
                 for i in range(20)]  # >=10 flat bars: no ATR flip -> no signal
    flat_quotes = [Quote(ts=_BASE + timedelta(minutes=i, seconds=30), bid=49.9, ask=50.1, last=50.0)
                   for i in range(20)]  # ticks present so the feed classifies OK (not a gap)
    src = _SheetSource({
        "GOOD": (_good_bars(), _good_quotes(), _good_tape()),   # a real replayed entry+trade
        "SPARSE": (flat_bars[:5], [], []),                      # < MIN_BARS_FOR_REPLAY -> SKIP
        "FLAT": (flat_bars, flat_quotes, []),                   # enough bars+ticks, no flip -> no signal
    })
    sheet = render_v2_sheet(src, 2026, 7, 23)

    # header + every qualified name present (no silent absence)
    assert "REPLAY engine" in sheet
    assert "== GOOD" in sheet and "== SPARSE" in sheet and "== FLAT" in sheet
    # GOOD: a replayed entry + a resolved static-OCO trade line
    assert "ENTRY resting/STOP_LIMIT" in sheet
    assert "[rth_static_oco]" in sheet and "[target]" in sheet
    # SPARSE: an explicit feed SKIP reason (not a silent gap)
    assert "SKIP sparse_schwab_feed" in sheet
    # FLAT: an explicit no-signal reason
    assert "0t — no ATR entry" in sheet
