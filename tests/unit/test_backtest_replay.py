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
from project_mai_tai.backtest.data import Trade as TapeTrade
from project_mai_tai.backtest.replay import (
    _static_oco_first_touch,
    build_replay_settings,
    replay_symbol_day,
)

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
    """Minimal in-memory MarketDataSource with just the methods the replay uses (bars + Schwab
    LEVELONE quotes for entry/EH-bids, and the trade tape for the RTH static-OCO first-touch)."""

    def __init__(self, bars, quotes, trades=None):
        self._bars, self._quotes, self._trades = bars, quotes, list(trades or [])

    def schwab_bars(self, symbol, start, end):
        lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        return [b for b in self._bars if lo <= b.ts < hi]

    def schwab_quotes(self, symbol, start, end):
        return [q for q in self._quotes if start <= q.ts < end]

    def trades(self, symbol, start, end):
        return [t for t in self._trades if start <= t.ts < end]


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


# ==================================================================== P2: EXIT side
# The exit geometry is chosen by the position's OPEN session (docs/schwab-1m-v2-live-spec.md §6):
#   RTH open -> STATIC native OCO (first-touch on the trade tape, else close-at-bell at 16:00).
#   EH  open -> software CW floor-RIDE driven by the SHARED `cw_exit_decision` over the bids.
# These synthetics reuse the SAME real ATR machinery the entry tests do; only the exit horizon
# differs. The v2 replay exit NEVER touches ExitEngine — it is `cw_exit_decision` / static-OCO only.

def _tp(minute: int, price: float) -> TapeTrade:
    """A trade print `minute` minutes past BASE (i.e. after the ~10:16 ET resting fill)."""
    return TapeTrade(ts=BASE + timedelta(minutes=minute), price=price, size=100)


def _run_rth(tape, cross_ask: float = RESTING_STOP, **overrides):
    """RTH resting entry (fills AT the ATR line so fill == OCO reference => a clean +2%/-5% frame),
    then resolve the static OCO against `tape`."""
    settings = build_replay_settings(**overrides)
    source = _MemSource(_bars(), _quotes(cross_ask), trades=tape)
    return replay_symbol_day(source, SYM, DAY, settings)


# ------------------------------------------------------------------ (4a) RTH tape hits +2% -> target
def test_rth_static_oco_target_first_touch_is_plus2() -> None:
    # Tape: dip to 99, then a print at 101.0 (>= target 98.2636*1.02 -> 100.23) -> the SELL LIMIT fills.
    res = _run_rth([_tp(20, 99.0), _tp(25, 101.0), _tp(30, 95.0)])
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.geometry == "rth_static_oco"
    assert t.exit_reason == "target"
    # OCO anchored off the CW reference (== the resting fill here), NOT re-struck off anything else.
    assert t.entry_ref == pytest.approx(t.entry_px, abs=1e-6)
    assert t.exit_px == pytest.approx(100.23, abs=1e-2)     # ref*1.02 rounded to the Schwab tick
    assert t.ret_pct == pytest.approx(2.0, abs=0.05)        # ~+2% off the fill


# ------------------------------------------------------------------ (4b) RTH neither leg -> close-at-bell
def test_rth_static_oco_neither_leg_closes_at_bell() -> None:
    # Tape ranges 99.0-99.5 the whole session: never reaches target (100.23) nor stop (93.35).
    # The DAY OCO lapses at 16:00 -> close at the last print (the SKYQ 07-23 shape).
    res = _run_rth([_tp(20, 99.0), _tp(25, 99.5), _tp(30, 99.2)])
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.geometry == "rth_static_oco"
    assert t.exit_reason == "close-at-bell"
    assert t.exit_px == pytest.approx(99.2, abs=1e-6)       # the last print <= the bell
    assert t.ret_pct == pytest.approx((99.2 - t.entry_px) / t.entry_px * 100.0, abs=1e-6)


def test_rth_static_oco_stop_first_touch() -> None:
    # A print at 90.0 <= stop (93.35) triggers the SELL STOP first -> -5%.
    res = _run_rth([_tp(20, 99.0), _tp(25, 90.0)])
    assert res.trades[0].exit_reason == "stop"
    assert res.trades[0].ret_pct == pytest.approx(-5.0, abs=0.05)


def test_static_oco_first_touch_unit_precedence() -> None:
    """The first-touch model directly: whichever leg the tape reaches FIRST (in time) wins."""
    et_ = ZoneInfo("America/New_York")
    t0 = datetime(2026, 7, 23, 11, 0, tzinfo=et_)
    close = datetime(2026, 7, 23, 16, 0, tzinfo=et_)
    tape = [(t0, 100.0), (t0.replace(minute=5), 102.5), (t0.replace(minute=6), 94.0)]
    # target = 100*1.02 = 102.0; the 102.5 print precedes the 94.0 print -> target.
    _ts, px, reason = _static_oco_first_touch(100.0, tape, target_pct=2.0, stop_pct=5.0, close_dt=close)
    assert reason == "target" and px == pytest.approx(102.0, abs=1e-9)
    # Same levels, but the stop print comes first -> stop.
    tape2 = [(t0, 100.0), (t0.replace(minute=5), 94.0), (t0.replace(minute=6), 102.5)]
    _, px2, reason2 = _static_oco_first_touch(100.0, tape2, target_pct=2.0, stop_pct=5.0, close_dt=close)
    assert reason2 == "stop" and px2 == pytest.approx(95.0, abs=1e-9)


# ------------------------------------------------------------------ (4c) EH open -> floor-ride
# A pre-market (EH) reactive entry: BUY flip arms the CW-v2 setup, two hold-bars build the 3-bar
# trigger, then a quote breaks it -> a marketable-LIMIT EH entry (fill @ ask). Resting is OFF so the
# reactive path fires (resting is RTH-only anyway).
EH_BASE = datetime(2026, 7, 23, 8, 0, tzinfo=ET)  # 08:00 ET = pre-market -> EH open
_EH_OHLC = (
    [(100.0, 100.2, 99.8, 100.0)] * 9
    + [
        (99.8, 99.9, 97.9, 98.0),   # 9  SELL flip -> short
        (97.8, 97.9, 97.5, 97.6),
        (97.4, 97.5, 97.1, 97.2),
        (97.1, 97.2, 96.8, 96.9),
        (96.9, 97.0, 96.6, 96.7),
        (96.8, 96.9, 96.5, 96.6),
        (96.7, 96.8, 96.4, 96.5),
        (96.7, 99.5, 96.6, 99.3),   # 16 BUY flip -> arm (trigger seeds at 99.5)
        (99.2, 99.4, 99.0, 99.2),   # 17 hold above flip level, high < 99.5
        (99.1, 99.4, 99.0, 99.2),   # 18 (bars_waited >= 2 -> trigger frozen at 99.5)
    ]
)


def _eh_bars():
    out = []
    for i, (o, h, lo, c) in enumerate(_EH_OHLC):
        ts_ms = int((EH_BASE + timedelta(minutes=i)).timestamp() * 1000)
        out.append(SchwabBar(ts=ts_ms, open=o, high=h, low=lo, close=c, volume=50_000))
    return out


def _eh_quotes(floor_bids):
    qs = []
    for i in range(9, 19):  # pre-cross quotes: last below the trigger (no premature break)
        c = _EH_OHLC[i][3]
        qs.append(TapeQuote(ts=EH_BASE + timedelta(minutes=i, seconds=30),
                            bid=c - 0.15, ask=c + 0.05, last=c))
    # the crossing quote: last 100.5 > trigger 99.5, whole forming bar above the flip level -> ENTRY.
    qs.append(TapeQuote(ts=EH_BASE + timedelta(minutes=19, seconds=10),
                        bid=100.3, ask=100.5, last=100.5))  # EH routing fills @ ask 100.5
    for mm, bid in floor_bids:  # the post-entry bid tape the floor-ride runs over
        qs.append(TapeQuote(ts=EH_BASE + timedelta(minutes=mm),
                            bid=bid, ask=bid + 0.2, last=bid))
    return qs


# entry fill = 100.5 -> +2% level = 102.51. Bids ride to 104 (arm) then fall back to 101.5 (< floor).
_EH_FLOOR_BIDS = [(20, 101.0), (21, 103.0), (22, 104.0), (23, 101.5)]


def _run_eh(*, floor_enabled: bool):
    settings = build_replay_settings(
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=False,  # let the reactive path fire
        oms_v2_cw_floor_exit_enabled=floor_enabled,
    )
    source = _MemSource(_eh_bars(), _eh_quotes(_EH_FLOOR_BIDS))
    return replay_symbol_day(source, SYM, DAY, settings)


def test_eh_open_floor_ride_arms_then_exits_on_fallback() -> None:
    res = _run_eh(floor_enabled=True)
    assert len(res.entries) == 1 and res.entries[0].mode == "reactive"
    assert len(res.trades) == 1, f"expected one EH trade; skips={res.skips} misses={res.misses}"
    t = res.trades[0]
    assert t.geometry == "eh_floor_ride"
    assert t.entry_px == pytest.approx(100.5, abs=1e-6)     # EH marketable-LIMIT fill @ ask
    # cw_exit_decision ARMED at +2% (bid 103/104 > 102.51) then closed on the fall-back to the floor.
    assert t.exit_reason == "floor"
    assert t.exit_px == pytest.approx(100.5 * 1.02, abs=1e-6)
    assert t.ret_pct == pytest.approx(2.0, abs=1e-6)
    # the exit fires on the FALL-BACK bid (minute 23), not the first +2% touch (minute 21).
    assert t.exit_ts.astimezone(ET).strftime("%H:%M") == "08:23"


# ------------------------------------------------------------------ mutation: the floor flag decides
def test_mutation_floor_flag_flips_eh_exit_shape() -> None:
    """Flip `cw_floor_exit_enabled` and the EH exit SHAPE changes: ON = floor-RIDE (arm past +2%,
    exit on fall-back), OFF = HARD target close at the first +2% touch. Same tape, same entry —
    only the shared `cw_exit_decision` mode differs. Proves the flag is load-bearing (mutation red)."""
    ride = _run_eh(floor_enabled=True).trades[0]
    hard = _run_eh(floor_enabled=False).trades[0]

    assert ride.exit_reason == "floor"      # rides past +2%, exits on the fall-back
    assert hard.exit_reason == "target"     # hard-closes at the first +2% bid
    assert ride.exit_reason != hard.exit_reason
    # both land on the +2% level, but the HARD close fires EARLIER (first touch) than the ride.
    assert ride.exit_px == pytest.approx(hard.exit_px, abs=1e-6)
    assert hard.exit_ts < ride.exit_ts
    assert hard.exit_ts.astimezone(ET).strftime("%H:%M") == "08:21"   # first +2% touch (bid 103)
    assert ride.exit_ts.astimezone(ET).strftime("%H:%M") == "08:23"   # the fall-back


# ------------------------------------------------------------------ EH bar-close ATR flip exit
# Proves the "flip" exit_reason is reachable in the replay: after the EH entry, a bar-close SELL
# flip while holding makes the REAL strategy emit a cw_flip CLOSE draft (`_maybe_cw_flip_close`) —
# which is only reachable because its staleness clock now routes through the `_now_ms()` seam the
# ReplayStrategy overrides (a behavior-identical live refactor). flip_pending -> cw_exit_decision
# returns "flip" on the next bid.
_EH_FLIP_TAIL = [
    (99.0, 99.1, 95.0, 95.2),   # 19  crash below the ATR trail -> SELL flip while holding
    (95.0, 95.1, 93.0, 93.2),   # 20
    (93.0, 93.1, 91.0, 91.2),   # 21
]


def test_eh_open_bar_close_atr_flip_exit() -> None:
    bars = _eh_bars()
    for i, (o, h, lo, c) in enumerate(_EH_FLIP_TAIL, start=len(_EH_OHLC)):
        ts_ms = int((EH_BASE + timedelta(minutes=i)).timestamp() * 1000)
        bars.append(SchwabBar(ts=ts_ms, open=o, high=h, low=lo, close=c, volume=50_000))
    # post-entry bids stay between -5% stop (95.475) and +2% target (102.51): no arm, no hard stop —
    # so the ONLY thing that can close the trade is the bar-close flip.
    quotes = _eh_quotes([(20, 99.5), (21, 99.0), (22, 98.5), (23, 98.0)])
    settings = build_replay_settings(
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=False,
        oms_v2_cw_floor_exit_enabled=True,
    )
    res = replay_symbol_day(_MemSource(bars, quotes), SYM, DAY, settings)
    assert len(res.trades) == 1, f"expected one EH trade; skips={res.skips} misses={res.misses}"
    t = res.trades[0]
    assert t.geometry == "eh_floor_ride"
    assert t.exit_reason == "flip"                 # the bar-close ATR SELL flip closed it
    assert t.exit_px < t.entry_px                  # closed at the (falling) bid, a trend exit


# ==================================================================== P3: EXTENDED-HOURS entry
# The replay now FILLS entries OPENED in extended hours (pre/post-market), so the replay is faithful
# for EH opens too (docs/backtest-replay-engine-design.md P3). Both EH modes run the REAL strategy code:
#   * resting-EH: `_eh_resting_cross_check` (P-B2) software-emulates the dead broker stop — on the ATR
#     up-cross it emits a marketable EH-LIMIT; the replay band-caps it to min(ask, level*(1+band)) and
#     ABANDONS a gap-through (mirrors the OMS `_apply_v2_eh_resting_entry`).
#   * reactive-EH: `_cw_v2_quote` breaks the trigger (with its EH live-bar guard); `route_extended_hours`
#     routes a session=AM/PM limit@ask; with P-B1 on the replay applies the cross-cap/abandon.
# EH is OFF in the LIVE deployed regime (LIVE_LOCKED); `build_replay_settings(eh_enabled=True)` turns it
# on — the ONE switch to replay an "EH-enabled" day. ⚠ EH REAL-DATA parity is DEFERRED (no real EH trades
# exist yet — the flags are dormant); these synthetics prove the EH MECHANISM only.

# --- resting-EH: reuse the SAME validated ATR sequence as the RTH resting entry, but pre-market (08:00).
EH_REST_BASE = datetime(2026, 7, 23, 8, 0, tzinfo=ET)  # 08:00 ET = pre-market -> EH open + EH resting window


def _eh_rest_bars() -> list[SchwabBar]:
    out = []
    for i, (o, h, lo, c) in enumerate(_OHLC):
        ts_ms = int((EH_REST_BASE + timedelta(minutes=i)).timestamp() * 1000)
        out.append(SchwabBar(ts=ts_ms, open=o, high=h, low=lo, close=c, volume=50_000))
    return out


def _eh_rest_quotes(cross_ask: float, floor_bids=None) -> list[TapeQuote]:
    """Bars 9..15 pre-cross quotes (last below the resting level -> no premature EH cross), then the
    crossing quote at bar 16 (last == cross_ask reaches the resting level -> the EH up-cross), then an
    optional post-entry bid tape for the floor-ride."""
    qs = []
    for i in range(9, 16):
        c = _OHLC[i][3]
        ts = EH_REST_BASE + timedelta(minutes=i, seconds=30)
        qs.append(TapeQuote(ts=ts, bid=c - 0.15, ask=c + 0.05, last=c))
    cross_ts = EH_REST_BASE + timedelta(minutes=16, seconds=30)
    qs.append(TapeQuote(ts=cross_ts, bid=cross_ask - 0.1, ask=cross_ask, last=cross_ask))
    for mm, bid in (floor_bids or []):
        qs.append(TapeQuote(ts=EH_REST_BASE + timedelta(minutes=mm), bid=bid, ask=bid + 0.2, last=bid))
    return qs


def _run_eh_rest(cross_ask: float, *, floor_bids=None, eh_enabled: bool = True, **overrides):
    settings = build_replay_settings(eh_enabled=eh_enabled, oms_v2_cw_floor_exit_enabled=True, **overrides)
    source = _MemSource(_eh_rest_bars(), _eh_rest_quotes(cross_ask, floor_bids))
    return replay_symbol_day(source, SYM, DAY, settings)


# ------------------------------------------------------------------ (a) resting-EH cross -> fill -> floor-ride
def test_p3_premarket_resting_eh_cross_fills_at_band_and_floor_rides() -> None:
    # Crossing ask 98.50 lands inside the band [98.264, 98.755] -> marketable EH-LIMIT fill at min(ask,cap)
    # = the ask; the EH-opened position then floor-rides (arm at +2%, exit on fall-back to the floor).
    res = _run_eh_rest(98.50, floor_bids=[(20, 101.0), (21, 99.0)])
    assert len(res.entries) == 1, f"expected one EH resting entry; skips={res.skips} misses={res.misses}"
    e = res.entries[0]
    assert e.mode == "resting" and e.order_type == "limit"      # software EH-LIMIT, NOT a broker STOP_LIMIT
    assert e.fill_price == pytest.approx(98.50, abs=1e-6)        # min(ask, level*(1+band)) = the in-band ask
    assert RESTING_STOP <= e.fill_price <= RESTING_LIMIT
    assert res.misses == []
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.geometry == "eh_floor_ride"                        # EH open -> floor-ride geometry (P2 wiring)
    assert t.exit_reason == "floor"
    assert t.exit_px == pytest.approx(98.50 * 1.02, abs=1e-6)   # rode past +2%, exited on the fall-back floor


# ------------------------------------------------------------------ (b) reactive-EH marketable fill (P-B1 on)
def test_p3_premarket_reactive_eh_marketable_fill() -> None:
    # A pre-market reactive break with the P-B1 cap ON: fills at the marketable ask (100.5), bounded by the
    # cross cap (signal 100.5 +1% = 101.5 -> ask 100.5 <= cap). Resting off so the reactive path fires.
    settings = build_replay_settings(
        eh_enabled=True,
        strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=False,
        oms_v2_cw_floor_exit_enabled=True,
    )
    res = replay_symbol_day(_MemSource(_eh_bars(), _eh_quotes(_EH_FLOOR_BIDS)), SYM, DAY, settings)
    assert len(res.entries) == 1, f"expected one reactive EH entry; skips={res.skips} misses={res.misses}"
    e = res.entries[0]
    assert e.mode == "reactive" and e.order_type == "limit"
    assert e.fill_price == pytest.approx(100.5, abs=1e-6)        # marketable at the ask (<= the cross cap)
    assert res.trades[0].geometry == "eh_floor_ride"


# ------------------------------------------------------------------ (c) gap-through EH entry -> ABANDON
def test_p3_gap_through_resting_eh_entry_abandons() -> None:
    # Same pre-market day, but the break gaps the whole band: crossing ask 99.00 > band cap ~98.755. The
    # up-cross fires but the ask is past the band -> ABANDON (no fill), the live no-chase / gap-through-miss.
    res = _run_eh_rest(99.00)
    assert res.entries == [] and res.trades == [], f"expected a gap-through ABANDON, got {res.entries}"
    assert len(res.misses) == 1 and res.misses[0].reason == "eh_entry_abandoned"
    assert "ASK_PAST_BAND" in res.misses[0].detail


# ------------------------------------------------------------------ (d) EH live-bar guard blocks a stale bar
def test_p3_eh_live_bar_guard_blocks_stale_bar_entry() -> None:
    """The reactive-EH break's driving bar is ~70s old at the crossing quote. At the default max bar age
    (180s) the entry fires; drop it to 30s and the EH live-bar guard (#528 mirror) suppresses the entry off
    the now-stale bar — pinning the guard's threshold value (mutation red)."""
    def _run(max_bar_age_secs: float):
        settings = build_replay_settings(
            eh_enabled=True,
            strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=False,
            strategy_schwab_1m_v2_cw_v2_reactive_entry_max_bar_age_secs=max_bar_age_secs,
        )
        return replay_symbol_day(_MemSource(_eh_bars(), _eh_quotes(_EH_FLOOR_BIDS)), SYM, DAY, settings)

    assert len(_run(180.0).entries) == 1                        # fresh bar -> the reactive-EH entry fires
    assert _run(30.0).entries == []                             # stale bar (>30s) -> guard suppresses it


# ------------------------------------------------------------------ mutation: the EH flag is load-bearing
def test_p3_mutation_eh_flag_off_no_premarket_entry() -> None:
    """The SAME pre-market resting day with the EH flag OFF (LIVE default): the resting window is closed
    pre-market (09:30 start) and the EH cross-check is inert -> NO entry (RTH-only). Proves the EH flag —
    not the fixture — is what makes the pre-market entry fire (mutation red vs test (a))."""
    res = _run_eh_rest(98.50, floor_bids=[(20, 101.0), (21, 99.0)], eh_enabled=False)
    assert res.entries == [] and res.trades == []
