"""Regression tests for SchwabNativeBarBuilder.on_trade late-trade revision.

When the Schwab WebSocket stalls and trades for a closed bucket arrive after
the bar has been force-closed by check_bar_closes(), the builder must:

1. Apply those trades' size to the closed bar's volume (revision).
2. Update bar.high/low if the late trade extends them.
3. Update _current_bar_last_cum_volume to max(current, late.cv) so the
   cum_vol baseline doesn't leak the late trades' volume into the next
   bar's first delta.
4. Stamp _recent_revised_closed_bar so the engine's persistence hook can
   re-write strategy_bar_history with the corrected values.

This is the fix for the PMAX 07:07-07:08 ET 2026-05-08 heavy-burst tick-loss
diagnosed in docs/session-handoff-global.md.
"""
from __future__ import annotations

from project_mai_tai.strategy_core.schwab_native_30s import SchwabNativeBarBuilder

# Pick a bucket-aligned epoch in seconds so bucket arithmetic is predictable.
# 1_777_000_020 / 30 == 59_233_334 exactly, so:
#   BASE_S + 0..29 -> bucket 1_777_000_020 (call it "bar 0")
#   BASE_S + 30..59 -> bucket 1_777_000_050 ("bar 1")
BASE_S = 1_777_000_020
BASE_NS = BASE_S * 1_000_000_000


def t_ns(secs_offset: float) -> int:
    """Build a nanosecond timestamp at BASE_S + secs_offset.

    `_resolve_timestamp` requires the value > 1e18 to be treated as ns; using
    real epoch-scale ns ensures the on_trade bucket math uses our offset, not
    the test's wall-clock fallback.
    """
    return BASE_NS + int(secs_offset * 1_000_000_000)


def _make_builder(close_grace: float = 5.0, fill_gap_bars: bool = False) -> tuple[SchwabNativeBarBuilder, dict]:
    clock = {"now": float(BASE_S)}
    builder = SchwabNativeBarBuilder(
        "TST",
        interval_secs=30,
        time_provider=lambda: clock["now"],
        close_grace_seconds=close_grace,
        fill_gap_bars=fill_gap_bars,
    )
    return builder, clock


def test_late_trade_revises_closed_bar_volume_and_trade_count() -> None:
    builder, clock = _make_builder()
    builder.on_trade(price=10.0, size=100, timestamp_ns=t_ns(0), cumulative_volume=1000)
    # Force-close bar 0 via periodic flush.
    clock["now"] = float(BASE_S + 35)
    closed = builder.check_bar_closes()
    assert len(closed) == 1
    assert closed[0].volume == 100
    assert closed[0].trade_count == 1

    # Late trade for bucket 0 arrives after bar 0 closed.
    builder.on_trade(price=10.05, size=50, timestamp_ns=t_ns(15), cumulative_volume=1050)

    last_closed = builder.bars[-1]
    assert last_closed.volume == 150, "late trade size must be added to closed bar"
    assert last_closed.trade_count == 2
    revised = builder.consume_recent_revised_closed_bar()
    assert revised is not None and revised.volume == 150


def test_late_trade_extends_high_and_low_on_revision() -> None:
    builder, clock = _make_builder()
    builder.on_trade(price=10.0, size=100, timestamp_ns=t_ns(0), cumulative_volume=1000)
    clock["now"] = float(BASE_S + 35)
    builder.check_bar_closes()
    assert builder.bars[-1].high == 10.0 and builder.bars[-1].low == 10.0

    builder.on_trade(price=10.5, size=10, timestamp_ns=t_ns(20), cumulative_volume=1010)
    assert builder.bars[-1].high == 10.5 and builder.bars[-1].low == 10.0

    builder.on_trade(price=9.8, size=5, timestamp_ns=t_ns(25), cumulative_volume=1015)
    assert builder.bars[-1].high == 10.5 and builder.bars[-1].low == 9.8


def test_late_trade_drags_cum_vol_baseline_so_next_bar_delta_is_correct() -> None:
    """The PMAX root cause: without dragging _current_bar_last_cum_volume up,
    the first trade of the NEXT bar gets a delta that includes the dropped
    trades' volume, leaking it into the wrong bucket."""
    builder, clock = _make_builder()
    builder.on_trade(price=10.0, size=100, timestamp_ns=t_ns(0), cumulative_volume=1000)
    clock["now"] = float(BASE_S + 35)
    builder.check_bar_closes()

    # Late trades for bar 0 climb cum_vol from 1000 -> 1300.
    builder.on_trade(price=10.1, size=100, timestamp_ns=t_ns(15), cumulative_volume=1100)
    builder.on_trade(price=10.2, size=200, timestamp_ns=t_ns(25), cumulative_volume=1300)

    # Fresh trade for bar 1 with cv=1310.
    # Without the cum_vol drag, delta = 1310 - 1000 = 310 (300 leaked).
    # With the drag, delta = 1310 - 1300 = 10 (correct).
    builder.on_trade(price=10.3, size=10, timestamp_ns=t_ns(31), cumulative_volume=1310)

    assert builder._current_bar is not None
    assert builder._current_bar.volume == 10, (
        "first trade of new bucket must compute delta from late-trade-updated "
        "baseline (1300), not from the stale pre-late-trade baseline (1000)"
    )
    assert builder.bars[-1].volume == 100 + 100 + 200


def test_late_trade_for_bar_more_than_one_step_back_still_drops() -> None:
    """We only revise the immediately-prior closed bar. Older buckets fall
    through to the existing stale-trade drop path."""
    builder, clock = _make_builder()
    builder.on_trade(price=10.0, size=100, timestamp_ns=t_ns(0), cumulative_volume=1000)
    clock["now"] = float(BASE_S + 35)
    builder.check_bar_closes()
    builder.on_trade(price=10.5, size=50, timestamp_ns=t_ns(31), cumulative_volume=1050)
    clock["now"] = float(BASE_S + 70)
    builder.check_bar_closes()
    assert len(builder.bars) == 2

    bar0_vol_before = builder.bars[0].volume
    bar1_vol_before = builder.bars[1].volume
    builder.on_trade(price=11.0, size=999, timestamp_ns=t_ns(10), cumulative_volume=2000)

    assert builder.bars[0].volume == bar0_vol_before, "bar 0 must not be touched"
    assert builder.bars[1].volume == bar1_vol_before, "bar 1 must not be touched"
    assert builder.consume_recent_revised_closed_bar() is None


def test_late_trade_during_open_current_bar_revises_immediately_prior_closed_bar() -> None:
    """When current_bar is open (bar 1's trades arrived first), late trades
    for bar 0 (bucket < current_bar_start) must still revise bar 0."""
    builder, clock = _make_builder()
    builder.on_trade(price=10.0, size=100, timestamp_ns=t_ns(0), cumulative_volume=1000)
    builder.on_trade(price=10.5, size=50, timestamp_ns=t_ns(31), cumulative_volume=1100)
    assert len(builder.bars) == 1
    assert builder.bars[0].volume == 100
    assert builder._current_bar is not None
    assert builder._current_bar_start == float(BASE_S + 30)

    builder.on_trade(price=10.2, size=25, timestamp_ns=t_ns(20), cumulative_volume=1080)

    assert builder.bars[0].volume == 125, "late trade size must add to bar 0"
    assert builder.bars[0].trade_count == 2
    revised = builder.consume_recent_revised_closed_bar()
    assert revised is not None and revised.timestamp == float(BASE_S)


def test_consume_recent_revised_closed_bar_is_one_shot() -> None:
    builder, clock = _make_builder()
    builder.on_trade(price=10.0, size=100, timestamp_ns=t_ns(0), cumulative_volume=1000)
    clock["now"] = float(BASE_S + 35)
    builder.check_bar_closes()
    builder.on_trade(price=10.05, size=50, timestamp_ns=t_ns(15), cumulative_volume=1050)

    first = builder.consume_recent_revised_closed_bar()
    assert first is not None
    second = builder.consume_recent_revised_closed_bar()
    assert second is None, "consume must clear the stamp"


def test_no_revision_signal_when_trade_lands_in_a_fresh_bucket() -> None:
    """Sanity check: a trade arriving for the CURRENT or NEXT bucket must NOT
    set _recent_revised_closed_bar. The signal is only for late-trade
    revisions on already-closed bars."""
    builder, clock = _make_builder()
    builder.on_trade(price=10.0, size=100, timestamp_ns=t_ns(0), cumulative_volume=1000)
    assert builder.consume_recent_revised_closed_bar() is None

    # A trade in the next bucket closes bar 0 and opens bar 1 -- still no revision.
    builder.on_trade(price=10.5, size=50, timestamp_ns=t_ns(31), cumulative_volume=1100)
    assert builder.consume_recent_revised_closed_bar() is None
