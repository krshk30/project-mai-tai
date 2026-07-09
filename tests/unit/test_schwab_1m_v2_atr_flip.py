"""Track 1 — ATR-Flip (P3-B) v2 entry path tests.

Load-bearing test = `test_atr_indicator_parity_vs_oracle`: it pins the production
INCREMENTAL ATR state machine (`SchwabV2Strategy._update_atr_state`) against the
parity-confirmed BATCH indicator from `analysis/atr_flip.py::compute_atr_trail`
(the TOS replica the operator validated). The batch function is **vendored
verbatim below** as `_oracle_compute_atr_trail` — the analysis module lives on a
separate (held) branch and pulls DB/REST imports, so a frozen copy keeps this test
self-contained and dependency-free. If the production port ever drifts from the
validated math, this test fails. Provenance: analysis/atr_flip.py compute_atr_trail
+ _short_segments/extract_signals(variant="B") from analysis/path3_backtest.py.

Other tests mirror test_v2_reference_price.py discipline: a REAL emit path drives
engineered bars and feeds the strategy's OWN metadata (verbatim) through the
SimulatedBrokerAdapter; nothing hand-injected.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.events import TradeIntentPayload
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar,
    PendingHold,
    SchwabV2Strategy,
    SymbolState,
)

ATR_PERIOD = 5
ATR_FACTOR = 3.5


# ====================== FROZEN ORACLE (verbatim analysis/atr_flip.py) ==========
# Trail kept UNROUNDED (the analysis _row rounded only for display; the internal
# cur_trail it ratchets on is unrounded — we mirror the unrounded internal math
# for a strict 1e-9 parity comparison).
@dataclass
class _OBar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: int


def _oracle_compute_atr_trail(bars, *, period=ATR_PERIOD, factor=ATR_FACTOR):
    n = len(bars)
    hl = [b.high - b.low for b in bars]

    def sma(arr, i, p):
        return sum(arr[i - p + 1:i + 1]) / p if i >= p - 1 else None

    tr = [None] * n
    for i in range(1, n):
        s = sma(hl, i, period)
        if s is None:
            continue
        prev, cur = bars[i - 1], bars[i]
        hilo = min(hl[i], 1.5 * s)
        href = (cur.high - prev.close) if cur.low <= prev.high \
            else (cur.high - prev.close) - 0.5 * (cur.low - prev.high)
        lref = (prev.close - cur.low) if cur.high >= prev.low \
            else (prev.close - cur.low) - 0.5 * (prev.low - cur.high)
        tr[i] = max(hilo, href, lref)

    valid = [i for i in range(n) if tr[i] is not None]
    w = [None] * n
    if len(valid) >= period:
        seed_idx = valid[:period]
        prev_w = sum(tr[i] for i in seed_idx) / period
        start = seed_idx[-1]
        w[start] = prev_w
        for i in range(start + 1, n):
            if tr[i] is None:
                continue
            prev_w = prev_w + (tr[i] - prev_w) / period
            w[i] = prev_w
    loss = [factor * w[i] if w[i] is not None else None for i in range(n)]

    rows = []
    cur_state = None
    cur_trail = None
    age = 0
    for i in range(n):
        b = bars[i]
        flip = None
        if loss[i] is None:
            rows.append({"trail": None, "state": None, "flip": None, "state_age": None})
            continue
        if cur_state is None:
            cur_state, cur_trail, age = "long", b.close - loss[i], 0
        else:
            age += 1
            if cur_state == "long":
                if b.close > cur_trail:
                    cur_trail = max(cur_trail, b.close - loss[i])
                else:
                    cur_state, cur_trail, flip, age = "short", b.close + loss[i], "SELL", 0
            else:
                if b.close < cur_trail:
                    cur_trail = min(cur_trail, b.close + loss[i])
                else:
                    cur_state, cur_trail, flip, age = "long", b.close - loss[i], "BUY", 0
        rows.append({"trail": cur_trail, "state": cur_state, "flip": flip, "state_age": age})
    return rows


def _oracle_b_signals(bars, rows):
    """extract_signals(variant='B'): first HIGH>=prior-trail per short segment."""
    n = len(bars)
    # maximal short-state runs
    segs, i = [], 0
    while i < n:
        if rows[i]["state"] == "short":
            j = i
            while j + 1 < n and rows[j + 1]["state"] == "short":
                j += 1
            segs.append((i, j))
            i = j + 1
        else:
            i += 1
    out = []
    for (s, e) in segs:
        for k in range(s + 1, e + 2):
            if k >= n:
                break
            tp = rows[k - 1]["trail"]
            if tp is not None and bars[k].high >= tp:
                out.append((k, tp))
                break
    return out
# ===============================================================================


def _gen_session_bars(n: int, base_ms: int) -> list[tuple]:
    """Deterministic single-session OHLCV. Every bar is RED (close <= open) so the
    Paths-1/2 require_green_bar gate can never fire — isolating the ATR path. A
    wide triangle wave (8.0..12.0, a 50% swing — penny-mover scale) clears the
    ATRFactor-3.5 stop so the state flips long↔short repeatedly, and upper wicks
    drive trail touches on the up-legs."""
    bars = []
    period = 24
    for i in range(n):
        phase = i % period
        tri = (phase / 12.0) if phase <= 12 else (2.0 - phase / 12.0)   # 0..1..0
        close = 8.0 + 4.0 * tri                            # 8.0 .. 12.0 .. 8.0
        open_ = close + 0.06                               # RED bar (open > close)
        high = max(open_, close) + 0.15 + 0.02 * (i % 5)   # wick up (drives touches)
        low = min(open_, close) - 0.10
        ts = base_ms + i * 60_000
        bars.append((ts, round(open_, 4), round(high, 4), round(low, 4),
                     round(close, 4), 10_000))
    return bars


def _mid_session_base_ms() -> int:
    """A fixed ms safely inside one 04:00–20:00 ET session (no anchor crossing)."""
    return int(datetime(2026, 6, 12, 15, 0, tzinfo=UTC).timestamp() * 1000)  # 11:00 ET


# --------------------------------------------------------------------------- (1)

def test_atr_indicator_parity_vs_oracle() -> None:
    """THE load-bearing test: incremental production state == validated batch
    oracle, bar for bar — trail (1e-9), state, state_age, flip — AND the set of
    variant-B touch entries matches extract_signals('B') exactly (one per short
    segment, at the correct trail level)."""
    raw = _gen_session_bars(120, _mid_session_base_ms())
    obars = [_OBar(*r) for r in raw]
    rows = _oracle_compute_atr_trail(obars)
    oracle_b = _oracle_b_signals(obars, rows)

    strat = SchwabV2Strategy(Settings())
    state = SymbolState(symbol="TEST")
    touches: list[tuple[int, float]] = []
    for idx, r in enumerate(raw):
        ts, o, h, low, c, v = r
        sig = strat._update_atr_state(state, OHLCVBar(ts, o, h, low, c, v))
        exp = rows[idx]
        if exp["trail"] is None:
            assert sig is None, f"bar {idx}: oracle undefined but strategy emitted {sig}"
            continue
        assert sig is not None, f"bar {idx}: oracle defined but strategy None"
        assert abs(sig["trail"] - exp["trail"]) < 1e-9, f"bar {idx} trail drift"
        assert sig["state"] == exp["state"], f"bar {idx} state"
        assert sig["state_age"] == exp["state_age"], f"bar {idx} age"
        assert sig["flip"] == exp["flip"], f"bar {idx} flip"
        if sig["touch"]:
            touches.append((idx, sig["touch_price"]))

    # touch parity: same entry bars, same trail levels (4dp, as emitted)
    assert [i for i, _ in touches] == [i for i, _ in oracle_b], "B touch bar set drift"
    for (si, sp), (oi, op) in zip(touches, oracle_b):
        assert round(sp, 4) == round(op, 4), f"touch {si} price {sp} != oracle {op}"
    assert len(oracle_b) >= 2, "fixture should exercise multiple short segments"


# --------------------------------------------------------------------------- (2)

def _build_short_then_fresh_touch(strat: SchwabV2Strategy, *, final_vol: int):
    """135+ warmup RED bars that settle into a SHORT segment with no touch yet,
    then a fresh final bar whose HIGH reaches the resting trail (a real B touch).
    Returns (all_chartbars, trail_level T). Deterministic, computed via the real
    indicator so the on_bar replay reproduces the same T."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    n_warm = 150
    # Declining closes → state flips short early and stays short; small upper
    # wicks stay below the (close+loss) trail so no touch fires during warmup.
    warm = []
    for i in range(n_warm):
        close = 12.0 - 0.02 * i
        open_ = close + 0.05                       # RED
        high = close + 0.04                        # below trail → no warmup touch
        low = close - 0.06
        ts = now_ms - (n_warm - i) * 60_000
        warm.append((ts, open_, high, low, close, 10_000))

    # Replay warmup through the REAL indicator to read the resting trail T.
    probe = SymbolState(symbol="WARM")
    for (ts, o, h, low, c, v) in warm:
        strat._update_atr_state(probe, OHLCVBar(ts, o, h, low, c, v))
    assert probe.atr_state == "short", "warmup must end in a short segment"
    assert not probe.atr_fired_in_short_seg, "no touch should have fired in warmup"
    T = probe.atr_trail

    # Fresh final bar: HIGH reaches T (touch), close stays < T and RED (no flip,
    # Paths-1/2 blocked). volume per the caller.
    final = (now_ms, T + 0.02, T + 0.05, T - 0.50, T - 0.20, final_vol)
    raw = warm + [final]
    chart = [ChartBar("TEST", o, h, low, c, v, ts) for (ts, o, h, low, c, v) in raw]
    return chart, T


@pytest.mark.asyncio
async def test_atr_real_emit_path_fills_at_trail() -> None:
    strat = SchwabV2Strategy(Settings())
    strat._atr_enabled = True           # flag ON (default OFF ships dormant)
    strat._atr_variant = "B"
    chart, T = _build_short_then_fresh_touch(strat, final_vol=100_000)

    draft = None
    for cb in chart:
        draft = strat.on_bar("TEST", cb)
    assert draft is not None, "the fresh final-bar touch must emit"
    assert draft.metadata["path"] == "ATR Flip"
    assert draft.metadata["atr_variant"] == "B"
    assert draft.quantity == Decimal("10")
    ref = Decimal(draft.metadata["reference_price"])
    assert ref == Decimal(f"{T:.4f}")          # fills at the touched trail level

    # Strategy's OWN metadata (verbatim) must fill on the simulated adapter.
    payload = TradeIntentPayload(
        strategy_code="schwab_1m_v2", broker_account_name="paper:schwab_1m_v2",
        symbol=draft.symbol, side=draft.side, quantity=draft.quantity,
        intent_type=draft.intent_type, reason=draft.reason, metadata=dict(draft.metadata),
    )
    request = OrderRequest(
        client_order_id="schwab_1m_v2-TEST-atr-1",
        broker_account_name=payload.broker_account_name, strategy_code=payload.strategy_code,
        symbol=payload.symbol, side=payload.side, intent_type=payload.intent_type,
        quantity=payload.quantity, reason=payload.reason, metadata=dict(payload.metadata),
    )
    adapter = SimulatedBrokerAdapter()
    reports = await adapter.submit_order(request)
    types = {r.event_type for r in reports}
    assert "filled" in types and "rejected" not in types
    filled = next(r for r in reports if r.event_type == "filled")
    assert filled.fill_price == ref


# --------------------------------------------------------------------------- (3)

def test_atr_dormant_when_flag_off() -> None:
    """Flag OFF (default): the SAME fresh touch emits nothing — the indicator
    state is computed but no intent fires."""
    strat = SchwabV2Strategy(Settings())
    assert strat._atr_enabled is False           # default ships dormant
    chart, _T = _build_short_then_fresh_touch(strat, final_vol=100_000)
    drafts = [strat.on_bar("TEST", cb) for cb in chart]
    assert all(d is None for d in drafts), "dormant flag must emit nothing"
    # but the indicator state IS warm (computed every bar)
    assert strat.watchlist_state("TEST").atr_state in ("long", "short")


def test_atr_on_does_not_perturb_paths_1_2() -> None:
    """A genuine MACD-Cross signal emits byte-identical metadata whether the ATR
    flag is off or on (precedence MACD>VWAP>ATR; write-disjoint state)."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)   # shared so bar_time_ms matches

    def drive(atr_on: bool):
        strat = SchwabV2Strategy(Settings())
        strat._atr_enabled = atr_on
        n_flat = 135
        for i in range(n_flat):
            ts = now_ms - (n_flat - i + 1) * 60_000
            strat.on_bar("TEST", ChartBar("TEST", 10.0, 10.0, 10.0, 10.0, 1000, ts))
        final = ChartBar("TEST", 10.0, 11.0, 10.0, 11.0, 100_000, now_ms)
        return strat.on_bar("TEST", final)

    off = drive(False)
    on = drive(True)
    assert off is not None and on is not None
    assert off.metadata["path"] == "MACD Cross" == on.metadata["path"]
    assert off.metadata == on.metadata           # byte-identical
    assert off.quantity == on.quantity


def test_atr_only_mode_hard_disables_paths_1_2() -> None:
    """GO-LIVE LOAD-BEARING: with atr_only_mode ON, the SAME MACD-Cross scenario
    that fires a 'MACD Cross' intent (control) emits NO Paths-1/2 intent — P1/P2
    are the 7wk losers and must never fire under live credentials. Proves the
    strategy-level chokepoint disable holds."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def drive(atr_only: bool):
        s = Settings()
        # ATR enabled in both arms (go-live has ATR on); only the P1/P2 disable toggles.
        object.__setattr__(s, "strategy_schwab_1m_v2_atr_flip_enabled", True)
        object.__setattr__(s, "strategy_schwab_1m_v2_atr_only_mode", atr_only)
        strat = SchwabV2Strategy(s)
        n_flat = 135
        for i in range(n_flat):
            ts = now_ms - (n_flat - i + 1) * 60_000
            strat.on_bar("TEST", ChartBar("TEST", 10.0, 10.0, 10.0, 10.0, 1000, ts))
        final = ChartBar("TEST", 10.0, 11.0, 10.0, 11.0, 100_000, now_ms)
        return strat.on_bar("TEST", final)

    # Control: P1/P2 live → this scenario fires a MACD Cross.
    control = drive(atr_only=False)
    assert control is not None and control.metadata["path"] == "MACD Cross"

    # ATR-only: the SAME scenario must NOT emit any MACD/VWAP intent.
    atr_only = drive(atr_only=True)
    if atr_only is not None:
        assert "ATR Flip" in atr_only.reason
        assert atr_only.metadata.get("path") not in ("MACD Cross", "VWAP Breakout")
    # (None is also acceptable — it means no entry fired at all, P1/P2 suppressed.)


# --------------------------------------------------------------------------- (5)

def test_atr_liquidity_floor_is_the_only_filter() -> None:
    """vol <= floor → no emit; vol > floor → emit. (Floor default 5000.)"""
    strat_lo = SchwabV2Strategy(Settings())
    strat_lo._atr_enabled = True
    chart_lo, _ = _build_short_then_fresh_touch(strat_lo, final_vol=5000)   # == floor
    assert [strat_lo.on_bar("TEST", cb) for cb in chart_lo][-1] is None

    strat_hi = SchwabV2Strategy(Settings())
    strat_hi._atr_enabled = True
    chart_hi, _ = _build_short_then_fresh_touch(strat_hi, final_vol=5001)   # > floor
    assert [strat_hi.on_bar("TEST", cb) for cb in chart_hi][-1] is not None


# ============ Fix (a): ATR fires at its OWN warmup, not MACD's 135-bar gate =====
# docs/v2-atr-early-warmup-fix-design.md. The QTEX 2026-06-15 miss: a correct ATR
# touch at ~67 bars was suppressed by the line-676 min_bars=135 MACD guard, which
# sits ABOVE the ATR emit. These pin the under-warmed (< 135 bars) ATR path.

MIN_BARS = 135  # macd_slow(26)+macd_signal(9)+settling(100); the guard threshold


def _build_short_then_fresh_touch_n(
    strat: SchwabV2Strategy, *, n_warm: int, final_vol: int
):
    """Same construction as `_build_short_then_fresh_touch` but with a caller-set
    warmup length, so we can build an UNDER-WARMED (< MIN_BARS) short-segment +
    fresh touch. Declining RED closes flip short early and stay short; small upper
    wicks stay below the trail (no warmup touch); a fresh final bar's HIGH reaches
    the resting trail T."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    warm = []
    for i in range(n_warm):
        close = 12.0 - 0.02 * i
        open_ = close + 0.05                       # RED
        high = close + 0.04                        # below trail → no warmup touch
        low = close - 0.06
        ts = now_ms - (n_warm - i) * 60_000
        warm.append((ts, open_, high, low, close, 10_000))

    probe = SymbolState(symbol="WARM")
    for (ts, o, h, low, c, v) in warm:
        strat._update_atr_state(probe, OHLCVBar(ts, o, h, low, c, v))
    assert probe.atr_state == "short", "warmup must end in a short segment"
    assert not probe.atr_fired_in_short_seg, "no touch should have fired in warmup"
    T = probe.atr_trail

    final = (now_ms, T + 0.02, T + 0.05, T - 0.50, T - 0.20, final_vol)
    raw = warm + [final]
    chart = [ChartBar("TEST", o, h, low, c, v, ts) for (ts, o, h, low, c, v) in raw]
    return chart, T


# --------------------------------------------------------------------------- (6)

@pytest.mark.asyncio
async def test_atr_fires_under_warmed_below_min_bars() -> None:
    """THE fix: with only ~41 bars (< MIN_BARS=135), a fresh ATR-Flip touch now
    EMITS (the ATR trail is warm after its own ~2*period warmup). This is the QTEX
    scenario — pre-fix the line-676 guard bailed and returned None."""
    strat = SchwabV2Strategy(Settings())
    strat._atr_enabled = True
    chart, T = _build_short_then_fresh_touch_n(strat, n_warm=40, final_vol=100_000)
    assert len(chart) < MIN_BARS, "fixture must be under-warmed for the test to matter"

    draft = None
    for cb in chart:
        draft = strat.on_bar("TEST", cb)
    assert draft is not None, "under-warmed fresh touch must now emit"
    assert draft.metadata["path"] == "ATR Flip"
    assert Decimal(draft.metadata["reference_price"]) == Decimal(f"{T:.4f}")


def test_atr_under_warmed_respects_flat_and_cooldown() -> None:
    """The under-warmed branch honors the SAME entry gates as the warm path:
    an open position OR an active cooldown suppresses the emit."""
    # Flat gate: position open → no emit.
    strat = SchwabV2Strategy(Settings())
    strat._atr_enabled = True
    chart, _ = _build_short_then_fresh_touch_n(strat, n_warm=40, final_vol=100_000)
    for cb in chart[:-1]:
        strat.on_bar("TEST", cb)
    strat.watchlist_state("TEST").position_qty = 10        # not flat
    assert strat.on_bar("TEST", chart[-1]) is None

    # Cooldown gate: cooldown active → no emit.
    strat2 = SchwabV2Strategy(Settings())
    strat2._atr_enabled = True
    chart2, _ = _build_short_then_fresh_touch_n(strat2, n_warm=40, final_vol=100_000)
    for cb in chart2[:-1]:
        strat2.on_bar("TEST", cb)
    strat2.watchlist_state("TEST").cooldown_bars_remaining = 3
    assert strat2.on_bar("TEST", chart2[-1]) is None


def test_atr_under_warmed_no_emit_on_stale_bar() -> None:
    """Replayed/stale history must NOT emit even under-warmed (the bar_is_fresh
    gate still applies) — only a FRESH under-warmed touch fires."""
    strat = SchwabV2Strategy(Settings())
    strat._atr_enabled = True
    chart, _ = _build_short_then_fresh_touch_n(strat, n_warm=40, final_vol=100_000)
    # Rewrite the final bar's timestamp far in the past → stale (not fresh).
    stale_final = ChartBar(
        "TEST", chart[-1].open, chart[-1].high, chart[-1].low, chart[-1].close,
        chart[-1].volume, chart[-1].timestamp_ms - 3_600_000,
    )
    chart = chart[:-1] + [stale_final]
    drafts = [strat.on_bar("TEST", cb) for cb in chart]
    assert drafts[-1] is None, "a stale under-warmed touch must not emit"


def test_macd_vwap_still_silent_under_warmed() -> None:
    """The fix unblocks ONLY ATR under-warmed — MACD/VWAP stay protected by the
    135-bar guard. The SAME flat-then-green cross that fires "MACD Cross" at
    n_flat=135 (see test_atr_on_does_not_perturb_paths_1_2) emits NOTHING at 40
    bars. ATR is disabled here so it can't mask the result — isolating the MACD/
    VWAP gate (the flat 10.0 bars otherwise drive a degenerate ATR oscillation)."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    strat = SchwabV2Strategy(Settings())
    strat._atr_enabled = False                             # isolate MACD/VWAP
    n_flat = 40                                            # < MIN_BARS
    for i in range(n_flat):
        ts = now_ms - (n_flat - i + 1) * 60_000
        strat.on_bar("TEST", ChartBar("TEST", 10.0, 10.0, 10.0, 10.0, 1000, ts))
    draft = strat.on_bar("TEST", ChartBar("TEST", 10.0, 11.0, 10.0, 11.0, 100_000, now_ms))
    assert draft is None, "MACD/VWAP must stay silent below the 135-bar guard"


# ============ Track-B: ATR fresh-flip qualifier (atr_state_age ceiling) ==========
# docs/v2-atr-fresh-flip-qualifier-design.md. ATR losers fire LATE in a long short
# segment (state_age ~16 = dead-cat bounce); winners fire fresh (~2-3). Gate screens
# state_age >= ceiling. ATR-Flip ONLY; OFF by default = behavior-neutral.

def test_atr_fresh_flip_gate_off_is_parity() -> None:
    """Gate OFF (default) → ATR fires exactly as today (behavior-neutral)."""
    strat = SchwabV2Strategy(Settings())
    strat._atr_enabled = True
    assert strat._atr_use_max_state_age is False          # default off
    chart, _T = _build_short_then_fresh_touch(strat, final_vol=100_000)
    draft = [strat.on_bar("TEST", cb) for cb in chart][-1]
    assert draft is not None and draft.metadata["path"] == "ATR Flip"


def test_atr_fresh_flip_screens_late_keeps_below_ceiling() -> None:
    """Gate ON: a LATE-segment touch (state_age ≥ ceiling) is screened; the SAME
    touch fires when the ceiling sits above its age (the fresh-keeps boundary)."""
    # Capture the fixture touch's state_age (gate off). The 150-bar decline makes a
    # long short segment → a high-age "dead-cat" touch.
    s0 = SchwabV2Strategy(Settings())
    s0._atr_enabled = True
    chart, _T = _build_short_then_fresh_touch(s0, final_vol=100_000)
    d0 = [s0.on_bar("TEST", cb) for cb in chart][-1]
    assert d0 is not None
    age = int(d0.metadata["atr_state_age"])
    assert age >= 5, "fixture should be a LATE (high-age) touch"

    # Gate ON at the default ceiling 5 → the late touch is SCREENED.
    s1 = SchwabV2Strategy(Settings())
    s1._atr_enabled = True
    s1._atr_use_max_state_age = True
    s1._atr_max_state_age = 5
    chart1, _ = _build_short_then_fresh_touch(s1, final_vol=100_000)
    assert [s1.on_bar("TEST", cb) for cb in chart1][-1] is None

    # Gate ON with the ceiling ABOVE the age → kept (fires).
    s2 = SchwabV2Strategy(Settings())
    s2._atr_enabled = True
    s2._atr_use_max_state_age = True
    s2._atr_max_state_age = age + 1
    chart2, _ = _build_short_then_fresh_touch(s2, final_vol=100_000)
    d2 = [s2.on_bar("TEST", cb) for cb in chart2][-1]
    assert d2 is not None and d2.metadata["path"] == "ATR Flip"


def test_atr_fresh_flip_does_not_touch_p1_p2() -> None:
    """ATR-only: a MACD-Cross entry is byte-identical with the ATR gate off vs on."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def drive(gate_on: bool):
        strat = SchwabV2Strategy(Settings())
        strat._atr_enabled = True
        strat._atr_use_max_state_age = gate_on
        strat._atr_max_state_age = 5
        n_flat = 135
        for i in range(n_flat):
            ts = now_ms - (n_flat - i + 1) * 60_000
            strat.on_bar("TEST", ChartBar("TEST", 10.0, 10.0, 10.0, 10.0, 1000, ts))
        return strat.on_bar("TEST", ChartBar("TEST", 10.0, 11.0, 10.0, 11.0, 100_000, now_ms))

    off = drive(False)
    on = drive(True)
    assert off is not None and on is not None
    assert off.metadata["path"] == "MACD Cross" == on.metadata["path"]
    assert off.metadata == on.metadata          # ATR gate did not perturb P1/P2


# ----------------------------------------------------------- ATR re-arm fix (live)
# The "burn-the-fake, miss-the-real-flip" fix (docs/schwab-1m-v2-atr-flip-rearm-*).
# Flag-OFF byte-identical is covered by the whole existing suite passing; these pin
# the flag-ON live machinery: the pending-order guard lifecycle + the re-arm entry.


def _rearm_strat(on: bool) -> SchwabV2Strategy:
    s = SchwabV2Strategy(Settings())
    s._atr_enabled = True                 # ATR path live (default ships dormant)
    s._atr_variant = "B"
    s._atr_rearm_enabled = on
    return s


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _warm_to_short(strat: SchwabV2Strategy, symbol: str = "TEST"):
    """Declining RED bars → settle into a SHORT segment with no touch yet (mirrors
    _build_short_then_fresh_touch's warmup, driven through the real indicator)."""
    now_ms = _now_ms()
    state = strat.watchlist_state(symbol)
    for i in range(150):
        close = 12.0 - 0.02 * i
        strat._update_atr_state(
            state, OHLCVBar(now_ms - (200 - i) * 60_000, close + 0.05, close + 0.04,
                            close - 0.06, close, 10_000))
    assert state.atr_state == "short", "warmup must end short"
    return state, now_ms


def test_rearm_poll_fill_promotes_to_claimed() -> None:
    s = _rearm_strat(True)
    st = s.watchlist_state("T")
    s._set_atr_guard(st, "PROVISIONAL", emit_ts_ms=_now_ms())
    st.position_qty = 0
    s.update_position("T", 10)                     # prev 0 -> 10 (a fill of our emit)
    assert st.atr_guard == "CLAIMED"


def test_rearm_poll_timeout_rearms_when_never_filled() -> None:
    s = _rearm_strat(True)
    st = s.watchlist_state("T")
    s._set_atr_guard(st, "PROVISIONAL", emit_ts_ms=0)   # emitted "long ago" (epoch)
    st.position_qty = 0
    s.update_position("T", 0)                      # still flat, past emit+timeout -> re-arm
    assert st.atr_guard == "UNCLAIMED"


def test_rearm_poll_holds_claim_within_window() -> None:
    s = _rearm_strat(True)
    st = s.watchlist_state("T")
    s._set_atr_guard(st, "PROVISIONAL", emit_ts_ms=_now_ms())   # just emitted
    st.position_qty = 0
    s.update_position("T", 0)                      # inside the timeout window -> still working
    assert st.atr_guard == "PROVISIONAL"


def test_rearm_off_update_position_never_touches_guard() -> None:
    """Byte-identical-off: with the flag off, update_position ignores the guard entirely."""
    s = _rearm_strat(False)
    st = s.watchlist_state("T")
    st.atr_guard, st.atr_emit_ts_ms, st.position_qty = "PROVISIONAL", 0, 0
    s.update_position("T", 10)
    assert st.atr_guard == "PROVISIONAL"           # untouched (off path skips _poll_atr_guard)


def test_rearm_emit_nofill_timeout_then_real_flip_ENTERS() -> None:
    """The fix, end-to-end through the live strategy: a variant-B touch emits (guard
    PROVISIONAL); the order never fills; the 5s poll times it out (re-arm); the
    subsequent REAL BUY flip is then entered."""
    s = _rearm_strat(True)
    state, now_ms = _warm_to_short(s)
    T = state.atr_trail
    # (1) touch emits -> PROVISIONAL. The flag-ON path does NOT set the legacy bool.
    tb = OHLCVBar(now_ms - 3 * 60_000, T + 0.02, T + 0.05, T - 0.50, T - 0.20, 100_000)
    sig = s._update_atr_state(state, tb)
    assert sig["touch"] and not state.atr_fired_in_short_seg
    d1 = s._maybe_atr_emit(state, tb, sig, bar_is_fresh=True)
    assert d1 is not None and state.atr_guard == "PROVISIONAL"
    # (2) emit never fills -> the poll re-arms it
    state.atr_emit_ts_ms = 0
    s.update_position("TEST", 0)
    assert state.atr_guard == "UNCLAIMED"
    # (3) the REAL BUY flip is entered (touch re-fires / backstop)
    trail = state.atr_trail
    fb = OHLCVBar(now_ms - 2 * 60_000, trail + 0.10, trail + 0.60, trail + 0.05, trail + 0.50, 100_000)
    sig2 = s._update_atr_state(state, fb)
    assert sig2["flip"] == "BUY"
    d2 = s._maybe_atr_emit(state, fb, sig2, bar_is_fresh=True)
    assert d2 is not None, "re-arm: the real BUY flip is entered"


def test_shipped_bool_MISSES_the_flip_after_a_spent_touch() -> None:
    """The bug, same fixture with the flag OFF: the first touch spends the segment
    (bool True); even though the emit never fills, the real BUY flip is un-enterable."""
    s = _rearm_strat(False)
    state, now_ms = _warm_to_short(s)
    T = state.atr_trail
    tb = OHLCVBar(now_ms - 3 * 60_000, T + 0.02, T + 0.05, T - 0.50, T - 0.20, 100_000)
    sig = s._update_atr_state(state, tb)
    assert sig["touch"] and state.atr_fired_in_short_seg   # legacy bool claims on touch
    assert s._maybe_atr_emit(state, tb, sig, bar_is_fresh=True) is not None
    trail = state.atr_trail
    fb = OHLCVBar(now_ms - 2 * 60_000, trail + 0.10, trail + 0.60, trail + 0.05, trail + 0.50, 100_000)
    sig2 = s._update_atr_state(state, fb)
    assert sig2["flip"] == "BUY"
    d2 = s._maybe_atr_emit(state, fb, sig2, bar_is_fresh=True)
    assert d2 is None, "shipped bug: the spent segment misses the real BUY flip"


def test_rearm_armed_hold_blocks_bar_close_touch_same_bar() -> None:
    """HIGHEST-CONSEQUENCE serialization pin (two drafts in one bar = two orders). Because the flag-ON
    arm no longer claims, the bar-close touch is gated on `atr_hold_pending is None` so an armed-but-
    unresolved intrabar hold BLOCKS a bar-close touch in the same bar (the legacy bool used to do this)."""
    T_now = _now_ms()
    # WITH an armed hold pending -> the bar-close touch must NOT fire
    s1 = _rearm_strat(True)
    st1, n1 = _warm_to_short(s1)
    T1 = st1.atr_trail
    st1.atr_hold_pending = PendingHold(touch_price=T1, touch_ms=n1, deadline_ms=n1 + 20_000,
                                       seg_age=0, last_px=T1, n_ticks=1)
    assert st1.atr_guard == "UNCLAIMED"
    sig1 = s1._update_atr_state(st1, OHLCVBar(n1 - 3 * 60_000, T1 + 0.02, T1 + 0.05, T1 - 0.50, T1 - 0.20, 100_000))
    assert sig1["touch"] is False, "an armed hold must block the bar-close touch (serialization)"
    # WITHOUT the pending, the SAME bar shape DOES touch -> proves the pending is the gate, not the price
    s2 = _rearm_strat(True)
    st2, n2 = _warm_to_short(s2)
    T2 = st2.atr_trail
    assert st2.atr_hold_pending is None
    sig2 = s2._update_atr_state(st2, OHLCVBar(n2 - 3 * 60_000, T2 + 0.02, T2 + 0.05, T2 - 0.50, T2 - 0.20, 100_000))
    assert sig2["touch"] is True
    assert T_now  # (silence unused)


def test_rearm_KNOWN_RESIDUAL_fast_scratch_between_polls_re_arms() -> None:
    """DOCUMENTS the accepted residual (schwab-1m-v2-reject-signal-release.md): the fill detection is
    poll-based, so a fill that OPENED and fully CLOSED within one 5s poll interval is never observed
    (position_qty reads 0-to-0) and the guard re-arms as if never filled — violating the one-entry
    invariant on that rare path. Measured RARE: 2/26 live fills had a < 5s lifetime. This pins the KNOWN
    behavior so it flips visibly when the order-terminal-events fix lands."""
    s = _rearm_strat(True)
    st = s.watchlist_state("T")
    s._set_atr_guard(st, "PROVISIONAL", emit_ts_ms=0)   # emitted long ago; open+close happened between polls
    st.position_qty = 0                                 # the poll only ever sees flat
    s.update_position("T", 0)
    assert st.atr_guard == "UNCLAIMED"                  # re-armed — the residual (a fill was missed)
