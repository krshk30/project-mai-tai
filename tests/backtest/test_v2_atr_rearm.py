"""RED suite for the schwab_1m_v2 ATR-flip re-arm fix (docs/schwab-1m-v2-atr-flip-rearm-fix-design.md).

The bug: a graze fires variant-B first and CLAIMS the short segment; a hold-confirm SKIP (a) or an
emit-without-fill (b) then leaves the segment "spent," so the subsequent REAL BUY flip is un-enterable.
The invariant fix: the guard is claimed only when a position actually OPENS (a fill); skip / no-fill
release it, and a BUY flip that finds the segment unclaimed enters at the flip close.

These are SYNTHETIC fixtures (branch isolation), verified against compute_atr_trail: bar 9 flips SHORT,
bar 12 is a GRAZE (high pokes the trail, close stays below — the fake), bar 14 is the real BUY flip.
Each test runs rearm=False (current SHIPPED behavior — flip MISSED) vs rearm=True (fix — flip TAKEN).
The real-data, chart-verified golden is (c) in test_v2_atr_rearm_golden.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

from project_mai_tai.backtest.data import Quote, SchwabBar
from project_mai_tai.backtest.v2_sim import simulate_v2

# (open, high, low, close) — verified: idx9 SELL->short, idx12 GRAZE, idx14 BUY flip (trail ~10.072)
_BASE = (
    [(10.00, 10.01, 9.99, 10.00)] * 6
    + [(10.00, 10.02, 9.99, 10.01), (10.01, 10.03, 10.00, 10.02), (10.02, 10.04, 10.01, 10.03)]
    + [(10.03, 10.03, 9.90, 9.91)]                       # 9  SELL -> short
    + [(9.91, 9.93, 9.90, 9.92), (9.92, 9.94, 9.91, 9.93)]
    + [(9.93, 10.20, 9.92, 9.94)]                        # 12 GRAZE (fake): high pokes trail, close low
    + [(9.94, 9.96, 9.93, 9.95)]                         # 13 still short
    + [(9.95, 10.40, 9.94, 10.30)]                       # 14 BUY flip (real)
    + [(10.30, 10.40, 10.29, 10.35)]
)


def _u(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _bars():
    return [SchwabBar((i + 1) * 60_000, o, h, lo, c, 50_000) for i, (o, h, lo, c) in enumerate(_BASE)]


def _sq(bar12_dense):
    q = []
    if bar12_dense:                                       # DENSE coverage that FALLS BACK -> hold-confirm SKIP
        for ms, last in [(781_000, 10.10), (782_000, 10.15), (783_000, 10.05),
                         (784_000, 9.98), (785_000, 9.94), (786_000, 9.93)]:
            q.append(Quote(_u(ms), last - 0.01, last + 0.01, last))
    else:                                                 # THIN coverage -> fallback_thin -> EMIT
        q.append(Quote(_u(781_000), 10.09, 10.11, 10.10))
    q.append(Quote(_u(901_000), 10.29, 10.31, 10.30))     # bar-14 flip: fill quote (ask 10.31)
    return q


def _mq():                                                # massive bids for the exit ladder (WINDOW_END)
    return [Quote(_u(ms), bid, bid + 0.02, bid) for ms, bid in
            [(922_000, 10.30), (930_000, 10.28), (945_000, 10.25)]]


def test_a_dense_graze_skip_then_real_flip():
    """(a) A dense graze REJECTS (hold-confirm skip). Current code consumed the segment -> misses the
    real flip. Fix: skip releases the segment -> the real BUY flip is entered."""
    bars, sq, mq = _bars(), _sq(bar12_dense=True), _mq()
    off = simulate_v2(bars, sq, mq, rearm=False)
    on = simulate_v2(bars, sq, mq, rearm=True)
    assert len(off) == 0, "SHIPPED: the skipped graze consumes the segment, real flip MISSED"
    assert len(on) == 1, "FIX: guard released on skip -> real BUY flip entered"
    assert 10.25 <= on[0].entry_price <= 10.35, "entered at the real-flip fill, not the fake"


def test_b_emit_without_fill_then_real_flip():
    """(b) A graze EMITS (fallback_thin) but the order is rejected (no fill — the restricted-name /
    AZI-TC-DXF class). Current code claimed the segment on the emit -> misses the flip. Fix: no-fill
    releases the PROVISIONAL claim after the timeout -> the real BUY flip is entered."""
    bars, sq, mq = _bars(), _sq(bar12_dense=False), _mq()
    off = simulate_v2(bars, sq, mq, rearm=False, reject_bar_idxs={12})
    on = simulate_v2(bars, sq, mq, rearm=True, reject_bar_idxs={12})
    assert len(off) == 0, "SHIPPED: emit-without-fill consumes the segment, real flip MISSED"
    assert len(on) == 1, "FIX: PROVISIONAL released on no-fill -> real BUY flip entered"


def test_d_emit_release_window_must_be_short_enough_to_not_straddle_the_flip():
    """(d) The emit->fill release is WALL-CLOCK, and must err SHORT. A window short enough (12s default,
    above the 5s poll / under the 60s bar) releases the dead emit before the real flip -> flip entered.
    A window long enough to reach the next flip STRADDLES it -> the emit that was never going to fill
    blocks the real flip -> the bug rebuilt. This is exactly why the release is seconds, not bars: a
    2-bar window can span a graze sitting 1-2 min before its flip."""
    bars, sq, mq = _bars(), _sq(bar12_dense=False), _mq()
    short = simulate_v2(bars, sq, mq, rearm=True, reject_bar_idxs={12}, rearm_timeout_secs=12.0)
    straddle = simulate_v2(bars, sq, mq, rearm=True, reject_bar_idxs={12}, rearm_timeout_secs=200.0)
    assert len(short) == 1, "12s window: dead emit released before the flip -> flip entered"
    assert len(straddle) == 0, "over-long window straddles the flip -> real flip blocked (bug rebuilt)"


def test_e_release_is_poll_quantized_to_the_upper_bound():
    """(2) The live bot releases a working order only on a POSITION POLL, so its real release is the first
    poll >= emit+timeout -> window [timeout, timeout+poll]. The backtest quantizes to the UPPER bound
    (emit+timeout+poll) so it is never MORE optimistic than the bot. Pin, using _BASE's ~159s graze->flip
    gap: a raw 157s timeout would release at 157s (<159 -> enter) if UNQUANTIZED; with the +5s poll
    quantization (162s > 159) the flip is correctly blocked -> proves the quantization is applied."""
    bars, sq, mq = _bars(), _sq(bar12_dense=False), _mq()
    # 150s: even quantized (155s) releases before the ~159s flip -> flip entered
    assert len(simulate_v2(bars, sq, mq, rearm=True, reject_bar_idxs={12}, rearm_timeout_secs=150.0)) == 1
    # 157s: unquantized would enter (157<159); quantized to 162s (>159) blocks -> the +poll is real
    assert len(simulate_v2(bars, sq, mq, rearm=True, reject_bar_idxs={12}, rearm_timeout_secs=157.0)) == 0


def test_flag_off_is_shipped_behavior():
    """rearm=False must be byte-identical to the shipped path (no reject hook)."""
    bars, mq = _bars(), _mq()
    assert simulate_v2(bars, _sq(True), mq, rearm=False) == simulate_v2(bars, _sq(True), mq)
