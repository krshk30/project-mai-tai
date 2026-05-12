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
    for bar 0 (bucket < current_bar_start) must still revise bar 0.

    The volume delta uses the bar's frozen baseline (_last_closed_bar_cum_volume
    snapshot at close = 1000) -- NOT the running _current_bar_last_cum_volume
    which has been dragged forward to 1100 by bar 1's first trade.
    Late trade cv=1080 -> delta = 1080-1000 = 80, bar 0 volume 100 -> 180.
    """
    builder, clock = _make_builder()
    builder.on_trade(price=10.0, size=100, timestamp_ns=t_ns(0), cumulative_volume=1000)
    builder.on_trade(price=10.5, size=50, timestamp_ns=t_ns(31), cumulative_volume=1100)
    assert len(builder.bars) == 1
    assert builder.bars[0].volume == 100
    assert builder._current_bar is not None
    assert builder._current_bar_start == float(BASE_S + 30)
    assert builder._last_closed_bar_cum_volume == 1000

    builder.on_trade(price=10.2, size=25, timestamp_ns=t_ns(20), cumulative_volume=1080)

    assert builder.bars[0].volume == 180, "late trade cum_vol delta (1080-1000=80) must add to bar 0"
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


def test_late_trade_uses_cum_vol_delta_not_size_for_volume_contribution() -> None:
    """LEVELONE_EQUITIES events aggregate multiple ticks. event.size is
    `last_size` (the size of the LAST tick only) while
    cum_vol delta represents the actual volume since the prior update.
    The revision must use the delta, not size, or it dramatically
    undercounts on heavy-burst LEVELONE updates.
    """
    builder, clock = _make_builder()
    builder.on_trade(price=10.0, size=10, timestamp_ns=t_ns(0), cumulative_volume=1000)
    clock["now"] = float(BASE_S + 35)
    builder.check_bar_closes()
    assert builder.bars[-1].volume == 10

    # Late LEVELONE event: 50 ticks aggregated, last_size=5 but cum_vol jumped 500.
    # Old (size-based) fix: bar.vol += 5  -> 15  (catastrophic undercount)
    # New (cum_vol-delta) fix: bar.vol += (1500 - 1000) = 500 -> 510  (correct)
    builder.on_trade(price=10.1, size=5, timestamp_ns=t_ns(15), cumulative_volume=1500)
    assert builder.bars[-1].volume == 510, (
        "late-trade revision must use cum_vol delta (=500) against the bar's "
        "frozen baseline (=1000), not the event's last_size (=5)"
    )


def test_late_trade_with_no_cum_volume_falls_back_to_size() -> None:
    """When cumulative_volume is None (e.g., source is TIMESALE-only without
    cum_vol field, or the bar's frozen baseline was never set), fall back to
    using size. Same as _resolve_volume_delta's fallback for in-progress trades."""
    builder, clock = _make_builder()
    builder.on_trade(price=10.0, size=100, timestamp_ns=t_ns(0), cumulative_volume=1000)
    clock["now"] = float(BASE_S + 35)
    builder.check_bar_closes()

    # Late trade with cumulative_volume=None -> falls back to size=25.
    builder.on_trade(price=10.05, size=25, timestamp_ns=t_ns(15), cumulative_volume=None)
    assert builder.bars[-1].volume == 125, "with no cv context, late-trade revision falls back to size"


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


# ---------------------------------------------------------------------------
# CHART_EQUITY canonical-source guard (schwab_1m bug fix, 2026-05-11)
#
# When `live_aggregate_bars_are_final=True` (schwab_1m), bars are persisted
# from CHART_EQUITY. The bug: _revise_last_closed_bar_from_trade ALSO ran
# on those CHART-sourced bars when late TIMESALE/LEVELONE ticks landed in
# the same bucket. The cum_vol baseline carried over from a much earlier
# tick-close (potentially many CHART-only bars back), so a single late tick
# computed volume_contrib = cum_vol_now - stale_baseline = MASSIVE delta,
# inflating the CHART bar to 4-10x its real volume and stamping
# trade_count=2. Fix: skip revision when bars[-1] came from on_final_bar.
# ---------------------------------------------------------------------------


def _make_aggregate_bar(*, bucket_start_s: int, open_p: float, high_p: float,
                       low_p: float, close_p: float, volume: int) -> "OHLCVBar":
    from project_mai_tai.strategy_core.models import OHLCVBar
    return OHLCVBar(
        open=open_p, high=high_p, low=low_p, close=close_p,
        volume=volume, timestamp=float(bucket_start_s), trade_count=1,
    )


def test_late_trade_does_not_revise_chart_sourced_bar() -> None:
    """Bug repro from 2026-05-11 audit: AEHL 07:19 bar persisted=1.45M
    while CHART live_bar=298K. CHART arrives, then a late TIMESALE tick for
    the same bucket arrives with cum_vol much higher than the baseline
    (because the baseline is from a tick-closed bar potentially many
    CHART-only bars back). Without the fix, the tick contributes a huge
    cum_vol delta to the CHART bar's volume.

    With the fix: bars[-1] is flagged as aggregate-sourced; the revision
    path is skipped; the CHART value is preserved.
    """
    builder, clock = _make_builder()

    # Establish a tick-built baseline far back so cum_vol baseline is stale.
    builder.on_trade(price=10.0, size=100, timestamp_ns=t_ns(0), cumulative_volume=1000)
    clock["now"] = float(BASE_S + 35)
    builder.check_bar_closes()
    # bars[-1] is now the tick-built bar 0; _last_closed_bar_cum_volume = 1000.

    # CHART_EQUITY for bar 1 arrives: append CHART-sourced bar with vol 5000.
    builder.on_final_bar(_make_aggregate_bar(
        bucket_start_s=BASE_S + 30,
        open_p=10.5, high_p=10.6, low_p=10.4, close_p=10.55, volume=5000,
    ))
    assert builder.bars[-1].volume == 5000
    assert builder._last_closed_bar_from_aggregate is True

    # Late TIMESALE tick for bar 1 with a MUCH higher cum_vol.
    # Without the fix: volume_contrib = 100000 - 1000 = 99000 added to the
    #                  CHART bar -> 5000 + 99000 = 104000 (inflated 20x).
    # With the fix:    revision is skipped; CHART bar stays at 5000.
    builder.on_trade(price=10.6, size=50, timestamp_ns=t_ns(45), cumulative_volume=100000)
    assert builder.bars[-1].volume == 5000, "CHART-sourced bar must not be revised by late ticks"
    assert builder.bars[-1].trade_count == 1, "trade_count must not be incremented"
    assert builder.consume_recent_revised_closed_bar() is None, (
        "no revision stamp should be set; engine must not re-persist"
    )


def test_chart_aggregate_flag_clears_on_subsequent_tick_close() -> None:
    """After a CHART bar lands, if the strategy goes back to building a
    tick bar and that tick bar closes naturally (or via check_bar_closes),
    the aggregate flag must clear so future late-trade revisions on the
    new tick-built bars[-1] work correctly.
    """
    builder, clock = _make_builder()

    # CHART bar 0.
    builder.on_final_bar(_make_aggregate_bar(
        bucket_start_s=BASE_S, open_p=10.0, high_p=10.1, low_p=9.9,
        close_p=10.05, volume=4000,
    ))
    assert builder._last_closed_bar_from_aggregate is True

    # Tick traffic resumes: builds bar 1, then bar 2 starts which closes bar 1.
    builder.on_trade(price=10.1, size=100, timestamp_ns=t_ns(31), cumulative_volume=5000)
    builder.on_trade(price=10.2, size=50, timestamp_ns=t_ns(61), cumulative_volume=5150)
    # bar 1 (tick-built) just closed via the bucket-change path.
    assert builder._last_closed_bar_from_aggregate is False, (
        "_close_current_bar must clear the aggregate flag when a tick-built "
        "bar replaces the CHART bar at bars[-1]"
    )

    # Late tick for bar 1 (now bars[-1] = bar 1 tick-built, vol=100, cv=5000).
    # Should revise normally: volume_contrib = 5100 - 5000 = 100; bar 1 -> 200.
    builder.on_trade(price=10.15, size=25, timestamp_ns=t_ns(45), cumulative_volume=5100)
    assert builder.bars[1].volume == 200, (
        "tick-built bar must accept revision once the aggregate flag clears"
    )
    assert builder.bars[1].trade_count == 2


def test_macd_30s_path_unaffected_when_on_final_bar_never_called() -> None:
    """Regression guard for macd_30s, which uses
    `live_aggregate_bars_are_final=False` and therefore never calls
    on_final_bar. The aggregate flag must stay False throughout, and the
    existing late-trade revision behavior must be preserved bit-for-bit.
    """
    builder, _ = _make_builder()

    # Tick-built bar 0.
    builder.on_trade(price=10.0, size=100, timestamp_ns=t_ns(0), cumulative_volume=1000)
    # Bar 1 first tick closes bar 0.
    builder.on_trade(price=10.5, size=50, timestamp_ns=t_ns(31), cumulative_volume=1100)
    assert builder._last_closed_bar_from_aggregate is False

    # Late tick for bar 0 -- existing test asserts vol becomes 180.
    builder.on_trade(price=10.2, size=25, timestamp_ns=t_ns(20), cumulative_volume=1080)
    assert builder.bars[0].volume == 180, "macd_30s tick-only late-trade revision must still work"
    assert builder.bars[0].trade_count == 2
    assert builder._last_closed_bar_from_aggregate is False


def test_reset_clears_aggregate_flag() -> None:
    builder, _ = _make_builder()
    builder.on_final_bar(_make_aggregate_bar(
        bucket_start_s=BASE_S, open_p=10.0, high_p=10.0, low_p=10.0,
        close_p=10.0, volume=1000,
    ))
    assert builder._last_closed_bar_from_aggregate is True
    builder.reset()
    assert builder._last_closed_bar_from_aggregate is False
