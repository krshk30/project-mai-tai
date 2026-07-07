"""v2/ATR strategy adapter — plugs into the validated engine core (data/harness).

THREE feeds (the KIDZ anchor picked this split): ATR signal + entry hold-window + entry FILL on
the SCHWAB LEVELONE feed (what v2 saw; reproduces the real 1.1887 Schwab fill); EXIT ladder on
the MASSIVE bid (the gateway the OMS reads). Schwab latency ~0s.

Entry (mirror schwab_1m_v2.py): variant-B ATR touch (analysis.atr_flip.compute_atr_trail) + the
intrabar hold-confirm fallback_thin path. Exit (mirror oms _evaluate_v2_managed_exit): the
ExitEngine ladder — hard -1.5% / floor tiers / scales — MARKET fills at the observed massive bid.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from project_mai_tai.backtest.atr_oracle import ATR_FACTOR, ATR_PERIOD, compute_atr_trail
from project_mai_tai.exit_logic.config import TradingConfig
from project_mai_tai.exit_logic.engine import ExitEngine
from project_mai_tai.exit_logic.position import Position

SCHWAB_LATENCY_S = 0.0     # measured near-instant (median 0s) — NOT Webull's band
VOL_FLOOR = 10000          # env override on the live schwab-1m-v2 service
HOLD_N_SECONDS, HOLD_MIN_TICKS, HOLD_BPS = 20, 5, 5.0
BAR_MS = 60_000


@dataclass(frozen=True)
class V2Trade:
    entry_ts: datetime
    entry_price: float       # Schwab ask fill
    touch_price: float       # ATR trail level (idealized reference)
    exit_ts: datetime | None
    exit_price: float | None  # massive bid, qty-weighted over legs
    qty: int
    pnl: float
    exit_reason: str
    n_legs: int


def _v2_cfg():
    return TradingConfig().make_v2_variant()


def _utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _px(q):  # hold-confirm px = last, fallback mid (mirror on_quote:487-493)
    return q.last if (q.last is not None and q.last > 0) else (q.bid + q.ask) / 2.0


class _Book:
    def __init__(self, quotes):
        self._q = quotes
        self._ts = [q.ts for q in quotes]

    def at(self, t):
        i = bisect_right(self._ts, t) - 1
        return self._q[i] if i >= 0 else None

    def slice(self, t0, t1):
        return self._q[bisect_left(self._ts, t0):bisect_left(self._ts, t1)]

    def index_at_or_after(self, t):
        return bisect_left(self._ts, t)


def detect_atr_touches(bars):
    """variant-B touches (mirror _update_atr_state:709-751): (bar_idx, touch_ms, touch_price).
    One per short segment; fired resets on a SELL flip (new short opens)."""
    rows = compute_atr_trail(bars, period=ATR_PERIOD, factor=ATR_FACTOR)
    fired = False
    out = []
    for i in range(1, len(bars)):
        prev = rows[i - 1]
        if prev["state"] == "short" and prev["trail"] is not None and bars[i].high >= prev["trail"] and not fired:
            out.append((i, bars[i].ts, prev["trail"]))
            fired = True
        if rows[i]["flip"] == "SELL":
            fired = False
    return out


def detect_atr_touches_independent(bars, period=ATR_PERIOD, factor=ATR_FACTOR):
    """INDEPENDENT 2nd touch-detector for the PARITY check — a single forward pass mirroring the
    live `_update_atr_state` structure (TR/Wilders/trail/flip/touch inline), a DIFFERENT code path
    from detect_atr_touches (which derives from the multi-pass oracle). Same spec (seed=sma5); if
    the two agree, that's strong evidence of no silent entry-signal bug (there's no real intrabar
    fill anchor beyond the single KIDZ trade). Returns [(bar_idx, ts, touch_price)]."""
    hl = [b.high - b.low for b in bars]
    tr_seed: list[float] = []
    wilders = None
    state = trail = prev_state = prev_trail = None
    fired = False
    out = []
    for i, b in enumerate(bars):
        tr = None
        if i >= period - 1:
            s = sum(hl[i - period + 1:i + 1]) / period
            prev = bars[i - 1]
            hilo = min(hl[i], 1.5 * s)
            href = (b.high - prev.close) if b.low <= prev.high else (b.high - prev.close) - 0.5 * (b.low - prev.high)
            lref = (prev.close - b.low) if b.high >= prev.low else (prev.close - b.low) - 0.5 * (prev.low - b.high)
            tr = max(hilo, href, lref)
        loss = None
        if tr is not None:
            if wilders is None:
                tr_seed.append(tr)
                if len(tr_seed) == period:
                    wilders = sum(tr_seed) / period
            else:
                wilders = wilders + (tr - wilders) / period
            if wilders is not None:
                loss = factor * wilders
        if loss is None:
            continue
        if prev_state == "short" and prev_trail is not None and b.high >= prev_trail and not fired:
            out.append((i, b.ts, prev_trail))
            fired = True
        if state is None:
            state, trail = "long", b.close - loss
        elif state == "long":
            if b.close > trail:
                trail = max(trail, b.close - loss)
            else:
                state, trail, fired = "short", b.close + loss, False
        else:
            if b.close < trail:
                trail = min(trail, b.close + loss)
            else:
                state, trail = "long", b.close - loss
        prev_state, prev_trail = state, trail
    return out


def _run_exit(massive, start_idx, entry_price, qty, cfg, engine):
    """ExitEngine ladder on the massive bid; legs fill at the observed bid (MARKET, ~0s).
    Returns (exit_ts, wavg_price, pnl, reason, n_legs)."""
    pos = Position("V2", entry_price, qty, scale_profile="NORMAL")
    legs = []  # (qty, price)
    reason = "OPEN"
    exit_ts = None
    i, n = start_idx, len(massive)
    while i < n and pos.quantity > 0:
        q = massive[i]
        if q.bid > 0:
            pos.update_price(q.bid)
            action = engine.check_hard_stop(pos, q.bid) or engine.check_intrabar_exit(pos)
            if action and action["action"] == "CLOSE":
                legs.append((pos.quantity, q.bid))
                reason = action["reason"]
                exit_ts = q.ts
                pos.quantity = 0
                break
            if action and action["action"] == "SCALE":
                sell = min(int(action["sell_qty"]), pos.quantity)
                if sell > 0:
                    legs.append((sell, q.bid))
                    pos.apply_scale(action["level"], sell, q.bid)
                    reason = f"{action['reason']}+"
                    exit_ts = q.ts
        i += 1
    if pos.quantity > 0:  # window ended holding the remainder
        if massive:
            legs.append((pos.quantity, massive[-1].bid))
            reason = "WINDOW_END" if not legs[:-1] else f"{reason}WINDOW_END"
            exit_ts = massive[-1].ts
        else:
            return None, None, 0.0, "NO_QUOTES", 0
    tot = sum(lq for lq, _ in legs)
    pnl = sum((p - entry_price) * lq for lq, p in legs)
    wavg = sum(p * lq for lq, p in legs) / tot if tot else None
    return exit_ts, wavg, pnl, reason, len(legs)


def simulate_v2(schwab_bars, schwab_quotes, massive_quotes, *, qty=10, vol_floor=VOL_FLOOR,
                mode="intrabar", latency_s=SCHWAB_LATENCY_S):
    """mode='intrabar' = live hold-confirm path (the leak path); mode='bar_close' = variant-B
    touch at bar close (hold-confirm OFF, comparison). Entry fill = Schwab ask; exit = massive-bid
    ExitEngine ladder. One position at a time (flat gate)."""
    cfg = _v2_cfg()
    engine = ExitEngine(cfg)
    sbook = _Book(schwab_quotes)
    mbook = _Book(massive_quotes)
    trades = []
    flat_after = None
    for bar_idx, touch_ms, touch_price in detect_atr_touches(schwab_bars):
        bar = schwab_bars[bar_idx]
        if bar.volume <= vol_floor:            # liquidity gate (mirror _build_hold_draft:577)
            continue
        if mode == "bar_close":
            decision_ts = _utc(bar.ts + BAR_MS)
        else:  # intrabar hold-confirm
            win = sbook.slice(_utc(bar.ts), _utc(bar.ts + BAR_MS))
            touch_q = next((q for q in win if _px(q) >= touch_price), None)
            if touch_q is None:                # sparse feed, no intrabar cross -> bar-close settle
                decision_ts = _utc(bar.ts + BAR_MS)
            else:
                t1 = touch_q.ts + timedelta(seconds=HOLD_N_SECONDS)
                window = sbook.slice(touch_q.ts, t1)
                n_ticks = len(window)
                last_px = _px(window[-1]) if window else _px(touch_q)
                net_bps = (last_px - touch_price) / touch_price * 1e4
                if not (n_ticks < HOLD_MIN_TICKS or net_bps >= HOLD_BPS):
                    continue                   # skip (not thin, not confirmed)
                decision_ts = t1               # fallback_thin OR confirm -> ENTER at window end
        if flat_after is not None and decision_ts < flat_after:
            continue                           # still holding a prior position
        fq = sbook.at(decision_ts + timedelta(seconds=latency_s))
        if fq is None or fq.ask <= 0:
            continue
        entry_ts = decision_ts + timedelta(seconds=latency_s)
        entry_price = fq.ask                   # honest Schwab ask (market buy)
        start = mbook.index_at_or_after(entry_ts)
        exit_ts, wavg, pnl, reason, n_legs = _run_exit(massive_quotes, start, entry_price, qty, cfg, engine)
        trades.append(V2Trade(entry_ts, entry_price, touch_price, exit_ts, wavg, qty, pnl, reason, n_legs))
        flat_after = exit_ts
    return trades
